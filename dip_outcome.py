"""押し目エントリーの成否判定と、前向き検証台帳
============================================================
「答え合わせ」の定義をこのファイル1つに集約する。定義を変えたいときは
ここだけ触れば dip_rebound_analysis / notify_analysis_slack の双方に効く。

判定定義(2026-07-30 確定):
  エントリー : 25日線タッチ日の *翌営業日の始値*
               (Slack通知が16:30=引け後に飛ぶので、実際に買える最初の価格)
  損切り     : 10営業日以内に -8% に到達したらその時点で「下落」で打ち切り
  それ以外   : 10営業日後の終値がエントリーより上なら「上昇」、下なら「下落」
  上バリア   : 置かない(利確を急がない実運用に合わせる)

損切り到達の判定は2通り両方を記録する:
  intraday … ザラ場の安値が -8% にタッチ (逆指値。主表示に使う)
  close    … 引けで -8% 以下 (終値確認して翌日切る運用)

生の最大上昇/最大下落(mfe/mae)も列に残す。あとで幅や期間を見直したくなっても
全銘柄の株価を再取得せずに再集計できるようにするため。

前向き検証:
  S/A/B のルールは 2026-07-22 に多数の組み合わせを試した中から選ばれている。
  したがって dip_date が FREEZE_DATE 以降のイベントだけが真の out-of-sample。
  validation_since_freeze() がその分だけを集計する。混ぜた数字を「性能」と
  呼ばないための仕組み。
"""

from datetime import date, datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

JST = timezone(timedelta(hours=9))

HORIZON = 10          # 判定期間(営業日)
STOP_PCT = 0.08       # 損切り幅
FREEZE_DATE = date(2026, 7, 22)   # S/A/B ルールを凍結した日

UP, DOWN = "上昇", "下落"
WHY_STOP, WHY_HORIZON = "損切り", "期間満了"

# judge() が返す列。pending(判定不能)のときは outcome 系が None になる。
OUTCOME_COLS = [
    "entry_date", "entry_price",
    "outcome_v2", "why_v2", "ret_v2", "stop_day_v2",
    "outcome_v2c", "why_v2c", "ret_v2c",
    "mfe_10d", "mae_10d",
]


def _empty() -> dict:
    return {c: None for c in OUTCOME_COLS}


def judge(px: pd.DataFrame, touch_i: int) -> dict:
    """px の touch_i 行目(25日線タッチ日)に対する成否を判定する。

    まだ観測期間が足りない(エントリー翌日以降が HORIZON 営業日ぶん無い)場合は
    outcome を None にして返す = 「答え合わせ待ち」。
    """
    e = touch_i + 1                      # エントリー日 = タッチ日の翌営業日
    if e >= len(px):
        return _empty()                  # 翌営業日がまだ来ていない

    op = px["Open"].values
    entry = op[e]
    if not np.isfinite(entry) or entry <= 0:
        return _empty()

    out = _empty()
    out["entry_date"] = px.index[e].date()
    out["entry_price"] = round(float(entry), 2)

    last = e + HORIZON
    if last >= len(px):
        # エントリーはしているが判定日までまだ届いていない。
        # 途中経過(mfe/mae)は分かる範囲だけ埋めておく。
        seg_hi = px["High"].values[e:] / entry - 1
        seg_lo = px["Low"].values[e:] / entry - 1
        if len(seg_hi):
            out["mfe_10d"] = round(float(seg_hi.max()), 4)
            out["mae_10d"] = round(float(seg_lo.min()), 4)
        return out

    hi = px["High"].values[e:last + 1] / entry - 1
    lo = px["Low"].values[e:last + 1] / entry - 1
    cl = px["Close"].values[e:last + 1] / entry - 1
    final = float(cl[-1])

    out["mfe_10d"] = round(float(hi.max()), 4)
    out["mae_10d"] = round(float(lo.min()), 4)

    # 主定義: ザラ場の安値ベース(逆指値)
    hit = np.where(lo <= -STOP_PCT)[0]
    if len(hit):
        out["outcome_v2"] = DOWN
        out["why_v2"] = WHY_STOP
        out["ret_v2"] = -STOP_PCT
        out["stop_day_v2"] = int(hit[0])
    else:
        out["outcome_v2"] = UP if final > 0 else DOWN
        out["why_v2"] = WHY_HORIZON
        out["ret_v2"] = round(final, 4)

    # 副定義: 引けで -8% 以下なら損切り(終値確認)
    hitc = np.where(cl <= -STOP_PCT)[0]
    if len(hitc):
        out["outcome_v2c"] = DOWN
        out["why_v2c"] = WHY_STOP
        out["ret_v2c"] = -STOP_PCT
    else:
        out["outcome_v2c"] = UP if final > 0 else DOWN
        out["why_v2c"] = WHY_HORIZON
        out["ret_v2c"] = round(final, 4)

    return out


# ---------------------------------------------------------------- 重複排除

TIER_PRIORITY = {"S": 0, "A": 1, "B": 2, "-": 3}


def unique_trades(df: pd.DataFrame) -> pd.DataFrame:
    """(銘柄, 押し目日) 1組につき1行に潰す。

    同じ銘柄が複数の急騰日で検知され、そのどれもが同じ25日線タッチ日に
    行き着くことがある(354行 → 実質298機会)。潰さないと同じ1トレードを
    複数回カウントし、検知回数の多い銘柄ほど重く効いてしまう。

    tier が行間で食い違う場合(15組)は良い方を採用する。実際の運用では
    どれか1つの検知でS級と出れば S級として扱うため。
    """
    d = df[df["dip_date"].notna()].copy()
    if d.empty:
        return d
    d["_tp"] = d["tier"].map(TIER_PRIORITY).fillna(9)
    d = d.sort_values(["_tp", "surge_date"])
    d = d.drop_duplicates(subset=["stock_code", "dip_date"], keep="first")
    return d.drop(columns="_tp")


# ---------------------------------------------------------------- 集計

def _stats(g: pd.DataFrame) -> dict:
    up = g["outcome_v2"] == UP
    stop = g["why_v2"] == WHY_STOP
    return {
        "n": len(g),
        "上昇%": round(100 * up.mean(), 1),
        "損切り%": round(100 * stop.mean(), 1),
        "平均リターン%": round(100 * g["ret_v2"].mean(), 2),
    }


def validation_table_v2(df: pd.DataFrame) -> pd.DataFrame:
    """新定義での階層別成績(全期間 = in-sample を含む)。"""
    sub = df.dropna(subset=["outcome_v2"])
    rows = []
    for tier in ("S", "A", "B", "-"):
        g = sub[sub["tier"] == tier]
        if len(g):
            rows.append({"階層": tier, **_stats(g)})
    return pd.DataFrame(rows)


def validation_since_freeze(df: pd.DataFrame) -> pd.DataFrame:
    """ルール凍結日以降に発生した押し目だけの成績 = 真の前向き検証。

    ここが 0 件から積み上がっていくのが正常。in-sample の数字と混ぜないこと。
    """
    sub = df.dropna(subset=["outcome_v2"]).copy()
    if sub.empty:
        return pd.DataFrame()
    sub["dip_date"] = pd.to_datetime(sub["dip_date"]).dt.date
    sub = sub[sub["dip_date"] >= FREEZE_DATE]
    rows = []
    for tier in ("S", "A", "B", "-"):
        g = sub[sub["tier"] == tier]
        if len(g):
            rows.append({"階層": tier, **_stats(g)})
    return pd.DataFrame(rows)


def freeze_summary_line(df: pd.DataFrame) -> str:
    """Slack 1行用。凍結後の成績を S/A/B だけ簡潔に。"""
    vt = validation_since_freeze(df)
    if vt.empty:
        return (f"凍結後({FREEZE_DATE:%Y/%m/%d}以降の押し目)の判定済み: 0件 "
                f"— 前向き検証はまだ何も言えない状態")
    parts = []
    for _, r in vt.iterrows():
        if r["階層"] == "-":
            continue
        parts.append(f"{r['階層']}:{r['上昇%']:.0f}%(n={int(r['n'])})")
    base = vt[vt["階層"] == "-"]
    tail = f" / 対象外 {base.iloc[0]['上昇%']:.0f}%(n={int(base.iloc[0]['n'])})" if len(base) else ""
    return f"凍結後({FREEZE_DATE:%Y/%m/%d}以降)の実績 " + " ".join(parts) + tail


# ---------------------------------------------------------------- 台帳

def ledger_dir(out_dir: Path) -> Path:
    d = out_dir / "ledger"
    d.mkdir(parents=True, exist_ok=True)
    return d


LEDGER_COLS = ["snapshot_date", "tier", "stock_code", "stock_name", "dip_date",
               "dip_depth_vs_peak", "market_cap", "continuation_type",
               "theme_heat", "unresolved", "reason",
               # 予測を出した時点の確率とモデル。あとでモデルを差し替えても
               # 過去に実際に出した予測は書き換わらない(答え合わせの前提)。
               "prob_up", "model_set",
               # 中身を全文読んだ判断も送信時点で凍結する
               "llm_prob", "llm_rank", "llm_note"]


def save_snapshot(live: pd.DataFrame, out_dir: Path) -> Path | None:
    """その日 Slack に出した候補をそのまま日付付きで保存する。

    dip_events.csv は毎回上書きなので、これが無いと「7/28にS級として送ったのは
    どれか」が後から復元できない。市価総額の分位で B 級の線引きが毎回動く以上、
    送った時点のラベルを凍結して残すことが答え合わせの前提になる。
    """
    d = ledger_dir(out_dir)
    stamp = datetime.now(JST).strftime("%Y%m%d")
    path = d / f"{stamp}.csv"
    if live is None or live.empty:
        pd.DataFrame(columns=LEDGER_COLS).to_csv(path, index=False,
                                                 encoding="utf-8-sig")
        return path
    snap = live.copy()
    snap["snapshot_date"] = datetime.now(JST).strftime("%Y-%m-%d")
    cols = [c for c in LEDGER_COLS if c in snap.columns]
    snap[cols].to_csv(path, index=False, encoding="utf-8-sig")
    return path


def load_ledger(out_dir: Path) -> pd.DataFrame:
    """全スナップショットを結合し、銘柄×押し目日ごとに *最初に送った日* の
    tier を採用する(あとから tier が変わっても初回の判断で評価する)。"""
    d = ledger_dir(out_dir)
    files = sorted(d.glob("20*.csv"))
    if not files:
        return pd.DataFrame()
    frames = [pd.read_csv(f) for f in files]
    frames = [f for f in frames if not f.empty]
    if not frames:
        return pd.DataFrame()
    led = pd.concat(frames, ignore_index=True)
    led.columns = [c.lstrip("﻿") for c in led.columns]
    led = led.sort_values("snapshot_date")
    return led.drop_duplicates(subset=["stock_code", "dip_date"], keep="first")


def ledger_scorecard(out_dir: Path, dips: pd.DataFrame) -> pd.DataFrame:
    """台帳(送った時点の tier)に実際の結果を突き合わせる。

    validation_since_freeze() との違い: あちらは「今の分類基準で過去を再判定」、
    こちらは「その日実際に送ったラベル」で評価する。B級は時価総額分位で
    ラベルが揺れるので、本当の答え合わせはこちら。
    """
    led = load_ledger(out_dir)
    if led.empty or dips.empty:
        return pd.DataFrame()
    # 突合相手は1トレード1行にしておく(でないと台帳1行が複数行に膨らむ)
    res = unique_trades(dips).dropna(subset=["outcome_v2"])[
        ["stock_code", "dip_date", "outcome_v2", "why_v2", "ret_v2"]].copy()
    if res.empty:
        return pd.DataFrame()
    res["dip_date"] = pd.to_datetime(res["dip_date"]).dt.strftime("%Y-%m-%d")
    led["dip_date"] = pd.to_datetime(led["dip_date"]).dt.strftime("%Y-%m-%d")
    led["stock_code"] = led["stock_code"].astype(str)
    res["stock_code"] = res["stock_code"].astype(str)
    m = led.merge(res, on=["stock_code", "dip_date"], how="inner")
    if m.empty:
        return pd.DataFrame()
    rows = []
    for tier in ("S", "A", "B"):
        g = m[m["tier"] == tier]
        if len(g):
            rows.append({"階層(送信時)": tier, **_stats(g)})
    return pd.DataFrame(rows)


def prediction_scorecard(out_dir: Path, dips: pd.DataFrame) -> pd.DataFrame:
    """台帳に刻んだ予測確率と実績の突合。予測を出した *当時* のモデルの成績で、
    あとから学習し直した数字ではないので、これが唯一の本物の答え合わせ。
    """
    led = load_ledger(out_dir)
    if led.empty or "prob_up" not in led.columns:
        return pd.DataFrame()
    led = led[led["prob_up"].notna()]
    if led.empty:
        return pd.DataFrame()
    res = unique_trades(dips).dropna(subset=["outcome_v2"])[
        ["stock_code", "dip_date", "outcome_v2", "ret_v2"]].copy()
    if res.empty:
        return pd.DataFrame()
    for f in (led, res):
        f["dip_date"] = pd.to_datetime(f["dip_date"]).dt.strftime("%Y-%m-%d")
        f["stock_code"] = f["stock_code"].astype(str)
    m = led.merge(res, on=["stock_code", "dip_date"], how="inner")
    if m.empty:
        return pd.DataFrame()
    m["up"] = (m["outcome_v2"] == UP).astype(int)
    rows = []
    for mdl, g in m.groupby("model_set"):
        rows.append({
            "予測モデル": mdl, "n": len(g),
            "予測平均%": round(100 * g["prob_up"].mean(), 1),
            "実測上昇率%": round(100 * g["up"].mean(), 1),
            "平均リターン%": round(100 * g["ret_v2"].mean(), 2),
        })
    return pd.DataFrame(rows)
