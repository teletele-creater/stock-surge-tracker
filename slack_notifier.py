"""
Slack approval flow for X posting.
Sends approval requests to Slack, waits for y/n replies, and posts on approval.
"""

import json
import time
import unicodedata
import urllib.parse
import urllib.request
import uuid

from config import SLACK_BOT_TOKEN, SLACK_CHANNEL
from storage import discard_pending_post, get_pending_posts, mark_posted


APPROVE_TOKENS = {"y", "yes", "はい", "ok", "承認"}
REJECT_TOKENS = {"n", "no", "いいえ", "スキップ", "skip", "reject", "却下"}


def _classify_reply(text: str) -> str | None:
    """Normalize Slack reply text and classify it as approval/rejection."""
    normalized = unicodedata.normalize("NFKC", text or "").strip().lower()
    if not normalized:
        return None

    token = normalized.split()[0]
    token = token.strip("`'\".,!?()[]{}<>:;")

    if token in APPROVE_TOKENS:
        return "y"
    if token in REJECT_TOKENS:
        return "n"
    return None


def _slack_api_post(method: str, payload: dict) -> dict:
    """Call the Slack Web API with a JSON POST request."""
    url = f"https://slack.com/api/{method}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _slack_api_get(method: str, params: dict) -> dict:
    """Call the Slack Web API with a GET request."""
    query = urllib.parse.urlencode(params)
    url = f"https://slack.com/api/{method}?{query}"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get_channel_id() -> str:
    """Resolve the Slack channel ID from the configured channel name."""
    channel_name = SLACK_CHANNEL.lstrip("#")
    result = _slack_api_get(
        "conversations.list",
        {"types": "public_channel,private_channel", "limit": 200},
    )
    if not result.get("ok"):
        raise RuntimeError(f"Slack conversations.list error: {result.get('error')}")

    for channel in result.get("channels", []):
        if channel.get("name") == channel_name:
            return channel["id"]

    raise RuntimeError(f"Slack channel #{channel_name} was not found")


def _slack_files_upload_v2(*, channel_id: str, image_bytes: bytes,
                           filename: str, initial_comment: str) -> str:
    """Upload an image file with caption to a Slack channel via files.upload_v2.

    Returns an approximate ts (taken before completing the upload) usable for
    wait_for_reply's history polling.
    """
    # Step 1: getUploadURLExternal
    url_res = _slack_api_get(
        "files.getUploadURLExternal",
        {"filename": filename, "length": str(len(image_bytes))},
    )
    if not url_res.get("ok"):
        raise RuntimeError(f"Slack getUploadURLExternal error: {url_res.get('error')}")

    upload_url = url_res["upload_url"]
    file_id = url_res["file_id"]

    # Step 2: multipart POST to the upload URL
    boundary = uuid.uuid4().hex
    body_parts = [
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode(),
        b"Content-Type: image/png\r\n\r\n",
        image_bytes,
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    upload_body = b"".join(body_parts)
    upload_req = urllib.request.Request(
        upload_url,
        data=upload_body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(upload_req) as resp:
        if resp.status >= 300:
            raise RuntimeError(f"Slack upload POST failed with status {resp.status}")

    # 完了直前にタイムスタンプを取得 (チャネル履歴のフォールバック用)
    ts_floor = f"{time.time():.6f}"

    # Step 3: completeUploadExternal (channel_id + initial_comment で投稿される)
    complete_res = _slack_api_post(
        "files.completeUploadExternal",
        {
            "files": [{"id": file_id}],
            "channel_id": channel_id,
            "initial_comment": initial_comment,
        },
    )
    if not complete_res.get("ok"):
        raise RuntimeError(f"Slack completeUploadExternal error: {complete_res.get('error')}")

    # Step 4: 本物のメッセージ ts を取得 (スレッド返信を拾うため)
    # まず completeUpload のレスポンスに含まれる shares を見る
    files_list = complete_res.get("files") or []
    for f in files_list:
        shares = f.get("shares") or {}
        for visibility in ("public", "private"):
            entries = (shares.get(visibility) or {}).get(channel_id) or []
            for entry in entries:
                ts = entry.get("ts")
                if ts:
                    return ts

    # フォールバック: チャネル履歴から file_id を含むメッセージを探す
    deadline = time.time() + 8
    while time.time() < deadline:
        time.sleep(0.5)
        hist = _slack_api_get(
            "conversations.history",
            {"channel": channel_id, "limit": 10},
        )
        if hist.get("ok"):
            for msg in hist.get("messages", []):
                for mf in msg.get("files", []) or []:
                    if mf.get("id") == file_id:
                        ts = msg.get("ts")
                        if ts:
                            return ts

    # 最終手段: 時刻ベース ts (チャネル直書きの返信は拾える、スレッド返信は拾えない)
    return ts_floor


def send_approval_request(stock_code: str, stock_name: str, post_text: str,
                          *, image_bytes: bytes | None = None) -> str:
    """Send an approval request to Slack and return the message timestamp.

    If image_bytes is provided, the image is uploaded with the approval text as
    the file's initial comment. Falls back to text-only if image_bytes is None
    or the upload fails.
    """
    channel_id = _get_channel_id()
    message = (
        f"投稿確認 ({stock_code} {stock_name})\n"
        f"```\n{post_text}\n```\n"
        f"文字数: {len(post_text)}\n\n"
        "投稿するなら `y`\n"
        "スキップするなら `n`"
    )

    if image_bytes is not None:
        try:
            safe_name = stock_code or "post"
            return _slack_files_upload_v2(
                channel_id=channel_id,
                image_bytes=image_bytes,
                filename=f"{safe_name}.png",
                initial_comment=message,
            )
        except Exception as e:
            print(f"  [WARN] Slack image upload failed, falling back to text-only: {e}")

    result = _slack_api_post(
        "chat.postMessage",
        {
            "channel": channel_id,
            "text": message,
        },
    )
    if not result.get("ok"):
        raise RuntimeError(f"Slack chat.postMessage error: {result.get('error')}")
    return result["ts"]


def _get_bot_user_id() -> str:
    """Return the bot user ID if available."""
    result = _slack_api_get("auth.test", {})
    if not result.get("ok"):
        return ""
    return result.get("user_id", "")


def wait_for_reply(message_ts: str, timeout_min: int = 300, poll_interval: int = 5) -> str:
    """Wait for a y/n reply in the thread or channel."""
    channel_id = _get_channel_id()
    bot_user_id = _get_bot_user_id()
    deadline = time.time() + (timeout_min * 60)
    last_checked_ts = message_ts

    while time.time() < deadline:
        time.sleep(poll_interval)

        reply_result = _slack_api_get(
            "conversations.replies",
            {
                "channel": channel_id,
                "ts": message_ts,
                "limit": 20,
            },
        )
        if reply_result.get("ok"):
            for msg in reply_result.get("messages", [])[1:]:
                if msg.get("user") == bot_user_id:
                    continue
                decision = _classify_reply(msg.get("text", ""))
                if decision:
                    return decision

        history_result = _slack_api_get(
            "conversations.history",
            {
                "channel": channel_id,
                "oldest": last_checked_ts,
                "limit": 20,
            },
        )
        if history_result.get("ok"):
            latest_seen = last_checked_ts
            for msg in history_result.get("messages", []):
                msg_ts = msg.get("ts", last_checked_ts)
                if msg_ts > latest_seen:
                    latest_seen = msg_ts
                if msg.get("user") == bot_user_id:
                    continue
                if msg.get("thread_ts"):
                    continue
                decision = _classify_reply(msg.get("text", ""))
                if decision:
                    return decision
            last_checked_ts = latest_seen

    return "timeout"


def _notify_posted(stock_code: str, stock_name: str, message_ts: str) -> None:
    """Post a lightweight success message back to the Slack thread."""
    try:
        channel_id = _get_channel_id()
        _slack_api_post(
            "chat.postMessage",
            {
                "channel": channel_id,
                "text": f"投稿完了: {stock_name}({stock_code})",
                "thread_ts": message_ts,
            },
        )
    except Exception:
        pass


def _handle_slack_decision(
    *,
    tweet_id: str,
    stock_code: str,
    stock_name: str,
    post_text: str,
    message_ts: str,
    reply: str,
    image_bytes: bytes | None = None,
) -> bool:
    from poster import _post_to_x

    if reply == "y":
        print("  [Slack] approved, posting to X...")
        success = _post_to_x(post_text, image_bytes=image_bytes)
        if success:
            mark_posted(tweet_id)
            _notify_posted(stock_code, stock_name, message_ts)
        return success

    if reply == "n":
        print("  [Slack] skipped")
        discard_pending_post(tweet_id)
        return False

    print("  [Slack] timeout, leaving the post in the pending queue")
    return False


def _generate_card_safely(analysis) -> bytes | None:
    """Generate the chart card from an Analysis; return None on failure."""
    try:
        from image_generator import generate_card
        return generate_card(analysis)
    except Exception as e:
        print(f"  [WARN] image generation failed: {e}")
        return None


def _build_analysis_from_pending(entry: dict):
    """Reconstruct a minimal analysis-like object from a stored pending entry."""
    from types import SimpleNamespace
    return SimpleNamespace(
        stock_code=entry.get("stock_code", ""),
        stock_name=entry.get("stock_name", ""),
        reason=entry.get("reason", ""),
        business=entry.get("business", ""),
        keywords=entry.get("keywords", ""),
        backgrounds=entry.get("backgrounds", []) or [],
    )


def slack_confirm_and_post(analysis) -> bool:
    """Send one approval request to Slack and post on approval."""
    from poster import build_post_text

    tweet_id = analysis.tweet.id
    text = build_post_text(analysis)
    image_bytes = _generate_card_safely(analysis)

    print(f"\n  [Slack] sending approval request to #{SLACK_CHANNEL}...")
    try:
        message_ts = send_approval_request(
            analysis.stock_code,
            analysis.stock_name,
            text,
            image_bytes=image_bytes,
        )
        print("  [Slack] waiting for y/n reply...")
    except Exception as e:
        print(f"  [ERROR] Slack approval request failed: {e}")
        return False

    reply = wait_for_reply(message_ts)
    return _handle_slack_decision(
        tweet_id=tweet_id,
        stock_code=analysis.stock_code,
        stock_name=analysis.stock_name,
        post_text=text,
        message_ts=message_ts,
        reply=reply,
        image_bytes=image_bytes,
    )


def slack_confirm_and_post_batch(analyses: list) -> list[bool]:
    """Send approval requests for multiple analyses, then process replies in order."""
    from poster import build_post_text

    if not analyses:
        return []

    pending = []
    for index, analysis in enumerate(analyses, 1):
        text = build_post_text(analysis)
        image_bytes = _generate_card_safely(analysis)
        print(f"\n  [Slack] sending approval request ({index}/{len(analyses)})...")
        try:
            message_ts = send_approval_request(
                analysis.stock_code,
                analysis.stock_name,
                text,
                image_bytes=image_bytes,
            )
            pending.append((analysis, text, message_ts, image_bytes))
        except Exception as e:
            print(f"  [ERROR] Slack approval request failed: {e}")

    if not pending:
        return [False] * len(analyses)

    print(f"\n  [Slack] waiting for {len(pending)} approval reply/replies...")

    results = []
    for index, (analysis, text, message_ts, image_bytes) in enumerate(pending, 1):
        print(f"\n  [Slack] waiting ({index}/{len(pending)}): {analysis.stock_code} {analysis.stock_name}")
        reply = wait_for_reply(message_ts)
        results.append(
            _handle_slack_decision(
                tweet_id=analysis.tweet.id,
                stock_code=analysis.stock_code,
                stock_name=analysis.stock_name,
                post_text=text,
                message_ts=message_ts,
                reply=reply,
                image_bytes=image_bytes,
            )
        )

    return results


def process_pending_slack_queue() -> list[bool]:
    """Resume pending Slack approvals after a restart."""
    from poster import normalize_post_text

    pending_entries = get_pending_posts()
    if not pending_entries:
        return []

    pending = []
    print(f"\n[INFO] resuming {len(pending_entries)} pending Slack approval(s)")
    for index, entry in enumerate(pending_entries, 1):
        stock_code = entry.get("stock_code", "")
        stock_name = entry.get("stock_name", "")
        post_text = normalize_post_text(entry.get("post_text", ""))
        tweet_id = entry.get("tweet_id", "")

        # 保存済みフィールドから画像を再生成 (古いエントリで欠けている場合は None になる)
        image_bytes = _generate_card_safely(_build_analysis_from_pending(entry))

        print(f"  [Slack] sending pending approval request ({index}/{len(pending_entries)}): {stock_code} {stock_name}")
        try:
            message_ts = send_approval_request(stock_code, stock_name, post_text,
                                               image_bytes=image_bytes)
            pending.append((tweet_id, stock_code, stock_name, post_text, message_ts, image_bytes))
        except Exception as e:
            print(f"  [ERROR] Slack approval request failed: {e}")

    results = []
    for index, (tweet_id, stock_code, stock_name, post_text, message_ts, image_bytes) in enumerate(pending, 1):
        print(f"\n  [Slack] waiting ({index}/{len(pending)}): {stock_code} {stock_name}")
        reply = wait_for_reply(message_ts)
        results.append(
            _handle_slack_decision(
                tweet_id=tweet_id,
                stock_code=stock_code,
                stock_name=stock_name,
                post_text=post_text,
                message_ts=message_ts,
                reply=reply,
                image_bytes=image_bytes,
            )
        )

    return results
