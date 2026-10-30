"""
ストレージ管理
分析結果をMarkdownファイルに保存し、インデックスを更新する
"""

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from analyzer import Analysis
from config import NOTES_DIR


STATE_FILE = Path(__file__).parent / ".state.json"

JST = timezone(timedelta(hours=9))


def _to_jst(dt: datetime) -> datetime:
    """tweet.created_at をJSTに変換する。

    @shodousan経由はUTC、kyutou経由はJSTで入ってくるため、表示・保存前に
    必ずここを通して統一する。naive（tz情報なし）はUTCとみなす。
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(JST)


def _default_state() -> dict:
    return {
        "last_tweet_id": None,
        "processed_ids": [],
        "last_posted_tweet_id": None,
        "posted_ids": [],
        "pending_posts": [],
        # クロスソースの当日重複防止: {"YYYY-MM-DD": ["5367", ...]}
        # @shodousan と自前検知(kyutou)の両方を通じて、同一銘柄は1日1回だけ処理する。
        "daily_codes": {},
    }


def _load_state() -> dict:
    """前回実行時の状態を読み込む（差分取得用）"""
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            loaded = json.load(f)
        state = _default_state()
        state.update(loaded)
        state["processed_ids"] = list(state.get("processed_ids") or [])
        state["posted_ids"] = list(state.get("posted_ids") or [])
        state["pending_posts"] = list(state.get("pending_posts") or [])
        state["daily_codes"] = dict(state.get("daily_codes") or {})
        return state
    return _default_state()


def _maybe_int(value) -> int | None:
    """数値IDのみ int に変換する。自前検知の文字列ID(kyutou-...)は None を返す。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _today_jst() -> str:
    return datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d")


def _normalize_code(code: str) -> str:
    """銘柄コードの表記ゆれ（テンプレート由来の括弧など）を正規化する。

    Claude出力が「(5982)」のように括弧付きで返ることがあり、素の「5982」と
    別物扱いになると当日重複防止が突破されるため、比較前に必ず剥がす。
    """
    t = (code or "").strip()
    while len(t) >= 2 and t[0] in "(（" and t[-1] in ")）":
        t = t[1:-1].strip()
    return t


def was_code_handled_today(code: str) -> bool:
    """この銘柄コードが本日すでに処理（記録/投稿対象化）済みか。

    @shodousan と自前検知(kyutou)をまたいだ重複防止に使う。
    """
    code = _normalize_code(code)
    if not code:
        return False
    state = _load_state()
    today_codes = state.get("daily_codes", {}).get(_today_jst(), [])
    return code in {_normalize_code(c) for c in today_codes}


def mark_code_handled_today(code: str) -> None:
    """この銘柄コードを本日処理済みとして記録する（直近7日分のみ保持）。"""
    code = _normalize_code(code)
    if not code:
        return
    state = _load_state()
    daily = state.setdefault("daily_codes", {})
    codes = daily.setdefault(_today_jst(), [])
    if code not in codes:
        codes.append(code)
    if len(daily) > 7:
        for old_date in sorted(daily.keys())[:-7]:
            daily.pop(old_date, None)
    _save_state(state)


def _save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def get_last_tweet_id() -> str | None:
    """最後に処理したツイートIDを返す（差分取得に使用）"""
    return _load_state().get("last_tweet_id")


def get_last_posted_tweet_id() -> str | None:
    return _load_state().get("last_posted_tweet_id")


def get_pending_posts() -> list[dict]:
    return list(_load_state().get("pending_posts", []))


def queue_pending_post(
    tweet_id: str,
    stock_code: str,
    stock_name: str,
    post_text: str,
    *,
    reason: str = "",
    business: str = "",
    keywords: str = "",
    backgrounds: list[str] | None = None,
    force: bool = False,
) -> bool:
    """Queue a post for approval, optionally storing analysis fields needed for
    regenerating the chart image after a process restart.

    Args:
        force: If True, skip the posted_ids check (allows re-queueing of
               already-posted tweets, used by --force-recent).

    Returns:
        True if queued (or updated), False if skipped as already posted.
    """
    state = _load_state()
    if not force and tweet_id in state.get("posted_ids", []):
        return False

    extra_fields = {
        "reason": reason,
        "business": business,
        "keywords": keywords,
        "backgrounds": list(backgrounds or []),
    }

    pending_posts = state.setdefault("pending_posts", [])
    for entry in pending_posts:
        if entry.get("tweet_id") == tweet_id:
            entry["stock_code"] = stock_code
            entry["stock_name"] = stock_name
            entry["post_text"] = post_text
            entry.update(extra_fields)
            _save_state(state)
            return True

    pending_posts.append({
        "tweet_id": tweet_id,
        "stock_code": stock_code,
        "stock_name": stock_name,
        "post_text": post_text,
        **extra_fields,
    })
    _save_state(state)
    return True


def discard_pending_post(tweet_id: str) -> None:
    state = _load_state()
    pending_posts = state.get("pending_posts", [])
    state["pending_posts"] = [entry for entry in pending_posts if entry.get("tweet_id") != tweet_id]
    _save_state(state)


def mark_posted(tweet_id: str) -> None:
    state = _load_state()
    pending_posts = state.get("pending_posts", [])
    state["pending_posts"] = [entry for entry in pending_posts if entry.get("tweet_id") != tweet_id]

    posted_ids = state.setdefault("posted_ids", [])
    if tweet_id not in posted_ids:
        posted_ids.append(tweet_id)

    tid = _maybe_int(tweet_id)
    if tid is not None:
        last_posted = _maybe_int(state.get("last_posted_tweet_id"))
        if last_posted is None or tid > last_posted:
            state["last_posted_tweet_id"] = tweet_id

    _save_state(state)


def _format_markdown(analysis: Analysis) -> str:
    """Analysis を Markdown テキストに変換する"""
    tweet = analysis.tweet
    jst_time = _to_jst(tweet.created_at).strftime("%Y-%m-%d %H:%M")

    # 背景を箇条書きに
    backgrounds_md = "\n".join(f"・{bg}" for bg in analysis.backgrounds) if analysis.backgrounds else "・（情報不足）"

    # テンプレートに沿った形式
    template_section = f"""{analysis.reason}によるもの

事業内容:{analysis.business}
キーワード:{analysis.keywords}
{backgrounds_md}

関連銘柄
{analysis.related_stocks if analysis.related_stocks else "（該当なし）"}"""

    tweet_text_quoted = tweet.text.replace("\n", "  \n> ")
    return f"""# {analysis.stock_code} {analysis.stock_name}

**日時**: {jst_time} JST
**ツイートID**: [{tweet.id}](https://x.com/{_get_username()}/status/{tweet.id})

---

## 元ポスト

> {tweet_text_quoted}

---

## 株価上昇理由分析

{template_section}

---

*分析日時: {datetime.now(JST).strftime("%Y-%m-%d %H:%M")} JST*
"""


def _get_username() -> str:
    from config import TARGET_USERNAME
    return TARGET_USERNAME


def save_analysis(analysis: Analysis) -> Path:
    """
    分析結果をMarkdownファイルに保存する

    Returns:
        保存したファイルのパス
    """
    tweet = analysis.tweet
    date_str = _to_jst(tweet.created_at).strftime("%Y%m%d_%H%M")
    code = analysis.stock_code.replace("/", "-")
    filename = f"{date_str}_{code}.md"
    filepath = NOTES_DIR / filename

    content = _format_markdown(analysis)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"  [保存] {filepath.name}")

    # Notion に保存
    from notion_storage import save_to_notion
    save_to_notion(analysis)

    # サイトへ同期（Supabase）
    from site_sync import sync_to_site
    sync_to_site(analysis)

    # 状態を更新
    state = _load_state()
    tid = _maybe_int(tweet.id)
    if tid is not None:
        # last_tweet_id は @shodousan の差分取得(since_id)用。数値IDのみ更新する。
        current = _maybe_int(state.get("last_tweet_id"))
        if current is None or tid > current:
            state["last_tweet_id"] = tweet.id
    if tweet.id not in state.get("processed_ids", []):
        state.setdefault("processed_ids", []).append(tweet.id)
    _save_state(state)

    return filepath


def save_analyses(analyses: list[Analysis]) -> list[Path]:
    """複数の分析結果を保存しインデックスを更新する"""
    paths = []
    for analysis in analyses:
        path = save_analysis(analysis)
        paths.append(path)

    if paths:
        update_index()

    return paths


def update_index() -> None:
    """notes/index.md を全ファイルから再生成する"""
    md_files = sorted(NOTES_DIR.glob("*.md"), reverse=True)
    md_files = [f for f in md_files if f.name != "index.md"]

    lines = [
        "# 株価上昇理由メモ (@shodousan)\n",
        f"*最終更新: {datetime.now(JST).strftime('%Y-%m-%d %H:%M')} JST*\n",
        "",
        "---",
        "",
        "| 日時 | 銘柄 | 理由 | キーワード |",
        "|------|------|------|-----------|",
    ]

    for path in md_files:
        try:
            text = path.read_text(encoding="utf-8")
            # ヘッダー行から銘柄情報を取得
            first_line = text.split("\n")[0].replace("# ", "")
            # 日時を抽出
            date_line = next((l for l in text.split("\n") if l.startswith("**日時**:")), "")
            date_val = date_line.replace("**日時**:", "").strip()
            # 理由を抽出
            reason_line = next((l for l in text.split("\n") if "によるもの" in l), "")
            reason = reason_line.replace("によるもの", "").strip()
            # キーワードを抽出
            kw_line = next((l for l in text.split("\n") if l.startswith("キーワード:")), "")
            kw = kw_line.replace("キーワード:", "").strip()

            lines.append(f"| {date_val} | [{first_line}]({path.name}) | {reason} | {kw} |")
        except Exception:
            continue

    lines.append("")
    index_path = NOTES_DIR / "index.md"
    with open(index_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"[INDEX] {index_path} を更新しました ({len(md_files)} 件)")
