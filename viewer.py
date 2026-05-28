"""
HTMLビューワー生成
notes/ 以下のMarkdownを読みやすいHTMLに変換する
"""

from pathlib import Path
from datetime import datetime, timezone
from config import NOTES_DIR

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>株価上昇理由メモ</title>
<style>
  body {{ font-family: 'Hiragino Kaku Gothic Pro', 'Meiryo', sans-serif;
         background: #0d1117; color: #e6edf3; max-width: 1000px;
         margin: 0 auto; padding: 20px; }}
  h1 {{ color: #58a6ff; border-bottom: 2px solid #21262d; padding-bottom: 10px; }}
  h2 {{ color: #79c0ff; margin-top: 0; }}
  .card {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px;
           padding: 20px; margin-bottom: 20px; }}
  .card:hover {{ border-color: #58a6ff; }}
  .meta {{ color: #8b949e; font-size: 0.85em; margin-bottom: 12px; }}
  .tweet {{ background: #0d1117; border-left: 3px solid #58a6ff;
            padding: 10px 15px; margin: 10px 0; font-size: 0.9em;
            border-radius: 0 4px 4px 0; white-space: pre-wrap; }}
  .template {{ background: #0d1117; border: 1px solid #30363d; border-radius: 6px;
               padding: 15px; margin: 10px 0; }}
  .reason {{ font-size: 1.15em; font-weight: bold; color: #ffa657; }}
  .keyword-label {{ color: #8b949e; font-size: 0.85em; }}
  .keyword {{ color: #79c0ff; }}
  .bg-item {{ margin: 6px 0; padding-left: 10px; border-left: 2px solid #30363d; }}
  .related {{ margin-top: 12px; padding-top: 12px; border-top: 1px solid #21262d; }}
  .related-title {{ color: #8b949e; font-size: 0.85em; margin-bottom: 6px; }}
  .tag {{ display: inline-block; background: #21262d; border-radius: 20px;
          padding: 2px 10px; font-size: 0.8em; margin: 2px; color: #79c0ff; }}
  .updated {{ color: #8b949e; font-size: 0.8em; text-align: right; margin-bottom: 20px; }}
  a {{ color: #58a6ff; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  .count {{ color: #8b949e; font-size: 0.9em; }}
</style>
</head>
<body>
<h1>📈 株価上昇理由メモ</h1>
<div class="updated">@shodousan | 最終更新: {updated} UTC | {count} 件</div>
{cards}
</body>
</html>"""

CARD_TEMPLATE = """<div class="card">
  <h2>{stock_code} {stock_name}</h2>
  <div class="meta">{date} &nbsp;|&nbsp; <a href="https://x.com/{username}/status/{tweet_id}" target="_blank">元ポスト →</a></div>
  <div class="tweet">{tweet_text}</div>
  <div class="template">
    <div class="reason">🔼 {reason}によるもの</div>
    <div style="margin: 8px 0"><span class="keyword-label">キーワード: </span><span class="keyword">{keywords}</span></div>
    {backgrounds_html}
    <div class="related">
      <div class="related-title">関連銘柄</div>
      <div>{related}</div>
    </div>
  </div>
</div>"""


def _parse_md_file(path: Path) -> dict | None:
    """Markdownファイルを解析してdict形式に変換する"""
    try:
        text = path.read_text(encoding="utf-8")
        lines = text.split("\n")

        data = {
            "stock_code": "",
            "stock_name": "",
            "date": "",
            "tweet_id": "",
            "tweet_text": "",
            "reason": "",
            "keywords": "",
            "backgrounds": [],
            "related": "",
        }

        in_tweet = False
        in_analysis = False
        in_related = False
        tweet_lines = []

        for line in lines:
            if line.startswith("# "):
                parts = line[2:].strip().split(" ", 1)
                data["stock_code"] = parts[0]
                data["stock_name"] = parts[1] if len(parts) > 1 else ""

            elif line.startswith("**日時**:"):
                data["date"] = line.replace("**日時**:", "").strip()

            elif line.startswith("**ツイートID**:"):
                # [tweet_id](url) から ID を抽出
                import re
                m = re.search(r'\[(\d+)\]', line)
                if m:
                    data["tweet_id"] = m.group(1)

            elif line.strip() == "> " or line.startswith("> "):
                tweet_lines.append(line[2:].replace("  \n", "\n"))

            elif "によるもの" in line and not line.startswith("関連"):
                data["reason"] = line.replace("によるもの", "").strip()
                in_analysis = True

            elif line.startswith("キーワード:"):
                data["keywords"] = line.replace("キーワード:", "").strip()

            elif line.startswith("・"):
                data["backgrounds"].append(line[1:].strip())

            elif line.strip() == "関連銘柄":
                in_related = True

            elif in_related and line.strip() and not line.startswith("*"):
                data["related"] += line.strip() + "<br>"

        data["tweet_text"] = "\n".join(tweet_lines).strip()
        return data

    except Exception as e:
        print(f"[WARN] {path.name} のパース失敗: {e}")
        return None


def generate_html() -> Path:
    """
    notes/ 以下のMarkdownを読みHTMLビューワーを生成する

    Returns:
        生成したHTMLファイルのパス
    """
    from config import TARGET_USERNAME

    md_files = sorted(NOTES_DIR.glob("*.md"), reverse=True)
    md_files = [f for f in md_files if f.name != "index.md"]

    cards_html = ""
    for path in md_files:
        data = _parse_md_file(path)
        if not data:
            continue

        backgrounds_html = "\n".join(
            f'<div class="bg-item">・{bg}</div>'
            for bg in data["backgrounds"]
        )

        card = CARD_TEMPLATE.format(
            stock_code=data["stock_code"],
            stock_name=data["stock_name"],
            date=data["date"],
            username=TARGET_USERNAME,
            tweet_id=data["tweet_id"],
            tweet_text=data["tweet_text"].replace("<", "&lt;").replace(">", "&gt;"),
            reason=data["reason"],
            keywords=data["keywords"],
            backgrounds_html=backgrounds_html,
            related=data["related"] or "（該当なし）",
        )
        cards_html += card

    if not cards_html:
        cards_html = '<div class="card"><p>まだメモがありません。main.py を実行してください。</p></div>'

    html = HTML_TEMPLATE.format(
        updated=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        count=len(md_files),
        cards=cards_html,
    )

    output_path = NOTES_DIR / "index.html"
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"[HTML] {output_path} を生成しました")
    return output_path
