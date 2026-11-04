"""
X (Twitter) posting helpers using Playwright.
Builds post text from an analysis result and optionally resumes pending posts.
"""

import re
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from analyzer import Analysis
from config import AUTH_STATE_FILE, SITE_BASE_URL, X_ATTACH_IMAGE
from storage import discard_pending_post, get_pending_posts, mark_posted


def _strip_parens(text: str) -> str:
    """Trim surrounding ASCII parentheses if the whole string is wrapped."""
    text = text.strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()
    return text


def normalize_post_text(text: str) -> str:
    """Keep older pending posts compatible when the title line changed.

    タイトル行は2026-07-14に廃止（銘柄行から始める）。旧形式でキュー済みの
    投稿文からは、先頭のタイトル行を剥がして新形式に揃える。
    """
    for old_title in ("※株価急騰要因", "【初動メモ】"):
        if text.startswith(old_title):
            return text[len(old_title):].lstrip("\n")
    return text


def _extract_change_pct(analysis) -> str | None:
    """急騰検知の疑似ツイート本文から前日比騰落率を取り出す（例: "+13.28"）。

    kyutou_source が生成する本文の「前日比 +13.28%」を拾う。
    形式が違う（手動投稿・旧データ等）場合は None を返し、1行目は騰落率なしになる。
    """
    text = getattr(getattr(analysis, "tweet", None), "text", "") or ""
    m = re.search(r"前日比\s*([+＋\-−]?\d+(?:\.\d+)?)\s*[%％]", text)
    if not m:
        return None
    pct = m.group(1).replace("＋", "+").replace("−", "-")
    if pct and pct[0] not in "+-":
        pct = "+" + pct
    return pct


def _fetch_stats_via_yfinance(stock_code: str) -> tuple[float, float, float | None] | None:
    """(現在値, 前日比%, 出来高比) を yfinance から取得する。

    kyutou の疑似ツイートと違い @shodousan のツイート本文には株価・出来高が無いため、
    こちらで補完する。出来高比は「当日出来高 ÷ 直近5営業日の平均出来高」。
    当日足は未確定・遅延のことがあるので分足の実値で上書きしてから計算する。
    """
    try:
        import yfinance as yf
        from image_generator import _patch_today_with_intraday
        ticker = yf.Ticker(f"{stock_code}.T")
        hist = ticker.history(period="1mo")
        hist = _patch_today_with_intraday(ticker, hist)
        if hist is None or len(hist) < 2:
            return None
        price = float(hist["Close"].iloc[-1])
        prev_close = float(hist["Close"].iloc[-2])
        if prev_close <= 0:
            return None
        pct = (price / prev_close - 1.0) * 100.0
        vol_ratio = None
        if "Volume" in hist.columns and len(hist) >= 6:
            avg_vol = float(hist["Volume"].iloc[-6:-1].mean())
            if avg_vol > 0:
                vol_ratio = float(hist["Volume"].iloc[-1]) / avg_vol
        return price, pct, vol_ratio
    except Exception as e:
        print(f"  [WARN] 株価データ補完失敗 {stock_code}: {e}")
        return None


def _volume_line(ratio_str: str) -> str:
    """出来高比の行を組み立てる。特に高い（3倍以上）ときは🔥で強調する。"""
    line = f"出来高比 {ratio_str}倍"
    try:
        if float(ratio_str) >= 3:
            line += " 🔥"
    except ValueError:
        pass
    return line


def _build_stats_lines(analysis) -> list[str]:
    """銘柄行直下の数字行（株価・前日比%・出来高比、各1行）を組み立てる。

    数字は kyutou_source の疑似ツイート本文から抽出し、無ければ（@shodousan の
    初動検知ツイート等）yfinance で補完する。どちらも取れなければ空リスト
    （数字行なしのフォーマットになる）。
    """
    text = getattr(getattr(analysis, "tweet", None), "text", "") or ""
    lines = []

    m_price = re.search(r"株価\s*([\d,]+(?:\.\d+)?)\s*円", text)
    if m_price:
        price = float(m_price.group(1).replace(",", ""))
        pct = _extract_change_pct(analysis)
        if pct:
            lines.append(f"株価 {price:,.0f}円（前日比 {pct}%）")
        else:
            lines.append(f"株価 {price:,.0f}円")

        m_vol = re.search(r"出来高比\s*([\d.]+)\s*倍", text)
        if m_vol:
            lines.append(_volume_line(m_vol.group(1)))
        return lines

    # 本文に数字なし → yfinance で補完
    stock_code = _strip_parens(getattr(analysis, "stock_code", "") or "")
    if not stock_code or stock_code == "不明":
        return []
    stats = _fetch_stats_via_yfinance(stock_code)
    if stats is None:
        return []
    price, pct, vol_ratio = stats
    lines.append(f"株価 {price:,.0f}円（前日比 {pct:+.2f}%）")
    if vol_ratio is not None:
        lines.append(_volume_line(f"{vol_ratio:.2f}"))
    return lines


def site_stock_url(stock_code: str, when: datetime | None = None) -> str | None:
    """サイトの銘柄詳細ページへの直リンクURLを返す（コード不明なら None）。

    パス形式 /stock/{code}/{YYYYMMDD} を使う。Cloudflare Pages の _worker.js が
    このパスで、X のクローラーには OGP メタタグ（og:image=チャート画像）を返すので
    X がリンクカードを描画し、人と検索エンジンにはそのまま銘柄詳細ページを返す。

    日付を付けるのは X のカードキャッシュ対策。X はURL単位でカードを数日キャッシュ
    するため、同じ銘柄が再急騰したときに古い（または画像取得に失敗した）カードが
    使い回されて「チャートが表示されない」ことがある。
    """
    code = _strip_parens(stock_code or "")
    if not code or code == "不明":
        return None
    jst = timezone(timedelta(hours=9))
    stamp = when or datetime.now(jst)
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone(jst)
    day = stamp.strftime("%Y%m%d")
    return f"{SITE_BASE_URL}/stock/{code}/{day}"


def append_site_link(text: str, stock_code: str, when: datetime | None = None) -> str:
    """ポスト本文の末尾にサイトの銘柄ページリンクを付ける。

    既にサイトURLが含まれている場合（キュー済みの新形式ポスト等）は二重に付けない。
    """
    url = site_stock_url(stock_code, when)
    if not url or SITE_BASE_URL in text:
        return text
    return f"{text}\n\n📊 チャート・過去の急騰履歴\n{url}"


def build_post_text(analysis: Analysis) -> str:
    """Build X post text from an analysis result."""
    reason = _strip_parens(analysis.reason)
    keywords = _strip_parens(analysis.keywords)

    backgrounds = (
        "\n\n".join(f"・{_strip_parens(bg)}" for bg in analysis.backgrounds)
        if analysis.backgrounds
        else "・関連情報なし"
    )

    related_lines = []
    for line in analysis.related_stocks.splitlines():
        line = line.strip()
        if not line or line.startswith("---") or line.startswith("・") or line.startswith("関連"):
            continue
        related_lines.append(_strip_parens(line))
    related_stocks = "\n".join(related_lines) if related_lines else "・該当なし"

    stock_name = _strip_parens(analysis.stock_name)
    stock_code = _strip_parens(analysis.stock_code)
    stock_label = f"{stock_name}({stock_code})" if stock_name else stock_code

    # 銘柄行の直下に数字行（株価・前日比%・出来高比）を添える
    stats_lines = _build_stats_lines(analysis)
    stats_block = "\n".join(stats_lines) + "\n\n" if stats_lines else ""

    text = (
        f"📈 {stock_label}　大幅上昇❗️\n"
        "\n"
        f"{stats_block}"
        f"✅ {reason}によるもの\n"
        "\n"
        f"🏢 事業内容：\n{_strip_parens(analysis.business)}\n"
        "\n"
        f"🔑 キーワード：\n{keywords}\n"
        "\n"
        f"📝 強材料ポイント\n"
        f"{backgrounds}\n"
        "\n"
        "🔗 関連銘柄\n"
        f"{related_stocks}"
    )
    detected_at = getattr(getattr(analysis, "tweet", None), "created_at", None)
    return append_site_link(normalize_post_text(text.strip()), stock_code, detected_at)


def confirm_and_post(analysis: Analysis) -> bool:
    """Show the post text in the terminal and post on approval."""
    text = build_post_text(analysis)
    tweet_id = analysis.tweet.id

    print("\n" + "=" * 50)
    print(f"投稿確認 ({analysis.stock_code} {analysis.stock_name})")
    print("=" * 50)
    print(text)
    print("=" * 50)
    print(f"文字数: {len(text)}")

    try:
        answer = input("\nXに投稿しますか？ [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n  [INFO] pending post kept in queue")
        return False

    if answer != "y":
        print("  [SKIP] スキップしました")
        discard_pending_post(tweet_id)
        return False

    success = _post_to_x(text)
    if success:
        mark_posted(tweet_id)
    return success


def _generate_card_safely(analysis_like) -> bytes | None:
    """Generate the chart image; swallow errors and return None on failure."""
    try:
        from image_generator import generate_card
        return generate_card(analysis_like)
    except Exception as e:
        print(f"  [WARN] image generation failed: {e}")
        return None


def auto_post(analysis: Analysis) -> bool:
    """Post directly to X without any approval step.

    既定はカード方式（画像添付なし）。X_ATTACH_IMAGE=1 のときのみ画像を添付する。
    """
    text = build_post_text(analysis)
    image_bytes = _generate_card_safely(analysis) if X_ATTACH_IMAGE else None

    print(f"\n[AUTO] posting to X: {analysis.stock_code} {analysis.stock_name}")
    success = _post_to_x(text, image_bytes=image_bytes)
    if success:
        mark_posted(analysis.tweet.id)
    else:
        print(f"  [WARN] auto post failed, leaving in pending queue: {analysis.tweet.id}")
    return success


def process_pending_auto_queue() -> list[bool]:
    """Process the pending queue without any approval; post each to X immediately."""
    from types import SimpleNamespace

    pending_posts = get_pending_posts()
    if not pending_posts:
        return []

    print(f"\n[INFO] auto-posting {len(pending_posts)} pending post(s)")

    results = []
    for index, entry in enumerate(pending_posts, 1):
        stock_code = entry.get("stock_code", "")
        stock_name = entry.get("stock_name", "")
        post_text = append_site_link(
            normalize_post_text(entry.get("post_text", "")), stock_code
        )
        tweet_id = entry.get("tweet_id", "")

        analysis_like = SimpleNamespace(
            stock_code=stock_code,
            stock_name=stock_name,
            reason=entry.get("reason", ""),
            business=entry.get("business", ""),
            keywords=entry.get("keywords", ""),
            backgrounds=entry.get("backgrounds", []) or [],
        )
        image_bytes = _generate_card_safely(analysis_like) if X_ATTACH_IMAGE else None

        print(f"\n[AUTO {index}/{len(pending_posts)}] {stock_code} {stock_name}")
        success = _post_to_x(post_text, image_bytes=image_bytes)
        if success:
            mark_posted(tweet_id)
        else:
            print(f"  [WARN] auto post failed, kept in pending: {tweet_id}")
        results.append(success)

    return results


def process_pending_terminal_queue() -> list[bool]:
    """Resume pending post approvals from the terminal."""
    pending_posts = get_pending_posts()
    if not pending_posts:
        return []

    print(f"\n[INFO] resuming {len(pending_posts)} pending post(s) from the queue")

    results = []
    for index, entry in enumerate(pending_posts, 1):
        stock_code = entry.get("stock_code", "")
        stock_name = entry.get("stock_name", "")
        post_text = append_site_link(
            normalize_post_text(entry.get("post_text", "")), stock_code
        )
        tweet_id = entry.get("tweet_id", "")

        print("\n" + "=" * 50)
        print(f"[Pending {index}/{len(pending_posts)}] {stock_code} {stock_name}")
        print("=" * 50)
        print(post_text)
        print("=" * 50)
        print(f"文字数: {len(post_text)}")

        try:
            answer = input("\nXに投稿しますか？ [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n  [INFO] pending queue paused")
            results.append(False)
            break

        if answer != "y":
            print("  [SKIP] pending post removed from queue")
            discard_pending_post(tweet_id)
            results.append(False)
            continue

        success = _post_to_x(post_text)
        if success:
            mark_posted(tweet_id)
        results.append(success)

    return results


def _post_to_x(text: str, *, image_bytes: bytes | None = None) -> bool:
    """Post to X using Playwright and a saved authenticated browser state.

    If image_bytes is provided, the image is attached to the tweet via the
    file input. Attachment failure logs a warning and falls back to text-only.
    """
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise ImportError(
            "playwright がインストールされていません。\n"
            "pip install playwright && playwright install chromium"
        )

    if not AUTH_STATE_FILE.exists():
        print("❌ 認証状態ファイルがありません。python main.py --setup でログインしてください")
        return False

    print("  [INFO] Xに投稿中...")

    # 画像を一時ファイルに書き出す (X のファイル入力はパスを要求するため)
    image_tmp_path: Path | None = None
    if image_bytes is not None:
        try:
            tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            tmp.write(image_bytes)
            tmp.close()
            image_tmp_path = Path(tmp.name)
        except Exception as e:
            print(f"  [WARN] image temp file create failed, posting text only: {e}")
            image_tmp_path = None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox"],
            )
            context = browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                locale="ja-JP",
                storage_state=str(AUTH_STATE_FILE),
            )
            page = context.new_page()

            try:
                page.goto("https://x.com/home", wait_until="networkidle", timeout=30000)
            except PlaywrightTimeout:
                print("  [WARN] page load timed out, continuing")

            try:
                compose_box = page.wait_for_selector(
                    '[data-testid="tweetTextarea_0"]',
                    timeout=15000,
                )
                compose_box.click()
                time.sleep(0.5)

                compose_box.fill(text)
                time.sleep(0.5)

                # 画像添付 (失敗してもテキスト投稿は続行)
                if image_tmp_path is not None:
                    try:
                        file_input = page.locator('input[data-testid="fileInput"]').first
                        file_input.set_input_files(str(image_tmp_path), timeout=10000)
                        # X が画像プレビュー生成・アップロード完了を待つ
                        time.sleep(3)
                        print("  [INFO] 画像を添付しました")
                    except Exception as e:
                        print(f"  [WARN] 画像添付失敗、テキストのみで投稿します: {e}")

                post_btn = page.locator('[data-testid="tweetButtonInline"]').first
                post_btn.wait_for(timeout=10000)
                post_btn.click()
                time.sleep(3)

                print("  [OK] 投稿しました")
                browser.close()
                return True

            except PlaywrightTimeout as e:
                print(f"  [ERROR] 投稿タイムアウト: {e}")
                browser.close()
                return False
            except Exception as e:
                print(f"  [ERROR] 投稿エラー: {e}")
                browser.close()
                return False
    finally:
        if image_tmp_path is not None:
            try:
                image_tmp_path.unlink(missing_ok=True)
            except Exception:
                pass
