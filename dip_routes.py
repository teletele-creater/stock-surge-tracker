"""初押し引けまでの情報で判定するMACD/出来高ルート。送信・取得は行わない。"""
from __future__ import annotations

import numpy as np
import pandas as pd

RULE_VERSION = "2026-09-17-macd-volume-v1"
MACD_HIST_PCT_MAX = -0.4444720534512462
INITIAL3_VOLUME_MAX = 5.465080186239007
COMMON_MIN_SCORE = 3
ROUTE_COLUMNS = ["rule_version", "base_pass", "macd_hist_pct", "volume_ratio",
                 "volume_status", "cond_macd", "cond_volume", "route_macd",
                 "route_volume", "route_selected", "screening_route"]


def _ema(values: np.ndarray, period: int) -> np.ndarray:
    """最初の連続period本の単純平均で初期化する、検証と同じEMA。"""
    values = np.asarray(values, dtype=float)
    result = np.full(len(values), np.nan)
    active = False
    alpha = 2 / (period + 1)
    for i in range(period - 1, len(values)):
        if not active:
            window = values[i - period + 1:i + 1]
            if np.isfinite(window).all():
                result[i] = window.mean()
                active = True
        elif np.isfinite(values[i]):
            result[i] = (1 - alpha) * result[i - 1] + alpha * values[i]
        else:
            active = False
    return result


def price_features(px: pd.DataFrame, surge_i: int, asof_i: int) -> dict:
    """未来の足を使用しない。初動3本が未完成なら出来高は欠測。"""
    if not 0 <= surge_i <= asof_i < len(px):
        raise ValueError("急騰日・判定日の位置が不正です")
    close = px["Close"].iloc[:asof_i + 1].to_numpy(dtype=float)
    line = _ema(close, 12) - _ema(close, 26)
    hist = line - _ema(line, 9)
    macd = float(100 * hist[-1] / close[-1]) if np.isfinite(close[-1]) and close[-1] > 0 else np.nan
    ratio = np.nan
    status = "available"
    if asof_i < surge_i + 2:
        status = "initial_3_bars_not_yet_available"
    else:
        prior = px["Volume"].iloc[max(0, surge_i - 20):surge_i]
        initial = px["Volume"].iloc[surge_i:surge_i + 3]
        if len(prior) != 20 or not np.isfinite(prior).all() or (prior <= 0).any():
            status = "invalid_baseline_20_bars"
        elif len(initial) != 3 or not np.isfinite(initial).all():
            status = "invalid_initial_3_bars"
        else:
            ratio = float(initial.mean() / prior.median())
    return {"macd_hist_pct": macd, "volume_ratio": ratio, "volume_status": status}


def add_routes(df: pd.DataFrame, *, dipped: bool) -> pd.DataFrame:
    """追加2条件は共通スコアに足さず、共通条件の後でOR判定する。"""
    out = df.copy()
    blank = pd.Series(np.nan, index=out.index, dtype=float)
    macd = pd.to_numeric(out.get("macd_hist_pct", blank), errors="coerce")
    volume = pd.to_numeric(out.get("volume_ratio", blank), errors="coerce")
    out["cond_macd"] = np.isfinite(macd) & (macd <= MACD_HIST_PCT_MAX)
    out["cond_volume"] = np.isfinite(volume) & (volume >= 0) & (volume <= INITIAL3_VOLUME_MAX)
    out["base_pass"] = out["score"].ge(COMMON_MIN_SCORE) & dipped
    out["route_macd"] = out["base_pass"] & out["cond_macd"]
    out["route_volume"] = out["base_pass"] & out["cond_volume"]
    out["route_selected"] = out["route_macd"] | out["route_volume"]
    out["screening_route"] = np.select(
        [out["route_macd"] & out["route_volume"], out["route_macd"], out["route_volume"]],
        ["MACD・出来高", "MACD", "出来高"], default="対象外" if dipped else "押し目前")
    out["rule_version"] = RULE_VERSION
    return out
