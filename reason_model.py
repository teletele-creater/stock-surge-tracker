"""急騰理由から押し目の成否を予測し、答え合わせで基準を更新していく仕組み
============================================================
狙いは「当てるモデルを作る」ことではなく、**予測基準が本当にベースを超えて
いるかを自己欺瞞なしに測り続ける**こと。235件のデータに毎日ルールを引き直せば
毎日「今のデータに一番合う基準」が出てくるが、それは 2026-07-22 に S/A/B の
条件を選んだときと同じ過学習でしかない。

そうならないための3つの規律:

1. ウォークフォワード
   ある押し目の予測には、その押し目日より前に *結果が確定していた* データだけで
   学習したモデルを使う。walk_forward() が1件ずつ学習をやり直して実現する。
   したがって出てくるスコアは全て out-of-sample。

2. 予測を先に刻む
   予測確率を台帳(analysis_out/ledger/)に保存してから答え合わせする。あとで
   モデルを差し替えても、過去に実際に出した予測は書き換わらない。

3. チャンピオン / チャレンジャー
   複数の特徴セットを常に並走させ、ウォークフォワード成績で比較する。
   乗り換えはブートストラップ信頼区間が重ならないときだけ(promote 判定)。
   「今日いちばん数字が良かった候補」に毎日飛び移るのを防ぐ。

現状(2026-07-30, 判定済み235件)の測定結果:
  理由系の特徴(category / continuation_type / unresolved / followup_score /
  theme_heat)はどれも上昇率の差がノイズ幅(±6.7pt、有意には約13pt必要)を
  超えない。信号らしきものは値動き・規模側(押し目の深さ・時価総額)にある。
  よって当面 reason セットがベースに勝てなくても、それが正常な観測結果。
  n が積み上がるまで判定を保留するための仕組みがこのファイル。
"""

from __future__ import annotations

import json
import warnings
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=FutureWarning)

MIN_TRAIN = 60          # これ未満の学習データでは予測を出さない(burn-in)
N_BOOTSTRAP = 2000      # 信頼区間の反復数
RANDOM_SEED = 0

# ---------------------------------------------------------------- 特徴量

# 急騰理由から作られる特徴。ここに信号があるかを検証したい本命。
REASON_NUM = ["followup_score", "theme_heat"]
REASON_CAT = ["category", "continuation_type", "unresolved", "source"]

# 値動き・規模の特徴。理由系と比較するための対照。
# market_cap は「現在の」時価総額なので、急騰して伸びた銘柄は高く出る
# 弱い先読みを含む。単独で強く出たら額面どおり受け取らないこと。
PRICE_NUM = ["dip_depth_vs_peak", "retention_at_dip", "days_to_dip",
             "dip_vol_vs_normal", "dip_vol_vs_surge", "surge_ret",
             "market_cap_log"]

# 絶対に特徴にしてはいけない列(結果そのもの、または結果由来)。
LEAKY = {"outcome", "outcome_v2", "outcome_v2c", "why_v2", "why_v2c",
         "ret_v2", "ret_v2c", "ret_5d_from_dip", "stop_day_v2",
         "mfe_10d", "mae_10d", "entry_price", "entry_date", "tier"}

try:
    from reason_axes import AXIS_COLS, MAT_COLS
except Exception:                      # 軸分解モジュールが無くても動くように
    AXIS_COLS, MAT_COLS = [], []

# 強材料ポイント(backgrounds)由来。reason の要約では完全に落ちていた情報。
# post_vol_ratio / post_turnover_oku は自前検知(kyutou)にしか無く 68/235 の
# 充填率しかないため、中央値補完が7割を占めてしまうので既定では入れない。
MATERIAL_NUM = list(MAT_COLS) + ["len_backgrounds", "n_keywords",
                                 "n_related_real", "len_business"]
MATERIAL_MIN = ["mat_n_num", "mat_n_hedge"]

# 出来高・値動きの形(押し目日までの情報のみ)
SHAPE_NUM = ["vol_decay", "vol_trend5", "down_day_ratio", "atr_pct",
             "days_from_peak"]

try:
    from market_context import IDX_COLS, REL_COLS
except Exception:
    IDX_COLS, REL_COLS = [], []

FEATURE_SETS: dict[str, dict] = {
    # 常にベース率だけを返す対照。これに勝てないなら予測に価値は無い。
    "baseline": {"num": [], "cat": []},
    # 最小構成。特徴を盛るほど n=235 では過学習するので、仮説に一番有利な
    # 「少数の筋の通った特徴だけ」の形も必ず並走させる。
    "reason_min": {"num": [], "cat": ["continuation_type", "unresolved"]},
    "price_min": {"num": ["dip_depth_vs_peak", "market_cap_log"], "cat": []},
    # 期間安定性の検査で、押し目の深さだけが前半+15pt/後半+15ptと符号も大きさも
    # 一致した(時価総額は+26pt→-0ptで反転)。単独で試す価値がある唯一の候補。
    "depth_only": {"num": ["dip_depth_vs_peak"], "cat": []},
    "reason": {"num": REASON_NUM, "cat": REASON_CAT},
    "price": {"num": PRICE_NUM, "cat": []},
    "combined": {"num": REASON_NUM + PRICE_NUM, "cat": REASON_CAT},
    # 細分化した理由の軸。one-hot は 23列前後になり n=235 では重いので、
    # 縮小付きターゲットエンコーディングで5列に潰した版を本命として並走させる。
    "axes_onehot": {"num": [], "cat": AXIS_COLS},
    "axes_te": {"num": [], "cat": [], "te": AXIS_COLS},
    "axes_te_price": {"num": ["dip_depth_vs_peak", "market_cap_log"],
                      "cat": [], "te": AXIS_COLS},
    # 強材料ポイント由来。material_min の2特徴(数字の個数・推測語の個数)が
    # 現時点で理由データから見つかった最有力の候補。
    "material": {"num": MATERIAL_NUM, "cat": []},
    "material_min": {"num": MATERIAL_MIN, "cat": []},
    "material_price": {"num": MATERIAL_MIN + ["dip_depth_vs_peak",
                                              "market_cap_log"], "cat": []},
    "material_axes": {"num": MATERIAL_MIN, "cat": [], "te": AXIS_COLS},
    # 中身の全文読み込み判断。生確率は evaluate_fixed で当てはめ無しに評価するが、
    # 校正がずれている場合に1パラメータで直せるかも見る。
    "llm_only": {"num": ["llm_prob"], "cat": []},
    "llm_depth": {"num": ["llm_prob", "dip_depth_vs_peak"], "cat": []},
    # 地合い。月ごとの上昇率が日経の方向でほぼ説明できたので本命候補。
    # ただし同月の全銘柄に同時に効くため、昇格判定は月ブロックCIで行う。
    "regime_min": {"num": ["idx_n225_ret20", "idx_n225_above_ma25"], "cat": []},
    "regime": {"num": IDX_COLS, "cat": []},
    "depth_regime": {"num": ["dip_depth_vs_peak", "idx_n225_ret20"], "cat": []},
    "depth_regime_rel": {"num": ["dip_depth_vs_peak", "idx_n225_ret20",
                                 "rel_str_n225"], "cat": []},
    # 出来高・値動きの形
    "shape": {"num": SHAPE_NUM, "cat": []},
    "depth_shape": {"num": ["dip_depth_vs_peak", "vol_decay", "atr_pct"],
                    "cat": []},
    "depth_regime_shape": {"num": ["dip_depth_vs_peak", "idx_n225_ret20",
                                   "vol_decay", "atr_pct"], "cat": []},
    # 個別特徴の安定性検査で生き残った3つ。日中変動幅は36pt幅・完全単調・
    # 前半後半とも同符号で最強。ただし半分は -8%損切りの機械的帰結
    # (荒い銘柄は10営業日内に-8%へ触れやすい)なので、意味は「材料の良さ」では
    # なく「この損切り幅を生き残れるか」。
    "atr_only": {"num": ["atr_pct"], "cat": []},
    "atr_depth": {"num": ["atr_pct", "dip_depth_vs_peak"], "cat": []},
    "atr_depth_vol": {"num": ["atr_pct", "dip_depth_vs_peak", "vol_trend5"],
                      "cat": []},
}
DEFAULT_CHAMPION = "baseline"


def prepare(df: pd.DataFrame) -> pd.DataFrame:
    """特徴量の前処理。dip_date を日付にし、派生列を足す。"""
    d = df.copy()
    d["dip_date"] = pd.to_datetime(d["dip_date"])
    cap = pd.to_numeric(d.get("market_cap"), errors="coerce")
    d["market_cap_log"] = np.log10(cap.where(cap > 0))
    d["unresolved"] = d["unresolved"].astype(str)
    for c in list(REASON_CAT) + list(AXIS_COLS):
        if c in d.columns:
            d[c] = d[c].fillna("欠損").astype(str)
    return d


TE_SMOOTH = 20.0   # 縮小の強さ。n件の水準は n/(n+20) の重みしか信用しない


def _target_encode(train: pd.DataFrame, test: pd.DataFrame, col: str):
    """縮小付きターゲットエンコーディング。

    水準ごとの上昇率を、件数が少ないほどベース率に引き戻して1列の数値にする。
      enc = (上昇数 + m*ベース率) / (件数 + m)
    これで「細分化された水準」を、水準数ぶんの列ではなく1列で扱える。
    n=5 の水準が全勝しても enc は 1.0 ではなく 0.55 付近に留まるので暗記が効かない。

    学習データだけで作ることが絶対条件(test の答えを混ぜたら検証が壊れる)。
    """
    y = (train["outcome_v2"] == "上昇").astype(float)
    base = float(y.mean())
    agg = y.groupby(train[col].astype(str)).agg(["sum", "count"])
    enc = (agg["sum"] + TE_SMOOTH * base) / (agg["count"] + TE_SMOOTH)
    tr = train[col].astype(str).map(enc).fillna(base)
    te = test[col].astype(str).map(enc).fillna(base)
    return tr, te


def _design(train: pd.DataFrame, test: pd.DataFrame, spec: dict):
    """学習用/予測用の設計行列を作る。カテゴリは学習側の水準で one-hot 化し、
    予測側に未知の水準が来たら全0(=基準水準扱い)にする。"""
    num, cat, te_cols = spec["num"], spec["cat"], spec.get("te", [])
    if not num and not cat and not te_cols:
        return np.zeros((len(train), 0)), np.zeros((len(test), 0))

    blocks_tr, blocks_te = [], []
    for c in num:
        if c not in train.columns:
            continue
        tr = pd.to_numeric(train[c], errors="coerce")
        te = pd.to_numeric(test[c], errors="coerce")
        med = tr.median()
        if not np.isfinite(med):
            continue
        # 中央値補完 + 学習側の分位でクリップ(外れ値に引きずられないように)
        lo, hi = tr.quantile(0.02), tr.quantile(0.98)
        tr = tr.fillna(med).clip(lo, hi)
        te = te.fillna(med).clip(lo, hi)
        sd = tr.std()
        if not np.isfinite(sd) or sd == 0:
            continue
        blocks_tr.append(((tr - tr.mean()) / sd).to_numpy()[:, None])
        blocks_te.append(((te - tr.mean()) / sd).to_numpy()[:, None])

    for c in cat:
        if c not in train.columns:
            continue
        # 学習データに2件以上ある水準のみ採用(1件しかない水準は暗記になる)
        vc = train[c].value_counts()
        levels = [v for v in vc[vc >= 2].index][1:]   # 最頻水準を基準にして落とす
        for lv in levels:
            blocks_tr.append((train[c] == lv).to_numpy()[:, None].astype(float))
            blocks_te.append((test[c] == lv).to_numpy()[:, None].astype(float))

    for c in te_cols:
        if c not in train.columns:
            continue
        tr, te = _target_encode(train, test, c)
        sd = tr.std()
        if not np.isfinite(sd) or sd == 0:
            continue
        mu = tr.mean()
        blocks_tr.append(((tr - mu) / sd).to_numpy()[:, None])
        blocks_te.append(((te - mu) / sd).to_numpy()[:, None])

    if not blocks_tr:
        return np.zeros((len(train), 0)), np.zeros((len(test), 0))
    return np.hstack(blocks_tr), np.hstack(blocks_te)


def _fit_predict(train: pd.DataFrame, test: pd.DataFrame, spec: dict) -> np.ndarray:
    """学習して test の上昇確率を返す。特徴が無ければ学習期間のベース率。"""
    y = (train["outcome_v2"] == "上昇").to_numpy().astype(int)
    base = float(y.mean()) if len(y) else 0.5
    Xtr, Xte = _design(train, test, spec)
    if Xtr.shape[1] == 0 or len(np.unique(y)) < 2:
        return np.full(len(test), base)

    from sklearn.linear_model import LogisticRegression
    # C=0.1 の強い L2。n が小さいので容量を絞る側に倒す。
    m = LogisticRegression(C=0.1, max_iter=2000, solver="lbfgs")
    try:
        m.fit(Xtr, y)
    except Exception:
        return np.full(len(test), base)
    return m.predict_proba(Xte)[:, 1]


# ---------------------------------------------------------------- 検証

_WF_CACHE: dict[tuple, pd.DataFrame] = {}


def walk_forward(df: pd.DataFrame, set_name: str,
                 min_train: int = MIN_TRAIN) -> pd.DataFrame:
    """1件ずつ、その押し目日より前に結果が確定していたデータだけで学習して予測。

    「結果が確定していた」の判定には判定日(entry_date + HORIZON営業日)ではなく
    押し目日 + 余裕日数を使う近似を取る。厳密な判定日を持たない行があるため。

    1回の実行で compare_sets とレポートのキャリブレーションが同じ組み合わせを
    要求するので、プロセス内でメモ化する(日次バッチの実行時間を伸ばさないため)。
    """
    import dip_outcome
    ck = (set_name, min_train, len(df), str(df["dip_date"].max()),
          int(df["outcome_v2"].notna().sum()))
    if ck in _WF_CACHE:
        return _WF_CACHE[ck]
    spec = FEATURE_SETS[set_name]
    d = prepare(df).dropna(subset=["outcome_v2", "dip_date"])
    d = d.sort_values("dip_date").reset_index(drop=True)
    # 結果が判明する日 ≒ 押し目日 + (判定期間 + 数日の暦ずれ)
    resolved_at = d["dip_date"] + pd.Timedelta(days=dip_outcome.HORIZON * 7 / 5 + 3)

    rows = []
    for i in range(len(d)):
        asof = d.loc[i, "dip_date"]
        # asof 時点で既に答えが出ていた事例のみ
        mask = (resolved_at < asof)
        train = d[mask]
        if len(train) < min_train:
            continue
        p = _fit_predict(train, d.iloc[[i]], spec)[0]
        rows.append({
            "stock_code": d.loc[i, "stock_code"],
            "dip_date": asof.date(),
            "set": set_name,
            "n_train": len(train),
            "prob_up": round(float(p), 4),
            "actual_up": int(d.loc[i, "outcome_v2"] == "上昇"),
            "ret_v2": d.loc[i, "ret_v2"],
            "tier": d.loc[i, "tier"],
        })
    out = pd.DataFrame(rows)
    _WF_CACHE[ck] = out
    return out


def _brier(p, y):
    return float(np.mean((p - y) ** 2))


def _logloss(p, y):
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def _auc(p, y):
    y = np.asarray(y)
    if len(np.unique(y)) < 2:
        return np.nan
    order = np.argsort(p)
    ranks = np.empty(len(p), float)
    ranks[order] = np.arange(1, len(p) + 1)
    n1, n0 = y.sum(), len(y) - y.sum()
    return float((ranks[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def score(wf: pd.DataFrame) -> dict:
    """ウォークフォワード予測の成績。skill は「常にベース率」に対する改善率。"""
    if wf.empty:
        return {}
    p = wf["prob_up"].to_numpy()
    y = wf["actual_up"].to_numpy()
    base = float(y.mean())
    b_model = _brier(p, y)
    b_base = _brier(np.full(len(y), base), y)
    return {
        "n": len(wf),
        "実測上昇率%": round(100 * base, 1),
        "Brier": round(b_model, 4),
        "Brier(ベース)": round(b_base, 4),
        "skill%": round(100 * (1 - b_model / b_base), 1) if b_base > 0 else 0.0,
        "logloss": round(_logloss(p, y), 4),
        "AUC": round(_auc(p, y), 3),
    }


MIN_BLOCKS = 4          # これ未満のブロック数では区間を出さない


def n_blocks(wf: pd.DataFrame, freq: str = "W") -> int:
    if wf.empty or "dip_date" not in wf.columns:
        return 0
    return pd.to_datetime(wf["dip_date"]).dt.to_period(freq).nunique()


def skill_ci(wf: pd.DataFrame, n_boot: int = N_BOOTSTRAP,
             block: bool = False, freq: str = "W") -> tuple[float, float]:
    """skill% のブートストラップ95%信頼区間。0 を挟んでいたら「ベースに勝てて
    いるとは言えない」= 乗り換えてはいけない。

    block=True で月ブロック・ブートストラップ(イベントではなく月を復元抽出)。
    地合い系の特徴は同じ月の全銘柄に同時に効くのでイベントは独立でなく、
    実質サンプル数は月数に近い。イベント単位だと信頼区間が過小評価されるため、
    昇格判定にはこちらの保守的な区間を使う。
    """
    if wf.empty or len(wf) < 20:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(RANDOM_SEED)
    p = wf["prob_up"].to_numpy()
    y = wf["actual_up"].to_numpy()

    groups = None
    if block:
        if "dip_date" not in wf.columns:
            return (float("nan"), float("nan"))
        # 週ブロック。月にすると評価対象が2ヶ月しか無くブロック数2で成立しない。
        # 保有期間が10営業日=約2週なので週ブロックでも隣接ブロックは相関するが、
        # イベント単位より遥かに実態に近い。
        per = pd.to_datetime(wf["dip_date"]).dt.to_period(freq).astype(str)
        groups = [np.where(per == m)[0] for m in per.unique()]
        if len(groups) < MIN_BLOCKS:
            return (float("nan"), float("nan"))

    out = []
    for _ in range(n_boot):
        if block:
            pick = rng.integers(0, len(groups), len(groups))
            idx = np.concatenate([groups[k] for k in pick])
        else:
            idx = rng.integers(0, len(y), len(y))
        ys, ps = y[idx], p[idx]
        if len(np.unique(ys)) < 2:
            continue
        bb = _brier(np.full(len(ys), ys.mean()), ys)
        if bb <= 0:
            continue
        out.append(100 * (1 - _brier(ps, ys) / bb))
    if not out:
        return (float("nan"), float("nan"))
    return (round(float(np.percentile(out, 2.5)), 1),
            round(float(np.percentile(out, 97.5)), 1))


def calibration(wf: pd.DataFrame, bins: int = 4) -> pd.DataFrame:
    """予測確率の帯ごとに、実際の上昇率がその帯に収まっているか。
    予測が高いほど実測も高くなっていなければ、順位付けとして機能していない。"""
    if wf.empty or len(wf) < bins * 5:
        return pd.DataFrame()
    d = wf.copy()
    try:
        d["帯"] = pd.qcut(d["prob_up"], bins, duplicates="drop")
    except ValueError:
        return pd.DataFrame()
    g = d.groupby("帯", observed=True).agg(
        n=("actual_up", "size"),
        予測平均=("prob_up", "mean"),
        実測上昇率=("actual_up", "mean"),
        平均リターン=("ret_v2", "mean"))
    g["予測平均"] = (100 * g["予測平均"]).round(1)
    g["実測上昇率"] = (100 * g["実測上昇率"]).round(1)
    g["平均リターン"] = (100 * g["平均リターン"]).round(2)
    return g.reset_index().rename(columns={"予測平均": "予測平均%",
                                           "実測上昇率": "実測上昇率%",
                                           "平均リターン": "平均リターン%"})


def evaluate_fixed(df: pd.DataFrame, prob_col: str = "llm_prob") -> pd.DataFrame:
    """当てはめを伴わない予測器(LLMの中身判断)を評価する。

    LLM は結果データを一切見ていないので、学習/検証の分割が要らない。
    判定済み全件がそのまま out-of-sample になる。ウォークフォワードの候補が
    126件しか評価できないのに対し、こちらは235件で評価できる。
    """
    if prob_col not in df.columns:
        return pd.DataFrame()
    d = df.dropna(subset=["outcome_v2", prob_col])
    if len(d) < 30:
        return pd.DataFrame()
    wf = pd.DataFrame({
        "prob_up": d[prob_col].astype(float).to_numpy(),
        "actual_up": (d["outcome_v2"] == "上昇").astype(int).to_numpy(),
        "ret_v2": d["ret_v2"].to_numpy(),
    })
    s = score(wf)
    lo, hi = skill_ci(wf)
    return pd.DataFrame([{"予測器": prob_col, **s,
                          "skill95%下限": lo, "skill95%上限": hi}])


def fixed_calibration(df: pd.DataFrame, prob_col: str = "llm_prob",
                      bins: int = 4) -> pd.DataFrame:
    if prob_col not in df.columns:
        return pd.DataFrame()
    d = df.dropna(subset=["outcome_v2", prob_col])
    if len(d) < bins * 5:
        return pd.DataFrame()
    wf = pd.DataFrame({
        "prob_up": d[prob_col].astype(float).to_numpy(),
        "actual_up": (d["outcome_v2"] == "上昇").astype(int).to_numpy(),
        "ret_v2": d["ret_v2"].to_numpy(),
    })
    return calibration(wf, bins)


def llm_rank_table(df: pd.DataFrame) -> pd.DataFrame:
    """LLM が付けた S/A/B/C ランクごとの実績。中身を読んだ順位付けが
    機能しているなら S > A > B > C の順に上昇率が並ぶはず。"""
    if "llm_rank" not in df.columns:
        return pd.DataFrame()
    d = df.dropna(subset=["outcome_v2", "llm_rank"])
    if d.empty:
        return pd.DataFrame()
    rows = []
    for r in ("S", "A", "B", "C"):
        g = d[d["llm_rank"] == r]
        if len(g) < 3:
            continue
        rows.append({
            "LLMランク": r, "n": len(g),
            "上昇%": round(100 * (g["outcome_v2"] == "上昇").mean(), 1),
            "平均リターン%": round(100 * g["ret_v2"].mean(), 2),
        })
    return pd.DataFrame(rows)


def stability_check(df: pd.DataFrame) -> pd.DataFrame:
    """候補フラグの優位性が期間を通して安定しているかを見る。

    これを入れた理由: 「強材料ポイントに定量的な数字があると上昇率+22pt」という
    一見有望な差が見つかったが、期間で割ると 5月 -7pt / 6月 +8pt / 7月 +61pt で、
    実体は「7月が全体的に悪い月(数字なし14%)で8件がたまたま耐えた」だけだった。
    しかも数字入りの材料は3〜4月に0件で最近に偏っており、市場の信号ではなく
    backgrounds の生成様式が変わった副産物(=新しさの代理変数)だった。

    前半と後半で符号が反転する差は、n がいくら大きく見えても採用してはいけない。
    """
    d = prepare(df).dropna(subset=["outcome_v2", "dip_date"])
    if d.empty:
        return pd.DataFrame()
    d = d.copy()
    d["_up"] = (d["outcome_v2"] == "上昇").astype(float)
    half = d["dip_date"].median()

    flags: list[tuple[str, pd.Series]] = []
    if "atr_pct" in d.columns:
        flags.append(("日中変動幅が小さい(下位半分)",
                      d["atr_pct"] < d["atr_pct"].median()))
    if "vol_trend5" in d.columns:
        flags.append(("直前5日で出来高が増加",
                      d["vol_trend5"] > d["vol_trend5"].median()))
    if "vol_decay" in d.columns:
        flags.append(("押し目で出来高が細る(定説)",
                      d["vol_decay"] < d["vol_decay"].median()))
    if "idx_n225_ret20" in d.columns:
        flags.append(("地合い良好(日経20日プラス)", d["idx_n225_ret20"] > 0))
    if "mat_n_num" in d.columns:
        flags.append(("強材料に定量的な数字あり", d["mat_n_num"] >= 1))
    if "mat_n_hedge" in d.columns:
        flags.append(("推測語が2つ以上", d["mat_n_hedge"] >= 2))
    if "dip_depth_vs_peak" in d.columns:
        flags.append(("押し目が浅い(上位半分)",
                      d["dip_depth_vs_peak"] > d["dip_depth_vs_peak"].median()))
    if "market_cap_log" in d.columns:
        flags.append(("時価総額が大きい(上位半分)",
                      d["market_cap_log"] > d["market_cap_log"].median()))
    if "unresolved" in d.columns:
        flags.append(("未確定(期待が残る)", d["unresolved"].astype(str) == "True"))

    rows = []
    for label, m in flags:
        rec = {"候補": label}
        ok = True
        diffs = []
        for lab, seg in (("前半", d[d["dip_date"] <= half]),
                         ("後半", d[d["dip_date"] > half])):
            sm = m.reindex(seg.index).fillna(False)
            a, b = seg[sm]["_up"], seg[~sm]["_up"]
            if len(a) < 3 or len(b) < 3:
                rec[f"{lab}差pt"] = None
                ok = False
                continue
            diff = 100 * (a.mean() - b.mean())
            diffs.append(diff)
            rec[f"{lab}差pt"] = round(diff, 0)
            rec[f"{lab}n"] = len(a)
        rec["符号一致"] = ("—" if not ok or len(diffs) < 2
                          else ("○" if diffs[0] * diffs[1] > 0 else "×反転"))
        rows.append(rec)
    return pd.DataFrame(rows)


def compare_sets(df: pd.DataFrame) -> pd.DataFrame:
    """全特徴セットのウォークフォワード成績を並べる。これが「基準を改良する」
    ための唯一の判断材料。in-sample の当てはまりでは決めない。"""
    rows = []
    for name in FEATURE_SETS:
        wf = walk_forward(df, name)
        s = score(wf)
        if not s:
            rows.append({"セット": name, "n": 0})
            continue
        lo, hi = skill_ci(wf)
        blo, bhi = skill_ci(wf, block=True)
        rows.append({"セット": name, **s, "skill95%下限": lo, "skill95%上限": hi,
                     "週block下限": blo, "週block上限": bhi,
                     "週数": n_blocks(wf), "月数": n_blocks(wf, "M")})
    return pd.DataFrame(rows)


def choose_champion(cmp: pd.DataFrame) -> tuple[str, str]:
    """乗り換え判定。**月ブロック**信頼区間の下限が 0 を上回る候補のうち最良を採用。
    どれも 0 を跨いでいれば baseline に留まる。

    月ブロックを使うのは、地合い系の特徴が同じ月の全銘柄に同時に効くため。
    イベント単位の区間だと独立でないサンプルを独立として数えてしまい、
    偽の有意性が出る。保守的な方を門にする。

    「今日いちばん数字が良かった候補」に飛び移らないための門。
    """
    if cmp.empty or "skill95%下限" not in cmp.columns:
        return DEFAULT_CHAMPION, "比較データ不足"
    gate = "週block下限" if "週block下限" in cmp.columns else "skill95%下限"
    c = cmp[(cmp["セット"] != "baseline")]
    if gate in c.columns and c[gate].isna().all():
        # 区間が計算できない = 評価期間が短すぎる。黙って門を閉じるのではなく
        # 「まだ判定できない」ことを明示する。
        wk = int(c["週数"].max()) if "週数" in c.columns else 0
        mo = int(c["月数"].max()) if "月数" in c.columns else 0
        return (DEFAULT_CHAMPION,
                f"評価期間が短すぎて信頼区間が出せない(週ブロック{wk}個 / "
                f"{mo}ヶ月分、最低{MIN_BLOCKS}ブロック必要)ため baseline を維持")
    cc = c[c[gate].notna()]
    win = cc[cc[gate] > 0]
    if win.empty:
        best_try = cc.sort_values("skill%", ascending=False).iloc[0] if len(cc) else None
        detail = ""
        if best_try is not None:
            detail = (f" 先頭は {best_try['セット']} skill{best_try['skill%']:+.1f}% "
                      f"(週block {best_try[gate]}〜{best_try.get('週block上限')})")
        return (DEFAULT_CHAMPION,
                f"ベース超えを主張できる候補が無い({gate}が0以下)ため"
                f"baseline を維持。{detail}")
    best = win.sort_values("skill%", ascending=False).iloc[0]
    return best["セット"], (f"{best['セット']} が skill {best['skill%']}% "
                            f"(週block95%CI {best[gate]}〜"
                            f"{best.get('週block上限')}, {int(best.get('週数',0))}週分) "
                            f"でベース超えを維持")


# ---------------------------------------------------------------- 実運用

def predict_pending(df: pd.DataFrame, set_name: str) -> pd.DataFrame:
    """まだ結果が出ていない押し目に、判定済み全件で学習したモデルで確率を付ける。"""
    spec = FEATURE_SETS[set_name]
    d = prepare(df)
    train = d.dropna(subset=["outcome_v2"])
    pend = d[d["outcome_v2"].isna() & d["dip_date"].notna()].copy()
    if pend.empty:
        return pend
    if len(train) < MIN_TRAIN:
        pend["prob_up"] = float("nan")
        pend["model_set"] = "学習データ不足"
        return pend
    pend["prob_up"] = _fit_predict(train, pend, spec).round(4)
    pend["model_set"] = set_name
    pend["model_n_train"] = len(train)
    return pend


STATE_FILE = "model_state.json"


def load_state(out_dir: Path) -> dict:
    f = out_dir / STATE_FILE
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"champion": DEFAULT_CHAMPION, "history": []}


def save_state(out_dir: Path, champion: str, reason: str,
               cmp: pd.DataFrame) -> dict:
    """チャンピオンの変遷を残す。いつ何を根拠に基準を変えたかの記録が無いと、
    あとから「性能が上がった」のか「基準を変え続けただけ」なのか判別できない。"""
    st = load_state(out_dir)
    today = datetime.now().strftime("%Y-%m-%d")
    prev = st.get("champion", DEFAULT_CHAMPION)
    entry = {"date": today, "champion": champion, "prev": prev,
             "reason": reason,
             "scores": cmp.to_dict("records") if not cmp.empty else []}
    hist = [h for h in st.get("history", []) if h.get("date") != today]
    hist.append(entry)
    st = {"champion": champion, "changed_at": today if champion != prev
          else st.get("changed_at", today), "history": hist[-90:]}
    (out_dir / STATE_FILE).write_text(
        json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    return st
