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

import time
from datetime import datetime, timedelta, timezone

import requests

JST = timezone(timedelta(hours=9))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


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


def fetch_tdnet_titles(code: str, day_str: str, limit: int = 30) -> list[str]:
    """yanoshin TDnet API から、窓内の開示タイトルを返す。失敗時は空リスト。"""
    try:
        url = f"https://webapi.yanoshin.jp/webapi/tdnet/list/{code}.json?limit={limit}"
        r = requests.get(url, headers=UA, timeout=20)
        r.raise_for_status()
        items = r.json().get("items", [])
    except Exception as e:
        print(f"  [WARN] TDnet取得失敗 {code}: {e}")
        return []
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
    return titles


def fetch_kabutan_headlines(code: str, day_str: str, max_pages: int = 5) -> list[str]:
    """株探の銘柄別ニュースから、窓内の見出しを返す。失敗時は空リスト。"""
    from bs4 import BeautifulSoup

    start, end = _window(day_str)
    headlines: list[str] = []
    try:
        for page in range(1, max_pages + 1):
            url = f"https://kabutan.jp/stock/news?code={code}&page={page}"
            r = requests.get(url, headers=UA, timeout=20)
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
            time.sleep(1)  # 過剰アクセス回避
    except Exception as e:
        print(f"  [WARN] 株探取得失敗 {code}: {e}")
    return headlines


def fetch_materials(code: str, day_str: str) -> dict[str, list[str]]:
    """TDnet開示とニュース見出しをまとめて取得する。各ソース独立に失敗を握りつぶす。"""
    return {
        "tdnet": fetch_tdnet_titles(code, day_str),
        "news": fetch_kabutan_headlines(code, day_str),
    }
