"""
X (Twitter) posting helpers using Playwright.
Builds post text from an analysis result and optionally resumes pending posts.
"""

import tempfile
import time
from pathlib import Path

from analyzer import Analysis
from config import AUTH_STATE_FILE
from storage import discard_pending_post, get_pending_posts, mark_posted


def _strip_parens(text: str) -> str:
    """Trim surrounding ASCII parentheses if the whole string is wrapped."""
    text = text.strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()
    return text


def normalize_post_text(text: str) -> str:
    """Keep older pending posts compatible when the title line changed."""
    if text.startswith("【初動メモ】"):
        return text.replace("【初動メモ】", "※株価急騰要因", 1)
    return text


def build_post_text(analysis: Analysis) -> str:
    """Build X post text from an analysis result."""
    reason = _strip_parens(analysis.reason)
    keywords = _strip_parens(analysis.keywords)

    backgrounds = (
        "\n".join(f"・{_strip_parens(bg)}" for bg in analysis.backgrounds)
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

    text = (
        "※株価急騰要因\n"
        "\n"
        f"{stock_label}\n"
        "\n"
        f"{reason}によるもの\n"
        "\n"
        f"事業内容:{_strip_parens(analysis.business)}\n"
        "\n"
        f"キーワード:{keywords}\n"
        "\n"
        f"{backgrounds}\n"
        "\n"
        "関連銘柄\n"
        f"{related_stocks}"
    )
    return normalize_post_text(text.strip())


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
    """Post directly to X without any approval step (with image)."""
    text = build_post_text(analysis)
    image_bytes = _generate_card_safely(analysis)

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
        post_text = normalize_post_text(entry.get("post_text", ""))
        tweet_id = entry.get("tweet_id", "")

        analysis_like = SimpleNamespace(
            stock_code=stock_code,
            stock_name=stock_name,
            reason=entry.get("reason", ""),
            business=entry.get("business", ""),
            keywords=entry.get("keywords", ""),
            backgrounds=entry.get("backgrounds", []) or [],
        )
        image_bytes = _generate_card_safely(analysis_like)

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
        post_text = normalize_post_text(entry.get("post_text", ""))
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
