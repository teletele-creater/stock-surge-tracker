"""
株価OHLC同期スクリプト — サイトの「リアルチャート」用データを Supabase に保存する

日足: 直近6ヶ月 / 週足: 直近1年半(78本) を yfinance で取得し、
stock_ohlc テーブルに upsert する。サイトはこのテーブルを読むだけ。

事前準備:
  1. Supabase SQL Editor で supabase_ohlc.sql を実行（テーブル作成）
  2. pip install yfinance
  3. .env に SUPABASE_URL / SUPABASE_SERVICE_KEY（site_sync.py と同じ）

使い方:
  # 銘柄コードを指定して同期
  python sync_ohlc.py 7203 4935 9941

  # 直近60日に急騰検知された全銘柄を同期（cron 日次実行におすすめ）
  python sync_ohlc.py --recent 60

  # Python から（site_sync.py のフックで使用）
  from sync_ohlc import sync_codes
  sync_codes(["7203"])
"""

import os
import sys
import time
import json
from datetime import datetime, timedelta, timezone

import requests

# Windows コンソール(cp932)でも ✓ 等を出力できるようにする
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 単体実行時にも .env を読む（main.py 経由なら config.py が先にロード済み）
try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except ImportError:
    pass

SUPABASE_URL   = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY   = os.getenv("SUPABASE_SERVICE_KEY", "")
OHLC_TABLE     = "stock_ohlc"
EVENTS_TABLE   = os.getenv("SUPABASE_TABLE", "surge_events")
TIMEOUT_SEC    = 30


def _headers():
    return {
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "apikey":        SUPABASE_KEY,
        "Content-Type":  "application/json",
    }


def _fetch_bars(code: str, interval: str):
    """yfinance で OHLC を取得して [[epoch_ms, o, h, l, c, v], ...] を返す"""
    import yfinance as yf

    t = yf.Ticker(f"{code}.T")
    if interval == "W":
        df = t.history(period="2y", interval="1wk")
        df = df.tail(78)   # 約1年半
    else:
        df = t.history(period="6mo", interval="1d")

    bars = []
    for ts, row in df.iterrows():
        vals = [row.get("Open"), row.get("High"), row.get("Low"), row.get("Close")]
        # NaN チェック（x != x）
        if any(v is None or v != v for v in vals):
            continue
        vol = row.get("Volume")
        bars.append([
            int(ts.timestamp() * 1000),
            round(float(vals[0]), 2),
            round(float(vals[1]), 2),
            round(float(vals[2]), 2),
            round(float(vals[3]), 2),
            int(vol) if vol == vol and vol is not None else 0,
        ])
    return bars


def sync_codes(codes) -> int:
    """指定コードの日足/週足を Supabase に upsert。成功した行数を返す"""
    if not (SUPABASE_URL and SUPABASE_KEY):
        print("  [OHLC] SUPABASE_URL / SUPABASE_SERVICE_KEY 未設定のためスキップ")
        return 0

    rows = []
    for code in codes:
        code = str(code).strip()
        if not code:
            continue
        for interval in ("D", "W"):
            try:
                bars = _fetch_bars(code, interval)
            except Exception as e:
                print(f"  [OHLC] {code} {interval} 取得失敗: {e}")
                continue
            if len(bars) < 5:
                print(f"  [OHLC] {code} {interval} データ不足 ({len(bars)}本) — スキップ")
                continue
            rows.append({
                "code":       code,
                "interval":   interval,
                "bars":       bars,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
        time.sleep(0.4)  # yfinance レート制限対策

    if not rows:
        return 0

    url = f"{SUPABASE_URL}/rest/v1/{OHLC_TABLE}?on_conflict=code,interval"
    headers = {**_headers(), "Prefer": "resolution=merge-duplicates"}
    resp = requests.post(url, headers=headers, data=json.dumps(rows), timeout=TIMEOUT_SEC)
    if resp.status_code in (200, 201, 204):
        print(f"  [OHLC] {len(rows)} 行 反映 ({', '.join(sorted(set(r['code'] for r in rows)))})")
        return len(rows)
    print(f"  [OHLC] upsert失敗 ({resp.status_code}): {resp.text[:200]}")
    return 0


def _recent_codes(days: int):
    """直近 N 日に急騰検知された銘柄コード一覧を surge_events から取得"""
    # "+00:00" の "+" は URL 中で空白扱いになり 400 になるため "Z" 表記にする
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat().replace("+00:00", "Z")
    url = (f"{SUPABASE_URL}/rest/v1/{EVENTS_TABLE}"
           f"?select=stock_code&detected_at=gte.{since}&limit=2000")
    resp = requests.get(url, headers=_headers(), timeout=TIMEOUT_SEC)
    resp.raise_for_status()
    codes = []
    for row in resp.json():
        c = str(row.get("stock_code") or "").strip()
        if c and c not in codes:
            codes.append(c)
    return codes


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(0)

    if args[0] == "--recent":
        days = int(args[1]) if len(args) > 1 else 60
        codes = _recent_codes(days)
        print(f"[OHLC] 直近{days}日の急騰銘柄 {len(codes)} 件を同期します")
    else:
        codes = args

    n = sync_codes(codes)
    print(f"✓ 完了: {n} 行 反映")
