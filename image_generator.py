"""
画像生成モジュール

Analysis オブジェクトから「株価急騰要因」の見出し画像を生成する。
ローソク足チャート + 銘柄情報 + キーワードパネルを 1200x630 PNG で出力。

使い方:
    from image_generator import generate_card
    image_bytes = generate_card(analysis)  # bytes (PNG)

    # 単体動作確認
    python image_generator.py --demo
"""

import io
import logging
import re
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Windows 同梱の日本語フォント
FONT_BOLD = "C:/Windows/Fonts/YuGothB.ttc"
FONT_REG = "C:/Windows/Fonts/YuGothM.ttc"

# 図のサイズ (SNS カード標準)
FIG_W_IN, FIG_H_IN = 12, 6.3
DPI = 100


def _clean(text: str) -> str:
    """analyzer 出力に残った余分な括弧/空白を除去する"""
    if not text:
        return ""
    t = str(text).strip()
    while t.startswith("(") and t.endswith(")"):
        t = t[1:-1].strip()
    return t


def _split_keywords(keywords: str) -> list[str]:
    """キーワード文字列を区切り文字で分割し各要素の括弧も除去、最大5件返す"""
    cleaned = _clean(keywords)
    return [k.strip().strip("()（）") for k in re.split(r"[・、,，/]+", cleaned)
            if k.strip()][:5]


def fetch_current_price(stock_code: str) -> float | None:
    """日本株コードから最新株価を取得する (yfinance使用)。

    1分足の最新終値を優先し、無ければ日足の最新終値にフォールバック。
    取得失敗時は None。括弧付きコード "(6544)" でも自動的に剥がす。
    """
    cleaned = _clean(stock_code)
    if not cleaned:
        return None
    try:
        import yfinance as yf
        ticker = yf.Ticker(f"{cleaned}.T")
        intraday = ticker.history(period="1d", interval="1m")
        if intraday is not None and not intraday.empty:
            return float(intraday["Close"].iloc[-1])
        daily = ticker.history(period="5d")
        if daily is not None and not daily.empty:
            return float(daily["Close"].iloc[-1])
    except Exception as e:
        logger.warning(f"price fetch failed for {stock_code}: {e}")
    return None


def _patch_today_with_intraday(ticker, hist):
    """当日の日足を分足ベースの実値で更新したコピーを返す (急騰反映用)。

    yfinance の日足は当日分が未確定 / 数十分遅延で急騰が見えないことがある。
    1分足を取得して当日の OHLC を再計算し、既存の当日行を差し替える。
    既存行が無ければ新規追加する。
    """
    if hist is None or hist.empty:
        return hist
    try:
        intraday = ticker.history(period="1d", interval="1m")
    except Exception as e:
        logger.warning(f"intraday fetch failed: {e}")
        return hist
    if intraday is None or intraday.empty:
        return hist

    import pandas as pd

    today_idx = intraday.index[-1].normalize()
    today_open = float(intraday["Open"].iloc[0])
    today_high = float(intraday["High"].max())
    today_low = float(intraday["Low"].min())
    today_close = float(intraday["Close"].iloc[-1])
    today_volume = float(intraday["Volume"].sum())

    result = hist.copy()
    last_idx_normalized = result.index[-1].normalize()

    if last_idx_normalized == today_idx:
        # 既存の当日行を差し替え
        result.iloc[-1, result.columns.get_loc("Open")] = today_open
        result.iloc[-1, result.columns.get_loc("High")] = today_high
        result.iloc[-1, result.columns.get_loc("Low")] = today_low
        result.iloc[-1, result.columns.get_loc("Close")] = today_close
        if "Volume" in result.columns:
            result.iloc[-1, result.columns.get_loc("Volume")] = today_volume
    else:
        # 当日行が無いので追加
        new_data = {
            "Open": today_open,
            "High": today_high,
            "Low": today_low,
            "Close": today_close,
            "Volume": today_volume,
        }
        # hist にある他カラムは 0 埋め
        for col in result.columns:
            new_data.setdefault(col, 0)
        new_row = pd.DataFrame([new_data], index=pd.DatetimeIndex([today_idx]))[result.columns]
        # tz 合わせ
        if result.index.tz is not None and new_row.index.tz is None:
            new_row.index = new_row.index.tz_localize(result.index.tz)
        result = pd.concat([result, new_row])

    return result


def _keyword_font_size(kw: str) -> int:
    """キーワード文字数に応じてフォントサイズを返す (パネル幅に収める)"""
    n = len(kw)
    if n <= 6:
        return 22
    if n <= 8:
        return 19
    if n <= 10:
        return 16
    if n <= 12:
        return 14
    return 12


def generate_card(analysis) -> bytes:
    """
    Analysis から株価急騰要因カード (PNG bytes) を生成する。

    yfinance の株価取得に失敗した場合はチャート部分のみ
    「データ取得失敗」と表示し、その他の見出しは正常に出力する。
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle, FancyBboxPatch, Ellipse
    from matplotlib.lines import Line2D
    from matplotlib import font_manager
    import mplfinance as mpf
    import yfinance as yf

    fm_entry = font_manager.FontEntry(fname=FONT_BOLD, name="YuGothB")
    font_manager.fontManager.ttflist.insert(0, fm_entry)
    plt.rcParams["font.family"] = "YuGothB"
    plt.rcParams["axes.unicode_minus"] = False

    # analyzer 出力の括弧を除去 (株コード、銘柄名、理由、事業)
    stock_code = _clean(analysis.stock_code)
    stock_name = _clean(analysis.stock_name)
    reason = _clean(analysis.reason)

    # 株価データ取得 (失敗してもチャート以外は描画する)
    hist = None
    try:
        ticker = yf.Ticker(f"{stock_code}.T")
        hist = ticker.history(period="3mo")
        # 当日の足は未確定 or 遅延で急騰が反映されてないことが多いので
        # 分足から実値で上書きする (急騰直後の現在値まで日足を伸ばす)
        hist = _patch_today_with_intraday(ticker, hist)
    except Exception as e:
        logger.warning(f"yfinance fetch failed for {stock_code}: {e}")

    # 前日比騰落率（画像を流し見する層向けにヘッダへ大きく出す）
    # 当日足は _patch_today_with_intraday で実値化済みなので、直近2本の終値から計算
    change_pct = None
    if hist is not None and len(hist) >= 2:
        try:
            prev_close = float(hist["Close"].iloc[-2])
            last_close = float(hist["Close"].iloc[-1])
            if prev_close > 0:
                change_pct = (last_close / prev_close - 1.0) * 100.0
        except Exception as e:
            logger.warning(f"change pct calc failed: {e}")

    fig = plt.figure(figsize=(FIG_W_IN, FIG_H_IN), dpi=DPI, facecolor="white")
    ASPECT = FIG_W_IN / FIG_H_IN

    # ====== HEADER ======
    HEADER_Y, HEADER_H = 0.72, 0.27
    # 左の青いアクセントバー
    bar = Rectangle((0.022, HEADER_Y + 0.025), 0.009, HEADER_H - 0.04,
                    transform=fig.transFigure, facecolor="#1E5AA8", edgecolor="none")
    fig.add_artist(bar)

    ax_header = fig.add_axes([0.035, HEADER_Y, 0.95, HEADER_H])
    ax_header.set_xticks([]); ax_header.set_yticks([])
    ax_header.set_facecolor("white")
    for s in ax_header.spines.values():
        s.set_visible(False)

    ax_header.text(0.015, 0.93, "※株価急騰要因",
                   fontsize=34, color="#1a1a1a", fontweight="bold",
                   transform=ax_header.transAxes, va="top")

    # 騰落率をヘッダ右上に大きく表示（陽線=赤・陰線=青のチャート配色と揃える）
    if change_pct is not None:
        pct_color = "#D93030" if change_pct >= 0 else "#1E5AA8"
        ax_header.text(0.985, 0.70, f"{change_pct:+.1f}%",
                       fontsize=52, color=pct_color, fontweight="bold",
                       transform=ax_header.transAxes, ha="right", va="center")
        ax_header.text(0.985, 0.24, "前日比",
                       fontsize=15, color="#888888",
                       transform=ax_header.transAxes, ha="right", va="center")

    # 銘柄名 (大・黒) + コード (小・グレー) を横並び
    # 名前が長すぎる場合はフォント縮小
    display_name = stock_name or stock_code
    name_size = 32 if len(display_name) <= 8 else 26 if len(display_name) <= 14 else 20
    name_artist = ax_header.text(0.015, 0.50, display_name,
                                 fontsize=name_size, color="#1a1a1a", fontweight="bold",
                                 transform=ax_header.transAxes, va="center")
    fig.canvas.draw()
    name_bbox = name_artist.get_window_extent()
    inv = ax_header.transAxes.inverted()
    name_right_ax = inv.transform((name_bbox.x1, 0))[0]
    if stock_name and stock_code:
        ax_header.text(name_right_ax + 0.012, 0.49, f"({stock_code})",
                       fontsize=22, color="#888888",
                       transform=ax_header.transAxes, va="center")

    reason_text = f"{reason}によるもの"
    reason_size = 30 if len(reason_text) <= 14 else 26 if len(reason_text) <= 18 else 22 if len(reason_text) <= 24 else 18
    ax_header.text(0.015, 0.13, reason_text,
                   fontsize=reason_size, color="#D93030", fontweight="bold",
                   transform=ax_header.transAxes, va="center", clip_on=False)

    # ====== CHART PANEL ======
    CHART_X, CHART_Y = 0.04, 0.04
    CHART_W, CHART_H = 0.68, 0.62

    chart_panel = FancyBboxPatch(
        (CHART_X - 0.005, CHART_Y - 0.005),
        CHART_W + 0.01, CHART_H + 0.01,
        boxstyle="round,pad=0.005,rounding_size=0.012",
        transform=fig.transFigure,
        facecolor="none", edgecolor="#c8d4e3", linewidth=1.2,
        zorder=1,
    )
    fig.add_artist(chart_panel)

    # チャートヘッダ
    fig.text(
        CHART_X + 0.015, CHART_Y + CHART_H - 0.03,
        "■ 株価推移（日足）",
        fontsize=14, color="#1E5AA8", fontweight="bold",
        ha="left", va="center", zorder=5,
    )

    ax_chart = fig.add_axes([CHART_X + 0.025, CHART_Y + 0.03, CHART_W - 0.04, CHART_H - 0.10])

    if hist is None or hist.empty:
        ax_chart.text(0.5, 0.5, "(株価データ取得失敗)", ha="center", va="center",
                      fontsize=20, color="#888888", transform=ax_chart.transAxes)
        ax_chart.set_xticks([])
        ax_chart.set_yticks([])
        for s in ax_chart.spines.values():
            s.set_visible(False)
    else:
        # 日本式: 陽線=赤、陰線=青
        mc = mpf.make_marketcolors(
            up="#D93030", down="#1E5AA8",
            edge={"up": "#D93030", "down": "#1E5AA8"},
            wick={"up": "#D93030", "down": "#1E5AA8"},
            volume="inherit",
        )
        style = mpf.make_mpf_style(
            base_mpf_style="classic",
            marketcolors=mc,
            facecolor="white",
            edgecolor="#cccccc",
            gridcolor="#e8e8e8",
            gridstyle="-",
            y_on_right=False,
            rc={"font.family": "YuGothB"},
        )
        mpf.plot(
            hist,
            type="candle",
            ax=ax_chart,
            style=style,
            datetime_format="%m/%d",
            xrotation=0,
            show_nontrading=False,
        )
        last_y = hist["Close"].iloc[-1]
        ax_chart.axhline(last_y, color="#D93030", linewidth=1, linestyle="--", alpha=0.4)
        ax_chart.text(
            0.995, last_y, f" ¥{last_y:,.0f} ",
            transform=ax_chart.get_yaxis_transform(),
            fontsize=12, color="white", fontweight="bold",
            ha="right", va="center",
            bbox=dict(boxstyle="round,pad=0.3", facecolor="#D93030", edgecolor="none"),
        )
        ax_chart.set_ylabel("")
        ax_chart.text(-0.005, 1.015, "(円)", fontsize=11, color="#666666",
                      transform=ax_chart.transAxes, va="bottom", ha="left")
        ax_chart.spines["top"].set_visible(False)
        ax_chart.spines["right"].set_visible(False)
        ax_chart.tick_params(labelsize=10)

    # ====== KEYWORD PANEL ======
    KW_X = CHART_X + CHART_W + 0.02
    KW_W = 1 - KW_X - 0.04
    KW_Y = CHART_Y
    KW_H = CHART_H
    KW_TOP = KW_Y + KW_H
    HDR_H = 0.08
    ROW_H = 0.085
    TOP_PAD = 0.025

    # 外枠
    kw_panel = FancyBboxPatch(
        (KW_X - 0.005, KW_Y - 0.005),
        KW_W + 0.01, KW_H + 0.01,
        boxstyle="round,pad=0.005,rounding_size=0.012",
        transform=fig.transFigure,
        facecolor="none", edgecolor="#c8d4e3", linewidth=1.2,
        zorder=1,
    )
    fig.add_artist(kw_panel)

    # 青いヘッダバー
    blue_hdr = FancyBboxPatch(
        (KW_X, KW_Y + KW_H - HDR_H),
        KW_W, HDR_H,
        boxstyle="round,pad=0,rounding_size=0.012",
        transform=fig.transFigure,
        facecolor="#1E5AA8", edgecolor="none", zorder=2,
    )
    fig.add_artist(blue_hdr)
    blue_hdr_bottom = Rectangle(
        (KW_X, KW_Y + KW_H - HDR_H),
        KW_W, HDR_H / 2,
        transform=fig.transFigure, facecolor="#1E5AA8", edgecolor="none", zorder=2,
    )
    fig.add_artist(blue_hdr_bottom)

    fig.text(
        KW_X + KW_W / 2, KW_Y + KW_H - HDR_H / 2,
        "■ キーワード",
        fontsize=20, color="white", fontweight="bold",
        ha="center", va="center", zorder=5,
    )

    # キーワード本体
    kw_items = _split_keywords(analysis.keywords)
    n = len(kw_items)
    if n > 0:
        body_top = KW_TOP - HDR_H - TOP_PAD
        first_y = body_top - ROW_H / 2

        for i, kw in enumerate(kw_items):
            cy = first_y - i * ROW_H

            dot_x = KW_X + KW_W * 0.10
            dot = Ellipse(
                (dot_x, cy), 0.007, 0.007 * ASPECT,
                transform=fig.transFigure,
                facecolor="#1E5AA8", edgecolor="none",
            )
            fig.add_artist(dot)

            text_x = KW_X + KW_W * 0.18
            fig.text(text_x, cy, kw, fontsize=_keyword_font_size(kw),
                     color="#1a1a1a", fontweight="bold", ha="left", va="center")

            if i < n - 1:
                sep_y = cy - ROW_H / 2
                line = Line2D(
                    [KW_X + KW_W * 0.08, KW_X + KW_W * 0.92],
                    [sep_y, sep_y],
                    transform=fig.transFigure,
                    color="#e0e0e0", linestyle=(0, (1, 2.5)), linewidth=1,
                )
                fig.add_artist(line)

    # ====== FOOTER: サイトの銘柄ページURL ======
    # 画像自体はクリックできないため、視認用（クリック可能なリンクはポスト本文側に付ける）
    if stock_code and stock_code != "不明":
        from config import SITE_BASE_URL
        site_host = re.sub(r"^https?://", "", SITE_BASE_URL)
        fig.text(0.965, 0.017, f"詳細・過去の急騰履歴 ▶ {site_host}/#stock/{stock_code}",
                 fontsize=11, color="#888888", ha="right", va="center")

    buf = io.BytesIO()
    fig.savefig(buf, format="PNG", facecolor="white")
    plt.close(fig)
    return buf.getvalue()


def _run_demo() -> None:
    """サンプル Analysis で画像生成し ./image_demo.png に保存する"""
    from dataclasses import dataclass
    from datetime import datetime, timezone

    @dataclass
    class _MockTweet:
        id: str = "demo"
        text: str = "demo"
        created_at: datetime = datetime.now(timezone.utc)
        stock_codes: list = None

    @dataclass
    class _MockAnalysis:
        tweet: _MockTweet
        stock_code: str = "7203"
        stock_name: str = "トヨタ自動車"
        reason: str = "第3四半期決算の上方修正"
        business: str = "自動車の製造・販売"
        keywords: str = "円安・HV車・北米市場"
        backgrounds: list = None
        related_stocks: str = ""
        raw_output: str = ""

    mock = _MockAnalysis(tweet=_MockTweet())
    image_bytes = generate_card(mock)

    out_path = Path(__file__).parent / "image_demo.png"
    out_path.write_bytes(image_bytes)
    print(f"[OK] サンプル画像を保存しました: {out_path.absolute()}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    if len(sys.argv) > 1 and sys.argv[1] == "--demo":
        _run_demo()
    else:
        print("使い方: python image_generator.py --demo")
        sys.exit(1)
