"""
当日材料の取得モジュール（TDnet適時開示 / 株探ニュース見出し）
==============================================================

理由分析の根拠付け（グラウンディング）用に、銘柄の「当日の一次情報」を取得する。
analyzer.py（本番）と eval_reason_accuracy.py（評価）の両方から使う共通実装。

- 取得窓は「前営業日15:00 〜 当日23:59 (JST)」。
  引け後開示が翌営業日の急騰理由になる典型パターンを拾うため。
- 取得失敗は呼び出し側を壊さない（警告ログ＋空リスト）。分析は材料なしで続行できる。
- データ源:
    TDnet  : yanoshin Web API（非公式ミラー、JSON）
    ニュース: 株探 銘柄別ニュース（スクレイピング、ページ遡及あり）
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import requests
from requests.adapters import HTTPAdapter

try:
    from urllib3.util.retry import Retry
except ImportError:  # 古いurllib3でも動くように
    Retry = None

JST = timezone(timedelta(hours=9))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# --- HTTP: 共有セッション + リトライ + 最小リクエスト間隔（過剰アクセス回避） ---
_MIN_INTERVAL = 0.7  # 秒。同一プロセスからの連続リクエストの最小間隔
_session = None
_session_lock = threading.Lock()
_throttle_lock = threading.Lock()
_last_request_at = 0.0


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        with _session_lock:
            if _session is None:
                s = requests.Session()
                s.headers.update(UA)
                if Retry is not None:
                    retry = Retry(
                        total=1, connect=1, read=1, backoff_factor=1.0,
                        status_forcelist=(429, 500, 502, 503, 504),
                        allowed_methods=frozenset(["GET"]),
                    )
                    adapter = HTTPAdapter(max_retries=retry)
                    s.mount("https://", adapter)
                    s.mount("http://", adapter)
                _session = s
    return _session


def _throttled_get(url: str, timeout: int = 20) -> requests.Response:
    global _last_request_at
    with _throttle_lock:
        wait = _MIN_INTERVAL - (time.monotonic() - _last_request_at)
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()
    return _get_session().get(url, timeout=timeout)


# --- TDnet(yanoshin)用サーキットブレーカー ---------------------------------
# yanoshinは時々ホストごと落ちる/ブロックされる。連続失敗したら一定時間リクエスト
# 自体を止め、ログ氾濫と無駄な待ち時間を防ぐ（その間は材料なしで分析続行）。
_TDNET_FAIL_THRESHOLD = 3
_TDNET_COOLDOWN_SEC = 300
_tdnet_fail_streak = 0
_tdnet_cooldown_until = 0.0


def _tdnet_available() -> bool:
    return time.monotonic() >= _tdnet_cooldown_until


def _tdnet_record_success() -> None:
    global _tdnet_fail_streak
    _tdnet_fail_streak = 0


def _tdnet_record_failure() -> None:
    global _tdnet_fail_streak, _tdnet_cooldown_until
    _tdnet_fail_streak += 1
    if _tdnet_fail_streak >= _TDNET_FAIL_THRESHOLD and _tdnet_available():
        _tdnet_cooldown_until = time.monotonic() + _TDNET_COOLDOWN_SEC
        print(f"  [INFO] TDnet(yanoshin)が連続失敗。{_TDNET_COOLDOWN_SEC // 60}分間スキップします"
              "（その間は株探ニュースのみで分析）")


# --- 材料キャッシュ（同一(銘柄,日付)の短時間の重複取得を防ぐ） --------------
# kyutou_source（持ち越し判定）とanalyzer（分析）が同じ銘柄を別々に取得するため、
# 短いTTLで重複を吸収する。TTLはkyutouの交互間隔(約10分)より短くし、ティックを
# またいだ再取得（新着材料の反映）は妨げない。
_MATERIALS_TTL = 240  # 秒
_materials_cache: dict[tuple[str, str], tuple[float, dict]] = {}
_cache_lock = threading.Lock()


def _is_market_closed(d) -> bool:
    """東証の休場日か（土日・祝日・年末年始12/31〜1/3）。"""
    if d.weekday() >= 5:
        return True
    if (d.month == 12 and d.day == 31) or (d.month == 1 and d.day <= 3):
        return True  # 東証の年末年始休場（祝日ではないが市場は閉まる）
    try:
        import jpholiday
        if jpholiday.is_holiday(d):
            return True
    except ImportError:
        pass  # 未インストールなら土日・年末年始のみで判定（分析は止めない）
    return False


def _window(day_str: str) -> tuple[datetime, datetime]:
    """前営業日15:00(JST) 〜 当日23:59(JST) の窓を返す。

    連休前の引け後開示が連休明けの急騰材料になる典型パターンを取りこぼさないよう、
    「前営業日」は土日・祝日・年末年始休場をすべてスキップして求める。
    """
    day = datetime.strptime(day_str, "%Y-%m-%d").date()
    prev = day - timedelta(days=1)
    while _is_market_closed(prev):
        prev -= timedelta(days=1)
    start = datetime(prev.year, prev.month, prev.day, 15, 0, tzinfo=JST)
    end = datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=JST)
    return start, end


def _fetch_tdnet(code: str, day_str: str, limit: int = 30) -> tuple[list[str], bool]:
    """yanoshin TDnet APIから窓内の開示タイトルを取得。(タイトル一覧, 取得成功か) を返す。

    サーキットブレーカー作動中はリクエストせず ([], False) を即返す。
    """
    if not _tdnet_available():
        return [], False
    try:
        url = f"https://webapi.yanoshin.jp/webapi/tdnet/list/{code}.json?limit={limit}"
        r = _throttled_get(url)
        r.raise_for_status()
        items = r.json().get("items", [])
    except Exception as e:
        _tdnet_record_failure()
        if _tdnet_available():  # クールダウン未作動なら個別に簡潔表示
            print(f"  [WARN] TDnet取得失敗 {code}: {type(e).__name__}")
        return [], False
    _tdnet_record_success()
    start, end = _window(day_str)
    titles = []
    for it in items:
        td = it.get("Tdnet", {})
        try:
            pub = datetime.strptime(td.get("pubdate", ""), "%Y-%m-%d %H:%M:%S").replace(tzinfo=JST)
        except ValueError:
            continue
        if start <= pub <= end:
            titles.append(f"{pub.strftime('%m/%d %H:%M')} {td.get('title', '')}")
    return titles, True


def _fetch_kabutan(code: str, day_str: str, max_pages: int = 5) -> tuple[list[str], bool]:
    """株探の銘柄別ニュースから窓内の見出しを取得。(見出し一覧, 取得成功か) を返す。"""
    from bs4 import BeautifulSoup

    start, end = _window(day_str)
    headlines: list[str] = []
    try:
        for page in range(1, max_pages + 1):
            url = f"https://kabutan.jp/stock/news?code={code}&page={page}"
            r = _throttled_get(url)
            r.raise_for_status()
            soup = BeautifulSoup(r.content, "html.parser")
            rows = []
            for tr in soup.find_all("tr"):
                time_el = tr.find("time")
                a = tr.find("a")
                if not (time_el and a):
                    continue
                try:
                    ts = datetime.fromisoformat(time_el.get("datetime", ""))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=JST)
                except ValueError:
                    continue
                rows.append((ts, a.get_text(strip=True)))
            if not rows:
                break
            for ts, title in rows:
                if start <= ts <= end:
                    headlines.append(f"{ts.strftime('%m/%d %H:%M')} {title}")
            # このページの最古がもう窓より前なら次ページは不要
            if min(ts for ts, _ in rows) < start:
                break
    except Exception as e:
        print(f"  [WARN] 株探取得失敗 {code}: {type(e).__name__}")
        return headlines, False
    return headlines, True


# 後方互換: 一覧だけ欲しい呼び出し（eval_reason_accuracy 等）向け
def fetch_tdnet_titles(code: str, day_str: str, limit: int = 30) -> list[str]:
    return _fetch_tdnet(code, day_str, limit)[0]


def fetch_kabutan_headlines(code: str, day_str: str, max_pages: int = 5) -> list[str]:
    return _fetch_kabutan(code, day_str, max_pages)[0]


def fetch_materials(code: str, day_str: str) -> dict:
    """TDnet開示とニュース見出しをまとめて取得する。

    返り値:
      {"tdnet": [...], "news": [...], "tdnet_ok": bool, "news_ok": bool}
    *_ok は「そのソースの取得が成功したか」。False なら通信失敗で、空リストでも
    「材料が無い」とは断定できない（持ち越し判定で塩漬けを防ぐのに使う）。
    短時間の重複取得は TTL キャッシュで吸収する。
    """
    key = (code, day_str)
    now = time.monotonic()
    with _cache_lock:
        cached = _materials_cache.get(key)
        if cached and now - cached[0] < _MATERIALS_TTL:
            return cached[1]

    tdnet, tdnet_ok = _fetch_tdnet(code, day_str)
    news, news_ok = _fetch_kabutan(code, day_str)
    result = {"tdnet": tdnet, "news": news, "tdnet_ok": tdnet_ok, "news_ok": news_ok}

    with _cache_lock:
        _materials_cache[key] = (now, result)
    return result
