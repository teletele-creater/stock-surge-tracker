"""
押し目スクリーニング(S/A/B判定)
============================================================
「まだ消費されていない期待が、拾う買い手のいる場所に乗っているか」を
3段階のフィルタで判定する。

段階1【足切り・理由】バイオ/一過性完結を除外(押し目が効かない別ゲーム)
段階2【核・理由】   具体材料(続報期待型) AND 未確定(語尾が期待/思惑)
                    ※ 具体材料だけ(確定済み・材料消費)は最悪(29%)なので積極除外
段階3【需給ゲート】 テーマ非孤立(過熱度>=1) / 小型除外

階層:
  S級 = 核 × テーマ非孤立(過熱度>=1)
  A級 = 核だがテーマ孤立(需給の裏付けなし)
  B級 = 核でないが テーマ生存(1-3) × 中型以上
  −   = 対象外(材料消費済みを含む)

検証値(2026-07-22, 初押しn=273, ベース反発46.2%):
  S級 76.5%(n=17) / A級 50.0%(n=2) / B級 59.7%(n=62) / 対象外 39.1%(n=192)
  ※ A級は現状n=2で判断不能。数字は多数の組み合わせを試した中の最良構成のため
     上振れの可能性あり。週次で前向きに検証してnを積む必要がある。
"""

import pandas as pd

TIER_S, TIER_A, TIER_B, TIER_NONE = "S", "A", "B", "-"

EXCLUDE_TYPES = {"バイオ・イベント型", "一過性完結型"}
MATERIAL_TYPE = "続報期待型"


def market_cap_floor(df: pd.DataFrame) -> float:
    """小型除外の閾値(母集団の時価総額 下位1/3)。"""
    mc = df["market_cap"].dropna()
    return float(mc.quantile(1 / 3)) if len(mc) else 0.0


def assign_tier(row, mcap_floor: float) -> str:
    ctype = row.get("continuation_type")
    if ctype in EXCLUDE_TYPES:                      # 段階1: 足切り
        return TIER_NONE

    material = ctype == MATERIAL_TYPE               # 具体材料あり
    unresolved = bool(row.get("unresolved"))        # まだ期待段階
    core = material and unresolved                  # 段階2: 核
    # 具体材料はあるが期待が残っていない = 材料消費済み。実績29%で全条件中最悪。
    # 「良い材料だから」と拾いに行きやすい罠なので明示的に除外する。
    if material and not unresolved:
        return TIER_NONE

    heat = row.get("theme_heat")
    heat = -1 if pd.isna(heat) else heat
    not_isolated = heat >= 1                        # 段階3: 需給
    theme_alive = 1 <= heat <= 3
    mc = row.get("market_cap")
    big_enough = pd.notna(mc) and mc > mcap_floor

    if core:
        return TIER_S if not_isolated else TIER_A   # 核。需給が伴えばS、孤立ならA
    if theme_alive and big_enough:
        return TIER_B
    return TIER_NONE


def add_tiers(df: pd.DataFrame, mcap_floor: float | None = None) -> pd.DataFrame:
    floor = market_cap_floor(df) if mcap_floor is None else mcap_floor
    df = df.copy()
    df["tier"] = df.apply(lambda r: assign_tier(r, floor), axis=1)
    return df


def validation_table(df: pd.DataFrame) -> pd.DataFrame:
    """判定済み(outcome あり)の実績で階層の妥当性を検証する。"""
    sub = df.dropna(subset=["outcome"])
    rows = []
    for tier in [TIER_S, TIER_A, TIER_B, TIER_NONE]:
        g = sub[sub["tier"] == tier]
        if len(g) == 0:
            continue
        rows.append({
            "階層": tier,
            "n": len(g),
            "反発%": round(100 * (g["outcome"] == "反発").mean(), 1),
            "5日リターン中央値%": round(100 * g["ret_5d_from_dip"].median(), 1),
        })
    return pd.DataFrame(rows)


def live_candidates(df: pd.DataFrame, days: int = 10) -> pd.DataFrame:
    """まだ結果が出ていない直近の押し目(=今まさに拾える候補)を階層付きで返す。"""
    pending = df[df["outcome"].isna() & df["dip_date"].notna()].copy()
    if pending.empty:
        return pending
    pending["dip_date"] = pd.to_datetime(pending["dip_date"])
    cutoff = pending["dip_date"].max() - pd.Timedelta(days=days)
    pending = pending[pending["dip_date"] >= cutoff]
    pending = pending[pending["tier"].isin([TIER_S, TIER_A, TIER_B])]
    order = {TIER_S: 0, TIER_A: 1, TIER_B: 2}
    pending = pending.assign(_o=pending["tier"].map(order)).sort_values(
        ["_o", "dip_date"], ascending=[True, False]).drop(columns="_o")
    return pending
