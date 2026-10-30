# -*- coding: utf-8 -*-
"""急落イベントの Notion 保存

急騰側(notion_storage.py)が「毎日急騰分析DB」に1ツイート=1行を書くのに対して、
こちらは drop_detector.py が検知した1急落=1行を「毎日急落分析DB」に書く。
列構成が違うので急騰DBとは別DB・別モジュールにしてある(drop_events テーブルを
surge_events と分けているのと同じ理由)。

入力は drop_detector が作る DataFrame の1行(pandas Series か dict)。
Supabase / CSV への保存とは独立していて、Notion が落ちても検知は止めない。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from config import NOTION_DROP_DATABASE_ID, NOTION_TOKEN

JST = timezone(timedelta(hours=9))

# Notion の rich_text は1ブロック2000文字まで
TEXT_MAX = 2000

# select プロパティに入れてよい値。DB定義に無い値を送ると Notion 側で
# 勝手に選択肢が増えて表記ゆれが溜まるため、想定内の値だけ通す。
_ALLOWED = {
    "開示種別": {"下方修正", "決算", "特別損失", "増資希薄化", "減配無配",
                 "業務提携解消", "訴訟不祥事", "上場廃止関連", "その他開示", "開示なし"},
    "性質": {"一過性", "構造的", "不明"},
    "範囲": {"個別事情", "業界全体", "市場全体", "不明"},
    "織り込み": {"過剰反応", "妥当", "織り込み不足", "不明"},
    "次の材料": {"近い", "遠い", "不明"},
    "市場": {"プライム（内国株式）", "スタンダード（内国株式）", "グロース（内国株式）"},
    "検知方法": {"intraday", "eod"},
    "戻り判定": {"全戻し", "半戻し", "未回復"},
}


def _s(v) -> str:
    """NaN/None を空文字にして str 化する。"""
    if v is None:
        return ""
    try:
        if pd.isna(v):
            return ""
    except (TypeError, ValueError):
        pass
    return str(v).strip()


def _is_true(v) -> bool:
    """真とみなせるか。CSVを経由すると "True" 文字列や NaN で戻ってくる。"""
    if v is None:
        return False
    try:
        if pd.isna(v):
            return False
    except (TypeError, ValueError):
        pass
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes")
    return bool(v)


def _num(v):
    """数値として使えるなら float、無理なら None。"""
    try:
        if v is None or pd.isna(v):
            return None
        f = float(v)
        return f if f == f and abs(f) != float("inf") else None
    except (TypeError, ValueError):
        return None


def _text(v) -> dict | None:
    s = _s(v)
    return {"rich_text": [{"type": "text", "text": {"content": s[:TEXT_MAX]}}]} if s else None


def _select(prop: str, v) -> dict | None:
    s = _s(v)
    if not s:
        return None
    allowed = _ALLOWED.get(prop)
    if allowed is not None and s not in allowed:
        return None
    return {"select": {"name": s}}


def _number(v) -> dict | None:
    f = _num(v)
    return {"number": f} if f is not None else None


def _date(v) -> dict | None:
    """YYYY-MM-DD の日付プロパティ。"""
    s = _s(v)
    if not s:
        return None
    try:
        return {"date": {"start": pd.to_datetime(s).strftime("%Y-%m-%d")}}
    except Exception:
        return None


def build_analysis_text(r) -> str:
    """理由・背景・補足・根拠を1つのテキストにまとめる。

    Notion で列が増えすぎるため表示は1本化する。CSV / Supabase 側は
    reason / backgrounds / note / evidence を別列のまま持たせてあるので、
    集計や絞り込みはそちらでできる(ここは読むための整形だけ)。
    """
    get = r.get
    parts = []
    reason = _s(get("reason"))
    if reason:
        parts.append(reason)
    bg = _s(get("backgrounds"))
    if bg:
        parts.append(bg)
    note = _s(get("note"))
    if note and note != reason:
        parts.append(f"所見: {note}")
    ev = _s(get("evidence"))
    if ev:
        parts.append(f"根拠: {ev}")
    return "\n\n".join(parts)


def build_properties(r) -> dict:
    """1行ぶんの Notion プロパティを組み立てる。値が無い項目は入れない。"""
    get = r.get
    code, name = _s(get("stock_code")), _s(get("stock_name"))

    # 「開示なし」を信じてよいのは、TDnetもニュースも**取りに行けた**ときだけ。
    # 取得に失敗した場合に加えて、そもそも materials を引いていない行
    # (--no-llm で検知だけした、分析が途中で落ちた等で ok 列が空)も立てる。
    # 「材料が0件だった」観測と「材料を確認していない」を混同しないための印。
    missing = not (_is_true(get("tdnet_ok")) and _is_true(get("news_ok")))

    props = {
        "銘柄名": {"title": [{"type": "text", "text": {"content": name or code or "?"}}]},
        "一次情報の欠落": {"checkbox": bool(missing)},
        "検知時刻": {"date": {"start": datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S+09:00")}},
    }

    optional = {
        "銘柄コード": _text(code),
        "急落日": _date(get("drop_date")),
        "下落率": _number(get("drop_pct")),
        "終値": _number(get("close")),
        # 出来高は検知条件から外したが、後から分析できるよう記録は厚くする
        "出来高": _number(get("volume")),
        "20日平均出来高": _number(get("vol20")),
        "5日平均出来高": _number(get("vol5")),
        "出来高倍率": _number(get("vol_ratio")),
        "出来高倍率(5日)": _number(get("vol_ratio5")),
        "売買代金": _number(get("turnover")),
        "時価総額": _number(get("market_cap")),
        "開示種別": _select("開示種別", get("disclosure")),
        "分析": _text(build_analysis_text(r)),
        "性質": _select("性質", get("nature")),
        "範囲": _select("範囲", get("scope")),
        "織り込み": _select("織り込み", get("priced")),
        "次の材料": _select("次の材料", get("next_catalyst")),
        "関連銘柄": _text(get("related_stocks")),
        "業種": _select("業種", get("sector33")),
        "規模": _text(get("scale")),
        "市場": _select("市場", get("market")),
        "ギャップ寄与": _number(get("gap_share")),
        "業種比": _number(get("excess_drop")),
        "地合い": _number(get("breadth_dn3")),
        "売買代金20日平均": _number(get("turnover20")),
        "検知方法": _select("検知方法", get("detect_mode")),
    }
    props.update({k: v for k, v in optional.items() if v is not None})
    return props


_schema_cache: dict[str, set] = {}


def _schema_props(client) -> set:
    """Notion DB に実在するプロパティ名。

    ユーザーが Notion 側で列を消しても壊れないようにするため、送信前に
    ここで突き合わせる。存在しない列を1つでも送ると Notion は
    「X is not a property that exists」で400を返し、その行が丸ごと保存
    できなくなる(列を消すたびにコードを直す運用は現実的でない)。
    """
    key = NOTION_DROP_DATABASE_ID
    if key in _schema_cache:
        return _schema_cache[key]
    ds = client.data_sources.retrieve(data_source_id=_data_source_id(client))
    _schema_cache[key] = set((ds.get("properties") or {}).keys())
    return _schema_cache[key]


def _filtered_properties(client, r) -> dict:
    props = build_properties(r)
    live = _schema_props(client)
    return {k: v for k, v in props.items() if k in live}


_ds_cache: dict[str, str] = {}


def _data_source_id(client) -> str:
    """DBのデータソースIDを引く。

    notion-client 3.x (Notion API 2025-09-03) では databases.query が廃止され、
    行の検索は data_sources.query になった。DBは1データソース構成なので先頭を使う。
    """
    cached = _ds_cache.get(NOTION_DROP_DATABASE_ID)
    if cached:
        return cached
    db = client.databases.retrieve(database_id=NOTION_DROP_DATABASE_ID)
    sources = db.get("data_sources") or []
    if not sources:
        raise RuntimeError("データソースが見つかりません")
    _ds_cache[NOTION_DROP_DATABASE_ID] = sources[0]["id"]
    return sources[0]["id"]


def _existing_pages(client, days_back: int = 40) -> dict[tuple[str, str], str]:
    """直近の {(銘柄コード, 急落日): ページID} を集める。

    Notion にはユニーク制約が無いので、書き込む前にこちらで突き合わせる。
    ページIDまで持つのは、列を後から追加したときに既存行を更新できるようにするため。
    """
    since = (datetime.now(JST) - timedelta(days=days_back)).strftime("%Y-%m-%d")
    ds_id = _data_source_id(client)
    found: dict[tuple[str, str], str] = {}
    cursor = None
    while True:
        resp = client.data_sources.query(
            data_source_id=ds_id,
            filter={"property": "急落日", "date": {"on_or_after": since}},
            page_size=100,
            **({"start_cursor": cursor} if cursor else {}),
        )
        for page in resp.get("results", []):
            p = page.get("properties", {})
            rt = p.get("銘柄コード", {}).get("rich_text") or []
            code = rt[0]["plain_text"].strip() if rt else ""
            d = (p.get("急落日", {}).get("date") or {}).get("start") or ""
            if code and d:
                found[(code, d[:10])] = page["id"]
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")
    return found


def save_drops_to_notion(df: pd.DataFrame, *, dedupe: bool = True,
                         on_duplicate: str = "skip", days_back: int = 40,
                         verbose: bool = True) -> int:
    """急落を Notion DB に追加する。書き込めた件数を返す。

    on_duplicate="skip"   … 既に同じ(銘柄,急落日)があれば何もしない(日次運用)
    on_duplicate="update" … 既存ページのプロパティを上書きする
                            (列を後から足したときに過去分を埋め直す用)

    検知を止めないため、この関数は例外を投げない。
    """
    try:
        if df is None or len(df) == 0:
            return 0
        if not NOTION_TOKEN or not NOTION_DROP_DATABASE_ID:
            if verbose:
                print("  [drop][Notion] NOTION_TOKEN / NOTION_DROP_DATABASE_ID 未設定のためスキップ")
            return 0

        from notion_client import Client
        client = Client(auth=NOTION_TOKEN)

        seen: dict[tuple[str, str], str] = {}
        if dedupe:
            try:
                seen = _existing_pages(client, days_back=days_back)
            except Exception as e:
                # 突き合わせに失敗しても書き込みは続ける(取りこぼしより重複の方がまし)
                print(f"  [drop][Notion] 既存行の照会に失敗(重複チェックなしで続行): {e}")

        saved = 0
        for _, r in df.iterrows():
            code = _s(r.get("stock_code"))
            day = _s(r.get("drop_date"))[:10]
            if dedupe and (code, day) in seen:
                if on_duplicate != "update":
                    if verbose:
                        print(f"  [drop][Notion] 登録済みのためスキップ: {code} {day}")
                    continue
                try:
                    client.pages.update(page_id=seen[(code, day)],
                                        properties=_filtered_properties(client, r))
                    saved += 1
                    if verbose:
                        print(f"  [drop][Notion] 更新: {code} {day}")
                except Exception as e:
                    print(f"  [drop][Notion] 更新失敗 {code} {day}: {e}")
                continue
            try:
                client.pages.create(
                    parent={"database_id": NOTION_DROP_DATABASE_ID},
                    properties=_filtered_properties(client, r),
                )
                seen[(code, day)] = ""
                saved += 1
            except Exception as e:
                print(f"  [drop][Notion] 保存失敗 {code} {day}: {e}")

        if verbose and saved:
            print(f"  [drop][Notion] {saved}件を保存")
        return saved
    except Exception as e:
        print(f"  [drop][Notion] 保存処理に失敗(検知は継続): {type(e).__name__}: {e}")
        return 0
