"""地合い(市場全体の状態)と個別銘柄の相対強弱
============================================================
これを入れる理由は、月ごとの上昇率が地合いでほぼ説明できたため:

  月       上昇率        日経225
  2026-03   0.0%(n=3)   -12.0%
  2026-04  36.0%        +10.3%
  2026-05  39.7%        +11.5%
  2026-06  36.8%         +4.7%
  2026-07  25.6%        -12.3%

日経がマイナスだった2ヶ月が上昇率の最低2つ。これまで見つけた「強材料に定量的な
数字があると+22pt」という偽信号も、実体は7月(日経-12.3%)が悪かっただけだった。
つまり地合いは最大の欠落変数だった可能性が高い。

先読みの防止:
  使うのは押し目日までの指数の動きだけ。保有期間中(押し目日の翌日以降)の
  指数リターンは絶対に使わない。「地合いが続くか」を賭けているのであって
  結果を覗いてはいけない。

統計上の注意(重要):
  地合いは同じ月の全銘柄に同時に効くので、235件のイベントは独立ではない。
  実質的なサンプル数は月数(=5程度)に近い。イベント単位のブートストラップでは
  信頼区間が過小評価されるため、reason_model 側で月ブロック・ブートストラップを
  併記し、昇格判定には保守的な方(月ブロック)を使う。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

INDEXES = {"n225": "^N225", "growth": "2516.T"}
CACHE_SUB = "index"

# 押し目日時点で分かる指数の特徴。列名は idx_{指数}_{項目}
IDX_COLS: list[str] = []
for _k in INDEXES:
    IDX_COLS += [f"idx_{_k}_ret5", f"idx_{_k}_ret20",
                 f"idx_{_k}_above_ma25", f"idx_{_k}_ma25_slope"]
REL_COLS = ["rel_str_n225", "rel_str_growth"]
CONTEXT_COLS = IDX_COLS + REL_COLS


def load_indexes(cache_dir: Path, start: str = "2025-09-01") -> dict[str, pd.DataFrame]:
    """指数の日足を取得。当日取得済みキャッシュがあればそれを使う。"""
    import yfinance as yf
    d = cache_dir / CACHE_SUB
    d.mkdir(parents=True, exist_ok=True)
    out: dict[str, pd.DataFrame] = {}
    for key, ticker in INDEXES.items():
        f = d / f"{key}.csv"
        if f.exists():
            import time
            if (time.time() - f.stat().st_mtime) / 3600 < 6:
                try:
                    df = pd.read_csv(f, index_col=0, parse_dates=True)
                    if len(df) > 60:
                        out[key] = df
                        continue
                except Exception:
                    pass
        try:
            # end は排他的なので翌日を指定して当日の足まで含める
            h = yf.Ticker(ticker).history(
                start=start,
                end=(datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d"),
                auto_adjust=False)
            if h.empty:
                continue
            h.index = pd.to_datetime(h.index).tz_localize(None).normalize()
            h = h[["Open", "High", "Low", "Close", "Volume"]]
            h.to_csv(f)
            out[key] = h
        except Exception as e:
            print(f"  [WARN] 指数 {ticker} の取得に失敗: {e}")
    print(f"地合い指数: {len(out)}件 取得 ({', '.join(out)})")
    return out


def _prep(h: pd.DataFrame) -> pd.DataFrame:
    c = h["Close"]
    return pd.DataFrame({
        "close": c,
        "ma25": c.rolling(25).mean(),
        "ret5": c / c.shift(5) - 1,
        "ret20": c / c.shift(20) - 1,
    }, index=h.index)


def add_context(df: pd.DataFrame, indexes: dict[str, pd.DataFrame]
                ) -> pd.DataFrame:
    """押し目日時点の地合いと、急騰日→押し目日の相対強弱を足す。"""
    out = df.copy()
    for c in CONTEXT_COLS:
        out[c] = np.nan
    if not indexes or "dip_date" not in out.columns:
        return out

    prepped = {k: _prep(h) for k, h in indexes.items()}
    dip = pd.to_datetime(out["dip_date"])
    surge = pd.to_datetime(out["surge_date"]) if "surge_date" in out else None

    for key, p in prepped.items():
        idx = p.index
        # 押し目日「以前」の最終営業日を使う(その日の引けまでの情報)
        pos = idx.searchsorted(dip.values, side="right") - 1
        ok = (pos >= 24) & dip.notna().to_numpy()
        take = np.where(ok, pos, 0)

        close = p["close"].to_numpy()[take]
        ma25 = p["ma25"].to_numpy()[take]
        ret5 = p["ret5"].to_numpy()[take]
        ret20 = p["ret20"].to_numpy()[take]
        # 25日線の傾き: 5営業日前との比較
        prev = np.maximum(take - 5, 0)
        ma25_prev = p["ma25"].to_numpy()[prev]

        out.loc[:, f"idx_{key}_ret5"] = np.where(ok, ret5, np.nan)
        out.loc[:, f"idx_{key}_ret20"] = np.where(ok, ret20, np.nan)
        out.loc[:, f"idx_{key}_above_ma25"] = np.where(
            ok, (close > ma25).astype(float), np.nan)
        out.loc[:, f"idx_{key}_ma25_slope"] = np.where(
            ok, (ma25 / ma25_prev - 1), np.nan)

        # 相対強弱: 急騰日→押し目日の指数リターンを個別銘柄の保持率と比べる。
        # 「地合いのおかげで持ちこたえたのか、地合いに逆らって強いのか」を分ける。
        if surge is not None:
            spos = idx.searchsorted(surge.values, side="right") - 1
            ok2 = ok & (spos >= 0)
            s_take = np.where(ok2, spos, 0)
            idx_move = np.where(ok2, close / p["close"].to_numpy()[s_take] - 1,
                                np.nan)
            depth = pd.to_numeric(out.get("dip_depth_vs_peak"),
                                  errors="coerce").to_numpy()
            out.loc[:, f"rel_str_{key}"] = depth - idx_move
    return out


def regime_table(df: pd.DataFrame) -> pd.DataFrame:
    """地合いの状態別の実績。押し目買いが地合いに依存するかを見る。"""
    if "idx_n225_ret20" not in df.columns:
        return pd.DataFrame()
    d = df.dropna(subset=["outcome_v2", "idx_n225_ret20"]).copy()
    if len(d) < 20:
        return pd.DataFrame()
    d["_up"] = (d["outcome_v2"] == "上昇").astype(float)
    d["地合い"] = pd.cut(
        d["idx_n225_ret20"], [-9, -0.05, 0, 0.05, 9],
        labels=["日経20日-5%超下落", "日経やや下落", "日経やや上昇", "日経20日+5%超上昇"])
    g = d.groupby("地合い", observed=True).agg(
        n=("_up", "size"), 上昇率=("_up", "mean"), 平均R=("ret_v2", "mean"))
    g["上昇率%"] = (100 * g["上昇率"]).round(1)
    g["平均リターン%"] = (100 * g["平均R"]).round(2)
    return g.drop(columns=["上昇率", "平均R"]).reset_index()
