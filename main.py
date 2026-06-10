"""
メインスクリプト
@shodousan のツイートを取得・分析してメモに保存する

使い方:
  python main.py              # 通常実行（差分取得）
  python main.py --all        # 全件取得（since_id を無視）
  python main.py --demo       # デモモード（X APIなしでテスト）
  python main.py --html-only  # HTMLのみ再生成
"""

import sys
import io
import time
import argparse

# Windows cp932 コンソールで絵文字が出力できない問題を回避
def _configure_console_stream(stream):
    encoding = (getattr(stream, "encoding", "") or "").lower()
    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
        kwargs = {
            "errors": "replace",
            "line_buffering": True,
            "write_through": True,
        }
        if encoding.startswith("cp"):
            kwargs["encoding"] = "utf-8"
        reconfigure(**kwargs)
        return stream

    buffer = getattr(stream, "buffer", None)
    if buffer is None:
        return stream

    if not encoding.startswith("cp"):
        return stream

    return io.TextIOWrapper(
        buffer,
        encoding="utf-8",
        errors="replace",
        line_buffering=True,
        write_through=True,
    )


sys.stdout = _configure_console_stream(sys.stdout)
sys.stderr = _configure_console_stream(sys.stderr)
from pathlib import Path
from datetime import datetime, timezone, timedelta


def get_japanese_greeting() -> str:
    """現在時刻に応じた日本語の挨拶を返す"""
    jst = timezone(timedelta(hours=9))
    hour = datetime.now(jst).hour
    if 5 <= hour < 11:
        return "おはようございます！"
    elif 11 <= hour < 18:
        return "こんにちは！"
    else:
        return "こんばんは！"


def run_demo():
    """X APIなしで動作確認するデモモード"""
    from fetcher import Tweet
    from analyzer import analyze_tweet
    from storage import save_analyses
    from viewer import generate_html
    from datetime import datetime, timezone

    print("=" * 50)
    print(f"{get_japanese_greeting()} デモモード: サンプルツイートで動作確認")
    print("=" * 50)

    demo_tweets = [
        Tweet(
            id="1234567890001",
            text="7203 トヨタが第3四半期決算で過去最高益を更新。北米市場でのHV車販売が好調で、為替差益も加わり大幅増益。通期予想を上方修正。",
            created_at=datetime.now(timezone.utc),
            stock_codes=["7203"]
        ),
        Tweet(
            id="1234567890002",
            text="9984 ソフトバンクG、AI投資が実を結びビジョンファンドの評価益が急拡大。Arm株の上昇が貢献。反転攻勢の初動か。",
            created_at=datetime.now(timezone.utc),
            stock_codes=["9984"]
        ),
    ]

    from analyzer import analyze_tweets
    print(f"\n{len(demo_tweets)} 件のサンプルツイートを分析中...\n")
    analyses = analyze_tweets(demo_tweets)

    if analyses:
        paths = save_analyses(analyses)
        print(f"\n✅ {len(paths)} 件のメモを保存しました")
        html_path = generate_html()
        print(f"✅ HTMLビューワー: {html_path.absolute()}")
    else:
        print("❌ 分析結果がありません")


def run_setup():
    """Cookie-EditorでエクスポートしたJSONからPlaywright用認証状態を生成する"""
    import json
    from config import AUTH_STATE_FILE, BASE_DIR

    print("=" * 50)
    print("X クッキーセットアップ")
    print("=" * 50)

    export_file = BASE_DIR / "x_cookies_export.json"

    if not export_file.exists():
        print(f"""
x_cookies_export.json が見つかりません。

以下の手順でクッキーをエクスポートしてください:

1. Chrome拡張「Cookie-Editor」をインストール
   https://chrome.google.com/webstore/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalmdm

2. Chrome で https://x.com を開いてログイン

3. アドレスバー右の Cookie-Editor アイコンをクリック
   → 下部の「Export」→「Export as JSON」をクリック

4. コピーした内容を以下のファイルとして保存:
   {export_file}

5. 再度 python main.py --setup を実行
""")
        return

    try:
        with open(export_file, encoding="utf-8") as f:
            raw_cookies = json.load(f)
    except Exception as e:
        print(f"❌ ファイルの読み込みに失敗しました: {e}")
        return

    same_site_map = {
        "no_restriction": "None",
        "lax": "Lax",
        "strict": "Strict",
        "unspecified": "Lax",
    }

    playwright_cookies = []
    for c in raw_cookies:
        domain = c.get("domain", ".x.com")
        raw_same_site = c.get("sameSite", "Lax")
        same_site = same_site_map.get(raw_same_site.lower(), raw_same_site) if raw_same_site else "Lax"
        playwright_cookies.append({
            "name": c.get("name", ""),
            "value": c.get("value", ""),
            "domain": domain if domain.startswith(".") else f".{domain}",
            "path": c.get("path") or "/",
            "expires": float(c.get("expirationDate", -1)) if c.get("expirationDate") else -1,
            "httpOnly": bool(c.get("httpOnly", False)),
            "secure": bool(c.get("secure", False)),
            "sameSite": same_site,
        })

    state = {"cookies": playwright_cookies, "origins": []}
    with open(AUTH_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

    print(f"✅ {len(playwright_cookies)} 件のクッキーを保存しました")
    print("python main.py --latest または --watch で使えます")


def _queue_post_requests(analyses, *, force: bool = False) -> int:
    from poster import build_post_text
    from storage import queue_pending_post

    queued = 0
    for analysis in analyses:
        queue_pending_post(
            analysis.tweet.id,
            analysis.stock_code,
            analysis.stock_name,
            build_post_text(analysis),
            reason=analysis.reason,
            business=analysis.business,
            keywords=analysis.keywords,
            backgrounds=analysis.backgrounds,
            force=force,
        )
        queued += 1
    return queued


def _resume_pending_posts(*, post: bool = False, slack: bool = False,
                          auto: bool = False, reason: str = "resume"):
    from storage import get_pending_posts

    if not (post or slack or auto):
        return []

    pending_count = len(get_pending_posts())
    if not pending_count:
        return []

    print(f"\n[INFO] {reason}: resuming {pending_count} pending post(s)")
    if auto:
        from poster import process_pending_auto_queue
        return process_pending_auto_queue()
    if slack:
        from slack_notifier import process_pending_slack_queue
        return process_pending_slack_queue()

    from poster import process_pending_terminal_queue
    return process_pending_terminal_queue()


def run_latest(post: bool = False, slack: bool = False, auto: bool = False):
    """最新の株関連ツイート1件を取得して分析する"""
    from fetcher import fetch_latest_tweet
    from analyzer import analyze_tweet
    from storage import save_analysis
    from viewer import generate_html

    print("=" * 50)
    print(f"{get_japanese_greeting()} 最新ツイート分析モード")
    print("=" * 50)

    tweet = fetch_latest_tweet()
    if not tweet:
        print("株関連ツイートが見つかりませんでした")
        return

    print(f"\n[INFO] 最新ツイート: {tweet.text[:60]}...\n")
    try:
        analysis = analyze_tweet(tweet)
        path = save_analysis(analysis)
        html_path = generate_html()
        print(f"\n[OK] 保存: {path.name}")
        print(f"[HTML] {html_path.absolute()}")
        if slack or post or auto:
            _queue_post_requests([analysis])
            _resume_pending_posts(post=post, slack=slack, auto=auto, reason="latest")
    except Exception as e:
        print(f"[ERROR] エラー: {e}")


def run_watch(interval_min: int = 5, post: bool = False, slack: bool = False):
    """新しいツイートを定期監視して自動分析するループ"""
    from fetcher import fetch_tweets
    from analyzer import analyze_tweet
    from storage import get_last_tweet_id, save_analysis
    from viewer import generate_html

    mode_label = "Slack通知" if slack else "ターミナル確認" if post else "保存のみ"
    print("=" * 50)
    print(f"{get_japanese_greeting()} 監視モード起動 (チェック間隔: {interval_min}分, {mode_label})")
    print("Ctrl+C で停止")
    print("=" * 50)

    def _do_post(analysis):
        if slack:
            from slack_notifier import slack_confirm_and_post
            slack_confirm_and_post(analysis)
        elif post:
            from poster import confirm_and_post
            confirm_and_post(analysis)

    # 初回: 当日の全ツイートを取得して処理
    print("\n[初回] 最新ツイートを取得中...")
    last_id = get_last_tweet_id()
    if not last_id:
        tweets = fetch_tweets(max_count=20, today_only=False)
        if tweets:
            print(f"[INFO] {len(tweets)} 件のツイートを初回処理中...")
            for tweet in tweets:
                print(f"[INFO] 内容: {tweet.text[:60]}...")
                try:
                    analysis = analyze_tweet(tweet)
                    path = save_analysis(analysis)
                    print(f"✅ 分析完了: {path.name}")
                    _do_post(analysis)
                except Exception as e:
                    print(f"❌ 初回分析エラー: {e}")
            generate_html()
            last_id = get_last_tweet_id()
        else:
            print("[WARN] 初回取得で株関連ツイートが見つかりませんでした")

    # 監視ループ
    try:
        while True:
            next_check = datetime.now(timezone(timedelta(hours=9)))
            print(f"\n⏳ 次回チェック: {interval_min}分後 ({next_check.strftime('%H:%M')} JST 現在)")
            time.sleep(interval_min * 60)

            jst = timezone(timedelta(hours=9))
            now_str = datetime.now(jst).strftime("%H:%M")
            print(f"\n🔍 [{now_str}] 新着チェック中...")

            current_last_id = get_last_tweet_id() or last_id
            new_tweets = fetch_tweets(since_id=current_last_id, max_count=20, today_only=False)

            if not new_tweets:
                print("  → 新しい株関連ツイートなし")
                continue

            print(f"  → {len(new_tweets)} 件の新着ツイートを検出！")
            for tweet in new_tweets:
                print(f"\n📌 新着: {tweet.text[:60]}...")
                try:
                    analysis = analyze_tweet(tweet)
                    path = save_analysis(analysis)
                    print(f"  ✅ 分析・保存: {path.name}")
                    _do_post(analysis)
                except Exception as e:
                    print(f"  ❌ 分析エラー: {e}")

            generate_html()
            print("🌐 HTML更新完了")

    except KeyboardInterrupt:
        print("\n\n👋 監視を終了しました")


def run_watch(interval_min: int = 5, post: bool = False, slack: bool = False, auto: bool = False):
    """Watch for new tweets, analyze them, and optionally run posting flows."""
    from fetcher import fetch_tweets
    from analyzer import analyze_tweet
    from storage import get_last_tweet_id, save_analysis
    from viewer import generate_html

    mode_label = (
        "auto post (no approval)" if auto
        else "Slack approval" if slack
        else "terminal approval" if post
        else "save only"
    )
    print("=" * 50)
    print(f"{get_japanese_greeting()} watch mode (interval: {interval_min} min, {mode_label})")
    print("Press Ctrl+C to stop")
    print("=" * 50)

    def _run_post_flow(analyses, reason: str):
        if analyses:
            queued = _queue_post_requests(analyses)
            if queued:
                print(f"[INFO] {reason}: queued {queued} post(s)")
        _resume_pending_posts(post=post, slack=slack, auto=auto, reason=reason)

    def _process_tweets(tweets, phase_label: str):
        # クロスソースの当日重複防止: @shodousan / kyutou をまたいで同一銘柄は1日1回だけ
        from storage import was_code_handled_today, mark_code_handled_today

        analyses = []
        if not tweets:
            print(f"[INFO] {phase_label}: no new stock-related tweets")
            return analyses

        print(f"[INFO] {phase_label}: processing {len(tweets)} tweet(s)")
        for tweet in tweets:
            primary = tweet.stock_codes[0] if tweet.stock_codes else None
            # 分析前チェック（Claude呼び出しを節約）
            if primary and was_code_handled_today(primary):
                print(f"[SKIP] {phase_label}: {primary} は本日処理済み（重複）")
                continue
            print(f"[INFO] {phase_label}: {tweet.text[:60]}...")
            try:
                analysis = analyze_tweet(tweet)
            except Exception as e:
                print(f"[ERROR] {phase_label}: {e}")
                continue
            # 分析後、確定した銘柄コードで再チェック
            code = analysis.stock_code or primary
            if code and was_code_handled_today(code):
                print(f"[SKIP] {phase_label}: {code} は本日処理済み（重複）")
                continue
            try:
                path = save_analysis(analysis)
            except Exception as e:
                print(f"[ERROR] {phase_label}: save failed: {e}")
                continue
            if code:
                mark_code_handled_today(code)
            analyses.append(analysis)
            print(f"[OK] {phase_label}: saved {path.name}")

        if analyses:
            html_path = generate_html()
            print(f"[HTML] {html_path.absolute()}")
            _run_post_flow(analyses, phase_label)

        return analyses

    jst = timezone(timedelta(hours=9))
    _resume_pending_posts(post=post, slack=slack, auto=auto, reason="startup")
    last_id = get_last_tweet_id()

    # 起動時チェック（失敗してもループには入る）
    try:
        if last_id:
            print(f"\n[INFO] startup check: looking for tweets newer than {last_id}")
            startup_tweets = fetch_tweets(since_id=last_id, max_count=20, today_only=False)
            _process_tweets(startup_tweets, "startup")
        else:
            print("\n[INFO] startup check: no saved state, importing latest tweets")
            startup_tweets = fetch_tweets(max_count=20, today_only=False)
            _process_tweets(startup_tweets, "initial import")
    except Exception as e:
        print(f"[ERROR] startup check failed (continuing to watch loop): {e}")

    last_id = get_last_tweet_id() or last_id

    # 2ソースを1ティックごとに交互に拾う。startup が @shodousan のため
    # ループ最初のティックは kyutou から始める（tick=1）。
    sources = ["shodousan", "kyutou"]
    tick = 1

    try:
        while True:
            next_check = datetime.now(jst) + timedelta(minutes=interval_min)
            next_source = sources[tick % len(sources)]
            print(f"\n[WAIT] next check at {next_check.strftime('%H:%M')} JST "
                  f"({interval_min} min, source={next_source})")
            time.sleep(interval_min * 60)

            source = sources[tick % len(sources)]
            tick += 1
            now_str = datetime.now(jst).strftime("%H:%M")
            print(f"\n[CHECK] [{now_str}] source={source}")

            # 1ティックの失敗（X側のDOM変更・ネットワーク断等）でループ全体を
            # 殺さない。エラーを表示して次のティックに進む。
            try:
                if source == "shodousan":
                    current_last_id = get_last_tweet_id() or last_id
                    new_tweets = fetch_tweets(since_id=current_last_id, max_count=20, today_only=False)
                    analyses = _process_tweets(new_tweets, f"shodousan {now_str}")
                    if analyses:
                        last_id = get_last_tweet_id() or last_id
                else:
                    from kyutou_source import fetch_new_surge_tweets
                    surge_tweets = fetch_new_surge_tweets()
                    _process_tweets(surge_tweets, f"kyutou {now_str}")
            except Exception as e:
                print(f"[ERROR] {source} {now_str}: tick failed, will retry next cycle: "
                      f"{type(e).__name__}: {e}")

    except KeyboardInterrupt:
        print("\n\n[STOP] watch mode stopped")


def run_html_only():
    """HTMLのみ再生成する"""
    from viewer import generate_html
    from storage import update_index
    update_index()
    html_path = generate_html()
    print(f"✅ {html_path.absolute()}")


def run_force_recent(count: int, post: bool = False, slack: bool = False, auto: bool = False):
    """直近N件のツイートをposted_idsチェックを無視して強制的に全フロー処理する"""
    from fetcher import fetch_tweets
    from analyzer import analyze_tweets
    from storage import save_analyses
    from viewer import generate_html

    print("=" * 50)
    print(f"{get_japanese_greeting()} 強制再処理モード (直近{count}件)")
    print("=" * 50)

    tweets = fetch_tweets(max_count=count, today_only=False)
    if not tweets:
        print("ツイートが見つかりませんでした")
        return

    tweets = tweets[:count]
    print(f"\n[INFO] {len(tweets)} 件取得 (要求: {count}件)")

    print(f"\n[INFO] Claude で分析中...")
    analyses = analyze_tweets(tweets)
    if not analyses:
        print("分析結果なし")
        return

    save_analyses(analyses)
    html_path = generate_html()
    print(f"\n[OK] {len(analyses)} 件保存")
    print(f"[HTML] {html_path.absolute()}")

    if slack or post or auto:
        print(f"\n📤 投稿フロー ({len(analyses)} 件、強制モード)")
        _queue_post_requests(analyses, force=True)
        _resume_pending_posts(post=post, slack=slack, auto=auto, reason="force-recent")
    else:
        print(f"\n[INFO] 投稿フラグなし(--slack/--post/--auto-post)、分析・保存のみ")


def run_kyutou_once(count: int = 1, post: bool = False, slack: bool = False,
                    auto: bool = False, ignore_hours: bool = False):
    """自前検知(kyutou)を1回走らせ、先頭N件を分析→保存→X投稿まで通す（テスト用）。

    重複抑制はバイパスするので、同じ銘柄でも繰り返し試せる。
    投稿フラグ(--post/--slack/--auto-post)が無い場合は分析・保存まで。
    """
    from kyutou_source import fetch_new_surge_tweets
    from analyzer import analyze_tweets
    from storage import save_analyses
    from viewer import generate_html

    print("=" * 50)
    print(f"{get_japanese_greeting()} 急騰検知(kyutou)単発モード (先頭{count}件)")
    print("=" * 50)

    tweets = fetch_new_surge_tweets(
        market_hours_only=not ignore_hours, limit=count, ignore_dedup=True,
    )
    if not tweets:
        print("[INFO] 条件を満たす急騰銘柄がありませんでした")
        return

    print(f"\n[INFO] {len(tweets)} 件を分析します")
    analyses = analyze_tweets(tweets)
    if not analyses:
        print("分析結果なし")
        return

    save_analyses(analyses)
    html_path = generate_html()
    print(f"\n[OK] {len(analyses)} 件保存")
    print(f"[HTML] {html_path.absolute()}")

    if slack or post or auto:
        print(f"\n📤 投稿フロー ({len(analyses)} 件、kyutou単発)")
        _queue_post_requests(analyses, force=True)
        _resume_pending_posts(post=post, slack=slack, auto=auto, reason="kyutou-once")
    else:
        print("\n[INFO] 投稿フラグなし(--post/--slack/--auto-post)、分析・保存のみ")


def run(fetch_all: bool = False, post: bool = False, slack: bool = False, auto: bool = False):
    """メイン処理"""
    from fetcher import fetch_tweets
    from analyzer import analyze_tweets
    from storage import get_last_tweet_id, save_analyses
    from viewer import generate_html

    print("=" * 50)
    print(f"{get_japanese_greeting()} 株価初動メモシステム起動")
    print("=" * 50)

    # 差分取得（--all フラグがない場合）
    since_id = None if fetch_all else get_last_tweet_id()
    if since_id:
        print(f"[INFO] 前回取得 ID: {since_id} 以降を取得")
    else:
        print("[INFO] 全件取得モード")

    # ツイート取得
    try:
        tweets = fetch_tweets(since_id=since_id, today_only=False)
    except PermissionError as e:
        print(f"\n❌ {e}")
        print("\n📌 X API Basic プランが必要です:")
        print("   https://developer.twitter.com/en/portal/products")
        sys.exit(1)
    except ValueError as e:
        print(f"\n❌ {e}")
        sys.exit(1)

    if not tweets:
        print("新しい株関連ツイートはありませんでした")
        return

    print(f"\n📊 {len(tweets)} 件の株関連ツイートを分析します...\n")

    # Claude で分析
    analyses = analyze_tweets(tweets)

    if not analyses:
        print("分析できたツイートがありませんでした")
        return

    # 保存
    paths = save_analyses(analyses)

    # HTML生成
    html_path = generate_html()

    print("\n" + "=" * 50)
    print(f"✅ 完了: {len(paths)} 件のメモを保存")
    print(f"📄 メモフォルダ: {Path('notes').absolute()}")
    print(f"🌐 HTMLビューワー: {html_path.absolute()}")
    print("=" * 50)

    # 投稿フロー
    if auto:
        print(f"\n📤 自動投稿フロー ({len(analyses)} 件、承認なし)")
        _queue_post_requests(analyses)
        _resume_pending_posts(post=post, slack=slack, auto=auto, reason="batch")
    elif slack:
        print(f"\n📤 Slack 投稿フロー ({len(analyses)} 件)")
        _queue_post_requests(analyses)
        _resume_pending_posts(post=post, slack=slack, auto=auto, reason="batch")
    elif post:
        print(f"\n📤 投稿フロー ({len(analyses)} 件)")
        _queue_post_requests(analyses)
        _resume_pending_posts(post=post, slack=slack, auto=auto, reason="batch")


def main():
    parser = argparse.ArgumentParser(description="@shodousan 株価初動メモシステム")
    parser.add_argument("--all", action="store_true", help="差分取得せず全件取得")
    parser.add_argument("--demo", action="store_true", help="デモモード（X APIなし）")
    parser.add_argument("--html-only", action="store_true", help="HTMLのみ再生成")
    parser.add_argument("--latest", action="store_true", help="最新ツイート1件のみ分析")
    parser.add_argument("--watch", type=int, nargs="?", const=5, metavar="分",
                        help="監視ループ起動。チェック間隔を分で指定（デフォルト: 5分）")
    parser.add_argument("--setup", action="store_true", help="Xにログインして認証状態を保存")
    parser.add_argument("--post", action="store_true", help="分析後にXへの投稿確認ステップを実行 (ターミナルで y/n)")
    parser.add_argument("--slack", action="store_true", help="分析後にSlack経由で投稿確認 (スマホから y/n)")
    parser.add_argument("--auto-post", dest="auto_post", action="store_true",
                        help="分析後に承認なしで自動的にXへ投稿 (画像付き)")
    parser.add_argument("--force-recent", dest="force_recent", type=int, metavar="N",
                        help="直近N件のツイートをposted_idsチェック無視で強制再処理 (例: --force-recent 10)")
    parser.add_argument("--kyutou-once", dest="kyutou_once", type=int, nargs="?", const=1, metavar="N",
                        help="自前検知(kyutou)を1回走らせ先頭N件を分析→投稿 (既定1件、重複抑制バイパス)")
    parser.add_argument("--ignore-hours", dest="ignore_hours", action="store_true",
                        help="市場時間外でもkyutou検知を実行する (--kyutou-once のテスト用)")
    args = parser.parse_args()

    if args.setup:
        run_setup()
    elif args.demo:
        run_demo()
    elif args.html_only:
        run_html_only()
    elif args.force_recent is not None:
        run_force_recent(count=args.force_recent, post=args.post,
                         slack=args.slack, auto=args.auto_post)
    elif args.kyutou_once is not None:
        run_kyutou_once(count=args.kyutou_once, post=args.post, slack=args.slack,
                        auto=args.auto_post, ignore_hours=args.ignore_hours)
    elif args.latest:
        run_latest(post=args.post, slack=args.slack, auto=args.auto_post)
    elif args.watch is not None:
        run_watch(interval_min=args.watch, post=args.post, slack=args.slack, auto=args.auto_post)
    else:
        run(fetch_all=args.all, post=args.post, slack=args.slack, auto=args.auto_post)


if __name__ == "__main__":
    main()
