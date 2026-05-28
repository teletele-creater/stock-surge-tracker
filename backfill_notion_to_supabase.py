"""
Notion → Supabase 一括バックフィルスクリプト
============================================
既に Notion DB に蓄積されている分析履歴を、サイトが参照している
Supabase の `surge_events` テーブルに一度に流し込みます。

実行は1回でOK。以降の新規分析は通常フロー（site_sync.py）が随時反映します。

────────────────────────────────────────────
使い方
────────────────────────────────────────────
1) 環境変数 (.env または export) に以下を設定:
     NOTION_TOKEN            = secret_xxx                  # ntn_682650...
     NOTION_DATABASE_ID      = xxxxxxxxxxxxxxxxxxxxxxxxxxxxx
     SUPABASE_URL            = https://byywhukxggelizpckbba.supabase.co
     SUPABASE_SERVICE_KEY    = eyJhbGciOi...                # service_role キー（公開禁止）
     SUPABASE_TABLE          = surge_events                 # （任意）デフォルトと同じ

2) 依存パッケージ:
     pip install notion-client requests python-dotenv

3) 実行:
     python backfill_notion_to_supabase.py
     # ドライラン（書き込みなし、変換結果だけ確認）:
     python backfill_notion_to_supabase.py --dry-run

────────────────────────────────────────────
冪等性
────────────────────────────────────────────
各Notionページに対して  tweet_id = "notion-<page_id>"  を割り当てるため、
何度実行しても同じ行が上書き(upsert)されるだけで重複しません。
"""

import argparse
import os
import re
import sys
from typing import Optional

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

try:
    from notion_client import Client
except ImportError:
    print("notion-client が必要です:  pip install notion-client", file=sys.stderr)
    sys.exit(1)


NOTION_TOKEN        = os.getenv("NOTION_TOKEN", "")
NOTION_DATABASE_ID  = os.getenv("NOTION_DATABASE_ID", "")
SUPABASE_URL        = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY        = os.getenv("SUPABASE_SERVICE_KEY", "")
SUPABASE_TABLE      = os.getenv("SUPABASE_TABLE", "surge_events")

UPSERT_CHUNK = 50
HTTP_TIMEOUT = 30


# ─────────────────────────────────────────────
# Notion プロパティ → プレーンテキスト変換
# ─────────────────────────────────────────────
def _rt(rt_array) -> str:
    if not rt_array:
        return ""
    return "".join(
        (r.get("plain_text") or r.get("text", {}).get("content", ""))
        for r in rt_array
    ).strip()


def _multi_select(ms_array) -> list[str]:
    return [o.get("name", "").strip() for o in (ms_array or []) if o.get("name")]


def _parse_backgrounds(text: str) -> list[str]:
    """改行 + 「・」プレフィックスの背景テキストを配列に分解"""
    if not text:
        return []
    out = []
    for ln in text.split("\n"):
        ln = re.sub(r"^[・\-\s]+", "", ln).strip()
        if ln:
            out.append(ln)
    return out


def _strip_parens(s: str) -> str:
    """analyzer出力の "(...)" を取り除く"""
    s = (s or "").strip()
    while s.startswith("(") and s.endswith(")"):
        s = s[1:-1].strip()
    return s


# ─────────────────────────────────────────────
# Notion ページ → Supabase 行
# ─────────────────────────────────────────────
def page_to_row(page: dict) -> Optional[dict]:
    props = page.get("properties", {})

    stock_name  = _rt(props.get("銘柄名", {}).get("title", []))
    stock_code  = _rt(props.get("銘柄コード", {}).get("rich_text", []))
    detected_at = (props.get("日時", {}).get("date") or {}).get("start") or ""
    reason      = _rt(props.get("上昇理由", {}).get("rich_text", []))
    business    = _rt(props.get("事業内容", {}).get("rich_text", []))
    bg_text     = _rt(props.get("背景", {}).get("rich_text", []))
    related     = _rt(props.get("関連銘柄", {}).get("rich_text", []))
    keywords    = _multi_select(props.get("キーワード", {}).get("multi_select", []))
    price_prop  = props.get("株価", {})                              # Notion 側プロパティ名
    surge_price = price_prop.get("number") if price_prop else None   # Number 型のみ拾える

    if not detected_at:
        # 日時が無いページはスキップ
        return None

    return {
        "tweet_id":       f"notion-{page['id']}",   # 冪等キー
        "detected_at":    detected_at,
        "stock_code":     _strip_parens(stock_code) or "不明",
        "stock_name":     _strip_parens(stock_name),
        "reason":         _strip_parens(reason),
        "business":       _strip_parens(business),
        "keywords":       keywords,
        "backgrounds":    _parse_backgrounds(bg_text),
        "related_stocks": related,
        "raw_post":       "",
        "chart_url":      None,
        "surge_price":    surge_price,
    }


# ─────────────────────────────────────────────
# Notion 全ページ取得
# ─────────────────────────────────────────────
def fetch_all_notion_pages() -> list[dict]:
    client = Client(auth=NOTION_TOKEN)

    # notion-client 3.x / Notion API 2025-09-03 では databases.query が廃止され、
    # 「データベース → データソース → ページ」の二段構成になった。
    # まず database_id から data_source_id を解決する。
    db = client.databases.retrieve(database_id=NOTION_DATABASE_ID)
    data_sources = db.get("data_sources", [])
    if not data_sources:
        print("  [Notion] このデータベースに data_source が見つかりません。"
              "統合(インテグレーション)がDBに接続されているか確認してください。",
              file=sys.stderr)
        sys.exit(1)
    if len(data_sources) > 1:
        print(f"  [Notion] data_source が複数あります。先頭を使用します: "
              f"{data_sources[0].get('name')}")
    data_source_id = data_sources[0]["id"]

    pages, cursor = [], None
    while True:
        kwargs = {
            "data_source_id": data_source_id,
            "sorts": [{"property": "日時", "direction": "descending"}],
            "page_size": 100,
        }
        if cursor:
            kwargs["start_cursor"] = cursor
        resp = client.data_sources.query(**kwargs)
        pages.extend(resp.get("results", []))
        print(f"  [Notion] {len(pages)} 件取得済み …")
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")
    return pages


# ─────────────────────────────────────────────
# Supabase upsert
# ─────────────────────────────────────────────
def upsert_to_supabase(rows: list[dict]) -> int:
    if not rows:
        return 0
    url = f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}?on_conflict=tweet_id"
    headers = {
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "apikey":        SUPABASE_KEY,
        "Content-Type":  "application/json",
        "Prefer":        "resolution=merge-duplicates,return=minimal",
    }
    ok_count = 0
    for i in range(0, len(rows), UPSERT_CHUNK):
        batch = rows[i:i + UPSERT_CHUNK]
        resp = requests.post(url, headers=headers, json=batch, timeout=HTTP_TIMEOUT)
        if resp.ok:
            ok_count += len(batch)
            print(f"  [Supabase] {ok_count}/{len(rows)} 件 反映")
        else:
            print(
                f"  [Supabase] バッチ失敗 ({resp.status_code}): "
                f"{resp.text[:300]}",
                file=sys.stderr,
            )
    return ok_count


# ─────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Notion → Supabase 一括バックフィル")
    ap.add_argument("--dry-run", action="store_true",
                    help="Supabaseへの書き込みをスキップ（変換結果だけ表示）")
    ap.add_argument("--limit", type=int, default=0,
                    help="先頭 N 件だけ処理（テスト用）。0=制限なし")
    args = ap.parse_args()

    # 環境変数チェック
    missing = []
    if not NOTION_TOKEN:        missing.append("NOTION_TOKEN")
    if not NOTION_DATABASE_ID:  missing.append("NOTION_DATABASE_ID")
    if not args.dry_run:
        if not SUPABASE_URL:        missing.append("SUPABASE_URL")
        if not SUPABASE_KEY:        missing.append("SUPABASE_SERVICE_KEY")
    if missing:
        print(f"以下の環境変数が未設定です: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    print("[1/3] Notion から全ページ取得")
    pages = fetch_all_notion_pages()
    if args.limit:
        pages = pages[: args.limit]
    print(f"      → {len(pages)} ページ\n")

    print("[2/3] Supabase 行に変換")
    rows = []
    skipped = 0
    for p in pages:
        row = page_to_row(p)
        if row:
            rows.append(row)
        else:
            skipped += 1
    print(f"      → {len(rows)} 行 / 日時欠落 {skipped} 件スキップ\n")

    if args.dry_run:
        print("[3/3] DRY-RUN — Supabase 書き込みはスキップ")
        for r in rows[:3]:
            print("       sample:", {k: (str(v)[:60] + "…") if len(str(v)) > 60 else v
                                       for k, v in r.items()})
        return

    print(f"[3/3] Supabase {SUPABASE_TABLE} に upsert")
    n = upsert_to_supabase(rows)
    print(f"\n✓ 完了: {n} 行 反映 ({len(rows) - n} 件 失敗)")


if __name__ == "__main__":
    main()
