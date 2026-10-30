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

import numpy as np
import pandas as pd

import surge_outcome_analysis as soa
import dip_screener
import dip_outcome

# 急騰検知イベントから押し目レコードへ引き継ぐリッチフィールド由来の列。
# reason(20字程度の要約)しか使っていなかったので、事業内容・強材料ポイント・
# 関連銘柄・投稿本文の数値を予測特徴の候補として持ち込む。
RICH_COLS = ["n_backgrounds", "len_backgrounds", "backgrounds_text",
             "n_keywords", "business", "len_business",
             "n_related", "n_related_real",
             "post_surge_pct", "post_vol_ratio", "post_turnover_oku"]

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

        def shape_feats(t: int) -> dict:
            """急騰日→押し目日の値動き・出来高の「形」。すべて押し目日までの
            情報だけで作るので先読みにならない。"""
            seg_v = vol[pos:t + 1]
            seg_c = c[pos:t + 1]
            out: dict = {}
            # 出来高の減衰: 押し目日の出来高が急騰後の平均に比べてどれだけ細ったか。
            # 「出来高が細る押しは健全」という定説の検証用。
            out["vol_decay"] = (round(float(vol[t] / seg_v.mean()), 3)
                                if len(seg_v) and seg_v.mean() > 0 else None)
            # 押し目直前5日の出来高トレンド(負なら細りながら押している)
            if t - 5 >= pos:
                a, b = vol[t - 5:t - 2].mean(), vol[t - 2:t + 1].mean()
                out["vol_trend5"] = round(float(b / a - 1), 3) if a > 0 else None
            else:
                out["vol_trend5"] = None
            # 陰線比率: 押しの過程がじりじり下げか一気の急落か
            if len(seg_c) > 1:
                diffs = np.diff(seg_c)
                out["down_day_ratio"] = round(float((diffs < 0).mean()), 3)
            else:
                out["down_day_ratio"] = None
            # 日中変動幅の平均(ボラティリティ)。高いほど-8%に触れやすい。
            hi_seg = px["High"].values[pos:t + 1]
            lo_seg = px["Low"].values[pos:t + 1]
            with np.errstate(divide="ignore", invalid="ignore"):
                rng_pct = (hi_seg - lo_seg) / np.where(seg_c > 0, seg_c, np.nan)
            out["atr_pct"] = (round(float(np.nanmean(rng_pct)), 4)
                              if len(rng_pct) else None)
            # ピークから押し目までの日数(急落か時間をかけた調整か)
            out["days_from_peak"] = int(t - (pos + int(np.argmax(seg_c))))
            return out

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
                "followup_score": ev["followup_score"],
                "unresolved": ev["unresolved"],
                "reason": ev["reason"],
                "theme_heat": ev["theme_heat"],
                "source": ev["source"],
                "surge_ret": round(surge_close / pre_close - 1, 4),
                "market_cap": caps.get(code),
                # リッチフィールド由来。reason の要約では落ちていた情報。
                **{c: ev.get(c) for c in RICH_COLS},
                # 値動き・出来高の「形」(押し目日までの情報のみ)
                **shape_feats(t),
                # 新定義(翌営業日始値エントリー / -8%損切り / 10営業日)の判定。
                # 旧 outcome 列は互換のため残す。定義は dip_outcome.py に集約。
                **dip_outcome.judge(px, t),
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
                "followup_score": ev["followup_score"],
                "unresolved": ev["unresolved"],
                "reason": ev["reason"],
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
    # 同一の押し目に複数の急騰検知がぶら下がることがあるので、集計は
    # 1トレード1行に潰してから行う(そうしないと同じ結果を重複計上する)
    d25_raw = d[d["dip_type"] == "初押し(25日線)"]
    d25 = dip_outcome.unique_trades(d25_raw)
    d75 = d[d["dip_type"] == "2番押し(75日線)"]
    no_dip = d[d["dip_type"] == "押し目なし(60日走行)"]

    L = []
    L.append("# 押し目反発分析レポート\n")
    L.append(f"生成: {datetime.now(soa.JST).strftime('%Y-%m-%d %H:%M')} JST\n")
    L.append("- 対象: 急騰日に5/25/75日線の全てより上&25日線上向きの銘柄。"
             "初押し=安値が25日線に初タッチ、2番押し=75日線までタッチ\n")
    L.append(f"- 成否判定(主): タッチ日の**翌営業日始値**で買い、"
             f"{dip_outcome.HORIZON}営業日以内に日中安値が"
             f"-{dip_outcome.STOP_PCT*100:.0f}%に触れたらその時点で「下落」。"
             f"触れなければ{dip_outcome.HORIZON}営業日後の終値で上下を判定。"
             "上バリアは置かない(利確を急がない運用に合わせる)\n")
    L.append("- 成否判定(旧): タッチ日終値より5営業日後終値が上なら「反発」。"
             "過去レポートとの連続性のために併記\n")
    L.append(f"- 初押し(25日線): {len(d25)}件 = ユニークな取引機会"
             f"(検知行 {len(d25_raw)}件を(銘柄,押し目日)で1件に統合)"
             f" / うち新定義で判定済み {d25['outcome_v2'].notna().sum()}件")
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

    L.append("### 続報期待スコア帯別(閾値に依存しない生の並び)\n")
    d25s = d25.dropna(subset=["outcome", "followup_score"]).copy()
    d25s["スコア帯"] = pd.cut(d25s["followup_score"], bins=[-99, -1, 0, 1, 2, 3, 99],
                              labels=["<0", "0", "1", "2", "3", "4+"])
    L.append(rebound_table(d25s, "スコア帯"))
    L.append("### 続報期待スコア別 × 深押し(保持率<0.5) — 主仮説の追跡\n")
    deep = d25s[d25s["retention_at_dip"] < 0.5].copy()
    if len(deep) >= 10:
        deep["スコア"] = deep["followup_score"].apply(
            lambda s: "2以上" if s >= 2 else "1以下")
        L.append(rebound_table(deep, "スコア"))
        L.append("> どのスコア帯の深押しが反発しやすいかを見る。閾値2以上が現状の主仮説。\n")
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

    L.append("## スクリーニング階層(S/A/B)の検証\n")
    L.append("段階1 バイオ/一過性を足切り → 段階2 具体材料 AND 未確定(=核) → "
             "段階3 需給ゲート(テーマ非孤立・小型除外)\n")

    L.append("### 実運用定義(主): 翌営業日始値エントリー / -8%損切り / 10営業日\n")
    L.append(f"損切りは日中安値が -{dip_outcome.STOP_PCT*100:.0f}% にタッチした時点で"
             "打ち切り(逆指値)。上バリアは置かず、損切りに当たらなければ"
             f"{dip_outcome.HORIZON}営業日後の終値で上下を判定する。\n")
    L.append(soa.md_table(dip_outcome.validation_table_v2(d25)))
    L.append("> 平均リターン%は損切り分を -8% 固定で計上した値。これが実際に取れる"
             "期待値に最も近い。旧定義(下記)より数字が厳しく出るのが正常。\n")

    L.append("### 前向き検証: ルール凍結日以降に発生した押し目のみ\n")
    L.append(f"S/A/B の条件は {dip_outcome.FREEZE_DATE:%Y-%m-%d} に多数の組み合わせから"
             "選ばれている。したがって out-of-sample と呼べるのは押し目日が"
             "それ以降のものだけ。**性能を語れるのはこの表だけ**。\n")
    fz = dip_outcome.validation_since_freeze(d25)
    if fz.empty:
        L.append(f"判定済み 0件。押し目日が凍結日以降で、かつエントリー翌日から"
                 f"{dip_outcome.HORIZON}営業日が経過したイベントがまだ無い。"
                 "ここが積み上がるまで階層の優劣は未検証。\n")
    else:
        L.append(soa.md_table(fz))
    L.append("")

    L.append("### 台帳突合: その日 Slack に送った時点の階層で評価\n")
    L.append("上の表は「今の基準で過去を再判定」した数字。B級は時価総額の下位1/3分位を"
             "毎回引き直すためラベルが遡って揺れる。実際に送った判断の答え合わせは"
             "日次スナップショット(analysis_out/ledger/)との突合で行う。\n")
    sc = dip_outcome.ledger_scorecard(soa.OUT_DIR, d25)
    if sc.empty:
        L.append("突合できる履歴がまだ無い(台帳の蓄積待ち)。\n")
    else:
        L.append(soa.md_table(sc))

    L.append("## 急騰理由からの予測: 基準の学習と答え合わせ\n")
    L.append("目的は当てることではなく、**予測基準が本当にベース率を超えているかを"
             "自己欺瞞なしに測り続ける**こと。下の数字は全てウォークフォワード"
             "(その押し目日より前に結果が確定していたデータのみで学習)なので"
             "out-of-sample。in-sample の当てはまりは一切使わない。\n")

    import reason_model
    cmp = reason_model.compare_sets(d25)
    L.append("### 特徴セットの比較\n")
    L.append("skill% は「常にベース率を答える」対照に対する Brier スコアの改善率。"
             "プラスでなければ予測の価値は無い。95%信頼区間が0を跨いでいる間は"
             "「勝てているとは言えない」と読む。\n")
    L.append(soa.md_table(cmp))
    champ, why = reason_model.choose_champion(cmp)
    L.append(f"**現在のチャンピオン: `{champ}`** — {why}\n")

    L.append("### 中身の全文判断(事業内容・強材料ポイント・関連銘柄を読ませる)\n")
    L.append("LLM に材料の中身を通して読ませ、押し目の成否を直接判断させたもの。"
             "**結果データを一切見せていないので当てはめが無く、判定済み全件が"
             "そのまま out-of-sample**(ロジスティック回帰の候補が126件しか"
             "評価できないのに対しこちらは235件)。プロンプトは結果に合わせて"
             "調整していない(調整は人手による過学習の再導入になるため)。\n")
    ev = reason_model.evaluate_fixed(d25)
    if not ev.empty:
        L.append(soa.md_table(ev))
    rt = reason_model.llm_rank_table(d25)
    if not rt.empty:
        L.append("LLM が付けたランク別の実績。中身を読んだ順位付けが機能している"
                 "なら S > A > B > C の順に上昇率が並ぶはず。\n")
        L.append(soa.md_table(rt))
    fc = reason_model.fixed_calibration(d25)
    if not fc.empty:
        L.append("確率のキャリブレーション。\n")
        L.append(soa.md_table(fc))

    import market_context
    rg = market_context.regime_table(d25)
    if not rg.empty:
        L.append("### 地合い別の実績\n")
        L.append("月ごとの上昇率は日経の方向でほぼ説明できた(日経がマイナスだった"
                 "3月・7月が上昇率の最低2つ)。ただし地合いは同じ月の全銘柄に同時に"
                 "効くので、イベント235件は独立ではなく実質サンプルは月数に近い。"
                 "そのため昇格判定は週ブロック・ブートストラップで行う。\n")
        L.append(soa.md_table(rg))

    stab = reason_model.stability_check(d25)
    if not stab.empty:
        L.append("### 候補の期間安定性(騙されないための検査)\n")
        L.append("前半と後半で差の符号が反転する候補は、全体で見た差がどれだけ"
                 "大きくても採用してはいけない。実例: 「強材料に定量的な数字あり」は"
                 "全体で上昇率+22pt(z=1.97)だったが、期間で割ると"
                 "5月-7pt / 6月+8pt / 7月+61pt で、実体は7月が悪い月だったこと"
                 "(数字なし14%)と、数字入り材料が最近に偏っていること"
                 "(3〜4月は0件)の副産物だった。\n")
        L.append(soa.md_table(stab))

    import reason_axes
    ab = reason_axes.axis_breakdown(d25)
    if not ab.empty:
        L.append("### 理由の細分化(5軸)が効くかの診断\n")
        L.append("理由テキストは235件中191種がユニークなので、1本の細かいラベルは"
                 "暗記になる。そのため直交する5軸に分解し、縮小付きターゲット"
                 "エンコーディングで各軸1列に潰して使っている(axes_te)。\n")
        L.append("下表の読み方: 最大水準が全体の大半を占めていれば、"
                 "細分化しても情報は増えていない。「上昇率の幅」が大きくても"
                 "それを作っている水準の n が小さければノイズ。\n")
        L.append(soa.md_table(ab))

    for s in ("reason_min", "price_min", "axes_te"):
        cal = reason_model.calibration(reason_model.walk_forward(d25, s))
        if cal.empty:
            continue
        L.append(f"### キャリブレーション: {s}\n")
        L.append("予測が高い帯ほど実測上昇率も高くなっていなければ、順位付けとして"
                 "機能していない。予測平均%と実測上昇率%の乖離が確率の当たり具合。\n")
        L.append(soa.md_table(cal))

    L.append("### 台帳に刻んだ予測の答え合わせ\n")
    L.append("上の比較は毎回学習し直した数字。こちらは *予測を出した当時のモデル* の"
             "成績で、後出しの調整が入らない唯一の本物の答え合わせ。\n")
    ps = dip_outcome.prediction_scorecard(soa.OUT_DIR, d25)
    L.append(soa.md_table(ps) if not ps.empty
             else "突合できる予測履歴がまだ無い(台帳の蓄積待ち)。\n")

    st = reason_model.load_state(soa.OUT_DIR)
    hist = st.get("history", [])
    if hist:
        L.append("### チャンピオンの変遷\n")
        L.append("基準を変えた記録。これが無いと「性能が上がった」のか"
                 "「基準を変え続けただけ」なのか後から判別できない。\n")
        for h in hist[-10:]:
            mark = " ←変更" if h.get("champion") != h.get("prev") else ""
            L.append(f"- {h['date']}: `{h.get('champion')}`{mark} — {h.get('reason','')}")
        L.append("")

    L.append("### 旧定義(タッチ日終値 → 5営業日後終値)\n")
    L.append("過去のレポートとの連続性のために残している。引け後に届く通知で引け値を"
             "買う前提かつ途中のドローダウンを無視するので、数字は実運用より甘い。\n")
    L.append(soa.md_table(dip_screener.validation_table(d25)))
    L.append("> 「具体材料はあるが確定済み(材料消費)」は最悪クラスなので核から外している。"
             "多数の組み合わせを試した中の最良構成のため上振れの可能性あり、前向き検証が必要。\n")

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

    # 急騰理由を5つの直交軸に分解する(キャッシュ優先、未知の理由だけ LLM)。
    # 1本の細かいラベルは191/235がユニークで暗記になるため、低カーディナリティな
    # 軸に分けて掛け合わせで細かさを持たせる。
    import reason_axes
    from config import ANTHROPIC_API_KEY
    dips = reason_axes.add_axes(dips, soa.CACHE_DIR, ANTHROPIC_API_KEY)
    print(reason_axes.coverage(dips))
    # 強材料ポイント本文から「材料の硬さ」(定量的な数字の有無・推測語の密度)を作る
    dips = reason_axes.add_material_features(dips)

    # 中身(事業内容・強材料ポイント・キーワード・関連銘柄)を全文読ませて
    # 押し目の成否を直接判断させる。結果データを見せないので当てはめが無く、
    # 判定済み全件がそのまま out-of-sample になる。
    import reason_llm
    dips = reason_llm.add_judgement(dips, soa.CACHE_DIR, ANTHROPIC_API_KEY)
    print(reason_llm.coverage(dips))

    # 地合い(押し目日までの指数の動き)と相対強弱。月ごとの上昇率が日経の
    # 方向でほぼ説明できたため、最大の欠落変数だった可能性が高い。
    import market_context
    dips = market_context.add_context(dips, market_context.load_indexes(soa.CACHE_DIR))

    dips = dip_screener.add_tiers(dips)
    print("階層内訳:", dict(dips["tier"].value_counts()))
    # Slack が送るのは初押しのみ。台帳は「実際に送った内容」でなければ
    # 答え合わせにならないので、ここも初押しに絞って揃える。
    d25 = dip_outcome.unique_trades(dips[dips["dip_type"] == "初押し(25日線)"])
    live = dip_screener.live_candidates(d25)
    if not live.empty:
        print(f"直近の押し目候補(結果未確定): {len(live)}件 "
              f"S={sum(live['tier']=='S')} A={sum(live['tier']=='A')} B={sum(live['tier']=='B')}")

    # 予測基準の更新。ウォークフォワード成績で候補を比較し、信頼区間が0を
    # 跨がない候補が現れたときだけ乗り換える(なければ baseline のまま)。
    import reason_model
    cmp = reason_model.compare_sets(d25)
    champ, why = reason_model.choose_champion(cmp)
    reason_model.save_state(soa.OUT_DIR, champ, why, cmp)
    print(f"予測モデル: champion={champ} — {why}")

    # 予測確率を候補に付ける。送信時点の予測として台帳に凍結される。
    pred = reason_model.predict_pending(d25, champ)
    if not live.empty and not pred.empty:
        cols = ["stock_code", "dip_date", "prob_up", "model_set"]
        live = live.merge(pred[cols], on=["stock_code", "dip_date"], how="left")

    # 送った時点のラベルを日付付きで凍結保存する(dip_events.csv は毎回上書きなので
    # これが無いと過去の判断を後から答え合わせできない)
    snap = dip_outcome.save_snapshot(live, soa.OUT_DIR)
    print(f"台帳スナップショット: {snap} ({len(live)}件)")
    print(dip_outcome.freeze_summary_line(d25))

    csv_path = soa.OUT_DIR / "dip_events.csv"
    dips.to_csv(csv_path, index=False, encoding="utf-8-sig")
    md_path = soa.OUT_DIR / "dip_rebound_report.md"
    md_path.write_text(build_report(dips), encoding="utf-8")
    print(f"\n完了:\n  {csv_path}\n  {md_path}")


if __name__ == "__main__":
    main()
