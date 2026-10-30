"""
特定銘柄だけ分析をやり直すスクリプト
=====================================

既存ノート（notes/*_CODE.md）の「元ポスト」と日時を再利用して、その銘柄だけ
analyzer に通し直す。材料取得ロジック（materials.py の窓・ノイズ除去）を直したあと、
過去に「材料なし→推測」で誤った銘柄を作り直すのに使う。

- 同じ日時で保存するので、既存ノートを **上書き** する（Notion / サイト(Supabase) も更新）。
- 当日重複防止(daily_codes)には引っかからない（save_analysis はチェックしない）。
  投稿する場合も force=True で突破するので、同じ銘柄を何度でもやり直せる。

使い方:
    py redo_stock.py 6522                 # 最新の6522ノートを作り直し（保存のみ・X投稿なし）
    py redo_stock.py 6522 --date 2026-07-01   # 日付でノートを指定
    py redo_stock.py 6522 --dry-run       # 保存せず新しい分析結果を表示するだけ（確認用）
    py redo_stock.py 6522 --slack         # 作り直して Slack 承認 → X 投稿まで
    py redo_stock.py 6522 --auto-post     # 作り直して承認なしで X 投稿
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

from config import BASE_DIR
from fetcher import Tweet

JST = timezone(timedelta(hours=9))
NOTES_DIR = BASE_DIR / "notes"

# ファイル名: 20260701_0927_6522.md / 20260319_0349_(6522).md
_FNAME_RE = re.compile(r"^(\d{8})_(\d{4})_")
# ノート内の元ポストブロック（"## 元ポスト" 〜 次の "---" まで、"> " 行を拾う）
_TWEETID_RE = re.compile(r"\*\*ツイートID\*\*:\s*\[([^\]]+)\]")


def _find_note(code: str, date: str | None) -> Path | None:
    """code のノートを探す。date(YYYY-MM-DD)指定があればその日、無ければ最新。"""
    candidates = sorted(
        p for p in NOTES_DIR.glob(f"*_{code}.md")
        if _FNAME_RE.match(p.name)
    )
    # 旧形式 "(6522)" のファイル名も拾う
    candidates += sorted(
        p for p in NOTES_DIR.glob(f"*_({code}).md")
        if _FNAME_RE.match(p.name)
    )
    if not candidates:
        return None
    if date:
        ymd = date.replace("-", "")
        for p in candidates:
            if p.name.startswith(ymd):
                return p
        return None
    return max(candidates, key=lambda p: p.name)


def _reconstruct_tweet(note: Path, code: str) -> Tweet:
    """ノートから元ポスト本文・日時・ツイートIDを復元して Tweet を組み立てる。"""
    m = _FNAME_RE.match(note.name)
    dt = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M").replace(tzinfo=JST)

    text_lines: list[str] = []
    tweet_id = f"kyutou-{dt.strftime('%Y%m%d')}-{code}"
    in_post = False
    for line in note.read_text(encoding="utf-8").splitlines():
        idm = _TWEETID_RE.search(line)
        if idm:
            tweet_id = idm.group(1)
        if line.startswith("## 元ポスト"):
            in_post = True
            continue
        if in_post:
            if line.strip() == "---":
                break
            if line.lstrip().startswith(">"):
                text_lines.append(line.lstrip()[1:].strip())

    text = "\n".join(l for l in text_lines if l)
    if not text:
        raise ValueError(f"元ポストが読み取れませんでした: {note.name}")
    return Tweet(id=tweet_id, text=text, created_at=dt, stock_codes=[code])


def main() -> int:
    parser = argparse.ArgumentParser(description="特定銘柄だけ分析をやり直す")
    parser.add_argument("code", help="銘柄コード（例: 6522）")
    parser.add_argument("--date", help="対象ノートの日付 YYYY-MM-DD（省略時は最新）")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true",
                        help="保存せず新しい分析結果を表示するだけ")
    parser.add_argument("--post", action="store_true", help="ターミナルで投稿確認")
    parser.add_argument("--slack", action="store_true", help="Slack 承認 → X 投稿")
    parser.add_argument("--auto-post", dest="auto_post", action="store_true",
                        help="承認なしで X 投稿")
    args = parser.parse_args()

    code = args.code.strip()
    note = _find_note(code, args.date)
    if not note:
        where = f"（日付 {args.date}）" if args.date else ""
        print(f"[ABORT] {code} のノート{where}が見つかりませんでした")
        return 1

    print(f"[INFO] 対象ノート: {note.name}")
    tweet = _reconstruct_tweet(note, code)
    print(f"[INFO] 元ポスト: {tweet.text[:70].replace(chr(10), ' ')}...")
    print(f"[INFO] 日時: {tweet.created_at.strftime('%Y-%m-%d %H:%M JST')} / id={tweet.id}\n")

    from analyzer import analyze_tweet
    analysis = analyze_tweet(tweet)

    print("\n" + "=" * 50)
    print(f"銘柄: {analysis.stock_code} {analysis.stock_name}")
    print(f"理由: {analysis.reason}")
    print("=" * 50)

    if args.dry_run:
        print("\n[DRY-RUN] 保存・投稿はしていません")
        return 0

    from storage import save_analysis, update_index
    from viewer import generate_html

    path = save_analysis(analysis)
    update_index()
    generate_html()
    print(f"\n[OK] 上書き保存: {path.name}（Notion / サイトも更新）")

    if args.post or args.slack or args.auto_post:
        from main import _queue_post_requests, _resume_pending_posts
        _queue_post_requests([analysis], force=True)
        _resume_pending_posts(post=args.post, slack=args.slack,
                              auto=args.auto_post, reason="redo-stock")
    else:
        print("[INFO] 投稿フラグなし（--slack/--post/--auto-post）、上書き保存のみ")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
