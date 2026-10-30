"""当日の初押し合格銘柄と、押し目前の監視候補をSlackに通知する。

共通①③④⑤の3/4以上＋(MACDまたは出来高)。本文とチャートは同じ候補を使う。
run_surge_analysis.batから実行。--dry-runでは保存・Slack送信を行わない。
"""
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OUT_DIR = Path(__file__).parent / "analysis_out"
JST = timezone(timedelta(hours=9))
WATCH_MIN_SCORE = 2  # ①③④のうち2つ。⑤が加われば共通3/4に届く監視段階。


def _fmt_row(r, dipped: bool) -> list:
    """1銘柄ぶんの表示行。"""
    out = []
    code, name = r["stock_code"], r["stock_name"]
    if dipped:
        when = pd.to_datetime(r["dip_date"]).strftime("%m/%d")
        head = f"  ・{code} {name}｜押し{when}"
    else:
        d = r.get("dist_to_ma25")
        head = (f"  ・{code} {name}｜押し目前・監視｜25日線まで{100*float(d):.1f}%"
                f"｜確認{pd.to_datetime(r['as_of']):%m/%d}")
    atr = r.get("atr_pct_dip")
    if pd.notna(atr):
        head += f"｜14日値幅率{100*float(atr):.2f}%"
    tp = r.get("theme_prior30")
    if pd.notna(tp):
        head += f"｜テーマ先行{int(tp)}件"
    mh, vr = r.get("macd_hist_pct"), r.get("volume_ratio")
    if pd.notna(mh):
        head += f"｜MACD差{float(mh):+.3f}%"
    if pd.notna(vr):
        head += f"｜初動出来高{float(vr):.2f}倍"
    out.append(head)
    out.append(f"      共通: {r['該当条件']}")
    if dipped:
        out.append(f"      追加条件: {r['screening_route']}")
    else:
        labels = [label for label, key in [("MACD", "cond_macd"), ("出来高", "cond_volume")]
                  if bool(r.get(key, False))]
        out.append(f"      仮判定: 共通{int(r['score'])}/3、追加 {'・'.join(labels)}。⑤は初押し日に判定。")
    rsn = r.get("reason")
    if isinstance(rsn, str) and rsn.strip():
        out.append(f"      {rsn[:40]}")
    return out


def _route_block(df, picks: list | None = None) -> list:
    """MACDと出来高のみの追加群に分け、同じ初押しを重複表示しない。"""
    lines = ["", "━━━ *今日の初押し・条件合格* ━━━",
             "_共通①③④⑤の3/4以上＋(MACDまたは出来高)。両方に合格した銘柄はMACD群に1回だけ掲載。_"]
    if df is None or df.empty:
        return lines + ["  該当なし"]
    groups = [("MACDルート", df[df["route_macd"]]),
              ("出来高だけで追加", df[df["route_volume"] & ~df["route_macd"]])]
    for label, group in groups:
        lines += ["", f"*【{label}】{len(group)}件*"]
        if group.empty:
            lines.append("  該当なし")
        for _, row in group.sort_values(["score", "dip_date", "stock_code"], ascending=[False, False, True]).iterrows():
            lines += _fmt_row(row, True)
            if picks is not None:
                picks.append((row, True, f"{label} 共通{int(row['score'])}/4"))
    return lines


def _save_filter_tables(appr, dipped) -> None:
    """運用CSVと、新ルートを判定した時点の台帳を保存する。"""
    from dip_routes import RULE_VERSION
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for table, filename in [(appr, "dip_filter_approaching.csv"), (dipped, "dip_filter_result.csv")]:
        table.to_csv(OUT_DIR / filename, index=False, encoding="utf-8-sig")
    if dipped.empty:
        return
    observed = datetime.now(JST)
    # 既存のS/A/B台帳とは分け、同日初回の判定を再計算で上書きしない。
    ledger = OUT_DIR / "route_ledger"
    ledger.mkdir(parents=True, exist_ok=True)
    path = ledger / f"{observed:%Y%m%d}_{RULE_VERSION}.csv"
    if path.exists():
        return
    snapshot = dipped.copy()
    snapshot["observed_at"] = observed.isoformat()
    dip_dates = pd.to_datetime(snapshot["dip_date"]).dt.normalize()
    # 後日初めて拾った過去の押し目を、翌日始値より前の判定として扱わない。
    snapshot["prospective_eligible"] = (
        dip_dates.ge(pd.Timestamp("2026-09-17"))
        & dip_dates.eq(pd.Timestamp(observed.date())))
    snapshot.to_csv(path, index=False, encoding="utf-8-sig")


def _select_notifications(appr, dipped, today):
    """日付を送信日と照合し、確定合格と未タッチ監視を分ける。"""
    import dip_filter
    today = pd.Timestamp(today).normalize()
    confirmed = dipped.copy()
    if not confirmed.empty:
        dates = pd.to_datetime(confirmed["dip_date"], errors="coerce").dt.normalize()
        confirmed = confirmed[dates.eq(today)]
    touched_today = set(confirmed["stock_code"].astype(str)) if not confirmed.empty else set()
    if not confirmed.empty:
        confirmed = confirmed[confirmed["route_selected"].fillna(False)].drop_duplicates(
            ["stock_code", "dip_date"])

    watch = appr.copy()
    if not watch.empty:
        dates = pd.to_datetime(watch["as_of"], errors="coerce").dt.normalize()
        distance = pd.to_numeric(watch["dist_to_ma25"], errors="coerce")
        enough = pd.to_numeric(watch["score"], errors="coerce").ge(WATCH_MIN_SCORE)
        extra = watch["cond_macd"].fillna(False) | watch["cond_volume"].fillna(False)
        watch = watch[dates.eq(today) & distance.gt(0) & distance.le(dip_filter.APPROACH_MAX)
                      & enough & extra & ~watch["stock_code"].astype(str).isin(touched_today)]
        watch = watch.sort_values(["score", "dist_to_ma25"], ascending=[False, True]).drop_duplicates("stock_code")
    return confirmed, watch


def _watch_block(df, picks: list | None = None) -> list:
    lines = ["", "━━━ *押し目前の監視候補・未確定* ━━━",
             "_今日の安値が25日線の上4%以内・未タッチ。①③④の2/3以上＋現在のMACDまたは出来高が合格。_",
             "_⑤と最終的な3/4判定は初押し当日に確認。現時点では買い条件の確定ではありません。_"]
    if df is None or df.empty:
        return lines + ["  該当なし"]
    lines.append(f"*【事前監視】{len(df)}件*")
    for _, row in df.iterrows():
        lines += _fmt_row(row, False)
        if picks is not None:
            picks.append((row, False, f"押し目前・監視（未確定） 共通{int(row['score'])}/3"))
    return lines


def _score_sections(picks: list | None = None, *, persist: bool = False) -> list:
    """当日の初押し合格と当日足の監視候補だけを本文・チャートに渡す。"""
    try:
        import dip_filter
        ev = dip_filter.add_theme_prior(dip_filter.fetch_events())
        dipped = dip_filter.build_table("dipped", ev=ev, use_llm=False)
        appr = dip_filter.build_table("approaching", ev=ev, use_llm=False)
        if persist:
            _save_filter_tables(appr, dipped)
        today = pd.Timestamp(datetime.now(JST).date())
        confirmed, watch = _select_notifications(appr, dipped, today)
    except Exception as e:
        return ["", f"_[WARN] 共通条件・MACD/出来高の算出に失敗: {e}_"]
    return _route_block(confirmed, picks) + _watch_block(watch, picks)


def build_summary(picks: list | None = None, *, persist: bool = False) -> str:
    """当日の初押しと押し目前監視を、本文・チャートで共通にする。"""
    from dip_routes import MACD_HIST_PCT_MAX, INITIAL3_VOLUME_MAX
    today = datetime.now(JST).strftime("%Y-%m-%d (%a)")
    lines = [f"*【日次】急騰押し目スクリーニング  {today}*",
             "*当日の初押し合格 ＋ 押し目前の事前監視*",
             "初押し当日の採用条件: 共通4条件中3つ以上 ＋ (MACDまたは初動出来高)。",
             "共通: ①テーマ先行3～5件 ③14日平均値幅率<4% "
             "④25/75日線乖離2.6～8% ⑤安値の下抜け<0.5%。旧②は採点対象外。",
             f"追加M: MACDヒストグラム/終値×100 ≤ 約{MACD_HIST_PCT_MAX:.5f}%（12・26・9）。",
             f"追加V: 急騰当日から3営業日の平均出来高/急騰前20営業日の中央値 ≤ 約{INITIAL3_VOLUME_MAX:.5f}倍。",
             "出来高3日分が初押し時点で未完成ならVは不合格。Mでの採用は可能。",
             "引け後の判定。初押し済みは本日分だけを掲載し、押し目前は未確定の監視欄に分けます。"]
    lines += _score_sections(picks, persist=persist)
    return "\n".join(lines)


MAX_CHARTS = 30       # 暴走防止。通常は15〜20枚に収まる


def _chart_caption(row, dipped: bool, heading: str) -> str:
    """スレッドに出す1行キャプション。本文と同じ情報を最小限だけ繰り返す。"""
    head = f"*{row['stock_code']} {row['stock_name']}*｜{heading}"
    if dipped:
        head += f"｜押し{pd.to_datetime(row['dip_date']):%m/%d}"
    else:
        d = row.get("dist_to_ma25")
        if pd.notna(d):
            head += f"｜25日線まで{100 * float(d):.1f}%"
    return f"{head}\n{row['該当条件']}"


def _post_charts(channel_id: str, thread_ts: str, picks: list) -> None:
    """候補のチャートを親メッセージのスレッドに1銘柄1枚ぶら下げる。

    チャートで落ちても本文は既に投稿済みなので、ここでの失敗は握り潰す。
    """
    from slack_notifier import _slack_files_upload_v2
    import dip_chart

    sent = 0
    for row, dipped, heading in picks[:MAX_CHARTS]:
        png = dip_chart.render_safely(row, dipped)
        if not png:
            continue
        # 1枚ごとに独立して送るので、失敗しても他の銘柄は流す。
        # アップロード先(CloudFront)がたまに詰まるので1回だけ入れ直す。
        for attempt in (1, 2):
            try:
                _slack_files_upload_v2(
                    channel_id=channel_id, image_bytes=png,
                    filename=f"{row['stock_code']}_{'dip' if dipped else 'appr'}.png",
                    initial_comment=_chart_caption(row, dipped, heading),
                    thread_ts=thread_ts)
                sent += 1
                break
            except Exception as e:
                print(f"  [chart] {row['stock_code']} 送信失敗({attempt}回目): {e}")
    extra = len(picks) - MAX_CHARTS
    print(f"[OK] チャート {sent}/{len(picks)}枚 をスレッドに送信しました"
          + (f" (上限{MAX_CHARTS}枚のため{extra}件は省略)" if extra > 0 else ""))


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="本文のみ確認し、保存・Slack送信しない")
    args = parser.parse_args()

    picks: list = []
    text = build_summary(picks, persist=not args.dry_run)
    print(text)
    if args.dry_run:
        return

    from slack_notifier import _get_channel_id, _slack_api_post
    from config import SLACK_BOT_TOKEN, SLACK_CHANNEL

    if not SLACK_BOT_TOKEN:
        print("[WARN] SLACK_BOT_TOKEN 未設定のため Slack 送信をスキップしました")
        return

    try:
        channel_id = _get_channel_id()
        result = _slack_api_post("chat.postMessage",
                                 {"channel": channel_id, "text": text})
        if not result.get("ok"):
            print(f"[ERROR] Slack送信失敗: {result.get('error')}")
            return
        print(f"[OK] Slack #{SLACK_CHANNEL} に送信しました")
    except Exception as e:
        print(f"[ERROR] Slack送信で例外: {e}")
        return

    ts = result.get("ts")
    if picks and ts:
        try:
            _post_charts(channel_id, ts, picks)
        except Exception as e:
            print(f"[WARN] チャート送信で例外(本文は送信済み): {e}")


if __name__ == "__main__":
    main()
