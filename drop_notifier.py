# -*- coding: utf-8 -*-
"""急落検知の Slack 通知

急騰側(slack_notifier.py)は「Xに投稿していいか」を聞く承認フローだが、急落側は
Xに投稿しないので承認は要らない。検知した事実と drop_analyzer.py の判定を
一方通行で流すだけ。

呼ばれる場所:
  - drop_detector.watch_tick()  … ザラ場中、新規の急落を拾った直後
  - drop_detector.main() --slack … 引け後バッチから手動で流すとき

通知先は SLACK_DROP_CHANNEL(未設定なら SLACK_CHANNEL)。
監視ループを絶対に止めないため、この入口関数 notify_drops() は例外を投げない。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

JST = timezone(timedelta(hours=9))

# 1回の通知に載せる最大件数。これを超えた分は件数だけ添える。
MAX_ROWS = 10
# 各銘柄の「根拠」原文の表示上限。Slackが読めなくなるので切る。
EVIDENCE_MAX = 120
# スレッドにぶら下げるチャートの上限。1枚ごとに yfinance を1回引くので
# 無制限にはしない(watch_tick 側の limit=8 が実質の上限)。
MAX_CHARTS = 10


def _s(v) -> str:
    """NaN/None を空文字にして str 化する。"""
    if v is None:
        return ""
    try:
        if pd.isna(v):
            return ""
    except (TypeError, ValueError):   # 配列など isna が真偽を返さない型
        pass
    return str(v).strip()


def _f(v):
    """数値として使えるなら float、無理なら None。"""
    try:
        if v is None or pd.isna(v):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _fmt_row(r) -> str:
    code, name = _s(r.get("stock_code")), _s(r.get("stock_name"))
    lines = []

    # 1行目: 銘柄と下落の大きさ
    head = f"*{code} {name}*"
    dp = _f(r.get("drop_pct"))
    if dp is not None:
        head += f"  ▼{abs(100 * dp):.1f}%"
    vr = _f(r.get("vol_ratio"))
    vr5 = _f(r.get("vol_ratio5"))
    if vr is not None:
        head += f"  出来高{vr:.1f}倍"
        if vr5 is not None:
            head += f"(5日比{vr5:.1f}倍)"
    cl = _f(r.get("close"))
    if cl is not None:
        head += f"  終値{cl:,.0f}円"
    lines.append(head)

    # 2行目: 属性と「自分だけ下げたのか」の文脈
    ctx = []
    # scale は「-」(規模区分なし)が入ることがあるので落とす
    attr = "／".join([x for x in (_s(r.get("sector33")), _s(r.get("scale")))
                      if x and x not in ("-", "‐", "ー")])
    if attr:
        ctx.append(attr)
    ex = _f(r.get("excess_drop"))
    if ex is not None:
        ctx.append(f"業種比{100 * ex:+.1f}%")
    gs = _f(r.get("gap_share"))
    if gs is not None:
        # 1に近いほど寄り付きギャップ由来 = 引け後の個別ニュース起因
        ctx.append(f"ギャップ寄与{100 * gs:.0f}%")
    bd = _f(r.get("breadth_dn3"))
    if bd is not None:
        ctx.append(f"地合い(3%安){100 * bd:.0f}%")
    if ctx:
        lines.append("　" + "｜".join(ctx))

    # 3行目: LLMの判定軸
    axes = [x for x in (_s(r.get("nature")), _s(r.get("scope")), _s(r.get("priced")))
            if x and x != "不明"]
    disc = _s(r.get("disclosure"))
    judge = []
    if disc:
        judge.append(f"*{disc}*")
    if axes:
        judge.append("・".join(axes))
    nc = _s(r.get("next_catalyst"))
    if nc and nc != "不明":
        judge.append(f"次の材料:{nc}")
    if judge:
        lines.append("　" + "｜".join(judge))

    # 4行目以降: 要約・根拠
    reason = _s(r.get("reason"))
    if reason:
        lines.append(f"　{reason}")
    note = _s(r.get("note"))
    if note and note != reason:
        lines.append(f"　{note}")
    ev = _s(r.get("evidence"))
    if ev:
        if len(ev) > EVIDENCE_MAX:
            ev = ev[:EVIDENCE_MAX] + "…"
        lines.append(f"　根拠: {ev}")

    # 一次情報の取得に失敗している場合、「開示なし」と読ませてはいけない
    warn = []
    if r.get("tdnet_ok") is False:
        warn.append("TDnet取得失敗")
    if r.get("news_ok") is False:
        warn.append("ニュース取得失敗")
    if warn:
        lines.append(f"　:warning: {'・'.join(warn)}（「開示なし」と断定できません）")

    if code:
        lines.append(f"　<https://finance.yahoo.co.jp/quote/{code}.T|チャート>")

    return "\n".join(lines)


def build_message(df: pd.DataFrame) -> str:
    """通知本文を組み立てる。送信はしないのでテストから直接呼べる。"""
    now = datetime.now(JST).strftime("%m/%d %H:%M")
    n = len(df)
    header = f":small_red_triangle_down: *急落検知 {n}件*  ({now} JST)"

    # 下落率が大きい順(drop_pct は負なので昇順)
    if "drop_pct" in df.columns:
        df = df.sort_values("drop_pct")

    # 銘柄どうしは空行で区切る(Slackで詰まって読めなくなるため)
    body = [_fmt_row(r) for _, r in df.head(MAX_ROWS).iterrows()]
    out = [header, "", "\n\n".join(body)]
    if n > MAX_ROWS:
        out.append(f"_ほか {n - MAX_ROWS}件は drop_events を参照_")
    return "\n".join(out)


def _chart_caption(r) -> str:
    """スレッドに貼るチャートの短い見出し。詳細は親メッセージ側にある。"""
    bits = [f"*{_s(r.get('stock_code'))} {_s(r.get('stock_name'))}*"]
    dp = _f(r.get("drop_pct"))
    if dp is not None:
        bits.append(f"▼{abs(100 * dp):.1f}%")
    disc = _s(r.get("disclosure"))
    if disc:
        bits.append(disc)
    return "｜".join(bits)


def _post_charts(channel_id: str, thread_ts: str, df: pd.DataFrame,
                 verbose: bool = True) -> int:
    """各銘柄の日足チャートをスレッド返信としてぶら下げる。返り値は枚数。"""
    from drop_chart import render_safely
    from slack_notifier import _slack_files_upload_v2

    posted = 0
    for _, r in df.head(MAX_CHARTS).iterrows():
        code = _s(r.get("stock_code"))
        png = render_safely(r)
        if not png:
            continue
        try:
            _slack_files_upload_v2(
                channel_id=channel_id,
                image_bytes=png,
                filename=f"{code or 'drop'}.png",
                initial_comment=_chart_caption(r),
                thread_ts=thread_ts,
            )
            posted += 1
        except Exception as e:
            print(f"  [drop][WARN] チャート投稿に失敗 {code}: {e}")
    if verbose and posted:
        print(f"  [drop] チャート{posted}枚をスレッドに投稿")
    return posted


def notify_drops(df: pd.DataFrame, *, verbose: bool = True,
                 charts: bool = True) -> bool:
    """急落をSlackに通知する。成功したら True。

    サマリーを1本投げ、各銘柄の日足チャートをそのスレッドにぶら下げる。
    監視ループから呼ばれるので、何が起きても例外は投げない。
    """
    try:
        if df is None or len(df) == 0:
            return False

        from config import SLACK_BOT_TOKEN, SLACK_DROP_CHANNEL
        if not SLACK_BOT_TOKEN:
            if verbose:
                print("  [drop] SLACK_BOT_TOKEN 未設定のため通知をスキップ")
            return False

        from slack_notifier import _get_channel_id, _slack_api_post

        channel_id = _get_channel_id(SLACK_DROP_CHANNEL)
        if "drop_pct" in df.columns:
            df = df.sort_values("drop_pct")
        result = _slack_api_post("chat.postMessage", {
            "channel": channel_id,
            "text": build_message(df),
            "unfurl_links": False,
        })
        if not result.get("ok"):
            print(f"  [drop][WARN] Slack通知に失敗: {result.get('error')}")
            return False
        if verbose:
            print(f"  [drop] Slackに{len(df)}件を通知 (#{SLACK_DROP_CHANNEL.lstrip('#')})")

        # チャートは失敗しても通知そのものは成功扱いにする
        if charts:
            _post_charts(channel_id, result["ts"], df, verbose=verbose)
        return True
    except Exception as e:
        print(f"  [drop][WARN] Slack通知に失敗(検知は継続): {type(e).__name__}: {e}")
        return False
