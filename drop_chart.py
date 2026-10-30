# -*- coding: utf-8 -*-
"""急落銘柄の日足チャート(PNG)を作る。

drop_notifier が Slack のスレッドに1銘柄1枚ぶら下げるために使う。
押し目側の dip_chart.py と同じ見た目にそろえてあり、描画の下ごしらえ
(フォント・軸の押し込み・日付位置の解決)はあちらの実装をそのまま使う。

株価の取り方が dip_chart と違う点に注意:
  dip_chart  … analysis_out/cache/ohlc/{code}.csv をそのまま読む
  drop_chart … yfinance から1銘柄ぶん引き直す

ザラ場検知(intraday)は watch ループから呼ばれるが、そのループは
sync_prices() を通らないので ohlc_all のキャッシュには**急落した当日の足が
入っていない**。肝心の急落の陰線が描かれないチャートになるため、
描画時に1銘柄だけ取り直す(1ティックあたり数銘柄なので負荷は小さい)。
取得に失敗したときだけキャッシュにフォールバックする。
"""
from __future__ import annotations

import io

import pandas as pd

# 描画の共通処理は dip_chart のものを使う(同じ見た目にそろえるため)
from dip_chart import BARS, DOWN, MA_COLORS, UP, _fit_axes, _pos_of, _setup_font

FIG_W_IN, FIG_H_IN, DPI = 10.0, 6.4, 110
DROP_MARK = "#7A3DE8"     # 急落日のマーカー(陰線の青と紛れないよう紫)


def _fetch(code: str) -> pd.DataFrame | None:
    """日足を取る。当日の足を含めたいので yfinance を優先する。"""
    try:
        import logging
        import yfinance as yf
        logging.getLogger("yfinance").setLevel(logging.CRITICAL)
        px = yf.download(f"{code}.T", period="9mo", interval="1d",
                         progress=False, auto_adjust=False, threads=False)
        if px is not None and not px.empty:
            # yfinance は単一銘柄でも MultiIndex 列を返すことがある
            if isinstance(px.columns, pd.MultiIndex):
                px.columns = px.columns.get_level_values(0)
            px = px.dropna(how="all")
            if len(px) >= 60:
                return px
    except Exception as e:
        print(f"  [chart] {code} 取得失敗、キャッシュを使います: {e}")

    try:
        import drop_detector
        f = drop_detector.PX_DIR / f"{code}.csv"
        if f.exists():
            px = pd.read_csv(f, index_col=0, parse_dates=True)
            if {"Open", "High", "Low", "Close", "Volume"}.issubset(px.columns) and len(px) >= 60:
                return px
    except Exception:
        pass
    return None


def _head_lines(row, px: pd.DataFrame) -> tuple[str, str, str]:
    """タイトル / サブタイトル / 判定行 を作る。"""
    def g(k):
        v = row.get(k)
        try:
            return None if v is None or pd.isna(v) else v
        except (TypeError, ValueError):
            return v

    code, name = str(row.get("stock_code", "")), str(row.get("stock_name", ""))
    dp = g("drop_pct")
    title = f"{code} {name}"
    if dp is not None:
        title += f"   ▼{abs(100 * float(dp)):.1f}%"

    bits = []
    cl = g("close")
    day = g("drop_date")
    if cl is not None:
        when = f" ({pd.Timestamp(day):%m/%d})" if day is not None else ""
        # ザラ場検知の値は終値ではない(右端の水平線=最新値とズレるので区別する)
        label = "検知時" if str(g("detect_mode") or "") == "intraday" else "終値"
        bits.append(f"{label} {float(cl):,.0f}円{when}")
    vr = g("vol_ratio")
    vr5 = g("vol_ratio5")
    if vr is not None:
        vtxt = f"出来高 {float(vr):.1f}倍"
        if vr5 is not None:
            vtxt += f"(5日比 {float(vr5):.1f}倍)"
        bits.append(vtxt)
    gs = g("gap_share")
    if gs is not None:
        bits.append(f"ギャップ寄与 {100 * float(gs):.0f}%")
    attr = "／".join([str(x) for x in (g("sector33"), g("scale"))
                      if x is not None and str(x) not in ("-", "")])
    if attr:
        bits.append(attr)
    sub = "　".join(bits)

    judge = []
    disc = g("disclosure")
    if disc:
        judge.append(str(disc))
    axes = [str(x) for x in (g("nature"), g("scope"), g("priced"))
            if x is not None and str(x) != "不明"]
    if axes:
        judge.append("・".join(axes))
    cond = "　／　".join(judge)
    reason = g("reason")
    if reason:
        cond = (cond + "　／　" if cond else "") + str(reason)[:40]
    return title, sub, cond


def render(row) -> bytes | None:
    """急落1件ぶんのチャートPNGを返す。描けなければ None。"""
    code = str(row.get("stock_code", ""))
    px = _fetch(code)
    if px is None:
        return None

    _setup_font()
    import matplotlib.pyplot as plt
    import mplfinance as mpf

    close = px["Close"]
    # 移動平均は全期間で計算してから切り出す(左端も正しい値にする)
    mas = {n: close.rolling(n).mean() for n in MA_COLORS}
    view = px.tail(BARS)
    idx = view.index
    mas = {n: s.reindex(idx) for n, s in mas.items()}

    aps = [mpf.make_addplot(mas[n], color=MA_COLORS[n], width=1.1,
                            label=f"{n}日線")
           for n in (5, 25, 75) if mas[n].notna().any()]

    drop_pos = _pos_of(idx, row.get("drop_date"))
    if drop_pos is not None:
        mark = pd.Series(float("nan"), index=idx)
        mark.iloc[drop_pos] = float(view["High"].iloc[drop_pos]) * 1.025
        aps.append(mpf.make_addplot(mark, type="scatter", marker="v",
                                    markersize=140, color=DROP_MARK, label="急落日"))

    mc = mpf.make_marketcolors(
        up=UP, down=DOWN,
        edge={"up": UP, "down": DOWN}, wick={"up": UP, "down": DOWN},
        volume={"up": "#F0A0A0", "down": "#A0B8DC"})
    style = mpf.make_mpf_style(
        base_mpf_style="classic", marketcolors=mc, facecolor="white",
        edgecolor="#cccccc", gridcolor="#ededed", gridstyle="-",
        y_on_right=False, rc={"font.family": "YuGothB"})

    fig, axes = mpf.plot(
        view, type="candle", style=style, addplot=aps, volume=True,
        panel_ratios=(4, 1), figsize=(FIG_W_IN, FIG_H_IN),
        datetime_format="%m/%d", xrotation=0, show_nontrading=False,
        returnfig=True, tight_layout=False,
        scale_padding={"top": 0.4, "bottom": 0.5, "left": 0.4, "right": 0.5},
    )
    fig.set_dpi(DPI)
    fig.set_facecolor("white")
    _fit_axes(fig, axes)
    ax = axes[0]

    last_close = float(close.iloc[-1])
    ax.axhline(last_close, color=DOWN, linewidth=1, linestyle="--", alpha=0.45)
    ax.text(0.995, last_close, f" {last_close:,.0f} ",
            transform=ax.get_yaxis_transform(), fontsize=10, color="white",
            ha="right", va="center",
            bbox=dict(boxstyle="round,pad=0.25", facecolor=DOWN, edgecolor="none"))
    if drop_pos is not None:
        ax.axvline(drop_pos, color=DROP_MARK, linewidth=0.9, alpha=0.35)

    title, sub, cond = _head_lines(row, px)
    fig.text(0.045, 0.965, title, fontsize=17, color="#1a1a1a", va="top")
    fig.text(0.045, 0.918, sub, fontsize=10.5, color="#555555", va="top")
    if cond:
        fig.text(0.045, 0.884, cond, fontsize=10.5, color=DOWN, va="top")
    ax.legend(loc="upper left", fontsize=8.5, framealpha=0.85, ncol=5)
    ax.set_ylabel("")
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    # 出来高の目盛りは既定だと "1.0" + オフセット表記(1e6)で読めないので万株にする
    from matplotlib.ticker import FuncFormatter
    for a in axes[1:]:
        a.set_ylabel("")
        a.tick_params(labelsize=8)
        if a.get_ylim()[1] > 1000:
            a.yaxis.set_major_formatter(
                FuncFormatter(lambda v, _: f"{v / 1e4:,.0f}万"))

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=DPI, facecolor="white")
    plt.close(fig)
    return buf.getvalue()


def render_safely(row) -> bytes | None:
    """描画で落ちても通知全体は止めない。"""
    try:
        return render(row)
    except Exception as e:
        print(f"  [chart] {row.get('stock_code')} 描画失敗: {type(e).__name__}: {e}")
        return None


if __name__ == "__main__":
    import sys
    code = sys.argv[1] if len(sys.argv) > 1 else "6522"
    import pandas as _pd
    df = _pd.read_csv("analysis_out/drop_events.csv", encoding="utf-8-sig")
    hit = df[df["stock_code"].astype(str) == code]
    row = hit.iloc[-1] if len(hit) else _pd.Series({"stock_code": code})
    png = render_safely(row)
    if png:
        open(f"drop_chart_{code}.png", "wb").write(png)
        print(f"drop_chart_{code}.png ({len(png):,} bytes)")
    else:
        print("描画できませんでした")
