"""
押し目反発カタログPDF(学習用)
============================================================
初押し(25日線)で反発した急騰銘柄を全件、1銘柄1ページで
チャート + 背景(材料・事業・背景・キーワード・押し目メトリクス)にまとめる。

- 反発判定・押し目メトリクスは dip_events.csv(dip_rebound_analysis.py の出力)から
- 背景テキストは Supabase surge_events(読み取りのみ)から
- 株価チャートは analysis_out/cache/ohlc の日足キャッシュから

使い方(stock_memo ディレクトリで):
  py build_rebound_pdf.py
  py build_rebound_pdf.py --include-2nd   # 2番押し(75日線)の反発も含める

出力: analysis_out/rebound_catalog.pdf
"""

import argparse
import os
import sys
from datetime import timedelta

import pandas as pd
import requests

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib import gridspec, font_manager

import surge_outcome_analysis as soa

FONT = "C:/Windows/Fonts/YuGothM.ttc"
font_manager.fontManager.addfont(FONT)
plt.rcParams["font.family"] = font_manager.FontProperties(fname=FONT).get_name()
plt.rcParams["axes.unicode_minus"] = False

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
TABLE = os.getenv("SUPABASE_TABLE", "surge_events")

OUT_PDF = soa.OUT_DIR / "rebound_catalog.pdf"


# ─────────────────────────────────────────────
# Supabase 背景データ
# ─────────────────────────────────────────────
def fetch_backgrounds() -> dict:
    """(stock_code, JST日付) -> 背景dict の辞書を返す。"""
    rows, offset, page = [], 0, 1000
    while True:
        url = (
            f"{SUPABASE_URL}/rest/v1/{TABLE}"
            f"?select=stock_code,detected_at,reason,business,keywords,backgrounds,"
            f"raw_post,related_stocks,surge_price"
            f"&order=detected_at.asc&offset={offset}&limit={page}"
        )
        r = requests.get(url, headers={"Authorization": f"Bearer {SUPABASE_KEY}",
                                       "apikey": SUPABASE_KEY}, timeout=30)
        r.raise_for_status()
        batch = r.json()
        rows.extend(batch)
        if len(batch) < page:
            break
        offset += page

    lookup = {}
    for row in rows:
        dt = pd.to_datetime(row["detected_at"], utc=True, format="ISO8601")
        d = dt.tz_convert(soa.JST).date()
        code = str(row["stock_code"]).strip()
        lookup[(code, d)] = row
    print(f"背景データ: {len(rows)}件取得")
    return lookup


def bg_lookup(lookup: dict, code: str, surge_date):
    """日付が1日ずれても拾えるように±1日を試す。"""
    for off in (0, -1, 1):
        key = (code, surge_date + timedelta(days=off))
        if key in lookup:
            return lookup[key]
    return {}


# ─────────────────────────────────────────────
# チャート
# ─────────────────────────────────────────────
def draw_chart(ax_price, ax_vol, code, surge_date, dip_date):
    f = soa.OHLC_CACHE / f"{code}.csv"
    if not f.exists():
        ax_price.text(0.5, 0.5, "株価データなし", ha="center", va="center",
                      transform=ax_price.transAxes, color="#999999")
        ax_price.axis("off"); ax_vol.axis("off")
        return None

    px = pd.read_csv(f, index_col=0, parse_dates=True)
    dates = px.index.date
    c = px["Close"].values
    v = px["Volume"].values
    ma5 = px["Close"].rolling(5).mean().values
    ma25 = px["Close"].rolling(25).mean().values
    ma75 = px["Close"].rolling(75).mean().values

    def pos_of(target):
        for off in range(0, 4):
            hit = (dates == target + timedelta(days=off))
            if hit.any():
                return int(hit.argmax())
        return None

    sp = pos_of(surge_date)
    dp = pos_of(dip_date) if pd.notna(dip_date) else None
    if sp is None:
        ax_price.text(0.5, 0.5, "急騰日を特定できず", ha="center", va="center",
                      transform=ax_price.transAxes, color="#999999")
        ax_price.axis("off"); ax_vol.axis("off")
        return None

    lo = max(0, sp - 30)
    hi = min(len(c) - 1, (dp if dp else sp) + 20)
    xs = range(lo, hi + 1)

    ax_price.plot(xs, c[lo:hi + 1], color="#1a1a1a", lw=1.4, label="終値")
    ax_price.plot(xs, ma5[lo:hi + 1], color="#E8A33D", lw=1.0, label="5日")
    ax_price.plot(xs, ma25[lo:hi + 1], color="#3D8CE8", lw=1.0, label="25日")
    ax_price.plot(xs, ma75[lo:hi + 1], color="#B04FD0", lw=1.0, label="75日")

    # 急騰・ピーク・押し目・反発 のマーカー
    ax_price.scatter([sp], [c[sp]], color="#D93030", s=45, zorder=5)
    ax_price.annotate("急騰", (sp, c[sp]), textcoords="offset points",
                      xytext=(0, 8), ha="center", color="#D93030", fontsize=8)
    if dp:
        peak_pos = lo + int(c[sp:dp + 1].argmax()) if dp > sp else sp
        ax_price.scatter([peak_pos], [c[peak_pos]], color="#666666", s=30, zorder=5)
        ax_price.annotate("天井", (peak_pos, c[peak_pos]), textcoords="offset points",
                          xytext=(0, 8), ha="center", color="#666666", fontsize=8)
        ax_price.scatter([dp], [c[dp]], color="#2E9E5B", s=45, zorder=5)
        ax_price.annotate("押し目", (dp, c[dp]), textcoords="offset points",
                          xytext=(0, -14), ha="center", color="#2E9E5B", fontsize=8)
        rb = dp + 5
        if rb <= hi:
            ax_price.scatter([rb], [c[rb]], color="#1E5AA8", s=45, marker="^", zorder=5)
            ax_price.annotate("+5日", (rb, c[rb]), textcoords="offset points",
                              xytext=(0, 8), ha="center", color="#1E5AA8", fontsize=8)

    ax_price.legend(loc="upper left", fontsize=7, ncol=4, frameon=False)
    ax_price.set_ylabel("株価(円)", fontsize=8)
    ax_price.grid(alpha=0.25)
    ax_price.tick_params(labelbottom=False, labelsize=7)

    colors = ["#D93030" if c[i] >= c[i - 1] else "#3D8CE8" for i in xs]
    ax_vol.bar(list(xs), v[lo:hi + 1], color=colors, width=0.8)
    ax_vol.set_ylabel("出来高", fontsize=7)
    ax_vol.grid(alpha=0.2)
    # x軸は日付ラベルを数本だけ
    ticks = list(range(lo, hi + 1, max(1, (hi - lo) // 6)))
    ax_vol.set_xticks(ticks)
    ax_vol.set_xticklabels([dates[t].strftime("%m/%d") for t in ticks], fontsize=7)
    return True


# ─────────────────────────────────────────────
# 1銘柄ページ
# ─────────────────────────────────────────────
def _fmt_pct(x):
    return "-" if pd.isna(x) else f"{100*x:+.1f}%"


def _fmt(x, suf=""):
    return "-" if pd.isna(x) else f"{x}{suf}"


def _join_list(v):
    if isinstance(v, list):
        return " / ".join(str(x) for x in v if str(x).strip())
    return str(v or "")


def _wrap(text, width):
    """日本語向けの素朴な折り返し(文字数ベース)。"""
    text = str(text or "").replace("\n", " ").strip()
    if not text:
        return "-"
    lines, cur = [], ""
    for ch in text:
        cur += ch
        if len(cur) >= width:
            lines.append(cur); cur = ""
    if cur:
        lines.append(cur)
    return "\n".join(lines)


def draw_page(pdf, row, bg, idx, total):
    fig = plt.figure(figsize=(8.27, 11.69))  # A4縦
    gs = gridspec.GridSpec(3, 1, height_ratios=[3.2, 0.9, 5.0], hspace=0.28,
                           left=0.08, right=0.95, top=0.93, bottom=0.05)
    ax_price = fig.add_subplot(gs[0])
    ax_vol = fig.add_subplot(gs[1], sharex=ax_price)
    ax_text = fig.add_subplot(gs[2]); ax_text.axis("off")

    code, name = row["stock_code"], row["stock_name"]
    fig.suptitle(f"[{code}] {name}", fontsize=15, fontweight="bold", x=0.08, ha="left")
    fig.text(0.95, 0.955, f"{idx}/{total}", ha="right", fontsize=9, color="#999999")

    draw_chart(ax_price, ax_vol, str(code), row["surge_date"], row["dip_date"])

    # メトリクス行
    mcap = row.get("market_cap")
    mcap_s = f"{mcap/1e8:.0f}億円" if pd.notna(mcap) else "-"
    lines = []
    lines.append(f"急騰日: {row['surge_date']}   押し目: {row['dip_date']}   反発判定: 〇(初押し25日線)")
    lines.append(f"カテゴリ: {row['category']}   継続性: {row['continuation_type']}   "
                 f"続報スコア: {int(row['followup_score'])}   検知: {row['source']}")
    lines.append("")
    lines.append(f"■ 材料: {_wrap(bg.get('reason') or row.get('reason'), 46)}")
    biz = bg.get("business")
    if biz:
        lines.append(f"■ 事業: {_wrap(biz, 46)}")
    bgs = bg.get("backgrounds")
    if isinstance(bgs, list) and bgs:
        lines.append("■ 背景:")
        for b in bgs[:5]:
            lines.append("  ・" + _wrap(b, 44).replace("\n", "\n    "))
    kw = bg.get("keywords")
    if kw:
        lines.append(f"■ キーワード: {_join_list(kw)}")
    rel = bg.get("related_stocks")
    if rel:
        lines.append(f"■ 関連銘柄: {_wrap(rel, 44)}")
    lines.append("")
    lines.append("― 押し目メトリクス ―")
    lines.append(f"急騰日リターン {_fmt_pct(row['surge_ret'])}   "
                 f"天井からの押し {_fmt_pct(row['dip_depth_vs_peak'])}   "
                 f"押しまで {_fmt(row['days_to_dip'],'日')}")
    lines.append(f"押し時点の保持率 {row['retention_at_dip']:.2f}   "
                 f"押し日出来高(平常比) {_fmt(row['dip_vol_vs_normal'],'倍')}   "
                 f"反発5日 {_fmt_pct(row['ret_5d_from_dip'])}")
    lines.append(f"押し前に後続材料(再検知): {'あり' if row['redetect_before_dip'] else 'なし'}   "
                 f"テーマ過熱度 {_fmt(row['theme_heat'])}   時価総額 {mcap_s}")
    post = bg.get("raw_post")
    if post:
        lines.append("")
        lines.append(f"■ 元ポスト: {_wrap(post, 46)}")

    ax_text.text(0.0, 1.0, "\n".join(lines), va="top", ha="left", fontsize=8.3,
                 transform=ax_text.transAxes, family=plt.rcParams["font.family"],
                 linespacing=1.35)

    pdf.savefig(fig)
    plt.close(fig)


def draw_index(pdf, df):
    """先頭に一覧・集計ページ。"""
    fig = plt.figure(figsize=(8.27, 11.69))
    fig.text(0.08, 0.95, "押し目反発カタログ(学習用)", fontsize=18, fontweight="bold")
    from datetime import datetime
    fig.text(0.08, 0.925, f"初押し(25日線)で反発した急騰銘柄 全{len(df)}件   "
             f"生成 {datetime.now(soa.JST).strftime('%Y-%m-%d')}", fontsize=10, color="#666666")

    y = 0.89
    cat = df["category"].value_counts()
    fig.text(0.08, y, "【カテゴリ内訳】 " + "  ".join(f"{k}:{v}" for k, v in cat.items()),
             fontsize=9)
    y -= 0.02
    ct = df["continuation_type"].value_counts()
    fig.text(0.08, y, "【継続性内訳】 " + "  ".join(f"{k}:{v}" for k, v in ct.items()),
             fontsize=9)
    y -= 0.03

    # 固定列(比例フォントでも揃うよう各列を独立配置)
    cols = [(0.08, "コード"), (0.17, "銘柄名"), (0.44, "急騰日"),
            (0.62, "カテゴリ"), (0.85, "反発5日")]

    def header():
        for x, label in cols:
            fig.text(x, y, label, fontsize=8, fontweight="bold")

    header()
    y -= 0.017
    for _, r in df.iterrows():
        if y < 0.05:
            pdf.savefig(fig); plt.close(fig)
            fig = plt.figure(figsize=(8.27, 11.69)); y = 0.95
            header(); y -= 0.017
        vals = [str(r["stock_code"]), str(r["stock_name"])[:13],
                str(r["surge_date"]), str(r["category"])[:7],
                _fmt_pct(r["ret_5d_from_dip"])]
        for (x, _), val in zip(cols, vals):
            fig.text(x, y, val, fontsize=7.5)
        y -= 0.0135
    pdf.savefig(fig); plt.close(fig)


# ─────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--include-2nd", action="store_true", help="2番押し(75日線)の反発も含める")
    args = ap.parse_args()

    dip = pd.read_csv(soa.OUT_DIR / "dip_events.csv", parse_dates=["surge_date", "dip_date"])
    dip["surge_date"] = dip["surge_date"].dt.date
    dip["dip_date"] = dip["dip_date"].dt.date

    types = ["初押し(25日線)"]
    if args.include_2nd:
        types.append("2番押し(75日線)")
    reb = dip[(dip["dip_type"].isin(types)) & (dip["outcome"] == "反発")].copy()
    # 銘柄×急騰日で重複排除(初押しを優先)
    reb["_pri"] = (reb["dip_type"] == "初押し(25日線)").astype(int)
    reb = reb.sort_values("_pri", ascending=False).drop_duplicates(
        subset=["stock_code", "surge_date"], keep="first")
    reb = reb.sort_values(["category", "surge_date"]).reset_index(drop=True)
    print(f"反発カタログ対象: {len(reb)}件")

    lookup = fetch_backgrounds()

    with PdfPages(OUT_PDF) as pdf:
        draw_index(pdf, reb)
        for i, (_, row) in enumerate(reb.iterrows(), 1):
            bg = bg_lookup(lookup, str(row["stock_code"]), row["surge_date"])
            try:
                draw_page(pdf, row, bg, i, len(reb))
            except Exception as e:
                print(f"  [WARN] {row['stock_code']} ページ生成失敗: {e}")
            if i % 20 == 0:
                print(f"  生成 {i}/{len(reb)}")

    print(f"\n完了: {OUT_PDF}")


if __name__ == "__main__":
    main()
