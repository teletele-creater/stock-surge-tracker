"""スクリーニング候補の日足チャート(PNG)を作る。

notify_analysis_slack が Slack のスレッドに1銘柄1枚ぶら下げるために使う。

株価は analysis_out/cache/ohlc/{code}.csv (surge_outcome_analysis が
yfinance から落としたもの)をそのまま読む。あのキャッシュは
`end=翌日` で取っているので**当日の終値まで入っている**。ここで
yfinance を引き直すと 20銘柄ぶんのレート制限に当たるうえ、
判定に使った足とチャートの足がズレるので絶対に引き直さない。
"""

from __future__ import annotations

import io
from datetime import timedelta

import pandas as pd

import dip_filter

BARS = 126           # 描画する日足の本数(およそ6ヶ月)
FONT_BOLD = "C:/Windows/Fonts/YuGothB.ttc"
FIG_W_IN, FIG_H_IN, DPI = 10.0, 6.4, 110

UP, DOWN = "#D93030", "#1E5AA8"          # 日本式: 陽線=赤 / 陰線=青
MA_COLORS = {5: "#888888", 25: "#E8A33D", 75: "#2E9E6B"}


def _setup_font():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager

    if "YuGothB" not in {f.name for f in font_manager.fontManager.ttflist}:
        try:
            font_manager.fontManager.ttflist.insert(
                0, font_manager.FontEntry(fname=FONT_BOLD, name="YuGothB"))
        except Exception:
            return
    plt.rcParams["font.family"] = "YuGothB"
    plt.rcParams["axes.unicode_minus"] = False


def _pos_of(index, target, window: int = 4) -> int | None:
    """index(DatetimeIndex)の中で target 以後の最初の営業日の位置を返す。

    急騰日が休日だったり、その日の足が無いことがあるので window 日ぶん探す。
    """
    if target is None or pd.isna(target):
        return None
    t = pd.Timestamp(target).normalize()
    for off in range(window):
        hit = (index.normalize() == t + timedelta(days=off))
        if hit.any():
            return int(hit.argmax())
    return None


def _fit_axes(fig, axes, left=0.075, right=0.955, bottom=0.075, top=0.845):
    """mplfinance が置いた軸をまとめて指定領域に押し込む。

    mpf は fig.add_axes で軸を作るので subplots_adjust が効かない。
    ヘッダー3行ぶんの余白を上に空けるには位置を直接いじるしかない。
    """
    boxes = [a.get_position() for a in axes]
    x0 = min(b.x0 for b in boxes); x1 = max(b.x1 for b in boxes)
    y0 = min(b.y0 for b in boxes); y1 = max(b.y1 for b in boxes)
    if x1 <= x0 or y1 <= y0:
        return
    sx, sy = (right - left) / (x1 - x0), (top - bottom) / (y1 - y0)
    for a, b in zip(axes, boxes):
        a.set_position([left + (b.x0 - x0) * sx, bottom + (b.y0 - y0) * sy,
                        b.width * sx, b.height * sy])
    # 価格パネルの下端を少し上げる。詰めたままだと一番下の株価目盛りが
    # 出来高パネルの枠線に重なって読めない
    mid = (bottom + top) / 2
    for a in axes:
        b = a.get_position()
        if b.y0 > mid:
            a.set_position([b.x0, b.y0 + 0.022, b.width, b.height - 0.022])


def _load(code: str, surge_date=None) -> pd.DataFrame | None:
    px = dip_filter.load_px(code, surge_date)
    if px is None:
        return None
    need = {"Open", "High", "Low", "Close", "Volume"}
    if not need.issubset(px.columns) or len(px) < 77:
        return None
    return px


def _head_lines(row, dipped: bool, last_date, last_close) -> tuple[str, str, str]:
    """タイトル / サブタイトル / 条件行 を作る。"""
    code, name = row["stock_code"], row["stock_name"]
    score, full = int(row["score"]), int(row["満点"])
    title = f"{code} {name}   共通{score}/{full}件"
    if dipped and row.get("screening_route"):
        title += f"   {row['screening_route']}"

    if not dipped:
        title += "   押し目前・監視"

    bits = [f"終値 {last_close:,.0f}円 ({pd.Timestamp(last_date):%m/%d})"]
    if dipped:
        bits.append(f"押し目 {pd.Timestamp(row['dip_date']):%m/%d}")
    else:
        d = row.get("dist_to_ma25")
        if pd.notna(d):
            bits.append(f"25日線まで {100 * float(d):.1f}%")
    for label, key, fmt in (("14日値幅率", "atr_pct_dip", "{:.1f}%"),
                            ("25/75乖離", "ma_spread", "{:.1f}%")):
        v = row.get(key)
        if pd.notna(v):
            bits.append(f"{label} {fmt.format(100 * float(v))}")
    tp = row.get("theme_prior30")
    if pd.notna(tp):
        bits.append(f"テーマ先行 {int(tp)}件")
    sub = "　".join(bits)

    cond = f"該当: {row['該当条件']}"
    rsn = row.get("reason")
    if isinstance(rsn, str) and rsn.strip():
        cond += f"　／　{rsn.strip()[:38]}"
    return title, sub, cond


def render(row, dipped: bool) -> bytes | None:
    """候補1行ぶんのチャートPNGを返す。描けなければ None。"""
    px = _load(str(row["stock_code"]), row.get("surge_date"))
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

    # ── マーカー(急騰日 / 押し目日) ─────────────────────────
    def _marker(pos, series, offset):
        s = pd.Series(float("nan"), index=idx)
        if pos is not None:
            s.iloc[pos] = float(series.iloc[pos]) * offset
        return s

    aps = [mpf.make_addplot(mas[n], color=MA_COLORS[n], width=1.1,
                            label=f"{n}日線")
           for n in (5, 25, 75) if mas[n].notna().any()]

    surge_pos = _pos_of(idx, row.get("surge_date"))
    if surge_pos is not None:
        aps.append(mpf.make_addplot(
            _marker(surge_pos, view["Low"], 0.975), type="scatter",
            marker="^", markersize=130, color=UP, label="急騰日"))

    dip_pos = _pos_of(idx, row.get("dip_date")) if dipped else None
    if dip_pos is not None:
        aps.append(mpf.make_addplot(
            _marker(dip_pos, view["Low"], 0.975), type="scatter",
            marker="v", markersize=130, color="#7A3DE8", label="押し目"))

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

    last_date, last_close = idx[-1], float(close.iloc[-1])
    # 当日終値の水平線。「今どこにいるか」が一目で分かるようにする
    ax.axhline(last_close, color=UP, linewidth=1, linestyle="--", alpha=0.45)
    ax.text(0.995, last_close, f" {last_close:,.0f} ",
            transform=ax.get_yaxis_transform(), fontsize=10, color="white",
            ha="right", va="center",
            bbox=dict(boxstyle="round,pad=0.25", facecolor=UP, edgecolor="none"))
    if dip_pos is not None:
        ax.axvline(dip_pos, color="#7A3DE8", linewidth=0.9, alpha=0.35)
    if surge_pos is not None:
        ax.axvline(surge_pos, color=UP, linewidth=0.9, alpha=0.3)

    title, sub, cond = _head_lines(row, dipped, last_date, last_close)
    fig.text(0.045, 0.965, title, fontsize=17, color="#1a1a1a", va="top")
    fig.text(0.045, 0.918, sub, fontsize=10.5, color="#555555", va="top")
    fig.text(0.045, 0.884, cond, fontsize=10.5, color="#1E5AA8", va="top")
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


def render_safely(row, dipped: bool) -> bytes | None:
    """描画で落ちても通知全体は止めない。"""
    try:
        return render(row, dipped)
    except Exception as e:
        print(f"  [chart] {row.get('stock_code')} 描画失敗: {e}")
        return None


if __name__ == "__main__":
    import sys

    code = sys.argv[1] if len(sys.argv) > 1 else "1418"
    ev = dip_filter.add_theme_prior(dip_filter.fetch_events())
    for mode, dipped in (("dipped", True), ("approaching", False)):
        df = dip_filter.build_table(mode, use_llm=False, ev=ev)
        if df.empty:
            continue
        hit = df[df["stock_code"].astype(str) == code]
        if hit.empty:
            continue
        png = render(hit.iloc[0], dipped)
        out = dip_filter.BASE / "analysis_out" / f"chart_{code}_{mode}.png"
        out.write_bytes(png)
        print(f"→ {out} ({len(png):,} bytes)")
