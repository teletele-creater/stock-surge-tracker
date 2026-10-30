"""
急騰時株価バックフィルスクリプト
============================================
`surge_events` の `surge_price` が NULL の行について、
検知日 (`detected_at`, JST) の日足株価を yfinance から取得して埋めます。

急騰時の正確な約定値は残っていないため、**検知日の終値**で近似します。
（--price-field open|high|close で変更可能）

────────────────────────────────────────────
使い方
────────────────────────────────────────────
1) 環境変数 (.env または export) — site_sync.py と同じものでOK:
     SUPABASE_URL         = https://xxxx.supabase.co
     SUPABASE_SERVICE_KEY = eyJhbGciOi...   # service_role キー（公開禁止）

2) 依存パッケージ:
     pip install requests yfinance python-dotenv

3) 実行（stock_memo/ ディレクトリで .env を読ませる）:
     cd stock_memo
     python ../stock_memo_patch/backfill_surge_prices.py --dry-run   # まず確認
     python ../stock_memo_patch/backfill_surge_prices.py             # 本番反映

────────────────────────────────────────────
冪等性
────────────────────────────────────────────
surge_price が NULL の行だけを対象に UPDATE するので、
何度実行しても既に入っている値を上書きしません。
（--overwrite を付けた場合のみ全行を再計算して上書き）
"""

import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

try:
    import yfinance as yf
except ImportError:
    print("yfinance が必要です: pip install yfinance", file=sys.stderr)
    sys.exit(1)

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
TABLE        = os.getenv("SUPABASE_TABLE", "surge_events")

HTTP_TIMEOUT = 30
JST = timezone(timedelta(hours=9))


def _headers():
    return {
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "apikey":        SUPABASE_KEY,
        "Content-Type":  "application/json",
    }


# ─────────────────────────────────────────────
# 対象行の取得
# ─────────────────────────────────────────────
def fetch_target_rows(overwrite: bool) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    page = 1000
    while True:
        url = (
            f"{SUPABASE_URL}/rest/v1/{TABLE}"
            f"?select=id,stock_code,stock_name,detected_at,surge_price"
            f"&order=detected_at.asc&offset={offset}&limit={page}"
        )
        if not overwrite:
            url += "&surge_price=is.null"
        resp = requests.get(url, headers=_headers(), timeout=HTTP_TIMEOUT)
        if not resp.ok:
            print(f"[Supabase] 取得失敗 ({resp.status_code}): {resp.text[:300]}", file=sys.stderr)
            sys.exit(1)
        batch = resp.json()
        rows.extend(batch)
        if len(batch) < page:
            break
        offset += page
    return rows


# ─────────────────────────────────────────────
# 株価取得（銘柄ごとに日足をまとめて1回取得してキャッシュ）
# ─────────────────────────────────────────────
_history_cache: dict[str, "object"] = {}


def _get_history(code: str, start: datetime, end: datetime):
    """code の日足 DataFrame を取得（銘柄単位でキャッシュ）"""
    key = code
    if key in _history_cache:
        return _history_cache[key]
    ticker = f"{code}.T"
    df = None
    for attempt in range(3):
        try:
            df = yf.Ticker(ticker).history(
                start=start.strftime("%Y-%m-%d"),
                end=(end + timedelta(days=1)).strftime("%Y-%m-%d"),
                interval="1d",
                auto_adjust=False,
            )
            break
        except Exception as e:
            if attempt == 2:
                print(f"    [yf] {ticker} 取得失敗: {e}", file=sys.stderr)
            time.sleep(1.5 * (attempt + 1))
    if df is not None and df.empty:
        df = None
    _history_cache[key] = df
    return df


def lookup_price(code: str, detected_at_iso: str, field: str,
                 span_start: datetime, span_end: datetime):
    """検知日（JST）の株価を返す。当日データが無ければ直近の前営業日（3日以内）。"""
    df = _get_history(code, span_start, span_end)
    if df is None:
        return None, "no-data"

    try:
        dt = datetime.fromisoformat(detected_at_iso.replace("Z", "+00:00")).astimezone(JST)
    except Exception:
        return None, "bad-date"
    target = dt.date()

    col = {"open": "Open", "high": "High", "close": "Close"}[field]
    # index は DatetimeIndex（日本市場は JST 日付）
    dates = [d.date() for d in df.index]
    # 当日 → 1〜3日前の順で探す
    for back in range(0, 4):
        want = target - timedelta(days=back)
        if want in dates:
            v = float(df.iloc[dates.index(want)][col])
            note = "exact" if back == 0 else f"-{back}d"
            return (round(v, 1) if v < 100 else round(v)), note
    return None, "no-session"


# ─────────────────────────────────────────────
# Supabase UPDATE
# ─────────────────────────────────────────────
def update_price(row_id: str, price) -> bool:
    url = f"{SUPABASE_URL}/rest/v1/{TABLE}?id=eq.{row_id}"
    resp = requests.patch(
        url, headers={**_headers(), "Prefer": "return=minimal"},
        json={"surge_price": price}, timeout=HTTP_TIMEOUT,
    )
    if not resp.ok:
        print(f"    [Supabase] 更新失敗 ({resp.status_code}): {resp.text[:200]}", file=sys.stderr)
    return resp.ok


# ─────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="surge_price バックフィル（検知日の日足で近似）")
    ap.add_argument("--dry-run", action="store_true", help="書き込みなし。取得結果だけ表示")
    ap.add_argument("--limit", type=int, default=0, help="先頭 N 件だけ処理（テスト用）")
    ap.add_argument("--price-field", choices=["open", "high", "close"], default="close",
                    help="検知日のどの値を使うか（デフォルト: close=終値）")
    ap.add_argument("--overwrite", action="store_true",
                    help="既に値が入っている行も再計算して上書き")
    args = ap.parse_args()

    missing = [k for k, v in [("SUPABASE_URL", SUPABASE_URL), ("SUPABASE_SERVICE_KEY", SUPABASE_KEY)] if not v]
    if missing:
        print(f"以下の環境変数が未設定です: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    print(f"[1/3] {TABLE} から対象行を取得")
    rows = fetch_target_rows(args.overwrite)
    if args.limit:
        rows = rows[: args.limit]
    print(f"      → {len(rows)} 件（surge_price {'全件' if args.overwrite else 'NULL のみ'}）\n")
    if not rows:
        print("対象なし。完了。")
        return

    # 取得期間（全行の検知日をカバー）
    dts = [datetime.fromisoformat(r["detected_at"].replace("Z", "+00:00")) for r in rows]
    span_start = min(dts) - timedelta(days=7)
    span_end   = max(dts) + timedelta(days=1)

    print(f"[2/3] yfinance で日足取得（{args.price_field}） 期間 {span_start.date()} 〜 {span_end.date()}")
    print(f"[3/3] {'DRY-RUN（書き込みなし）' if args.dry_run else 'Supabase に書き込み'}\n")

    ok = ng = skipped = 0
    for i, r in enumerate(rows, 1):
        code = (r.get("stock_code") or "").strip()
        if not code.isdigit() and not (len(code) == 4 and code[:3].isdigit()):
            # 4桁英数（例: 130A）は許可、それ以外の不明コードはスキップ
            if not (len(code) == 4):
                skipped += 1
                continue
        price, note = lookup_price(code, r["detected_at"], args.price_field, span_start, span_end)
        label = f"{i:>3}/{len(rows)} {code} {r.get('stock_name','')[:12]:<12} {r['detected_at'][:10]}"
        if price is None:
            print(f"  ✗ {label} → 取得不可 ({note})")
            ng += 1
            continue
        if args.dry_run:
            print(f"  ・ {label} → {price} 円 ({note})")
            ok += 1
        else:
            if update_price(r["id"], price):
                print(f"  ✓ {label} → {price} 円 ({note})")
                ok += 1
            else:
                ng += 1
        time.sleep(0.15)  # yfinance / API への負荷を抑える

    print(f"\n✓ 完了: 成功 {ok} / 失敗 {ng} / スキップ {skipped}")
    if args.dry_run:
        print("  問題なければ --dry-run を外して再実行してください。")


if __name__ == "__main__":
    main()
