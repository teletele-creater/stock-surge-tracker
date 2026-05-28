"""
X timeline fetcher using Playwright.
Fetches tweets from @shodousan and extracts stock-alert posts.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from config import AUTH_STATE_FILE, MAX_TWEETS, TARGET_USERNAME


@dataclass
class Tweet:
    id: str
    text: str
    created_at: datetime
    stock_codes: list[str]


def _extract_stock_codes(text: str) -> list[str]:
    """Extract stock codes like 3778, 278A, or 1234A from text."""
    code_pattern = r"(?:[0-9]{4}[A-Za-z]?|[0-9]{3}[A-Za-z])"
    matches = re.findall(rf"(?<![0-9A-Za-z])({code_pattern})(?![0-9A-Za-z])", text or "")
    codes: list[str] = []
    for code in matches:
        normalized = code.upper()
        if normalized.isdigit() and len(normalized) == 4 and 1000 <= int(normalized) <= 9999:
            codes.append(normalized)
        elif normalized[:-1].isdigit() and normalized[-1].isalpha() and len(normalized) in {4, 5}:
            codes.append(normalized)
    return list(dict.fromkeys(codes))


def _is_shodou_alert(text: str) -> bool:
    """Return True for the stock-alert format we actually want to process."""
    if not text:
        return False
    if not text.startswith("【初動検知】"):
        return False
    code_pattern = r"(?:[0-9]{4}[A-Za-z]?|[0-9]{3}[A-Za-z])"
    return bool(re.search(rf"[（(]{code_pattern}[）)]", text))


def _is_today_jst(dt: datetime) -> bool:
    jst = timezone(timedelta(hours=9))
    return dt.astimezone(jst).date() == datetime.now(jst).date()


def _parse_twitter_date(date_str: str) -> datetime:
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except Exception:
        return datetime.now(timezone.utc)


def _make_id(text: str, timestamp: str) -> str:
    raw = f"{timestamp}:{text[:50]}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]


def _extract_full_text(node: dict) -> str:
    legacy = node.get("legacy", {})
    text = legacy.get("full_text", "")
    if text:
        return text

    note_result = (
        node.get("note_tweet", {})
        .get("note_tweet_results", {})
        .get("result", {})
    )
    if isinstance(note_result, dict):
        return note_result.get("text", "") or ""

    return ""


def fetch_tweets(
    since_id: Optional[str] = None,
    max_count: Optional[int] = None,
    today_only: bool = True,
) -> list[Tweet]:
    """
    Fetch recent stock-alert tweets from @shodousan.

    If since_id is provided, only returns tweets newer than that ID.
    """
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise ImportError(
            "playwright is not installed.\n"
            "Install it with:\n"
            "  pip install playwright\n"
            "  playwright install chromium"
        )

    url = f"https://x.com/{TARGET_USERNAME}"
    print(f"[INFO] {url} をスクレイピング中...")

    tweets: list[Tweet] = []
    seen_ids: set[str] = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox"],
        )
        auth_state = str(AUTH_STATE_FILE) if AUTH_STATE_FILE.exists() else None
        if auth_state:
            print("  [INFO] 認証状態を使用してログイン済みモードで取得")
        else:
            print("  [WARN] 認証状態がありません。python main.py --setup を実行してください")

        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            locale="ja-JP",
            storage_state=auth_state,
        )

        graphql_responses = []

        def _on_response(response) -> None:
            if "/i/api/graphql/" in response.url:
                tail = response.url[response.url.find("/graphql/") + 9 :][:60]
                print(f"  [DEBUG] graphql応答: {tail}")
                graphql_responses.append(response)

        page = context.new_page()
        page.on("response", _on_response)

        try:
            page.goto(url, wait_until="networkidle", timeout=30000)
        except PlaywrightTimeout:
            print("[WARN] ページ読み込みがタイムアウトしました。取得可能な範囲で続行します")

        for _ in range(3):
            page.keyboard.press("End")
            time.sleep(2)

        intercepted: list[dict] = []
        for resp in graphql_responses:
            try:
                intercepted.append(resp.json())
            except Exception as e:
                print(f"  [DEBUG] JSON解析失敗: {e}")

        browser.close()

    all_pinned_ids: set[str] = set()
    for body in intercepted:
        all_pinned_ids |= _collect_pinned_ids(body)
    if all_pinned_ids:
        print(f"  [INFO] 固定ツイート {len(all_pinned_ids)} 件をスキップ: {all_pinned_ids}")

    for body in intercepted:
        _extract_from_json(body, tweets, seen_ids, all_pinned_ids, today_only)

    if not tweets:
        print("[INFO] XHR から取れなかったため DOM fallback を試します...")
        return _fetch_via_dom(url, since_id, today_only=today_only)

    tweets.sort(key=lambda tw: tw.created_at, reverse=True)
    latest_seen = tweets[0] if tweets else None

    if since_id:
        before_filter_count = len(tweets)
        try:
            since_id_int = int(since_id)
            tweets = [tw for tw in tweets if int(tw.id) > since_id_int]
        except (ValueError, TypeError):
            new_tweets = []
            for tw in tweets:
                if tw.id == since_id:
                    break
                new_tweets.append(tw)
            tweets = new_tweets

        if before_filter_count and not tweets and latest_seen:
            relation = "matches" if str(latest_seen.id) == str(since_id) else "is not newer than"
            print(
                f"[INFO] No new matching alert tweets. "
                f"Latest fetched ID {latest_seen.id} {relation} saved ID {since_id}."
            )
            print(f"[INFO] Latest fetched tweet: {latest_seen.text[:80]}...")

    limit = max_count if max_count is not None else MAX_TWEETS
    tweets = tweets[:limit]

    print(f"[INFO] 【初動検知】ツイート: {len(tweets)} 件取得")
    return tweets


def _collect_pinned_ids(body: dict | list) -> set[str]:
    pinned_ids: set[str] = set()

    def _walk(obj, in_pinned: bool) -> None:
        if isinstance(obj, dict):
            is_pinned = in_pinned or obj.get("type") == "TimelinePinEntry"
            legacy = obj.get("legacy", {})
            if is_pinned and isinstance(legacy, dict) and legacy.get("id_str"):
                pinned_ids.add(legacy["id_str"])
            for value in obj.values():
                _walk(value, is_pinned)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item, in_pinned)

    _walk(body, False)
    return pinned_ids


def _extract_from_json(
    body: dict | list,
    tweets: list[Tweet],
    seen_ids: set[str],
    pinned_ids: set[str] | None = None,
    today_only: bool = True,
) -> None:
    if pinned_ids is None:
        pinned_ids = set()

    if isinstance(body, dict):
        legacy = body.get("legacy", {})
        if isinstance(legacy, dict) and (
            legacy.get("full_text")
            or body.get("note_tweet", {})
        ):
            text = _extract_full_text(body)
            tweet_id = legacy.get("id_str") or legacy.get("conversation_id_str", "")
            created_str = legacy.get("created_at", "")

            if text.startswith("RT @"):
                return
            if legacy.get("in_reply_to_user_id_str"):
                return
            if tweet_id in pinned_ids:
                print(f"  [SKIP] 固定ツイート: {text[:40]}...")
                return

            if tweet_id and tweet_id not in seen_ids:
                seen_ids.add(tweet_id)
                try:
                    created_at = datetime.strptime(
                        created_str, "%a %b %d %H:%M:%S +0000 %Y"
                    ).replace(tzinfo=timezone.utc)
                except Exception:
                    created_at = datetime.now(timezone.utc)

                if not _is_shodou_alert(text):
                    return
                if today_only and not _is_today_jst(created_at):
                    return

                tweets.append(
                    Tweet(
                        id=tweet_id,
                        text=text,
                        created_at=created_at,
                        stock_codes=_extract_stock_codes(text),
                    )
                )

        for value in body.values():
            _extract_from_json(value, tweets, seen_ids, pinned_ids, today_only)

    elif isinstance(body, list):
        for item in body:
            _extract_from_json(item, tweets, seen_ids, pinned_ids, today_only)


def _fetch_via_dom(url: str, since_id: Optional[str], today_only: bool = True) -> list[Tweet]:
    """Fallback: parse visible tweets from DOM."""
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        from playwright.sync_api import sync_playwright
    except ImportError:
        return []

    tweets: list[Tweet] = []
    seen_texts: set[str] = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox"],
        )
        auth_state = str(AUTH_STATE_FILE) if AUTH_STATE_FILE.exists() else None
        if auth_state:
            print("  [INFO] 認証状態を使用してログイン済みモードで取得")
        else:
            print("  [WARN] 認証状態がありません。python main.py --setup を実行してください")

        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            locale="ja-JP",
            storage_state=auth_state,
        )
        page = context.new_page()

        try:
            page.goto(url, wait_until="networkidle", timeout=30000)
        except PlaywrightTimeout:
            print("[WARN] DOM fallback page load timed out")

        for _ in range(4):
            page.keyboard.press("End")
            time.sleep(2)

        print(f"  [DEBUG] 現在のURL: {page.url}")
        article_els = page.query_selector_all('article[data-testid="tweet"]')
        print(f"  [DEBUG] article件数: {len(article_els)}")

        for article in article_els[: MAX_TWEETS * 2]:
            try:
                pinned_el = article.query_selector('[data-testid="socialContext"]')
                if pinned_el:
                    ctx_text = pinned_el.inner_text()
                    if "Pin" in ctx_text or "固定" in ctx_text:
                        print("  [SKIP] 固定ツイート (DOM)")
                        continue

                text_el = article.query_selector('[data-testid="tweetText"]')
                if not text_el:
                    continue
                text = text_el.inner_text()

                if text.startswith("RT @"):
                    continue
                if text in seen_texts:
                    continue
                seen_texts.add(text)

                time_el = article.query_selector("time")
                date_str = time_el.get_attribute("datetime") if time_el else ""
                created_at = _parse_twitter_date(date_str) if date_str else datetime.now(timezone.utc)

                link_el = article.query_selector('a[href*="/status/"]')
                tweet_id = ""
                if link_el:
                    href = link_el.get_attribute("href") or ""
                    match = re.search(r"/status/(\d+)", href)
                    if match:
                        tweet_id = match.group(1)
                if not tweet_id:
                    tweet_id = _make_id(text, date_str)

                if not _is_shodou_alert(text):
                    continue
                if today_only and not _is_today_jst(created_at):
                    continue

                tweets.append(
                    Tweet(
                        id=tweet_id,
                        text=text,
                        created_at=created_at,
                        stock_codes=_extract_stock_codes(text),
                    )
                )

            except Exception as e:
                print(f"  [WARN] DOM解析エラー: {e}")

        browser.close()

    tweets.sort(key=lambda tw: tw.created_at, reverse=True)
    latest_seen = tweets[0] if tweets else None

    if since_id:
        try:
            since_id_int = int(since_id)
            tweets = [tw for tw in tweets if int(tw.id) > since_id_int]
        except (ValueError, TypeError):
            new_tweets = []
            for tw in tweets:
                if tw.id == since_id:
                    break
                new_tweets.append(tw)
            tweets = new_tweets

        if latest_seen and not tweets:
            relation = "matches" if str(latest_seen.id) == str(since_id) else "is not newer than"
            print(
                f"[INFO] No new matching alert tweets in DOM fallback. "
                f"Latest fetched ID {latest_seen.id} {relation} saved ID {since_id}."
            )

    tweets = tweets[:MAX_TWEETS]
    print(f"[INFO] DOM fallback: 【初動検知】ツイート {len(tweets)} 件取得")
    return tweets


def fetch_latest_tweet() -> Optional[Tweet]:
    """Return the latest alert tweet, preferring today's alerts."""
    tweets = fetch_tweets(max_count=5, today_only=False)
    if not tweets:
        return None
    today_tweets = [tw for tw in tweets if _is_today_jst(tw.created_at)]
    return today_tweets[0] if today_tweets else tweets[0]
