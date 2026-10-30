"""
サイト同期モジュール

分析結果と急騰チャート画像を Supabase に送信して
「株価急騰要因メモ」サイトに即時反映する。

呼び出し元: storage.save_analysis() の最後で sync_to_site(analysis) を呼ぶ。
.env に SUPABASE_URL と SUPABASE_SERVICE_KEY が無い場合は何もしない（安全側）。
"""

import os
import re
import requests
import json
import logging
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from datetime import datetime, timezone, timedelta

from analyzer import Analysis


SUPABASE_URL    = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY    = os.getenv("SUPABASE_SERVICE_KEY", "")  # service_role キー（Pythonフロー専用、絶対に公開しない）
SUPABASE_TABLE  = os.getenv("SUPABASE_TABLE",  "surge_events")
SUPABASE_BUCKET = os.getenv("SUPABASE_BUCKET", "charts")

TIMEOUT_SEC = 20
CHART_MAX_ATTEMPTS = 3
CHART_RETRY_DELAY_SEC = 5
CHART_LOG_PATH = Path(__file__).resolve().parent / "logs" / "chart_retry.log"


def _log_chart_attempt(analysis, attempt: int, stage: str, outcome: str, error=None):
    """Persist each attempt without depending on console redirection."""
    event = {
        "time": datetime.now(timezone(timedelta(hours=9))).isoformat(),
        "tweet_id": str(analysis.tweet.id),
        "stock_code": _clean(analysis.stock_code),
        "attempt": attempt,
        "max_attempts": CHART_MAX_ATTEMPTS,
        "stage": stage,
        "outcome": outcome,
    }
    if error is not None:
        detail = f"{type(error).__name__}: {error}"
        if SUPABASE_KEY:
            detail = detail.replace(SUPABASE_KEY, "[REDACTED]")
        event["error"] = detail[:1000]
    message = json.dumps(event, ensure_ascii=False)
    print(f"  [Chart] {message}")
    try:
        logger = logging.getLogger("stock_memo.chart_retry")
        if not logger.handlers:
            CHART_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(CHART_LOG_PATH, maxBytes=5_000_000,
                                          backupCount=3, encoding="utf-8", delay=True)
            handler.setFormatter(logging.Formatter("%(message)s"))
            logger.addHandler(handler)
            logger.setLevel(logging.INFO)
            logger.propagate = False
        logger.info(message)
    except OSError as exc:
        print(f"  [Chart] log write failed: {type(exc).__name__}")


def _prepare_chart_with_retries(analysis: Analysis) -> str | None:
    """Try generation/upload three times, then let the text post proceed."""
    image_bytes = None
    for attempt in range(1, CHART_MAX_ATTEMPTS + 1):
        stage = "generate" if image_bytes is None else "upload"
        _log_chart_attempt(analysis, attempt, stage, "started")
        try:
            if image_bytes is None:
                from image_generator import generate_card
                image_bytes = generate_card(analysis)
                if not image_bytes:
                    image_bytes = None
                    raise ValueError("chart generation returned no image")
            stage = "upload"
            chart_url = _upload_chart(analysis.tweet.id, image_bytes)
            if not chart_url:
                raise RuntimeError("chart upload returned no URL")
            _log_chart_attempt(analysis, attempt, stage, "success")
            return chart_url
        except Exception as exc:
            _log_chart_attempt(analysis, attempt, stage, "failed", exc)
            if attempt < CHART_MAX_ATTEMPTS:
                time.sleep(CHART_RETRY_DELAY_SEC)
    _log_chart_attempt(analysis, CHART_MAX_ATTEMPTS, "fallback", "continue_without_image")
    return None


# ─────────────────────────────────────────────
def _clean(text) -> str:
    """analyzer 出力の () を除去"""
    t = (text or "").strip()
    while t.startswith("(") and t.endswith(")"):
        t = t[1:-1].strip()
    return t


def _split_keywords(kw_str: str) -> list[str]:
    if not kw_str:
        return []
    cleaned = _clean(kw_str)
    return [k.strip().strip("()（）") for k in re.split(r"[・、,，/]+", cleaned) if k.strip()]


# ─────────────────────────────────────────────
def _upload_chart(tweet_id: str, image_bytes: bytes) -> str | None:
    """急騰チャート PNG を Supabase Storage にアップロードし、公開URLを返す"""
    if not image_bytes:
        return None

    object_path = f"{tweet_id}.png"
    upload_url  = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{object_path}"
    public_url  = f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_BUCKET}/{object_path}"

    headers = {
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "apikey":        SUPABASE_KEY,
        "Content-Type":  "image/png",
        "x-upsert":      "true",
        "Cache-Control": "public, max-age=86400",
    }

    resp = requests.post(upload_url, headers=headers, data=image_bytes, timeout=TIMEOUT_SEC)
    if resp.status_code in (200, 201):
        return public_url
    # 同名ファイル存在 → PUT で上書き
    if resp.status_code in (400, 409):
        resp = requests.put(upload_url, headers=headers, data=image_bytes, timeout=TIMEOUT_SEC)
        if resp.ok:
            return public_url
    raise RuntimeError(f"chart upload HTTP {resp.status_code}: {resp.text[:200]}")


# ─────────────────────────────────────────────
def sync_to_site(analysis: Analysis) -> bool:
    """
    分析結果と急騰チャートを Supabase に送信し、サイトに即時反映する。

    Returns:
        送信成功なら True。未設定や失敗時は False（フロー本体は止めない）。
    """
    if not SUPABASE_URL or not SUPABASE_KEY:
        # サイト連携未設定: 静かにスキップ
        return False

    tweet = analysis.tweet

    # 1) 最大3回試行。3回とも失敗した場合だけ画像なしで続行。
    chart_url = _prepare_chart_with_retries(analysis)

    # 1.5) 急騰時株価を取得（失敗しても続行）
    surge_price = None
    try:
        from image_generator import fetch_current_price
        surge_price = fetch_current_price(_clean(analysis.stock_code))
    except Exception as e:
        print(f"  [Site] 株価取得失敗(スキップ): {e}")

    # 2) 行データを構築
    jst_iso = tweet.created_at.astimezone(timezone(timedelta(hours=9))).isoformat()

    row = {
        "tweet_id":       tweet.id,
        "detected_at":    jst_iso,
        "stock_code":     _clean(analysis.stock_code),
        "stock_name":     _clean(analysis.stock_name),
        "reason":         _clean(analysis.reason),
        "business":       _clean(analysis.business),
        "keywords":       _split_keywords(analysis.keywords),
        "backgrounds":    [_clean(b) for b in (analysis.backgrounds or [])],
        "related_stocks": (analysis.related_stocks or "").strip(),
        "raw_post":       tweet.text,
        "chart_url":      chart_url,
        "surge_price":    round(float(surge_price), 2) if surge_price is not None else None,
    }

    # 3) Supabase REST API に upsert（tweet_id がユニークキーなので重複は更新）
    insert_url = f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}?on_conflict=tweet_id"
    headers = {
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "apikey":        SUPABASE_KEY,
        "Content-Type":  "application/json",
        "Prefer":        "resolution=merge-duplicates,return=minimal",
    }

    try:
        resp = requests.post(insert_url, headers=headers, json=row, timeout=TIMEOUT_SEC)
        if resp.ok:
            chart_note = " (画像あり)" if chart_url else ""
            print(f"  [Site] 反映: {row['stock_code']} {row['stock_name']}{chart_note}")
            _sync_ohlc_safely(row["stock_code"])
            return True
        print(f"  [Site] 送信失敗 ({resp.status_code}): {resp.text[:200]}")
        return False
    except Exception as e:
        print(f"  [Site] 送信例外: {e}")
        return False


# ─────────────────────────────────────────────
def _sync_ohlc_safely(code: str) -> None:
    """リアルチャート用の OHLC（日足/週足）を同期。失敗してもフローは止めない。
    stock_ohlc テーブル未作成・yfinance未導入の場合は静かにスキップされる。"""
    try:
        from sync_ohlc import sync_codes
        sync_codes([code])
    except Exception as e:
        print(f"  [Site] OHLC同期スキップ: {e}")
