# -*- coding: utf-8 -*-
"""急落検知 日次バッチ

全上場銘柄(JPX内国株)から -10%以上の急落を拾い、
既存の materials.py で当日の一次情報(TDnet開示・株探/Yahooニュース)を取得し、
drop_analyzer.py で構造化して保存する。

急騰側との関係:
  検知    : kyutou_detector(リアルタイム) → こちらは日次バッチ(引け後で十分)
  材料取得: materials.fetch_materials() を **そのまま流用**(方向に依存しないため)
  分析    : analyzer.py(上昇) ↔ drop_analyzer.py(下落)
  保存    : surge_events ↔ drop_events(別テーブル。既存の分析コードを壊さないため)

使い方:
    py drop_detector.py                    # 直近の営業日を対象
    py drop_detector.py --date 2026-07-30  # 日付を指定
    py drop_detector.py --days 30          # 過去30日をまとめて(初期構築用)
    py drop_detector.py --no-llm           # 検知だけ(APIを使わない)
    py drop_detector.py --limit 20         # 1日あたりの分析上限
    py drop_detector.py --no-supabase      # CSVのみ

出力:
    analysis_out/drop_events.csv           # 追記(重複は上書き)
    Supabase drop_events                   # --no-supabase 以外
"""
from __future__ import annotations

import argparse
import io
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

OUT_DIR = BASE / "analysis_out"
CACHE = OUT_DIR / "cache"
UNIV_CSV = CACHE / "jpx_universe.csv"
PX_DIR = CACHE / "ohlc_all"
OUT_CSV = OUT_DIR / "drop_events.csv"
JST = timezone(timedelta(hours=9))

# ── 検知条件 ──────────────────────────────────────────
# 日次リターンの閾値。2026-09-03 に -8% から引き上げた。
# 実データ(7/14-9/2, 385件)で見ると、-8%では「開示もニュースも見つからない」
# 下落が46%を占めるのに対し、-10%では32%まで下がる。一方 -15% まで絞っても
# 開示の特定率は69%で頭打ちになり、件数だけが59件(検知ゼロの日が33日中14日)に
# 落ちて戻り率の検証ができなくなるため、-10% を採用した。
DROP_RET = -0.10
MIN_PRICE = 100           # 低位株の刻み由来のノイズを避ける
MIN_TURNOVER = 5e7        # 前20日平均の売買代金(円)。薄い銘柄を除く
# 出来高倍率は「検知条件」から外した(2026-09-02)。出来高を伴わない急落も
# 後から分析したいため。倍率は落とさず vol_ratio / vol_ratio5 として記録する。
VOL_MULT = 3.0            # ザラ場スキャン(廃止済み intraday_scan)が参照するだけ
UNIV_MAX_AGE_DAYS = 7     # JPXリストの再取得間隔

# 2026-09 に JPX が .xls をやめて .xlsx に差し替えた(旧URLは404)。
# 読むのに openpyxl が要る(xlrd では xlsx を開けない)。
JPX_URL = ("https://www.jpx.co.jp/markets/statistics-equities/misc/"
           "tvdivq0000001vg2-att/data_j.xlsx")
DOMESTIC = ("プライム（内国株式）", "スタンダード（内国株式）", "グロース（内国株式）")


# ═══════════════════════════════════════════ ユニバース
def load_universe(force: bool = False) -> pd.DataFrame:
    """JPXの上場銘柄一覧。内国株のみ。33業種区分をテーマの代理に使う。"""
    CACHE.mkdir(parents=True, exist_ok=True)
    cached, cached_age = None, None
    if UNIV_CSV.exists():
        cached = pd.read_csv(UNIV_CSV, dtype={"code": str})
        cached_age = (time.time() - UNIV_CSV.stat().st_mtime) / 86400
        if not force and cached_age < UNIV_MAX_AGE_DAYS:
            return cached
    print("JPX銘柄一覧を取得中...")
    try:
        r = requests.get(JPX_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=90)
        r.raise_for_status()
        df = pd.read_excel(io.BytesIO(r.content))
        df = df[df["市場・商品区分"].isin(DOMESTIC)].copy()
        u = pd.DataFrame({
            "code": df["コード"].astype(str).str.strip(),
            "name": df["銘柄名"].astype(str).str.strip(),
            "market": df["市場・商品区分"].astype(str),
            "sector33": df["33業種区分"].astype(str),
            "scale": df["規模区分"].astype(str),
        })
    except Exception as e:
        # JPXがURLやファイル形式を変えるとここで毎日落ちて、急落通知が丸ごと
        # 止まる(2026-09-08〜09-14 が実際にそうだった)。銘柄一覧は日単位では
        # ほぼ変わらないので、古くてもキャッシュがあるなら続行する。
        if cached is None:
            raise
        print(f"  [WARN] JPX取得に失敗({type(e).__name__}: {e})")
        print(f"  [WARN] {cached_age:.0f}日前のキャッシュ {len(cached)}銘柄 で続行します")
        return cached
    u.to_csv(UNIV_CSV, index=False, encoding="utf-8-sig")
    print(f"  内国株 {len(u)}銘柄")
    return u


# ═══════════════════════════════════════════ 株価
def sync_prices(codes: list[str], lookback_days: int = 200,
                batch: int = 150, force: bool = False) -> None:
    """yfinanceで日足を取得してCSVキャッシュする。当日取得済みならスキップ。"""
    import yfinance as yf
    PX_DIR.mkdir(parents=True, exist_ok=True)
    need = []
    for c in codes:
        f = PX_DIR / f"{c}.csv"
        if force or not f.exists():
            need.append(c)
        elif (time.time() - f.stat().st_mtime) / 3600 > 12:
            need.append(c)
    if not need:
        print("株価キャッシュは最新です")
        return
    print(f"株価を取得: {len(need)}銘柄")
    start = (datetime.now(JST) - timedelta(days=lookback_days * 2)).strftime("%Y-%m-%d")
    for i in range(0, len(need), batch):
        b = need[i:i + batch]
        syms = [f"{c}.T" for c in b]
        try:
            d = yf.download(syms, start=start, progress=False, auto_adjust=False,
                            group_by="ticker", threads=True)
        except Exception as e:
            print(f"  [WARN] バッチ失敗 {i}: {e}")
            continue
        for c, s in zip(b, syms):
            try:
                x = d[s].dropna(how="all")
            except Exception:
                continue
            if len(x) > 60:
                x.to_csv(PX_DIR / f"{c}.csv")
        print(f"  {min(i + batch, len(need))}/{len(need)}", flush=True)
        time.sleep(1)


# ═══════════════════════════════════════════ 検知
def detect(u: pd.DataFrame, target: pd.Timestamp | None,
           days: int = 1) -> pd.DataFrame:
    """条件を満たす急落を拾う。target が None なら各銘柄の最新営業日。"""
    hits = []
    for _, row in u.iterrows():
        f = PX_DIR / f"{row['code']}.csv"
        if not f.exists():
            continue
        try:
            px = pd.read_csv(f, index_col=0, parse_dates=True).sort_index()
        except Exception:
            continue
        need = {"Open", "High", "Low", "Close", "Volume"}
        if not need.issubset(px.columns) or len(px) < 40:
            continue
        px = px[px["Close"] > 0].dropna(subset=["Open", "High", "Low", "Close"])
        if len(px) < 40:
            continue
        c = px["Close"].values
        o = px["Open"].values
        v = px["Volume"].values
        ret = np.r_[np.nan, c[1:] / c[:-1] - 1]
        vavg = pd.Series(v).rolling(20).mean().shift(1).values
        vavg5 = pd.Series(v).rolling(5).mean().shift(1).values
        turn = pd.Series(c * v).rolling(20).mean().shift(1).values
        idx = px.index
        if target is not None:
            pos = idx.searchsorted(target)
            rng = [pos] if pos < len(idx) and idx[pos].date() == target.date() else []
        else:
            rng = range(max(21, len(idx) - days), len(idx))
        for i in rng:
            if i < 21 or not np.isfinite(ret[i]):
                continue
            if ret[i] > DROP_RET:
                continue
            # 出来高は検知条件にしない(記録だけする)。出来高を伴わない急落も
            # 後から見比べられるようにするため。
            if c[i] < MIN_PRICE or not (turn[i] >= MIN_TURNOVER):
                continue
            gap = float(o[i] / c[i - 1] - 1)
            hits.append({
                "stock_code": row["code"], "stock_name": row["name"],
                "sector33": row["sector33"], "scale": row["scale"],
                "market": row["market"],
                "drop_date": idx[i].strftime("%Y-%m-%d"),
                "detect_mode": "eod",     # 引け後バッチ由来(ザラ場検知と区別する)
                "drop_pct": round(float(ret[i]), 4),
                "gap_pct": round(gap, 4),
                # 下落のうち寄り付きギャップが占める割合。1に近いほど
                # 「引け後の個別ニュース起因」の可能性が高い(米国検証で最も効いた軸)
                "gap_share": round(gap / float(ret[i]), 3) if ret[i] else None,
                # ── 出来高(検知条件ではなく分析用の記録) ──────────
                "volume": int(v[i]),
                "vol20": int(vavg[i]) if np.isfinite(vavg[i]) else None,
                "vol5": int(vavg5[i]) if np.isfinite(vavg5[i]) else None,
                "vol_ratio": (round(float(v[i] / vavg[i]), 2)
                              if np.isfinite(vavg[i]) and vavg[i] > 0 else None),
                "vol_ratio5": (round(float(v[i] / vavg5[i]), 2)
                               if np.isfinite(vavg5[i]) and vavg5[i] > 0 else None),
                "turnover": int(c[i] * v[i]),
                "turnover20": int(turn[i]),
                "close": float(c[i]),
            })
    return pd.DataFrame(hits)


_returns_cache: dict = {}


def _daily_returns(u: pd.DataFrame):
    """全銘柄の日次リターン表と、コード→(業種, 社名) の対応を作る。

    3,700銘柄ぶんのCSVを読むので、プロセス内で1度だけ作って使い回す
    (市場文脈と関連銘柄の両方が同じ表を必要とするため)。
    """
    if _returns_cache:
        return _returns_cache["R"], _returns_cache["sec"], _returns_cache["name"]
    rets = {}
    for _, row in u.iterrows():
        f = PX_DIR / f"{row['code']}.csv"
        if not f.exists():
            continue
        try:
            px = pd.read_csv(f, index_col=0, parse_dates=True).sort_index()
        except Exception:
            continue
        if "Close" not in px.columns or len(px) < 30:
            continue
        s = px["Close"].pct_change()
        s.index = s.index.strftime("%Y-%m-%d")
        rets[row["code"]] = s
    _returns_cache["R"] = pd.DataFrame(rets)
    _returns_cache["sec"] = dict(zip(u["code"], u["sector33"]))
    _returns_cache["name"] = dict(zip(u["code"], u["name"]))
    return _returns_cache["R"], _returns_cache["sec"], _returns_cache["name"]


def add_market_context(df: pd.DataFrame, u: pd.DataFrame) -> pd.DataFrame:
    """その日、市場全体・同業も下げていたか(巻き込まれ急落の判定材料)。

    米国の検証では『ザラ場型 × 市場も下落』が最も戻りやすく(6年中5年プラス)、
    『ザラ場型 × 自分だけ』が最も戻らなかった(6年中1年)。同じ軸を作る。
    """
    if df.empty:
        return df
    days = sorted(df["drop_date"].unique())
    R, sec, _ = _daily_returns(u)
    out = []
    for d in days:
        if d not in R.index:
            out.append((d, np.nan, np.nan))
            continue
        r = R.loc[d].dropna()
        out.append((d, float(r.median()), float((r <= -0.03).mean())))
    ctx = pd.DataFrame(out, columns=["drop_date", "mkt_median", "breadth_dn3"])
    df = df.merge(ctx, on="drop_date", how="left")
    # セクター中央値との差(自分だけ落ちたか)
    secmed = {}
    for d in days:
        if d not in R.index:
            continue
        r = R.loc[d].dropna()
        g = pd.DataFrame({"r": r, "s": [sec.get(c, "?") for c in r.index]})
        secmed[d] = g.groupby("s")["r"].median().to_dict()
    df["sector_median"] = [secmed.get(d, {}).get(s, np.nan)
                           for d, s in zip(df["drop_date"], df["sector33"])]
    df["excess_drop"] = df["drop_pct"] - df["sector_median"]
    return df


# ═══════════════════════════════════════════ 分析
RELATED_MAX = 5


def add_market_cap(df: pd.DataFrame) -> pd.DataFrame:
    """時価総額を付ける。

    急騰側の surge_outcome_analysis.load_market_caps() をそのまま使う。
    analysis_out/cache/market_caps.json のキャッシュを共有し、未取得の
    銘柄だけ yfinance に問い合わせて追記してくれる。
    """
    if df.empty:
        return df
    try:
        import surge_outcome_analysis as soa
        caps = soa.load_market_caps(sorted(set(df["stock_code"].astype(str))))
    except Exception as e:
        print(f"  [WARN] 時価総額の取得に失敗(空のまま続行): {e}")
        return df
    df = df.copy()
    df["market_cap"] = [caps.get(str(c)) for c in df["stock_code"]]
    return df


def add_related_stocks(df: pd.DataFrame, u: pd.DataFrame) -> pd.DataFrame:
    """同じ日に同じ33業種で下げた銘柄を related_stocks に入れる。

    LLM には書かせない。急落側の入力は開示タイトルとニュース見出しだけで、
    そこから関連銘柄を挙げさせるとモデルの内部知識による推測になり、
    「一次情報に無いことは書かない」という drop_analyzer の方針と衝突する。
    ここに入るのは「実際にその日その業種で下げた銘柄」という観測事実だけ。

    母集団は検知済みの急落ではなく**全銘柄**にする。検知(-8%)に限ると
    同業が同時に-8%落ちた日しか埋まらず(実測68%・平均2.1銘柄)、しかも
    「業界ごと売られたのか、自分だけか」という肝心の判断材料にならない。
    """
    if df.empty or "sector33" not in df.columns:
        return df
    R, sec, name = _daily_returns(u)
    rel = []
    for _, r in df.iterrows():
        day, mysec, mycode = r["drop_date"], r["sector33"], r["stock_code"]
        if day not in R.index:
            rel.append("")
            continue
        day_ret = R.loc[day].dropna()
        peers = [(c, v) for c, v in day_ret.items()
                 if c != mycode and sec.get(c) == mysec and v < 0]
        peers.sort(key=lambda x: x[1])
        rel.append(" / ".join(f"{c} {name.get(c, '')}({100*v:.1f}%)"
                              for c, v in peers[:RELATED_MAX]))
    df = df.copy()
    df["related_stocks"] = rel
    return df


def analyze_all(df: pd.DataFrame, limit: int = 40) -> pd.DataFrame:
    """materials.py で一次情報を引き、drop_analyzer に渡す。"""
    import materials
    import drop_analyzer as da
    rows = []
    sub = df.sort_values("drop_pct").head(limit)      # 下落率が大きい順
    for k, (_, r) in enumerate(sub.iterrows(), 1):
        code, day = r["stock_code"], r["drop_date"]
        try:
            mat = materials.fetch_materials(code, day)
        except Exception as e:
            print(f"  [WARN] 材料取得失敗 {code}: {e}")
            mat = {"tdnet": [], "news": [], "tdnet_ok": False, "news_ok": False}
        try:
            prof = materials.fetch_company_profile(code) or ""
        except Exception:
            prof = ""
        if isinstance(prof, dict):
            prof = prof.get("business") or str(prof)
        # vol_ratio は前20日平均が取れないと None になる(出来高を検知条件から
        # 外したので、倍率が計算できない銘柄も検知に乗るようになった)
        try:
            vr = float(r["vol_ratio"])
            if vr != vr:            # NaN
                vr = 0.0
        except (TypeError, ValueError):
            vr = 0.0
        a = da.analyze(code, r["stock_name"], day, float(r["drop_pct"]),
                       vr, mat, str(prof)[:300])
        row = a.to_row()
        row["n_tdnet"] = len(mat.get("tdnet") or [])
        row["n_news"] = len(mat.get("news") or [])
        row["tdnet_ok"] = mat.get("tdnet_ok")
        row["news_ok"] = mat.get("news_ok")
        rows.append(row)
        print(f"  [{k}/{len(sub)}] {code} {r['stock_name'][:12]} "
              f"{100*r['drop_pct']:.1f}% → {a.disclosure or '判定不能'} / {a.reason[:28]}",
              flush=True)
        time.sleep(0.3)
    return pd.DataFrame(rows)


# ═══════════════════════════════════════════ ザラ場スキャン(5分ループ用)
STATE_FILE = OUT_DIR / ".drop_state.json"
LIQUID_TURNOVER = 5e7     # ザラ場スキャンの対象にする売買代金(前20日平均)
SESSION_MIN = 300         # 立会時間 9:00-11:30 + 12:30-15:00 = 300分


def _session_elapsed_frac(now: datetime) -> float:
    """立会時間のうち何割が経過したか。出来高の按分に使う。"""
    t = now.hour * 60 + now.minute
    if t <= 9 * 60:
        return 0.0
    am = min(max(t - 9 * 60, 0), 150)                 # 9:00-11:30
    pm = min(max(t - (12 * 60 + 30), 0), 150)         # 12:30-15:00
    return min((am + pm) / SESSION_MIN, 1.0)


def _load_state() -> set:
    import json
    if not STATE_FILE.exists():
        return set()
    try:
        return set(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    except Exception:
        return set()


def _save_state(done: set) -> None:
    import json
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # 直近5営業日ぶんだけ残す(無限に膨らませない)
    keys = sorted(done)[-5000:]
    STATE_FILE.write_text(json.dumps(keys, ensure_ascii=False), encoding="utf-8")


def liquid_universe(u: pd.DataFrame) -> pd.DataFrame:
    """ザラ場スキャンの対象。売買代金と株価の条件を満たすものだけに絞る。

    全3,700銘柄を5分ごとに引くとYahooに負荷をかけるうえ2分以上かかる。
    そもそも MIN_TURNOVER を満たさない銘柄は検知条件を通らないので、
    先に落としておく(1,880銘柄 ≒ 66秒)。
    """
    rows = []
    for _, r in u.iterrows():
        f = PX_DIR / f"{r['code']}.csv"
        if not f.exists():
            continue
        try:
            px = pd.read_csv(f, index_col=0, parse_dates=True).sort_index()
        except Exception:
            continue
        if "Close" not in px.columns or len(px) < 25:
            continue
        c = px["Close"].values
        v = px["Volume"].values
        turn = float(np.nanmean(c[-20:] * v[-20:]))
        if turn < LIQUID_TURNOVER or c[-1] < MIN_PRICE:
            continue
        rows.append({**r.to_dict(), "prev_close": float(c[-1]),
                     "vol20": float(np.nanmean(v[-20:])), "turnover20": int(turn),
                     "last_date": px.index[-1].strftime("%Y-%m-%d")})
    df = pd.DataFrame(rows)
    if not df.empty:
        # キャッシュの最新日付から10日以上遅れている銘柄は上場廃止・長期売買停止
        # とみなして外す(yfinanceに問い合わせても "possibly delisted" になるだけ)
        cutoff = (pd.Timestamp(df["last_date"].max())
                  - pd.Timedelta(days=10)).strftime("%Y-%m-%d")
        df = df[df["last_date"] >= cutoff].reset_index(drop=True)
    return df


def intraday_scan(liq: pd.DataFrame, batch: int = 200) -> pd.DataFrame:
    """ザラ場中の現値を取り、その場で急落条件を満たす銘柄を返す。

    出来高は立会経過分で按分して比較する(寄り直後に3倍を要求すると
    ほぼ検知できないため)。
    """
    import yfinance as yf
    import logging
    # 廃止済み銘柄が混ざると "possibly delisted" を毎ティック出力してくるので黙らせる
    # (欠損銘柄は下の dropna/empty チェックで元々スキップしている)
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    now = datetime.now(JST)
    frac = max(_session_elapsed_frac(now), 0.15)
    today = now.strftime("%Y-%m-%d")
    hits = []
    codes = liq["code"].tolist()
    info = liq.set_index("code")
    for i in range(0, len(codes), batch):
        b = codes[i:i + batch]
        try:
            d = yf.download([f"{c}.T" for c in b], period="1d", interval="1d",
                            progress=False, auto_adjust=False,
                            group_by="ticker", threads=True)
        except Exception:
            continue
        for c in b:
            try:
                x = d[f"{c}.T"].dropna(how="all")
            except Exception:
                continue
            if x.empty:
                continue
            bar = x.iloc[-1]
            if str(x.index[-1].date()) != today:
                continue                       # 当日足が来ていない
            row = info.loc[c]
            prev = float(row["prev_close"])
            if prev <= 0 or not np.isfinite(bar["Close"]):
                continue
            ret = float(bar["Close"]) / prev - 1
            if ret > DROP_RET:
                continue
            vol = float(bar["Volume"])
            if not (row["vol20"] > 0 and vol >= VOL_MULT * row["vol20"] * frac):
                continue
            gap = float(bar["Open"]) / prev - 1
            hits.append({
                "stock_code": c, "stock_name": row["name"],
                "sector33": row["sector33"], "scale": row["scale"],
                "market": row["market"], "drop_date": today,
                "detect_mode": "intraday",
                "drop_pct": round(ret, 4), "gap_pct": round(gap, 4),
                "gap_share": round(gap / ret, 3) if ret else None,
                "vol_ratio": round(vol / row["vol20"], 2),
                "turnover20": int(row["turnover20"]),
                "close": float(bar["Close"]),
            })
    return pd.DataFrame(hits)


_liq_cache: dict = {}


def watch_tick(limit: int = 8, use_llm: bool = True,
               to_supabase: bool = True, verbose: bool = True,
               notify: bool = True, to_notion: bool = True) -> pd.DataFrame:
    """[廃止] 5分監視ループから呼ぶ入口だったもの。現在どこからも呼ばれない。

    2026-09-02 に引け後1回のバッチ運用へ切り替えたため main.py から外した。
    この経路には既知の不具合がある: 前日終値を PX_DIR のキャッシュから取るが、
    そのキャッシュを更新する sync_prices() は main() 側にしかなく、watch から
    は呼ばれない。結果、古い終値と比較して株式分割を大幅安として拾ってしまう。
    再び使うならキャッシュ更新を入れること。

    新規に条件を満たした急落だけを処理する。

    - 同じ(銘柄,日付)は一度だけ分析する(.drop_state.json で管理)
    - 立会時間外は何もしない
    - 例外は握りつぶす(監視ループ本体を止めないため)
    """
    try:
        now = datetime.now(JST)
        if now.weekday() >= 5 or not (9 <= now.hour < 16):
            return pd.DataFrame()

        today = now.strftime("%Y-%m-%d")
        if _liq_cache.get("date") != today:
            u = load_universe()
            _liq_cache["date"] = today
            _liq_cache["liq"] = liquid_universe(u)
            _liq_cache["univ"] = u
            if verbose:
                print(f"  [drop] ザラ場対象 {len(_liq_cache['liq'])}銘柄")
        liq = _liq_cache["liq"]
        if liq.empty:
            return pd.DataFrame()

        hits = intraday_scan(liq)
        if hits.empty:
            return pd.DataFrame()

        done = _load_state()
        hits["_k"] = hits["stock_code"] + "|" + hits["drop_date"]
        fresh = hits[~hits["_k"].isin(done)].drop(columns="_k")
        if fresh.empty:
            return pd.DataFrame()

        fresh = fresh.sort_values("drop_pct").head(limit)
        if verbose:
            print(f"  [drop] 新規の急落 {len(fresh)}件")
            for _, r in fresh.iterrows():
                print(f"    {r['stock_code']} {str(r['stock_name'])[:14]} "
                      f"{100*r['drop_pct']:.1f}% (出来高{r['vol_ratio']:.1f}倍)")

        out = fresh
        if use_llm:
            res = analyze_all(fresh, limit=len(fresh))
            if not res.empty:
                out = fresh.merge(res, on=["stock_code", "drop_date"],
                                  how="left", suffixes=("", "_a"))
        merge_csv(out)
        if to_supabase:
            save_supabase(out)
        done |= set(out["stock_code"] + "|" + out["drop_date"])
        _save_state(done)
        if to_notion:
            from notion_drop_storage import save_drops_to_notion
            save_drops_to_notion(out, verbose=verbose)
        if notify:
            from drop_notifier import notify_drops
            notify_drops(out, verbose=verbose)
        return out
    except Exception as e:
        print(f"  [drop][WARN] スキャンに失敗(監視は継続): {e}")
        return pd.DataFrame()


# ═══════════════════════════════════════════ 保存

# drop_events.sql が定義する列。ここに無い列は Supabase に送らない。
SUPABASE_COLUMNS = {
    "stock_code", "stock_name", "drop_date", "detect_mode", "drop_pct", "close",
    "volume", "vol20", "vol5", "vol_ratio", "vol_ratio5", "turnover", "turnover20",
    "gap_pct", "gap_share", "breadth_dn3", "mkt_median", "sector_median",
    "excess_drop", "sector33", "scale", "market", "market_cap",
    "reason", "disclosure", "evidence", "nature", "scope", "priced",
    "next_catalyst", "note", "backgrounds", "related_stocks",
    "n_tdnet", "n_news", "tdnet_ok", "news_ok",
    "fwd_ret_5d", "fwd_ret_10d", "fwd_ret_20d", "recovery_20d", "outcome_20d",
}


# 整数で送らないと Postgres が受け付けない列。
INT_COLUMNS = {"volume", "vol20", "vol5", "turnover", "turnover20",
               "n_tdnet", "n_news"}


def save_supabase(df: pd.DataFrame) -> None:
    from dotenv import load_dotenv
    load_dotenv(BASE / ".env")
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_KEY")
    if not (url and key):
        print("[WARN] Supabaseの認証情報が無いのでCSVのみ保存しました")
        return
    hdr = {"Authorization": f"Bearer {key}", "apikey": key,
           "Content-Type": "application/json",
           "Prefer": "resolution=merge-duplicates,return=minimal"}
    # drop_events に無い列(raw_output や merge で付く stock_name_a など)を
    # そのまま送ると PostgREST が 400 を返すので、テーブルの列だけに絞る。
    cols = [c for c in df.columns if c in SUPABASE_COLUMNS]
    out = df[cols].copy()

    # 整数列を int に戻す。欠損が1つでもあると pandas が float64 にするため、
    # CSV を経由したり analyze_all の結果を merge した後は 5 が 5.0 になり、
    # Postgres の integer/bigint 列が "invalid input syntax" で 400 を返す。
    for c in INT_COLUMNS & set(out.columns):
        out[c] = pd.to_numeric(out[c], errors="coerce").round().astype("Int64")

    recs = out.astype(object).where(pd.notna(out), None).to_dict("records")
    for i in range(0, len(recs), 100):
        # on_conflict を付けないと upsert にならない。drop_events の一意制約は
        # 主キー(id)ではなく (stock_code, drop_date) なので、PostgREST には
        # 衝突対象を明示する必要がある(無いと再実行のたびに 409 になる)。
        r = requests.post(
            f"{url}/rest/v1/drop_events?on_conflict=stock_code,drop_date",
            headers=hdr, json=recs[i:i + 100], timeout=60)
        if r.status_code >= 300:
            print(f"[WARN] Supabase保存に失敗 ({r.status_code}): {r.text[:300]}")
            print("       drop_events テーブルは drop_events.sql で作成してください")
            return
    print(f"[OK] Supabase drop_events に {len(recs)}件 upsert")


def merge_csv(df: pd.DataFrame) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if OUT_CSV.exists():
        old = pd.read_csv(OUT_CSV, dtype={"stock_code": str})
        df = pd.concat([old, df], ignore_index=True)
    df = df.drop_duplicates(subset=["stock_code", "drop_date"], keep="last")
    df.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    print(f"[OK] {OUT_CSV}  計{len(df)}件")


# ═══════════════════════════════════════════
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="対象日 YYYY-MM-DD")
    ap.add_argument("--days", type=int, default=1, help="直近何営業日ぶんを見るか")
    ap.add_argument("--limit", type=int, default=40, help="1回に分析する上限")
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--no-supabase", action="store_true")
    ap.add_argument("--no-sync", action="store_true", help="株価取得をスキップ")
    ap.add_argument("--notion", action="store_true",
                    help="検知結果をNotionの「毎日急落分析DB」にも保存する")
    ap.add_argument("--slack", action="store_true",
                    help="検知結果をSlackに通知する(既定オフ。--days で過去分を回すと連投になるため)")
    ap.add_argument("--refresh-universe", action="store_true")
    args = ap.parse_args()

    u = load_universe(force=args.refresh_universe)
    if not args.no_sync:
        sync_prices(u["code"].tolist())

    target = pd.Timestamp(args.date) if args.date else None
    hits = detect(u, target, days=args.days)
    if hits.empty:
        print("条件を満たす急落はありませんでした")
        return
    hits = add_market_context(hits, u)
    hits = add_related_stocks(hits, u)
    hits = add_market_cap(hits)
    hits = hits.sort_values("drop_pct")
    print(f"\n急落検知 {len(hits)}件")
    show = hits.head(30)[["drop_date", "stock_code", "stock_name", "drop_pct",
                          "gap_share", "vol_ratio", "breadth_dn3"]]
    print(show.to_string(index=False))

    if args.no_llm:
        merge_csv(hits)
        if args.slack:
            from drop_notifier import notify_drops
            notify_drops(hits)
        return

    print(f"\n一次情報の取得と分析({min(args.limit, len(hits))}件)")
    res = analyze_all(hits, limit=args.limit)
    merged = hits.merge(res, on=["stock_code", "drop_date"], how="left",
                        suffixes=("", "_a"))
    merge_csv(merged)
    if not args.no_supabase:
        save_supabase(merged)
    if args.notion:
        from notion_drop_storage import save_drops_to_notion
        save_drops_to_notion(merged)
    if args.slack:
        from drop_notifier import notify_drops
        notify_drops(merged)

    got = merged[merged["disclosure"].notna() & (merged["disclosure"] != "")]
    if len(got):
        print("\n開示種別の内訳")
        print(got["disclosure"].value_counts().to_string())


if __name__ == "__main__":
    main()
