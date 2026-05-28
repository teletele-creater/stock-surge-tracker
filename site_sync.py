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
from datetime import timezone, timedelta

from analyzer import Analysis


SUPABASE_URL    = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY    = os.getenv("SUPABASE_SERVICE_KEY", "")  # service_role キー（Pythonフロー専用、絶対に公開しない）
SUPABASE_TABLE  = os.getenv("SUPABASE_TABLE",  "surge_events")
SUPABASE_BUCKET = os.getenv("SUPABASE_BUCKET", "charts")

TIMEOUT_SEC = 20


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

    try:
        resp = requests.post(upload_url, headers=headers, data=image_bytes, timeout=TIMEOUT_SEC)
        if resp.status_code in (200, 201):
            return public_url
        # 同名ファイル存在 → PUT で上書き
        if resp.status_code in (400, 409):
            resp = requests.put(upload_url, headers=headers, data=image_bytes, timeout=TIMEOUT_SEC)
            if resp.ok:
                return public_url
        print(f"  [Site] チャートUP失敗 ({resp.status_code}): {resp.text[:200]}")
        return None
    except Exception as e:
        print(f"  [Site] チャートUP例外: {e}")
        return None


# ─────────────────────────────────────────────
def _generate_chart_safely(analysis: Analysis) -> bytes | None:
    """急騰チャート画像を生成。失敗してもサイト送信は続行する"""
    try:
        from image_generator import generate_card
        return generate_card(analysis)
    except Exception as e:
        print(f"  [Site] チャート生成スキップ: {e}")
        return None


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

    # 1) チャート画像を生成してアップロード（失敗しても続行）
    image_bytes = _generate_chart_safely(analysis)
    chart_url   = _upload_chart(tweet.id, image_bytes) if image_bytes else None

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
            return True
        print(f"  [Site] 送信失敗 ({resp.status_code}): {resp.text[:200]}")
        return False
    except Exception as e:
        print(f"  [Site] 送信例外: {e}")
        return False
