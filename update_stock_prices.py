"""
現在株価更新スクリプト (cron用)

surge_events に登場するユニークな stock_code を Supabase から取得し、
Yahoo Finance v8/chart で現在値・前日終値を取って stock_prices に upsert する。

使い方:
    # 通常実行
    python update_stock_prices.py

    # ドライラン (取得だけ。Supabase には書き込まない)
    python update_stock_prices.py --dry-run

    # 銘柄を限定して試す
    python update_stock_prices.py --codes 7203,9984,6758

環境変数:
    SUPABASE_URL          Supabase プロジェクトURL
    SUPABASE_SERVICE_KEY  service_role キー (RLSバイパス用、絶対に公開しない)

設計方針:
- Yahoo v8/chart のみ使用 (v7/quote は 401、J-Quants は12週遅延で使えない)
- User-Agent 必須 (無いとブロックされやすい)
- 銘柄毎に 1.5秒 ディレイ + 429時は指数バックオフ最大3回
- 取得失敗銘柄は upsert しない (前回値を温存、ログには出す)
"""

import argparse
import json
import os
import random
import sys
import time
import urllib.parse
import urllib.request
from urllib.error import HTTPError, URLError

from dotenv import load_dotenv

# Windows cp932 で ¥ などが落ちる問題対策。UTF-8 + errors=replace に切り替え
for _stream_name in ("stdout", "stderr"):
    _stream = getattr(sys, _stream_name, None)
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

YAHOO_BASE = "https://query1.finance.yahoo.com/v8/finance/chart"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

REQUEST_DELAY_SEC = 1.5            # 銘柄毎の基本ディレイ
MAX_RETRIES = 3                    # 429/非200リトライ最大回数
RETRY_BASE_DELAY_SEC = 4.0         # リトライの指数バックオフ基底秒数
HTTP_TIMEOUT_SEC = 15


# ============================================================
# Supabase
# ============================================================

def _sb_headers() -> dict:
    return {
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "apikey": SUPABASE_KEY,
    }


def fetch_unique_stock_codes() -> list[str]:
    """surge_events からユニークな stock_code を取得して返す (空・None を除外)"""
    url = f"{SUPABASE_URL}/rest/v1/surge_events?select=stock_code"
    req = urllib.request.Request(url, headers=_sb_headers())
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
        rows = json.loads(resp.read())

    seen = set()
    for row in rows:
        code = (row.get("stock_code") or "").strip()
        # .T が付いていたら剥がして正規化 (yfinance時代の遺物対応)
        if code.endswith(".T"):
            code = code[:-2]
        if code and code not in seen:
            seen.add(code)
    return sorted(seen)


def upsert_stock_price(stock_code: str, price: float, prev_close: float | None) -> bool:
    """stock_prices に upsert する。on_conflict=stock_code で重複は更新"""
    url = f"{SUPABASE_URL}/rest/v1/stock_prices?on_conflict=stock_code"
    body = json.dumps({
        "stock_code": stock_code,
        "price": price,
        "prev_close": prev_close,
        # updated_at はサーバ側 DEFAULT now() で更新したいが、
        # upsert だと既存値が再利用される。明示的に現在時刻を送る。
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }).encode("utf-8")

    headers = {
        **_sb_headers(),
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
            return 200 <= resp.status < 300
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:200]
        print(f"  [Supabase] upsert失敗 ({e.code}): {body}")
        return False
    except URLError as e:
        print(f"  [Supabase] upsert例外: {e}")
        return False


# ============================================================
# Yahoo Finance v8/chart
# ============================================================

def _normalize_yahoo_code(stock_code: str) -> str:
    """日本株コードに .T を付ける (既に付いてれば剥がしてから再付与で正規化)"""
    code = stock_code.strip()
    if code.endswith(".T"):
        code = code[:-2]
    return f"{code}.T"


def fetch_price_from_yahoo(stock_code: str) -> tuple[float | None, float | None, str | None]:
    """
    Yahoo v8/chart から (price, prev_close, error_msg) を返す。

    成功: (price, prev_close, None)
    失敗: (None, None, "エラー説明")

    429/非200 は指数バックオフで最大 MAX_RETRIES 回リトライ。
    """
    yahoo_code = _normalize_yahoo_code(stock_code)
    url = f"{YAHOO_BASE}/{urllib.parse.quote(yahoo_code)}?interval=1d&range=1d"

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json",
                    "Accept-Language": "ja,en;q=0.9",
                },
            )
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
                if resp.status != 200:
                    last_error = f"HTTP {resp.status}"
                    continue
                data = json.loads(resp.read())

            result = (data.get("chart") or {}).get("result") or []
            if not result:
                err = (data.get("chart") or {}).get("error")
                return None, None, f"chart.result empty (err={err})"

            meta = result[0].get("meta") or {}
            price = meta.get("regularMarketPrice")
            prev_close = meta.get("chartPreviousClose")
            if price is None:
                return None, None, "regularMarketPrice missing"
            return float(price), (float(prev_close) if prev_close is not None else None), None

        except HTTPError as e:
            last_error = f"HTTP {e.code}"
            # 429/5xx はリトライ、それ以外(404等)は即終了
            if e.code not in (429, 500, 502, 503, 504):
                return None, None, last_error
        except URLError as e:
            last_error = f"URL error: {e.reason}"
        except (ValueError, json.JSONDecodeError) as e:
            last_error = f"JSON parse error: {e}"
            return None, None, last_error

        # リトライ前の指数バックオフ + ジッタ
        if attempt < MAX_RETRIES:
            wait = RETRY_BASE_DELAY_SEC * (2 ** (attempt - 1)) + random.uniform(0, 1.5)
            time.sleep(wait)

    return None, None, last_error or "unknown error"


# ============================================================
# メイン
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="現在株価を更新して Supabase stock_prices に保存")
    parser.add_argument("--dry-run", action="store_true",
                        help="取得結果を表示するだけで Supabase には書き込まない")
    parser.add_argument("--codes", type=str, default=None,
                        help="銘柄コードをカンマ区切りで指定 (例: 7203,9984)。指定時は surge_events を見ない")
    args = parser.parse_args()

    if not SUPABASE_URL or not SUPABASE_KEY:
        print("[ERROR] SUPABASE_URL / SUPABASE_SERVICE_KEY が環境変数に設定されていません")
        sys.exit(1)

    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
        print(f"[INFO] 指定銘柄 {len(codes)}件: {codes}")
    else:
        print("[INFO] surge_events から銘柄一覧を取得中...")
        try:
            codes = fetch_unique_stock_codes()
        except Exception as e:
            print(f"[ERROR] Supabase 取得失敗: {e}")
            sys.exit(1)
        print(f"[INFO] 対象 {len(codes)}銘柄")

    if not codes:
        print("[INFO] 対象銘柄なし。終了")
        return

    success_count = 0
    skip_count = 0
    fail_count = 0
    errors = []

    for i, code in enumerate(codes, 1):
        price, prev_close, err = fetch_price_from_yahoo(code)

        if err is not None or price is None:
            fail_count += 1
            errors.append((code, err))
            print(f"  [{i}/{len(codes)}] {code}: 取得失敗 ({err}) — 既存値を温存")
        else:
            if args.dry_run:
                skip_count += 1
                print(f"  [{i}/{len(codes)}] {code}: ¥{price:,.1f} (prev ¥{prev_close or 0:,.1f}) [dry-run]")
            else:
                ok = upsert_stock_price(code, price, prev_close)
                if ok:
                    success_count += 1
                    print(f"  [{i}/{len(codes)}] {code}: ¥{price:,.1f} (prev ¥{prev_close or 0:,.1f}) → 保存")
                else:
                    fail_count += 1
                    errors.append((code, "Supabase upsert error"))
                    print(f"  [{i}/{len(codes)}] {code}: 保存失敗")

        # 最後の銘柄以外は次のリクエストまでディレイ
        if i < len(codes):
            time.sleep(REQUEST_DELAY_SEC)

    print()
    print("=" * 50)
    print("[SUMMARY]")
    print(f"  対象       : {len(codes)}")
    print(f"  保存成功   : {success_count}")
    print(f"  ドライラン : {skip_count}")
    print(f"  失敗       : {fail_count}")
    if errors:
        print("  失敗銘柄(最大10件表示):")
        for code, err in errors[:10]:
            print(f"    - {code}: {err}")
    print("=" * 50)

    # 失敗が半数を超えたら異常終了でAlert (cron上で気付ける)
    if fail_count > 0 and fail_count >= len(codes) // 2:
        print("[WARN] 失敗率が高い。Yahoo にブロックされている可能性 → exit 1")
        sys.exit(1)


if __name__ == "__main__":
    main()
