"""
理由分析の精度A/B評価スクリプト（オフライン・投稿なし）
========================================================

「APIに聞くだけ」の現行方式に対し、当日の一次情報を渡すと精度が上がるかを
統合前に検証する。同じ銘柄・同じ入力で3変種の理由を生成し、目視採点用の
CSVに並べて出力する。

  ① baseline : 現行プロンプトそのまま（analyzer.py と同一）
  ② +TDnet   : ①に当日の適時開示タイトル（TDnet）を追加
  ③ +news    : ①に当日のニュース見出し（株探）を追加

材料の取得窓は「前営業日15:00 〜 当日いっぱい」。引け後開示が翌営業日の
急騰理由になる典型パターンを拾うため。

使い方:
    py eval_reason_accuracy.py --from-kyutou-log            # kyutou検知ログ全件
    py eval_reason_accuracy.py --from-notes 10              # 直近のnotes 10件
    py eval_reason_accuracy.py --codes 3687:2026-05-29,6994:2026-05-29
    py eval_reason_accuracy.py --from-kyutou-log --limit 5  # 件数制限
    py eval_reason_accuracy.py --codes 3687:2026-05-29 --dry-run  # 材料取得のみ(Claude呼ばない)

出力:
    eval_results_<時刻>.csv   … 採点用（Excelで開いて 採点①②③/メモ を記入）
    eval_outputs/<時刻>/      … 各変種のClaude全文出力（参照用）

既存パイプライン（analyzer/storage/poster等）には一切手を入れない。
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 既存モジュールを再利用（本番と同一条件で比較するため）
from fetcher import Tweet
from analyzer import SYSTEM_PROMPT, _build_analysis_prompt, _parse_output
from config import ANTHROPIC_API_KEY, BASE_DIR
from materials import fetch_tdnet_titles, fetch_kabutan_headlines

JST = timezone(timedelta(hours=9))
MODEL = "claude-sonnet-4-6"  # analyzer.py と同じ

KYUTOU_LOG_CANDIDATES = [
    BASE_DIR / "kyutou_logs" / "detections.csv",
    Path(r"C:\Users\moris\kyutou_detector\logs\detections.csv"),
]


# ---------------------------------------------------------------------------
# 評価対象の読み込み
# ---------------------------------------------------------------------------

def load_from_kyutou_logs() -> list[dict]:
    """kyutou検知ログ(detections.csv)から評価対象を作る。重複コード+日付は除去。"""
    entries: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for path in KYUTOU_LOG_CANDIDATES:
        if not path.exists():
            continue
        with open(path, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                day = row["detected_at"][:10]
                key = (row["code"], day)
                if key in seen:
                    continue
                seen.add(key)
                text = (
                    f"【急騰検知】{row['name']}（{row['code']}）\n"
                    f"株価 {float(row['price']):.0f}円 / 前日比 +{row['change_pct']}% / "
                    f"出来高比 {row['volume_ratio']}倍 / 売買代金 {row['turnover_oku']}億円"
                )
                entries.append({
                    "code": row["code"], "name": row["name"], "date": day,
                    "source": "kyutou", "text": text,
                })
    return entries


def load_from_notes(n: int) -> list[dict]:
    """notes/*.md の直近N件から評価対象を作る（@shodousan由来の元ポストを使用）。"""
    notes_dir = BASE_DIR / "notes"
    files = sorted(notes_dir.glob("*.md"), reverse=True)
    files = [f for f in files if f.name != "index.md"][:n]
    entries = []
    for path in files:
        m = re.match(r"(\d{8})_\d{4}_([0-9A-Za-z]+)\.md", path.name)
        if not m:
            continue
        day = f"{m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:8]}"
        code = m.group(2)
        text_md = path.read_text(encoding="utf-8")
        name_m = re.match(r"# \S+ (.+)", text_md.splitlines()[0])
        name = name_m.group(1).strip() if name_m else code
        # 「## 元ポスト」の引用部を抽出
        quote_lines = [l[2:] for l in text_md.splitlines() if l.startswith("> ")]
        source_text = "\n".join(quote_lines).strip() or text_md.splitlines()[0]
        entries.append({"code": code, "name": name, "date": day,
                        "source": "notes", "text": source_text})
    return entries


def load_from_codes(spec: str) -> list[dict]:
    """--codes 3687:2026-05-29,6994:2026-05-29 形式。"""
    entries = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            code, day = part.split(":", 1)
        else:
            code, day = part, datetime.now(JST).strftime("%Y-%m-%d")
        entries.append({
            "code": code, "name": code, "date": day, "source": "manual",
            "text": f"【急騰検知】（{code}）が急騰。詳細データなし。",
        })
    return entries


# ---------------------------------------------------------------------------
# 材料の取得は materials.py（本番と共通実装）を使用
# ---------------------------------------------------------------------------
# 3変種のプロンプト構築と生成
# ---------------------------------------------------------------------------

def build_prompts(entry: dict, tdnet: list[str], news: list[str]) -> dict[str, str]:
    """① baseline ② +TDnet ③ +news の userプロンプトを返す。"""
    tweet = Tweet(id=f"eval-{entry['code']}", text=entry["text"],
                  created_at=datetime.now(JST), stock_codes=[entry["code"]])
    base = _build_analysis_prompt(tweet)  # 本番と同一

    tdnet_block = "\n".join(f"・{t}" for t in tdnet) if tdnet else "（窓内の開示なし）"
    news_block = "\n".join(f"・{t}" for t in news) if news else "（窓内のニュースなし）"

    with_tdnet = base + (
        f"\n\n【参考: 当日のTDnet適時開示（前営業日15時以降〜当日）】\n{tdnet_block}\n"
        "上記の開示が急騰の材料である可能性を最優先で検討すること。"
        "該当する開示がなければ、開示以外の材料を慎重に検討すること。"
    )
    with_news = base + (
        f"\n\n【参考: 当日のニュース見出し（株探、前営業日15時以降〜当日）】\n{news_block}\n"
        "上記の見出しが急騰の材料である可能性を最優先で検討すること。"
        "該当する見出しがなければ、それ以外の材料を慎重に検討すること。"
    )
    return {"baseline": base, "tdnet": with_tdnet, "news": with_news}


def call_claude(client, user_prompt: str, max_attempts: int = 3) -> str:
    """analyzer と同条件（モデル・system・max_tokens）で1回生成する。"""
    last = None
    for attempt in range(1, max_attempts + 1):
        try:
            with client.messages.stream(
                model=MODEL, max_tokens=2048, system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            ) as stream:
                msg = stream.get_final_message()
            return "".join(b.text for b in msg.content if b.type == "text")
        except Exception as e:
            last = e
            if attempt == max_attempts:
                raise
            wait = 5 * attempt
            print(f"  [Claude] retry in {wait}s: {e}")
            time.sleep(wait)
    raise last


def extract_reason(raw: str, entry: dict) -> str:
    """生成全文から「(理由)によるもの」の理由部分を取り出す。"""
    tweet = Tweet(id="x", text=entry["text"], created_at=datetime.now(JST),
                  stock_codes=[entry["code"]])
    return _parse_output(raw, tweet).reason


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description="理由分析の精度A/B評価（オフライン）")
    ap.add_argument("--from-kyutou-log", action="store_true", help="kyutou検知ログから評価対象を読む")
    ap.add_argument("--from-notes", type=int, metavar="N", help="直近のnotes N件を評価対象に追加")
    ap.add_argument("--codes", help="手動指定: 3687:2026-05-29,6994:2026-05-29")
    ap.add_argument("--limit", type=int, help="評価対象の最大件数")
    ap.add_argument("--dry-run", action="store_true", help="材料取得まで（Claudeを呼ばない）")
    args = ap.parse_args(argv)

    entries: list[dict] = []
    if args.from_kyutou_log:
        entries += load_from_kyutou_logs()
    if args.from_notes:
        entries += load_from_notes(args.from_notes)
    if args.codes:
        entries += load_from_codes(args.codes)
    if not entries:
        ap.error("評価対象がありません (--from-kyutou-log / --from-notes N / --codes を指定)")
    if args.limit:
        entries = entries[: args.limit]

    if not args.dry_run and not ANTHROPIC_API_KEY:
        sys.exit("ANTHROPIC_API_KEY がありません (.env)")

    stamp = datetime.now(JST).strftime("%Y%m%d_%H%M")
    out_csv = BASE_DIR / f"eval_results_{stamp}.csv"
    out_dir = BASE_DIR / "eval_outputs" / stamp
    out_dir.mkdir(parents=True, exist_ok=True)

    client = None
    if not args.dry_run:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    header = [
        "code", "name", "date", "source",
        "tdnet_titles", "news_titles",
        "reason_baseline", "reason_tdnet", "reason_news",
        "採点①baseline", "採点②tdnet", "採点③news", "正しい材料/メモ",
    ]
    rows = []
    n_calls = 0

    for i, e in enumerate(entries, 1):
        print(f"\n[{i}/{len(entries)}] {e['code']} {e['name']} ({e['date']}, {e['source']})")
        tdnet = fetch_tdnet_titles(e["code"], e["date"])
        news = fetch_kabutan_headlines(e["code"], e["date"])
        print(f"  TDnet開示: {len(tdnet)}件 / ニュース見出し: {len(news)}件")

        reasons = {"baseline": "", "tdnet": "", "news": ""}
        if not args.dry_run:
            prompts = build_prompts(e, tdnet, news)
            for variant, prompt in prompts.items():
                print(f"  生成中: {variant} ...")
                raw = call_claude(client, prompt)
                n_calls += 1
                reasons[variant] = extract_reason(raw, e)
                (out_dir / f"{e['code']}_{e['date']}_{variant}.txt").write_text(
                    f"=== PROMPT ===\n{prompt}\n\n=== OUTPUT ===\n{raw}",
                    encoding="utf-8",
                )

        rows.append([
            e["code"], e["name"], e["date"], e["source"],
            " / ".join(tdnet), " / ".join(news),
            reasons["baseline"], reasons["tdnet"], reasons["news"],
            "", "", "", "",
        ])

    with open(out_csv, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)

    print(f"\n[OK] 採点用CSV: {out_csv}")
    print(f"[OK] 全文出力 : {out_dir}")
    if not args.dry_run:
        print(f"[INFO] Claude呼び出し: {n_calls}回 (model={MODEL})")
    print("\nExcelでCSVを開き、採点①②③に ○/×/△ と「正しい材料/メモ」を記入してください。")


if __name__ == "__main__":
    main()
