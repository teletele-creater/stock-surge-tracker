"""
押し目反発分析 — 初押しで反発する銘柄と突き抜ける銘柄の違い
============================================================
surge_outcome_analysis.py の基盤(イベント取得・分類・株価キャッシュ)を
再利用し、移動平均線ベースの押し目イベントを検出して比較する。

定義(ユーザー指定):
  対象   : 急騰日に終値が5日/25日/75日移動平均の全てより上、
           かつ25日線が上向き(5営業日前より上)の銘柄
  初押し : 急騰後、初めて安値が25日線にタッチした日(60営業日以内)
  2番押し: 安値が75日線までタッチした日(初押しと区別して集計)
  判定   : タッチ日の終値 < 5営業日後の終値 → 反発
           タッチ日の終値 > 5営業日後の終値 → 突き抜け

使い方(stock_memo ディレクトリで):
  py dip_rebound_analysis.py

出力:
  analysis_out/dip_events.csv
  analysis_out/dip_rebound_report.md
"""

import sys
from datetime import timedelta

import pandas as pd

import surge_outcome_analysis as soa

DIP_SEARCH_DAYS = 60   # 押し目探索ウィンドウ(営業日)
OUTCOME_DAYS = 5       # 反発/突き抜け判定(営業日)
TOUCH_TOL = 1.0        # 「タッチ」判定の許容(安値 <= MA * (1+0.0) を基本に誤差ゼロ)


def find_dips(df: pd.DataFrame, prices: dict, caps: dict) -> pd.DataFrame:
    recs = []
    n_qualified = 0
    n_no_ma = 0
    for _, ev in df.iterrows():
        code = ev["stock_code"]
        px = prices.get(code)
        if px is None:
            continue
        close = px["Close"]
        low = px["Low"].values
        vol = px["Volume"].values
        ma5 = close.rolling(5).mean().values
        ma25 = close.rolling(25).mean().values
        ma75 = close.rolling(75).mean().values
        c = close.values
        dates = px.index.date

        # 急騰日の位置(soa.compute_features と同じ突合ルール)
        pos = None
        for offset in range(0, 4):
            cand = ev["surge_date"] + timedelta(days=offset)
            hit = (dates == cand)
            if hit.any():
                pos = int(hit.argmax())
                break
        if pos is None or pos < 26:
            continue

        # 対象条件: 全MAより上 + 25日線上向き
        if pd.isna(ma75[pos]):
            n_no_ma += 1
            continue
        above_all = c[pos] > ma5[pos] and c[pos] > ma25[pos] and c[pos] > ma75[pos]
        ma25_rising = pos >= 5 and not pd.isna(ma25[pos - 5]) and ma25[pos] > ma25[pos - 5]
        if not (above_all and ma25_rising):
            continue
        n_qualified += 1

        pre_close = c[pos - 1]
        surge_close = c[pos]
        end = min(pos + DIP_SEARCH_DAYS, len(c) - 1)

        def dip_record(t: int, dip_type: str):
            peak = c[pos:t + 1].max()
            out_pos = t + OUTCOME_DAYS
            if out_pos >= len(c):
                outcome = None
                ret_5d = None
            else:
                ret_5d = c[out_pos] / c[t] - 1
                outcome = "反発" if c[out_pos] > c[t] else "突き抜け"
            vol_base = vol[max(0, pos - 20):pos].mean()
            # 押し目までに同一銘柄の後続検知があったか
            later = df[(df["stock_code"] == code)
                       & (df["surge_date"] > ev["surge_date"])
                       & (df["surge_date"] <= dates[t])]
            return {
                "stock_code": code,
                "stock_name": ev["stock_name"],
                "surge_date": ev["surge_date"],
                "dip_date": dates[t],
                "dip_type": dip_type,
                "outcome": outcome,
                "ret_5d_from_dip": round(ret_5d, 4) if ret_5d is not None else None,
                "days_to_dip": t - pos,
                "dip_depth_vs_peak": round(c[t] / peak - 1, 4),
                "retention_at_dip": round((c[t] - pre_close) / (surge_close - pre_close), 3),
                "dip_vol_vs_normal": round(vol[t] / vol_base, 2) if vol_base > 0 else None,
                "dip_vol_vs_surge": round(vol[t] / vol[pos], 2) if vol[pos] > 0 else None,
                "redetect_before_dip": len(later) > 0,
                "category": ev["category"],
                "continuation_type": ev["continuation_type"],
                "theme_heat": ev["theme_heat"],
                "source": ev["source"],
                "surge_ret": round(surge_close / pre_close - 1, 4),
                "market_cap": caps.get(code),
            }

        dip25 = None
        for t in range(pos + 1, end + 1):
            if pd.isna(ma25[t]):
                continue
            if low[t] <= ma25[t] * TOUCH_TOL:
                dip25 = t
                break
        if dip25 is not None:
            recs.append(dip_record(dip25, "初押し(25日線)"))
            # 2番押し: 25日線タッチ後、さらに75日線までタッチしたか
            for t in range(dip25, end + 1):
                if pd.isna(ma75[t]):
                    continue
                if low[t] <= ma75[t] * TOUCH_TOL:
                    recs.append(dip_record(t, "2番押し(75日線)"))
                    break
        else:
            # 押し目が来ないまま走り続けた銘柄も記録(比較用)
            recs.append({
                "stock_code": code, "stock_name": ev["stock_name"],
                "surge_date": ev["surge_date"], "dip_date": None,
                "dip_type": "押し目なし(60日走行)", "outcome": None,
                "ret_5d_from_dip": None, "days_to_dip": None,
                "dip_depth_vs_peak": None, "retention_at_dip": None,
                "dip_vol_vs_normal": None, "dip_vol_vs_surge": None,
                "redetect_before_dip": None,
                "category": ev["category"],
                "continuation_type": ev["continuation_type"],
                "theme_heat": ev["theme_heat"], "source": ev["source"],
                "surge_ret": round(surge_close / pre_close - 1, 4),
                "market_cap": caps.get(code),
            })

    out = pd.DataFrame(recs)
    print(f"対象(全MA上+25日線上向き): {n_qualified}件 / MA計算不能(上場浅い等): {n_no_ma}件")
    if not out.empty:
        print("押し目イベント内訳:", dict(out["dip_type"].value_counts()))
    return out


def rebound_table(d: pd.DataFrame, key: str) -> str:
    sub = d.dropna(subset=["outcome"])
    rows = []
    for g, gd in sub.groupby(key, dropna=False):
        n = len(gd)
        if n < 5:
            continue
        rows.append({
            key: g, "n": n,
            "反発%": round(100 * (gd["outcome"] == "反発").mean(), 1),
            "突き抜け%": round(100 * (gd["outcome"] == "突き抜け").mean(), 1),
            "5日リターン中央値%": round(100 * gd["ret_5d_from_dip"].median(), 1),
        })
    t = pd.DataFrame(rows)
    if t.empty:
        return "(該当なし)\n"
    return soa.md_table(t.sort_values("反発%", ascending=False))


def feature_medians(d: pd.DataFrame) -> str:
    """反発組と突き抜け組で押し目固有の数値特徴の中央値を比較する。"""
    sub = d.dropna(subset=["outcome"])
    feats = [
        ("days_to_dip", "押しまでの日数"),
        ("dip_depth_vs_peak", "ピークからの下落率"),
        ("retention_at_dip", "押し時点の保持率"),
        ("dip_vol_vs_normal", "押し日出来高/平常"),
        ("dip_vol_vs_surge", "押し日出来高/急騰日"),
        ("surge_ret", "急騰日リターン"),
        ("theme_heat", "テーマ過熱度"),
    ]
    rows = []
    for col, label in feats:
        r = sub[sub["outcome"] == "反発"][col].dropna()
        b = sub[sub["outcome"] == "突き抜け"][col].dropna()
        if len(r) < 10 or len(b) < 10:
            continue
        rows.append({
            "特徴量": label,
            "反発組の中央値": round(r.median(), 3),
            "突き抜け組の中央値": round(b.median(), 3),
        })
    return soa.md_table(pd.DataFrame(rows))


def build_report(d: pd.DataFrame) -> str:
    from datetime import datetime
    d25 = d[d["dip_type"] == "初押し(25日線)"]
    d75 = d[d["dip_type"] == "2番押し(75日線)"]
    no_dip = d[d["dip_type"] == "押し目なし(60日走行)"]

    L = []
    L.append("# 押し目反発分析レポート\n")
    L.append(f"生成: {datetime.now(soa.JST).strftime('%Y-%m-%d %H:%M')} JST\n")
    L.append("- 定義: 急騰日に5/25/75日線の全てより上&25日線上向きの銘柄が対象。"
             "初押し=安値が25日線に初タッチ、2番押し=75日線までタッチ。"
             "タッチ日終値より5営業日後終値が上なら「反発」、下なら「突き抜け」\n")
    L.append(f"- 初押し(25日線)イベント: {len(d25)}件 (うち判定可能 {d25['outcome'].notna().sum()}件)")
    L.append(f"- 2番押し(75日線)イベント: {len(d75)}件 (うち判定可能 {d75['outcome'].notna().sum()}件)")
    L.append(f"- 60営業日押し目なしで走行: {len(no_dip)}件\n")

    for name, dd in [("初押し(25日線)", d25), ("2番押し(75日線)", d75)]:
        sub = dd.dropna(subset=["outcome"])
        if len(sub) == 0:
            continue
        L.append(f"## {name}: 全体\n")
        k = (sub["outcome"] == "反発").sum()
        L.append(f"- 反発: {k}件 ({100*k/len(sub):.1f}%) / "
                 f"突き抜け: {len(sub)-k}件 ({100*(len(sub)-k)/len(sub):.1f}%)")
        L.append(f"- タッチ日終値からの5日リターン中央値: "
                 f"{100*sub['ret_5d_from_dip'].median():+.1f}%\n")

    L.append("## 初押し(25日線): 何が反発と突き抜けを分けるか\n")
    L.append("### 数値特徴の中央値比較\n")
    L.append(feature_medians(d25))
    L.append("### カテゴリ別\n")
    L.append(rebound_table(d25, "category"))
    L.append("### 継続性タイプ別\n")
    L.append(rebound_table(d25, "continuation_type"))
    L.append("### 押し目までに後続材料(再検知)が出ていたか\n")
    L.append(rebound_table(d25, "redetect_before_dip"))
    L.append("### 検知ソース別\n")
    L.append(rebound_table(d25, "source"))

    d25v = d25.dropna(subset=["outcome", "dip_vol_vs_normal"]).copy()
    if len(d25v) > 40:
        d25v["押し日出来高"] = pd.qcut(d25v["dip_vol_vs_normal"], q=3,
                                       labels=["細る", "普通", "膨らむ"], duplicates="drop")
        L.append("### 押し日の出来高(平常比)3分位 — 「出来高が細る押しは健全」の検証\n")
        L.append(rebound_table(d25v, "押し日出来高"))

    d25r = d25.dropna(subset=["outcome", "retention_at_dip"]).copy()
    if len(d25r) > 40:
        d25r["押し時点の保持率"] = pd.cut(
            d25r["retention_at_dip"], bins=[-100, 0, 0.5, 1.0, 100],
            labels=["マイ転", "0〜0.5", "0.5〜1", "1以上"])
        L.append("### 押し時点の保持率別(急騰の上昇分をどれだけ残して押しに入ったか)\n")
        L.append(rebound_table(d25r, "押し時点の保持率"))

    L.append("## 押し目なしで走行した組(参考)\n")
    L.append("60営業日以内に一度も25日線に触れなかった=最強トレンド組の内訳:\n")
    nd = no_dip.groupby("category").size().sort_values(ascending=False)
    for cat, n in nd.items():
        L.append(f"- {cat}: {n}件")
    L.append("")

    L.append("## 注意事項\n")
    L.append("- 反発/突き抜け判定は5営業日後の終値1点のみ。だましや遅い反発は「突き抜け」側に入る。")
    L.append("- 対象条件(全MA上+25日線上向き)を満たすのは急騰銘柄の一部のみで、"
             "そもそも生存バイアスのかかった母集団である点に注意。")
    L.append("- 検知期間約4ヶ月のin-sample。セルのnが小さい行は参考程度に。")
    return "\n".join(L)


def main():
    df = soa.fetch_events(None)
    df = soa.classify_reasons(df, use_llm=True)  # 既存キャッシュを利用(API課金なし)
    df = soa.add_followup_signals(df)

    codes = sorted(df["stock_code"].unique())
    prices = soa.load_price_data(codes)
    caps = soa.load_market_caps(codes)

    dips = find_dips(df, prices, caps)
    if dips.empty:
        print("押し目イベントが検出できませんでした")
        sys.exit(1)

    csv_path = soa.OUT_DIR / "dip_events.csv"
    dips.to_csv(csv_path, index=False, encoding="utf-8-sig")
    md_path = soa.OUT_DIR / "dip_rebound_report.md"
    md_path.write_text(build_report(dips), encoding="utf-8")
    print(f"\n完了:\n  {csv_path}\n  {md_path}")


if __name__ == "__main__":
    main()
