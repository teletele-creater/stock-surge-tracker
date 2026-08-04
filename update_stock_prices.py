"""
現在株価更新スクリプト (cron用)

surge_events に登場するユニークな stock_code を Supabase から取得し、
yfinance (yf.download) のバッチ取得で現在値・前日終値を取って
stock_prices に upsert する。

使い方:
    # 通常実行 (surge_events の全銘柄)
    python update_stock_prices.py

    # 直近N日に検知された銘柄のみに絞る
    python update_stock_prices.py --days 30

    # ドライラン (取得だけ。Supabase には書き込まない)
    python update_stock_prices.py --dry-run

    # 銘柄を限定して試す
    python update_stock_prices.py --codes 7203,9984,6758

環境変数:
    SUPABASE_URL          Supabase プロジェクトURL
    SUPABASE_SERVICE_KEY  service_role キー (RLSバイパス用、絶対に公開しない)

設計方針:
- 旧実装は Yahoo v8/chart を1銘柄ずつ叩いていて全銘柄だと約30分かかり、
  タイムアウト回避のため直近30日に絞っていた (→ 古い銘柄の価格が止まる不具合)。
  yf.download のバッチ取得 (80銘柄/回) なら全銘柄でも数分で終わるため、
  デフォルトを全銘柄に戻した。
- 4桁英数字コード (184A 等) も {code}.T でそのまま引ける
- 取得失敗銘柄は upsert しない (前回値を温存、ログには出す)
- stock_prices への書き込みは on_conflict=stock_code のバルク upsert (冪等)
- updated_at は明示的に送る (サイト側が鮮度判定に使うため必ず更新する)
"""

import argparse
import json
import math
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError

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

BATCH_SIZE = 80                 # yf.download の1回あたり銘柄数
BATCH_DELAY_SEC = 2.0           # バッチ間ディレイ
BATCH_MAX_RETRIES = 3           # バッチ丸ごと失敗時のリトライ回数
HTTP_TIMEOUT_SEC = 30


# ============================================================
# Supabase
# ============================================================

def _sb_headers() -> dict:
    return {
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "apikey": SUPABASE_KEY,
    }


def fetch_unique_stock_codes(days: int = 0) -> list[str]:
    """surge_events からユニークな stock_code を取得して返す (空・None を除外)

    days > 0 なら直近 days 日に検知された銘柄のみ。デフォルトは全銘柄。
    PostgREST は1リクエスト最大1000行 (Supabaseデフォルト) のため、
    offset でページネーションして全行取得する。
    """
    # tweet_id はユニークキーなのでページネーションの並びが安定する
    base_url = f"{SUPABASE_URL}/rest/v1/surge_events?select=stock_code&order=tweet_id"
    if days > 0:
        # "+00:00" の "+" は URL 中で空白扱いになるため "Z" 表記にする
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat().replace("+00:00", "Z")
        base_url += f"&detected_at=gte.{since}"

    rows: list[dict] = []
    page_size = 1000
    offset = 0
    while True:
        url = f"{base_url}&limit={page_size}&offset={offset}"
        req = urllib.request.Request(url, headers=_sb_headers())
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
            page = json.loads(resp.read())
        rows.extend(page)
        if len(page) < page_size:
            break
        offset += page_size

    seen = set()
    for row in rows:
        code = (row.get("stock_code") or "").strip()
        # .T が付いていたら剥がして正規化 (yfinance時代の遺物対応)
        if code.endswith(".T"):
            code = code[:-2]
        if code and code not in seen:
            seen.add(code)
    return sorted(seen)


def bulk_upsert_stock_prices(records: list[dict]) -> bool:
    """stock_prices にバルク upsert する。on_conflict=stock_code で重複は更新"""
    if not records:
        return True
    url = f"{SUPABASE_URL}/rest/v1/stock_prices?on_conflict=stock_code"
    body = json.dumps(records).encode("utf-8")
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
        detail = e.read().decode("utf-8", errors="replace")[:200]
        print(f"  [Supabase] upsert失敗 ({e.code}): {detail}")
        return False
    except OSError as e:
        print(f"  [Supabase] upsert例外: {e}")
        return False


# ============================================================
# yfinance バッチ取得
# ============================================================

def _extract_ticker_frame(df, ticker: str):
    """yf.download の結果から1銘柄分の DataFrame を取り出す (無ければ None)"""
    import pandas as pd

    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        if ticker not in df.columns.get_level_values(0):
            return None
        sub = df[ticker]
    else:
        # 1銘柄だけ渡した場合はフラットな列になることがある
        sub = df
    sub = sub.dropna(subset=["Close"]) if "Close" in sub.columns else None
    if sub is None or sub.empty:
        return None
    return sub


def fetch_prices_batch(codes: list[str]) -> dict[str, tuple[float, float | None]]:
    """codes (最大 BATCH_SIZE 件) の (price, prev_close) を yf.download で一括取得。

    返り値は {code: (price, prev_close)}。取得できなかった銘柄は含まれない。
    """
    import yfinance as yf

    tickers = [f"{c}.T" for c in codes]
    df = None
    for attempt in range(1, BATCH_MAX_RETRIES + 1):
        try:
            df = yf.download(
                tickers,
                period="7d",
                interval="1d",
                group_by="ticker",
                auto_adjust=False,
                threads=True,
                progress=False,
            )
            break
        except Exception as e:
            print(f"  [yfinance] バッチ取得失敗 (attempt {attempt}/{BATCH_MAX_RETRIES}): {e}")
            if attempt < BATCH_MAX_RETRIES:
                time.sleep(5.0 * attempt)
    if df is None:
        return {}

    results: dict[str, tuple[float, float | None]] = {}
    for code in codes:
        sub = _extract_ticker_frame(df, f"{code}.T")
        if sub is None:
            continue
        closes = [float(v) for v in sub["Close"].tolist() if not math.isnan(float(v))]
        if not closes:
            continue
        price = closes[-1]
        prev_close = closes[-2] if len(closes) >= 2 else None
        results[code] = (price, prev_close)
    return results


# ============================================================
# メイン
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="現在株価を更新して Supabase stock_prices に保存")
    parser.add_argument("--dry-run", action="store_true",
                        help="取得結果を表示するだけで Supabase には書き込まない")
    parser.add_argument("--codes", type=str, default=None,
                        help="銘柄コードをカンマ区切りで指定 (例: 7203,9984)。指定時は surge_events を見ない")
    parser.add_argument("--days", type=int, default=0,
                        help="直近N日に急騰検知された銘柄のみ更新 (0=全銘柄, デフォルト0)")
    args = parser.parse_args()

    if not SUPABASE_URL or not SUPABASE_KEY:
        print("[ERROR] SUPABASE_URL / SUPABASE_SERVICE_KEY が環境変数に設定されていません")
        sys.exit(1)

    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
        print(f"[INFO] 指定銘柄 {len(codes)}件: {codes}")
    else:
        scope = f"直近{args.days}日" if args.days > 0 else "全期間"
        print(f"[INFO] surge_events から銘柄一覧を取得中... ({scope})")
        try:
            codes = fetch_unique_stock_codes(args.days)
        except Exception as e:
            print(f"[ERROR] Supabase 取得失敗: {e}")
            sys.exit(1)
        print(f"[INFO] 対象 {len(codes)}銘柄")

    if not codes:
        print("[INFO] 対象銘柄なし。終了")
        return

    updated_at = datetime.now(timezone.utc).isoformat()
    success_count = 0
    fail_codes: list[str] = []
    n_batches = (len(codes) + BATCH_SIZE - 1) // BATCH_SIZE

    for bi in range(n_batches):
        batch = codes[bi * BATCH_SIZE:(bi + 1) * BATCH_SIZE]
        print(f"[BATCH {bi + 1}/{n_batches}] {len(batch)}銘柄を取得中...")
        prices = fetch_prices_batch(batch)

        records = []
        for code in batch:
            if code not in prices:
                fail_codes.append(code)
                continue
            price, prev_close = prices[code]
            records.append({
                "stock_code": code,
                "price": price,
                "prev_close": prev_close,
                "updated_at": updated_at,
            })

        if args.dry_run:
            for r in records:
                print(f"  {r['stock_code']}: ¥{r['price']:,.1f} (prev ¥{r['prev_close'] or 0:,.1f}) [dry-run]")
        elif records:
            if bulk_upsert_stock_prices(records):
                success_count += len(records)
                print(f"  取得 {len(records)}/{len(batch)}銘柄 → 保存OK")
            else:
                fail_codes.extend(r["stock_code"] for r in records)
                print(f"  取得 {len(records)}/{len(batch)}銘柄 → 保存失敗")

        if bi < n_batches - 1:
            time.sleep(BATCH_DELAY_SEC)

    print()
    print("=" * 50)
    print("[SUMMARY]")
    print(f"  対象       : {len(codes)}")
    print(f"  保存成功   : {success_count}")
    print(f"  失敗/データなし: {len(fail_codes)}")
    if fail_codes:
        print(f"  失敗銘柄(最大20件表示): {', '.join(fail_codes[:20])}")
    print("=" * 50)

    # 失敗が半数を超えたら異常終了でAlert (cron上で気付ける)
    if fail_codes and len(fail_codes) >= len(codes) // 2:
        print("[WARN] 失敗率が高い。Yahoo にブロックされている可能性 → exit 1")
        sys.exit(1)


if __name__ == "__main__":
    main()
