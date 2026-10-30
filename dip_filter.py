# -*- coding: utf-8 -*-
"""押し目反発フィルタ — 共通4条件中3点以上＋MACD/出来高のOR判定

仕様の全文は dip_filter_spec.md を参照。このスクリプト単体で動く。

使い方(stock_memo ディレクトリで):
    py dip_filter.py                    # 全期間の初押しを判定
    py dip_filter.py --since 2026-07-01 # 押し目日で絞る
    py dip_filter.py --min-score 3      # 3条件以上該当のみ出力
    py dip_filter.py --selected-only    # 共通条件＋MACD/出来高の採用候補
    py dip_filter.py --with-llm         # 旧②の参考値を追加取得(採点対象外)

出力: analysis_out/dip_filter_result.csv と標準出力の一覧
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from dip_routes import ROUTE_COLUMNS, add_routes, price_features

def _resolve_base() -> Path:
    """stock_memo ディレクトリ(analysis_out を持つ場所)を探す。

    このファイルがどこに置かれても動くようにする。優先順は
    環境変数 STOCK_MEMO_DIR → 自分のいる場所 → 親を遡る。
    """
    env = os.environ.get("STOCK_MEMO_DIR")
    if env and (Path(env) / "analysis_out").is_dir():
        return Path(env).resolve()
    here = Path(__file__).resolve().parent
    for cand in [here, *here.parents]:
        if (cand / "analysis_out").is_dir():
            return cand
    return here


BASE = _resolve_base()
CACHE = BASE / "analysis_out" / "cache"
OHLC = CACHE / "ohlc"
LLM_CACHE = CACHE / "angles_content.json"
OUT_CSV = BASE / "analysis_out" / "dip_filter_result.csv"

# ── 押し目の定義 ──────────────────────────────────────────
DIP_SEARCH_DAYS = 60      # 急騰日から何営業日以内の初タッチを押し目とみなすか
# ── 共通4条件の閾値（旧②は採点対象外） ──────────────────
THEME_WINDOW = 30         # ① 先行件数を数える暦日
THEME_LO, THEME_HI = 3, 5 # ① 該当とする先行件数
ATR_MAX = 0.04            # ③ 14日平均(高値−安値)/終値。True Rangeではない
SPREAD_LO, SPREAD_HI = 0.026, 0.080   # ④ 25日線/75日線の乖離
BELOW_MIN = -0.005        # ⑤ 安値の25日線乖離の下限

COND_NAMES = {"cond1": "①テーマ3-5先行", "cond3": "③14日値幅率<4%",
              "cond4": "④乖離2.6-8%", "cond5": "⑤安値の下抜け<0.5%"}


# ═══════════════════════════════════════════════════════
# Step 1  急騰イベントの取得
# ═══════════════════════════════════════════════════════
def fetch_events() -> pd.DataFrame:
    """Supabase surge_events から急騰イベントを取得する。

    既存の surge_outcome_analysis.fetch_events() が使えるならそれを使う
    (認証・重複除去・kwset 生成・backgrounds_text 生成が入っている)。
    無い環境では下の _fetch_events_rest() で同じものを自前で組み立てる。
    """
    sys.path.insert(0, str(BASE))
    try:
        import surge_outcome_analysis as soa
        ev = soa.fetch_events()
    except Exception as e:
        print(f"surge_outcome_analysis を使えないので直接取得します ({e})")
        ev = _fetch_events_rest()
    ev["stock_code"] = ev["stock_code"].astype(str).str.strip()
    ev["surge_date"] = pd.to_datetime(ev["surge_date"])
    return ev


def _fetch_events_rest() -> pd.DataFrame:
    """Supabase REST から直接引く版。surge_outcome_analysis と同じ整形をする。"""
    import requests
    try:
        from dotenv import load_dotenv
        load_dotenv(BASE / ".env")
    except Exception:
        pass
    url_base = os.environ["SUPABASE_URL"]
    key = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ["SUPABASE_KEY"]
    headers = {"Authorization": f"Bearer {key}", "apikey": key}
    rows, offset, page = [], 0, 1000
    while True:
        url = (f"{url_base}/rest/v1/surge_events"
               f"?select=id,tweet_id,stock_code,stock_name,detected_at,reason,"
               f"surge_price,keywords,business,backgrounds,related_stocks,raw_post"
               f"&order=detected_at.desc&offset={offset}&limit={page}")
        r = requests.get(url, headers=headers, timeout=30)
        r.raise_for_status()
        batch = r.json()
        rows.extend(batch)
        if len(batch) < page:
            break
        offset += page

    df = pd.DataFrame(rows)
    df["detected_at"] = pd.to_datetime(df["detected_at"], utc=True, format="ISO8601")
    df["surge_date"] = df["detected_at"].dt.tz_convert("Asia/Tokyo").dt.date
    df["reason"] = df["reason"].fillna("").astype(str).str.strip()
    df["source"] = df["tweet_id"].astype(str).apply(
        lambda t: "kyutou" if t.startswith("kyutou") else "shodousan")
    df["kwset"] = df["keywords"].apply(
        lambda v: frozenset(str(k).strip() for k in v if str(k).strip())
        if isinstance(v, list) else frozenset())
    df["backgrounds_text"] = df["backgrounds"].apply(
        lambda v: " / ".join(str(x).strip() for x in v if str(x).strip())
        if isinstance(v, list) else "")
    # 同じ銘柄が同じ日に複数回検知されることがあるので1件に潰す
    return df.sort_values(["detected_at", "id"]).drop_duplicates(
        subset=["stock_code", "surge_date"], keep="first").reset_index(drop=True)


# ═══════════════════════════════════════════════════════
# 条件①  テーマ内の先行件数
# ═══════════════════════════════════════════════════════
def add_theme_prior(ev: pd.DataFrame) -> pd.DataFrame:
    """自分の急騰日より前 THEME_WINDOW 日以内に、キーワードを1つ以上共有する
    *別銘柄* が何件急騰していたかを数える。

    同日(delta==0)は急騰時点で順番が確定しないので数えない。既存の
    theme_heat は `0 <= delta <= 14` で同日を含む点が異なる。
    """
    recs = ev[["stock_code", "surge_date", "kwset"]].to_dict("records")
    out = []
    for me in recs:
        if not me["kwset"]:
            out.append(np.nan)
            continue
        cnt = 0
        for other in recs:
            if other["stock_code"] == me["stock_code"]:
                continue
            delta = (me["surge_date"] - other["surge_date"]).days
            if 0 < delta <= THEME_WINDOW and me["kwset"] & other["kwset"]:
                cnt += 1
        out.append(cnt)
    ev = ev.copy()
    ev["theme_prior30"] = out
    return ev


# ═══════════════════════════════════════════════════════
# Step 2  押し目日の特定 + 条件③④⑤(価格から作る)
# ═══════════════════════════════════════════════════════
def load_px(code: str, surge_date=None) -> pd.DataFrame | None:
    f = OHLC / f"{code}.csv"
    def read(path):
        try:
            return pd.read_csv(path, index_col=0, parse_dates=True).sort_index()
        except (OSError, ValueError, pd.errors.ParserError):
            return None
    px = read(f)
    short = px is None or (surge_date is not None and
            int((px.index <= pd.Timestamp(surge_date)).sum()) < 77)
    if short:
        alternate = read(OHLC.parent / "ohlc_all" / f"{code}.csv")
        if alternate is not None:
            if px is None:
                px = alternate
            elif len(alternate) > len(px):
                overlap = px.index.intersection(alternate.index)
                cols = ["Open", "High", "Low", "Close", "Volume"]
                if len(overlap) and np.allclose(px.loc[overlap, cols], alternate.loc[overlap, cols], equal_nan=True):
                    px = alternate
    return px if px is not None and len(px) >= 77 else None


def find_first_dip(ev_row: pd.Series) -> dict | None:
    """1イベントについて、対象条件を満たすかを見たうえで初押しを探し、
    価格系の条件③④⑤を計算して返す。該当しなければ None。

    対象条件: 急騰日の終値が5/25/75日線すべてより上、かつ25日線が上向き
    初押し  : 急騰日の翌営業日以降、安値が初めて25日線にタッチした日
    """
    px = load_px(ev_row["stock_code"], ev_row["surge_date"])
    if px is None:
        return None
    close = px["Close"]
    c = close.values
    low = px["Low"].values
    ma5 = close.rolling(5).mean().values
    ma25 = close.rolling(25).mean().values
    ma75 = close.rolling(75).mean().values
    atr = ((px["High"] - px["Low"]) / close).rolling(14).mean().values
    dates = px.index.date

    # 急騰日の位置。検知日が休場なら最大3日後ろまで探す
    pos = None
    for offset in range(0, 4):
        cand = (ev_row["surge_date"] + timedelta(days=offset)).date()
        hit = (dates == cand)
        if hit.any():
            pos = int(hit.argmax())
            break
    if pos is None or pos < 76 or np.isnan(ma75[pos]):
        return None

    # 対象条件
    above_all = c[pos] > ma5[pos] and c[pos] > ma25[pos] and c[pos] > ma75[pos]
    ma25_rising = pos >= 5 and not np.isnan(ma25[pos - 5]) and ma25[pos] > ma25[pos - 5]
    if not (above_all and ma25_rising):
        return None

    # 初押し = 安値が初めて25日線にタッチした日
    end = min(pos + DIP_SEARCH_DAYS, len(c) - 1)
    t = None
    for i in range(pos + 1, end + 1):
        if not np.isnan(ma25[i]) and low[i] <= ma25[i]:
            t = i
            break
    if t is None:
        return None

    atr_dip = float(atr[t]) if not np.isnan(atr[t]) else np.nan
    spread = float(ma25[t] / ma75[t] - 1) if ma75[t] else np.nan
    below = float(low[t] / ma25[t] - 1)
    close_v = float(c[t] / ma25[t] - 1)
    return {
        "dip_date": pd.Timestamp(px.index[t]),
        "days_to_dip": int(t - pos),
        "atr_pct_dip": round(atr_dip, 4),
        "ma_spread": round(spread, 4),
        "dip_below_ma25": round(below, 4),
        "dip_close_vs_ma25": round(close_v, 4),
        "dip_close": float(c[t]),
        "ma25_at_dip": float(ma25[t]),
        **price_features(px, pos, t),
    }


# ═══════════════════════════════════════════════════════
# 条件②  材料の中身をLLMに読ませる
# ═══════════════════════════════════════════════════════
APPROACH_MAX = 0.04      # 「もうすぐ押し目」とみなす25日線までの距離(安値ベース)
                         # 2026-08-21 に 3%→4% へ緩和(セリア 8/18 が 3.07% で漏れた。
                         # 通知側で①③④の2/3以上＋M/Vを仮判定し、監視として区別する)


def find_approaching(ev_row: pd.Series, max_dist: float = APPROACH_MAX) -> dict | None:
    """まだ25日線にタッチしていないが、最新足で線の近くまで来ている銘柄を拾う。

    押し目済み(find_first_dip)と排他。条件⑤はタッチしていないと決まらないので
    ①③④の3条件だけを最新足で判定する。追加ルートは参考値で、採用前。
    """
    px = load_px(ev_row["stock_code"], ev_row["surge_date"])
    if px is None:
        return None
    close = px["Close"]
    c = close.values
    low = px["Low"].values
    ma5 = close.rolling(5).mean().values
    ma25 = close.rolling(25).mean().values
    ma75 = close.rolling(75).mean().values
    atr = ((px["High"] - px["Low"]) / close).rolling(14).mean().values
    dates = px.index.date

    pos = None
    for offset in range(0, 4):
        cand = (ev_row["surge_date"] + timedelta(days=offset)).date()
        hit = (dates == cand)
        if hit.any():
            pos = int(hit.argmax())
            break
    if pos is None or pos < 76 or np.isnan(ma75[pos]):
        return None

    above_all = c[pos] > ma5[pos] and c[pos] > ma25[pos] and c[pos] > ma75[pos]
    ma25_rising = pos >= 5 and not np.isnan(ma25[pos - 5]) and ma25[pos] > ma25[pos - 5]
    if not (above_all and ma25_rising):
        return None

    # 探索窓がまだ開いていること。閉じていればもう「もうすぐ」ではない
    last = len(c) - 1
    if last - pos > DIP_SEARCH_DAYS:
        return None
    # 一度でもタッチしていたら押し目済みなので対象外
    for i in range(pos + 1, last + 1):
        if not np.isnan(ma25[i]) and low[i] <= ma25[i]:
            return None

    t = last
    if np.isnan(ma25[t]) or np.isnan(ma75[t]) or ma75[t] == 0:
        return None
    dist = float(low[t] / ma25[t] - 1)      # 安値が25日線の何%上にいるか
    if not (0 <= dist <= max_dist):
        return None

    return {
        "as_of": pd.Timestamp(px.index[t]),
        "dist_to_ma25": round(dist, 4),
        "days_since_surge": int(t - pos),
        "atr_pct_dip": round(float(atr[t]), 4) if not np.isnan(atr[t]) else np.nan,
        "ma_spread": round(float(ma25[t] / ma75[t] - 1), 4),
        "dip_below_ma25": np.nan,           # タッチ前なので判定不能
        "dip_close_vs_ma25": np.nan,
        "dip_close": float(c[t]),
        "ma25_at_dip": float(ma25[t]),
        **price_features(px, pos, t),
    }


DIGEST = ["消化済み", "一部残り", "未消化"]
EVID = ["具体事実", "一般論のみ"]
NEXTC = ["近い", "遠い", "不明"]
GENRES = ["ソフトウェアSaaS", "半導体電子部品", "機械産業設備", "建設不動産",
          "商社卸売", "医薬バイオ", "金融", "小売外食サービス", "素材化学",
          "自動車輸送", "エネルギー環境", "情報通信メディア", "その他"]

LLM_SYSTEM = f"""あなたは日本株の急騰材料を読み解く専門家です。
各銘柄について「急騰理由の要約」「事業内容」「強材料ポイント(全文)」を渡します。
**強材料ポイントの中身まで読んで**、次の4項目を判定してください。

A 消化度: {' / '.join(DIGEST)}
  - 消化済み: 材料は既に発表・数値公表され、その内容を織り込めば話が終わるもの
  - 一部残り: 発表済みだが、上振れ余地や次に確認される数値が本文で示唆されているもの
  - 未消化: これから起きる/確認される話(受注の積み上がり、需要拡大の進行中)が中心

B 根拠の質: {' / '.join(EVID)}
  - 具体事実: 固有の受注・契約・製品・数値など検証できる事象が本文にある
  - 一般論のみ: 「需要拡大」「追い風」など業界トレンドの説明に終始している

C 次の確認機会: {' / '.join(NEXTC)}
  - 近い: 1ヶ月以内に次の確認材料(決算・イベント・続報)が来ると本文から読めるもの
  - 遠い: 次の確認が四半期以上先のもの
  - 不明: 本文から読み取れないもの

D 事業ジャンル: {' / '.join(GENRES)}

出力は1行1銘柄、`番号|A|B|C|D` の形式のみ。説明や前置きは書かないでください。"""


def _key(*parts) -> str:
    return hashlib.md5("||".join(str(p) for p in parts).encode()).hexdigest()[:16]


def add_content_labels(df: pd.DataFrame, use_llm: bool = True) -> pd.DataFrame:
    """digest / evid / nextc / genre 列を足す。md5キャッシュ優先で、
    未知の材料だけ Claude Haiku に投げる。"""
    rsn = df["reason"].fillna("").astype(str)
    biz = df["business"].fillna("").astype(str)
    bg = df["backgrounds_text"].fillna("").astype(str)
    keys = [_key(r, b, g) for r, b, g in zip(rsn, biz, bg)]

    cache = {}
    if LLM_CACHE.exists():
        try:
            cache = json.loads(LLM_CACHE.read_text(encoding="utf-8"))
        except Exception:
            cache = {}

    items, seen = [], set()
    for k, r, b, g in zip(keys, rsn, biz, bg):
        if k in cache or k in seen or not g.strip():
            continue
        seen.add(k)
        items.append({"key": k, "reason": r, "business": b, "bg": g})

    if items and use_llm:
        api = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api:
            try:
                from dotenv import load_dotenv
                load_dotenv(BASE / ".env")
                api = os.environ.get("ANTHROPIC_API_KEY", "")
            except Exception:
                pass
        if api:
            print(f"材料の読解: 未キャッシュ {len(items)}件 → LLM")
            cache.update(_call_llm(items, api))
            LLM_CACHE.parent.mkdir(parents=True, exist_ok=True)
            LLM_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1),
                                 encoding="utf-8")
        else:
            print("[WARN] ANTHROPIC_API_KEY が無いので条件②は判定できません")

    blank = {"digest": None, "evid": None, "nextc": None, "genre": None}
    recs = [cache.get(k, blank) for k in keys]
    out = df.copy()
    for col in ("digest", "evid", "nextc", "genre"):
        out[col] = [r.get(col) for r in recs]
    return out


def _call_llm(items: list[dict], api_key: str) -> dict:
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    got, B = {}, 12
    for i in range(0, len(items), B):
        batch = items[i:i + B]
        body = "\n\n".join(
            f"[{n}]\n急騰理由: {it['reason']}\n事業内容: {it['business']}\n"
            f"強材料ポイント: {it['bg']}" for n, it in enumerate(batch, 1))
        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001", max_tokens=1500,
                system=LLM_SYSTEM, messages=[{"role": "user", "content": body}])
            text = resp.content[0].text
        except Exception as e:
            print(f"  [WARN] LLM失敗(このバッチは欠損): {e}")
            continue
        for line in text.strip().splitlines():
            m = re.match(r"\s*\[?(\d+)\]?\s*[|｜]\s*(.+)", line)
            if not m:
                continue
            n = int(m.group(1))
            parts = [p.strip() for p in re.split(r"[|｜]", m.group(2))]
            if not (1 <= n <= len(batch)) or len(parts) < 4:
                continue
            a, b, cc, dd = parts[:4]
            if a in DIGEST and b in EVID and cc in NEXTC and dd in GENRES:
                got[batch[n - 1]["key"]] = {"digest": a, "evid": b,
                                            "nextc": cc, "genre": dd}
        print(f"  材料の読解: {min(i + B, len(items))}/{len(items)}")
        time.sleep(0.4)
    return got


# ═══════════════════════════════════════════════════════
# Step 3  共通4条件と追加ルートの判定
# ═══════════════════════════════════════════════════════
def judge(df: pd.DataFrame, with_cond5: bool = True) -> pd.DataFrame:
    """①③④⑤だけで共通スコアを付け、MACD/出来高を別途判定する。

    with_cond5=False は「まだ25日線にタッチしていない」銘柄向け。
    条件⑤はタッチしないと決まらないので判定から外し、満点を3にする。
    """
    p30 = pd.to_numeric(df["theme_prior30"], errors="coerce")
    atr = pd.to_numeric(df["atr_pct_dip"], errors="coerce")
    spr = pd.to_numeric(df["ma_spread"], errors="coerce")
    blw = pd.to_numeric(df["dip_below_ma25"], errors="coerce")

    out = df.copy()
    out["cond1"] = p30.between(THEME_LO, THEME_HI)
    # 旧②のキャッシュ値は参考として残すが、採点には使わない。
    empty = pd.Series(None, index=df.index, dtype=object)
    out["cond2"] = (df.get("digest", empty) == "一部残り") & (df.get("evid", empty) == "具体事実")
    out["cond3"] = atr < ATR_MAX
    out["cond4"] = spr.between(SPREAD_LO, SPREAD_HI)
    # 終値>=安値なので旧終値>-1.1%はこの安値条件に包含される。
    out["cond5"] = blw > BELOW_MIN
    cols = ["cond1", "cond3", "cond4"] + (["cond5"] if with_cond5 else [])
    for c in ("cond1", "cond2", "cond3", "cond4", "cond5"):
        out[c] = out[c].fillna(False).astype(bool)
    if not with_cond5:
        out["cond5"] = None
    out["score"] = out[cols].sum(axis=1)
    out["満点"] = len(cols)
    out["該当条件"] = [
        " ".join(COND_NAMES[c] for c, v in zip(cols, row) if v) or "-"
        for row in out[cols].values]
    return add_routes(out, dipped=with_cond5)


def build_table(mode: str = "dipped", use_llm: bool = False,
                ev: pd.DataFrame | None = None) -> pd.DataFrame:
    """スコア付きの候補表を返す。他モジュール(Slack通知など)から呼ぶ入口。

    mode="dipped"      … 共通4条件＋MACD/出来高の初押し候補
    mode="approaching" … まだタッチしていない監視対象(①③④の3条件)

    ev を渡すと Supabase の再取得を省ける(両モードを続けて作るとき用)。
    """
    if ev is None:
        ev = add_theme_prior(fetch_events())
    finder = find_first_dip if mode == "dipped" else find_approaching
    keep = ["stock_code", "stock_name", "surge_date", "reason", "business",
            "backgrounds_text", "source", "theme_prior30"]
    rows = []
    for _, r in ev.iterrows():
        d = finder(r)
        if d is not None:
            rows.append({**r[keep].to_dict(), **d})
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    if mode == "dipped":
        df = df.sort_values("surge_date").drop_duplicates(
            subset=["stock_code", "dip_date"], keep="first")
    else:
        # 同じ銘柄が複数の急騰から接近中になることがある。直近の急騰を残す
        df = df.sort_values("surge_date").drop_duplicates(
            subset=["stock_code"], keep="last")
    df = add_content_labels(df, use_llm=use_llm)
    return judge(df, with_cond5=(mode == "dipped"))


# ═══════════════════════════════════════════════════════
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=None, help="押し目日の下限 YYYY-MM-DD")
    ap.add_argument("--min-score", type=int, default=0)
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--with-llm", action="store_true", help="採点対象外の旧②を参考用に取得")
    ap.add_argument("--selected-only", action="store_true", help="共通条件＋(MACDまたは出来高)の採用行のみ")
    ap.add_argument("--mode", choices=["dipped", "approaching"], default="dipped",
                    help="dipped=押し目済み / approaching=もうすぐ押し目")
    args = ap.parse_args()

    if args.selected_only and args.mode != "dipped":
        ap.error("--selected-only は dipped のみ。接近中は買い候補の判定前です")
    df = build_table(mode=args.mode, use_llm=args.with_llm and not args.no_llm)
    if df.empty:
        print("該当する銘柄が1件も見つかりませんでした")
        return
    print(f"{'初押し' if args.mode == 'dipped' else '接近中'} {len(df)}件を検出")

    if args.mode == "approaching":
        cols = ["as_of", "stock_code", "stock_name", "score", "満点", "該当条件",
                "dist_to_ma25", "theme_prior30", "digest", "evid",
                "atr_pct_dip", "ma_spread", "surge_date", "reason"] + ROUTE_COLUMNS
        df = df[df["score"] >= args.min_score].sort_values(
            ["score", "dist_to_ma25"], ascending=[False, True])
        OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
        df[cols].to_csv(OUT_CSV.with_name("dip_filter_approaching.csv"),
                        index=False, encoding="utf-8-sig")
        print(f"\n該当 {len(df)}件 (score>={args.min_score})")
        print(df["score"].value_counts().sort_index(ascending=False).to_string())
        print()
        print(df[["stock_code", "stock_name", "score", "dist_to_ma25", "該当条件"]]
              .head(40).to_string(index=False))
        return

    if args.since:
        df = df[df["dip_date"] >= pd.Timestamp(args.since)]
    if args.selected_only:
        df = df[df["route_selected"]]
    df = df[df["score"] >= args.min_score].sort_values(
        ["score", "dip_date"], ascending=[False, False])

    cols = ["dip_date", "stock_code", "stock_name", "score", "該当条件",
            "theme_prior30", "digest", "evid", "atr_pct_dip", "ma_spread",
            "dip_below_ma25", "dip_close_vs_ma25", "surge_date", "reason"] + ROUTE_COLUMNS
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    df[cols].to_csv(OUT_CSV, index=False, encoding="utf-8-sig")

    print(f"\n該当 {len(df)}件 (score>={args.min_score})")
    print(df["score"].value_counts().sort_index(ascending=False).to_string())
    show = df[["dip_date", "stock_code", "stock_name", "score", "screening_route", "該当条件"]].head(40)
    if len(show):
        print()
        print(show.to_string(index=False))
    print(f"\n→ {OUT_CSV}")


if __name__ == "__main__":
    main()
