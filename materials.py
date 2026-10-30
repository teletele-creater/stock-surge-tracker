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
             + Yahoo!ファイナンス 銘柄ニュース（同上）
             Yahooはトレーダーズ・ウェブ等、株探にない配信元を拾える。
             実例: 8995 誠建設工業 2026-07-27 の「『副首都』法案が可決」は
             株探にはなくYahoo経由のトレーダーズ・ウェブ記事にだけ出ていた。
"""

from __future__ import annotations

import re
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
    """前営業日09:00(JST) 〜 当日23:59(JST) の窓を返す。

    連休前の引け後開示が連休明けの急騰材料になる典型パターンに加え、前営業日の
    「場中」に出た材料で翌日も続伸する（マルチデイ・モメンタム）パターンも拾うため、
    開始を前営業日の寄り付き(09:00)まで遡らせる。以前は15:00開始だったが、場中の
    好材料（例: 6522の6/30 12:00「全商品RFID化ソリューション本格展開」）を取りこぼし
    翌日の分析が推測に落ちる事故があった。
    「前営業日」は土日・祝日・年末年始休場をすべてスキップして求める。
    """
    day = datetime.strptime(day_str, "%Y-%m-%d").date()
    prev = day - timedelta(days=1)
    while _is_market_closed(prev):
        prev -= timedelta(days=1)
    start = datetime(prev.year, prev.month, prev.day, 9, 0, tzinfo=JST)
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


# --- 市場全体ノイズの除外 ----------------------------------------------------
# 株探の銘柄別ニュースには、その銘柄が「一覧の一員として名前が挙がっただけ」の
# 市場全体ランキング/まとめ記事が大量に混ざる。これらは急騰の“理由”ではないうえ、
# ①プロンプトに混入して本物の材料を埋もれさせ、②kyutou_source の has_materials 判定を
# True にして持ち越し（材料待ち）の安全網を突破させる。見出し単位で弾く。
# 注意: 銘柄固有の材料見出し（例「アスタリスク急伸、『全商品RFID化…』本格展開へ」)は
# 下記いずれにも一致しないため残る。ランキング/まとめ記事に特有の語だけを対象にする。
_NOISE_SUBSTRINGS = (
    "本日の【",          # ストップ高/安・均衡表・MACD・GC/DC・パラボリック等のランキング
    "＜動意株",           # 動意株まとめ
    "前日に動いた銘柄",    # 前日値動きまとめ
    "話題株ダイジェスト",  # 週間まとめ
    "材料株　ベスト",      # 「◯◯年前半の【急騰】材料株 ベスト50」等（全角スペース）
    "材料株 ベスト",       # 同上（半角スペース）
    "日経平均",           # 相場全体のマーケットラップ
    "本日の株式相場",
    "後場の東京株式市場",
    "前場の東京株式市場",
    "注目銘柄ダイジェスト",  # 複数銘柄を並べただけのまとめ
    "話題株ピックアップ",
    "＜特別気配＞",         # 「＜特別気配＞ＳＵＭＣＯ、東洋エンジ…が買い気配」
    "のランキング【",      # 「前場のランキング【値上がり率】」等
    "東証プライム（",      # 「東証スタンダード（前引け）＝値上がり優勢、誠建設がS高」等の市況まとめ
    "東証スタンダード（",
    "東証グロース（",
)


def _is_market_noise(title: str) -> bool:
    """銘柄名が一覧に載っただけの市場全体記事なら True（＝材料として採用しない）。"""
    return any(s in title for s in _NOISE_SUBSTRINGS)


def _fetch_kabutan(code: str, day_str: str, max_pages: int = 5) -> tuple[list[str], bool]:
    """株探の銘柄別ニュースから窓内の見出しを取得。(見出し一覧, 取得成功か) を返す。

    市場全体ランキング/まとめ記事（_is_market_noise）は除外し、銘柄固有の材料だけ残す。
    """
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
                if start <= ts <= end and not _is_market_noise(title):
                    headlines.append(f"{ts.strftime('%m/%d %H:%M')} {title}")
            # このページの最古がもう窓より前なら次ページは不要
            if min(ts for ts, _ in rows) < start:
                break
    except Exception as e:
        print(f"  [WARN] 株探取得失敗 {code}: {type(e).__name__}")
        return headlines, False
    return headlines, True


# --- Yahoo!ファイナンス 銘柄ニュース -----------------------------------------
# 株探は自社ニュースが中心で、トレーダーズ・ウェブ／時事通信などの配信を持たない。
# Yahooの銘柄ニュース欄はこれらを含むため、株探で材料0件でも急騰理由が見つかる。
#
# 注意点:
#  - 市場（東証/名証/福証/札証）でURLのサフィックスが変わる（8995.T / 1999.F など）。
#    .T から順に試して当たったものを30日キャッシュする。
#  - 「最新ニュース」欄には同業他社の決算速報など、その銘柄と無関係な記事も混ざる。
#    見出しに社名（または銘柄コード）が出ているものだけ採用して誤読を防ぐ。
#  - ページのクラス名はハッシュ付き（Yahoo再デプロイで変わる）なので、
#    <article>/<h3>/<time> という構造だけに依存して抽出する。
_YAHOO_SUFFIXES = (".T", ".F", ".N", ".S")
_YAHOO_SECTIONS = ("ピックアップ", "最新ニュース")  # 銘柄固有ニュースの見出し
_YAHOO_SYMBOL_TTL_SEC = 30 * 24 * 3600
_symbol_lock = threading.Lock()
_symbol_cache: dict | None = None  # {code: {"fetched_at": epoch秒, "suffix": ".T"}}


def _load_symbol_cache() -> dict:
    global _symbol_cache
    if _symbol_cache is None:
        import json
        try:
            with open(_cache_path("yahoo_symbols.json"), encoding="utf-8") as f:
                _symbol_cache = json.load(f)
        except Exception:
            _symbol_cache = {}
    return _symbol_cache


def _save_symbol_cache() -> None:
    import json
    try:
        with open(_cache_path("yahoo_symbols.json"), "w", encoding="utf-8") as f:
            json.dump(_symbol_cache, f, ensure_ascii=False, indent=1)
    except Exception as e:
        print(f"  [WARN] Yahooシンボルキャッシュ保存失敗: {type(e).__name__}")


def _cached_yahoo_suffix(code: str) -> str | None:
    with _symbol_lock:
        entry = _load_symbol_cache().get(code)
    if entry and time.time() - entry.get("fetched_at", 0) < _YAHOO_SYMBOL_TTL_SEC:
        return entry.get("suffix")
    return None


def _remember_yahoo_suffix(code: str, suffix: str) -> None:
    with _symbol_lock:
        cache = _load_symbol_cache()
        cache[code] = {"fetched_at": time.time(), "suffix": suffix}
        _save_symbol_cache()


def _fetch_yahoo_news_page(code: str) -> str | None:
    """銘柄ニュースページのHTMLを返す。市場サフィックスは順に試して学習する。"""
    known = _cached_yahoo_suffix(code)
    suffixes = ((known,) + tuple(s for s in _YAHOO_SUFFIXES if s != known)) if known \
        else _YAHOO_SUFFIXES
    for suffix in suffixes:
        r = _throttled_get(f"https://finance.yahoo.co.jp/quote/{code}{suffix}/news")
        if r.status_code == 200:
            if suffix != known:
                _remember_yahoo_suffix(code, suffix)
            return r.text
        if r.status_code != 404:
            r.raise_for_status()  # 404以外（429/5xx）は通信失敗として扱う
    return None  # どの市場にも存在しない（上場廃止・コード誤り）


def _parse_yahoo_time(text: str, now: datetime) -> datetime | None:
    """Yahooの相対表記を JST の datetime に変換する。

    当日は "10:44"、それ以前は "7/23"（時刻なし）。時刻なしはその日の23:59として
    扱う（窓の開始が前営業日09:00なので、時刻不明の記事を取りこぼさないため）。
    """
    text = text.strip()
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if m:
        return now.replace(hour=int(m.group(1)), minute=int(m.group(2)),
                           second=0, microsecond=0)
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})", text)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        try:
            ts = datetime(now.year, month, day, 23, 59, tzinfo=JST)
        except ValueError:
            return None
        if ts > now + timedelta(days=7):  # 年跨ぎ（1月に12月の記事を見ている）
            ts = ts.replace(year=now.year - 1)
        return ts
    return None


def _yahoo_name_keys(soup, code: str) -> set[str]:
    """見出しの銘柄一致判定に使う社名キー。

    ページ上の正式名（例「(株)誠建設工業」）から法人格・HD表記を落とし、
    さらに先頭3文字（株探等が使う略称「誠建設」に対応）も候補に入れる。
    """
    name = ""
    h1 = soup.find("h1")
    if h1:
        name = h1.get_text(strip=True).replace("のニュース", "")
    if not name:
        profile = fetch_company_profile(code) or {}
        name = profile.get("name", "")
    name = re.sub(r"(株式会社|\(株\)|（株）|ホールディングス|ＨＤ|ＨＬＤＧＳ)", "", name).strip()
    keys = {name, name[:3]} if len(name) >= 3 else {name}
    return {k for k in keys if len(k) >= 2}


def _fetch_yahoo(code: str, day_str: str) -> tuple[list[str], bool]:
    """Yahoo!ファイナンスの銘柄ニュースから窓内の見出しを取得。

    (見出し一覧, 取得成功か) を返す。市場全体まとめ記事と、社名・コードに触れて
    いない記事（同業他社の決算速報など）は除外する。
    """
    from bs4 import BeautifulSoup

    start, end = _window(day_str)
    now = datetime.now(JST)
    headlines: list[str] = []
    try:
        html = _fetch_yahoo_news_page(code)
    except Exception as e:
        print(f"  [WARN] Yahooニュース取得失敗 {code}: {type(e).__name__}")
        return [], False
    if html is None:
        return [], True  # ページ自体が無い＝取得は正常、材料なしと同義

    # Yahoo側のマークアップ変更で解析が壊れても監視ループを落とさない
    try:
        soup = BeautifulSoup(html, "html.parser")
        keys = _yahoo_name_keys(soup, code)

        sections = []
        for h2 in soup.find_all("h2"):
            if h2.get_text(strip=True) not in _YAHOO_SECTIONS:
                continue
            section = h2.find_parent("section")
            if section is not None and section not in sections:
                sections.append(section)

        seen: set[str] = set()
        for section in sections:
            for article in section.find_all("article"):
                heading, time_el = article.find("h3"), article.find("time")
                if not (heading and time_el):
                    continue
                title = heading.get_text(" ", strip=True)
                ts = _parse_yahoo_time(time_el.get_text(strip=True), now)
                if ts is None or not (start <= ts <= end):
                    continue
                if title in seen or _is_market_noise(title):
                    continue
                # 見出しに社名もコードも出ない記事は、他社の話が紛れているとみなす
                if not (code in title or any(k in title for k in keys)):
                    continue
                seen.add(title)
                headlines.append(f"{ts.strftime('%m/%d %H:%M')} {title}")
    except Exception as e:
        print(f"  [WARN] Yahooニュース解析失敗 {code}: {type(e).__name__}")
        return headlines, False
    return headlines, True


# 後方互換: 一覧だけ欲しい呼び出し（eval_reason_accuracy 等）向け
def fetch_tdnet_titles(code: str, day_str: str, limit: int = 30) -> list[str]:
    return _fetch_tdnet(code, day_str, limit)[0]


def fetch_kabutan_headlines(code: str, day_str: str, max_pages: int = 5) -> list[str]:
    return _fetch_kabutan(code, day_str, max_pages)[0]


def fetch_yahoo_headlines(code: str, day_str: str) -> list[str]:
    return _fetch_yahoo(code, day_str)[0]


# --- 企業プロフィール（正式社名・事業内容・業種・テーマ） -------------------
# analyzer が事業内容をモデルの記憶から創作するハルシネーション対策。
# 実例: 3168(ＭＥＲＦ=非鉄金属)を「中古車リユース」、3779(ジェイエスコムHD=理美容
# 商材/通販)を「NTT向け通信工事」と、社名からの連想で事業内容ごと捏造した。
# 株探の銘柄トップページ(会社情報ブロック)から正式情報を取得しプロンプトに注入する。
# プロフィールはほぼ不変なのでディスクキャッシュ（30日）で再取得を抑える。
_PROFILE_TTL_SEC = 30 * 24 * 3600
_profile_lock = threading.Lock()
_profile_cache: dict | None = None  # {code: {"fetched_at": epoch秒, "profile": {...}|None}}


def _cache_path(filename: str):
    from pathlib import Path
    try:
        from config import BASE_DIR
        base = Path(BASE_DIR)
    except Exception:
        base = Path(__file__).resolve().parent
    d = base / "kyutou_cache"
    d.mkdir(exist_ok=True)
    return d / filename


def _profile_cache_path():
    return _cache_path("company_profiles.json")


def _load_profile_cache() -> dict:
    global _profile_cache
    if _profile_cache is None:
        import json
        try:
            with open(_profile_cache_path(), encoding="utf-8") as f:
                _profile_cache = json.load(f)
        except Exception:
            _profile_cache = {}
    return _profile_cache


def _save_profile_cache() -> None:
    import json
    try:
        with open(_profile_cache_path(), "w", encoding="utf-8") as f:
            json.dump(_profile_cache, f, ensure_ascii=False, indent=1)
    except Exception as e:
        print(f"  [WARN] プロフィールキャッシュ保存失敗: {type(e).__name__}")


def _scrape_company_profile(code: str) -> dict | None:
    """株探の銘柄トップページから会社情報を取得する。見つからなければ None。"""
    from bs4 import BeautifulSoup

    url = f"https://kabutan.jp/stock/?code={code}"
    r = _throttled_get(url)
    r.raise_for_status()
    soup = BeautifulSoup(r.content, "html.parser")
    block = soup.select_one("div.company_block")
    if block is None:
        return None

    h3 = block.find("h3")
    name = h3.get_text(strip=True) if h3 else ""
    rows: dict[str, str] = {}
    themes: list[str] = []
    for tr in block.find_all("tr"):
        th, td = tr.find("th"), tr.find("td")
        if not (th and td):
            continue
        key = th.get_text(strip=True)
        if key == "テーマ":
            themes = [li.get_text(strip=True) for li in td.find_all("li")]
        else:
            rows[key] = td.get_text(" ", strip=True)

    profile = {
        "name": name,
        "summary": rows.get("概要", ""),
        "sector": rows.get("業種", ""),
        "themes": themes,
    }
    # 社名も概要も取れないなら実質失敗扱い（ページ構造変更や未上場コード）
    if not profile["name"] and not profile["summary"]:
        return None
    return profile


def fetch_company_profile(code: str) -> dict | None:
    """銘柄コードから正式な会社情報を返す（30日ディスクキャッシュ付き）。

    返り値: {"name": 正式社名, "summary": 事業概要, "sector": 業種, "themes": [...]}
    取得失敗時は None（呼び出し側はプロフィールなしで分析を続行する）。
    """
    with _profile_lock:
        cache = _load_profile_cache()
        entry = cache.get(code)
        if entry and time.time() - entry.get("fetched_at", 0) < _PROFILE_TTL_SEC:
            return entry.get("profile")

    try:
        profile = _scrape_company_profile(code)
    except Exception as e:
        print(f"  [WARN] 企業プロフィール取得失敗 {code}: {type(e).__name__}")
        # 失敗はキャッシュしない（次回リトライさせる）。古いキャッシュがあればそれを使う
        return entry.get("profile") if entry else None

    with _profile_lock:
        cache[code] = {"fetched_at": time.time(), "profile": profile}
        _save_profile_cache()
    return profile


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
    kabutan, kabutan_ok = _fetch_kabutan(code, day_str)
    yahoo, yahoo_ok = _fetch_yahoo(code, day_str)

    # 同じ記事が両サイトに載ることがあるので見出し本文で重複を落とす
    # "MM/DD HH:MM 見出し" の先頭2要素（日付・時刻）を除いた本文でキーを作る
    news = list(kabutan)
    seen = {h.split(" ", 2)[-1] for h in kabutan}
    for headline in yahoo:
        title = headline.split(" ", 2)[-1]
        if title not in seen:
            seen.add(title)
            news.append(headline)

    # どちらか一方でも生きていれば「空＝本当に材料なし」と信頼してよい
    news_ok = kabutan_ok or yahoo_ok
    result = {"tdnet": tdnet, "news": news, "tdnet_ok": tdnet_ok, "news_ok": news_ok}

    with _cache_lock:
        _materials_cache[key] = (now, result)
    return result
