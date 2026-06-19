"""
急騰検知ソースアダプタ
======================

自前の急騰検知システム `kyutou_detector` の検知結果を、既存パイプラインが扱う
`Tweet` 形式に変換する。@shodousan のスクレイピングと並ぶ「もう一つの検知ソース」。

- kyutou_detector 本体はコピーせず、パス参照で読み込む（単一ソースで開発継続可）。
  既定の場所は C:\\Users\\moris\\kyutou_detector。環境変数 KYUTOU_DETECTOR_PATH で変更可。
- 当日の重複検知抑制は kyutou_detector 側の DetectionState をそのまま使う。
- 市場時間（平日 9:00〜15:40 JST）外は検知をスキップする（引け後ランキングは前日扱いの
  ノイズになるため）。
- このモジュールは「検知してTweet化する」までが責務。分析(Claude)・保存・投稿は
  呼び出し側の既存フロー（_process_tweets → analyze → save → post）に委ねる。
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, time as dtime, timezone, timedelta

from fetcher import Tweet

_JST = timezone(timedelta(hours=9))

# kyutou_detector パッケージを import 可能にする（末尾に追加して既存モジュールを優先）
_KYUTOU_PATH = os.getenv("KYUTOU_DETECTOR_PATH", r"C:\Users\moris\kyutou_detector")
if os.path.isdir(_KYUTOU_PATH) and _KYUTOU_PATH not in sys.path:
    sys.path.append(_KYUTOU_PATH)

# 遅延初期化するシングルトン（プロファイルキャッシュ・検知状態をティック間で共有）
_settings = None
_store = None
_state = None

# 材料（TDnet開示・ニュース見出し）がまだ出ていない銘柄の持ち越し管理。
# 急騰の最中に検知すると解説記事・開示が未配信のことが多く、即分析すると
# 「材料なし→推測」の誤答が当日確定してしまう（重複防止により再投稿不可）。
# そこで材料0件のうちは検知済みマークを付けず、次ティック以降に再評価する。
# 上限回数を超えたら材料なしのまま処理を続行する（Slack承認が最後の防波堤）。
MATERIAL_MAX_DEFER = 3  # kyutouティックは約10分間隔 → 3回 ≒ 30分待つ
_defer_counts: dict[tuple[str, str], int] = {}  # {(日付, code): 持ち越し回数}


def _should_defer_for_materials(code: str, day: str, has_materials: bool) -> bool:
    """材料未出の銘柄を持ち越すべきか。持ち越すなら回数を記録して True。"""
    if has_materials:
        _defer_counts.pop((day, code), None)
        return False
    n = _defer_counts.get((day, code), 0) + 1
    if n > MATERIAL_MAX_DEFER:
        return False  # 上限到達: 材料なしで続行
    _defer_counts[(day, code)] = n
    return True


def _ensure_ready() -> bool:
    """kyutou_detector を読み込み、設定・キャッシュ・状態を初期化する。"""
    global _settings, _store, _state
    if _settings is not None:
        return True
    try:
        from kyutou_detector.config import Settings
        from kyutou_detector.volume_ratio import ProfileStore
        from kyutou_detector.state import DetectionState
    except ImportError as exc:
        print(f"  [kyutou] import 失敗 (KYUTOU_DETECTOR_PATH={_KYUTOU_PATH}): {exc}")
        return False

    from config import BASE_DIR

    settings = Settings()
    # キャッシュ・ログ・状態は stock_memo 配下に隔離（.gitignore 済み）
    settings.cache_dir = str(BASE_DIR / "kyutou_cache")
    settings.log_dir = str(BASE_DIR / "kyutou_logs")
    os.makedirs(settings.log_dir, exist_ok=True)

    _settings = settings
    _store = ProfileStore(settings)
    _state = DetectionState(os.path.join(settings.log_dir, settings.state_file))
    return True


def _within_market_hours(now: datetime) -> bool:
    if now.weekday() >= 5:  # 土日
        return False
    return dtime(9, 0) <= now.time() <= dtime(15, 40)


def _to_tweet(cand, result, now: datetime) -> Tweet:
    """検知結果(Candidate + EvaluationResult)を疑似 Tweet 化する。

    text は analyzer(Claude) が銘柄特定・理由推定に使う。シグナル自体（急騰検知）は
    理由にしない方針が SYSTEM_PROMPT に入っているため、ここでは事実（株価・騰落率・
    出来高比・売買代金）だけを渡す。
    """
    text = (
        f"【急騰検知】{cand.name}（{cand.code}）\n"
        f"株価 {cand.price:.0f}円 / 前日比 +{cand.change_pct:.2f}% / "
        f"出来高比 {result.volume_ratio:.2f}倍 / 売買代金 {result.turnover_oku:.2f}億円"
        + ("（地合い良好下）" if result.market_strong else "")
    )
    # 数値ツイートIDと衝突しない安定ID（当日・銘柄単位）
    tweet_id = f"kyutou-{now.strftime('%Y%m%d')}-{cand.code}"
    return Tweet(id=tweet_id, text=text, created_at=now, stock_codes=[cand.code])


def fetch_new_surge_tweets(market_hours_only: bool = True, limit: int | None = None,
                           ignore_dedup: bool = False) -> list[Tweet]:
    """自前検知で新たに条件を満たした急騰銘柄を Tweet のリストで返す。

    既存の fetch_tweets(@shodousan) と同じ戻り値型（list[Tweet]）なので、
    呼び出し側はソースを意識せず同じフローに流せる。

    limit: 検知をこの件数で打ち切る（テスト用に少数だけ欲しい場合）。
    ignore_dedup: True で当日重複抑制(DetectionState)をバイパスする（テスト用。
                  検知済みでも拾い、検知済みマークもしない）。
    """
    if not _ensure_ready():
        return []

    from kyutou_detector import data_source
    from kyutou_detector.data_source import RankingFetchError, RankingParseError
    from kyutou_detector.filters import evaluate_candidate, is_market_strong
    from kyutou_detector.volume_ratio import compute_volume_ratio, time_str_to_minute

    now = datetime.now(_JST)
    if market_hours_only and not _within_market_hours(now):
        print(f"  [kyutou] 市場時間外 ({now.strftime('%H:%M')} JST)、急騰検知スキップ")
        return []

    # 一次フィルタ: 値上がり率ランキング
    try:
        candidates = data_source.fetch_gainers_ranking(_settings)
    except (RankingFetchError, RankingParseError) as exc:
        print(f"  [kyutou] ランキング取得失敗（スキップ）: {exc}")
        return []

    # 地合い補正の判定
    nikkei = data_source.fetch_index_change_pct(_settings.nikkei_tickers, _settings)
    topix = data_source.fetch_index_change_pct(_settings.topix_tickers, _settings)
    market_strong = is_market_strong(nikkei, topix, _settings)

    def volume_ratio_fn(cand):
        profile = _store.get(cand.code)
        if profile is None:
            return None
        minute = time_str_to_minute(cand.quote_time)
        if minute is None:
            minute = now.hour * 60 + now.minute
        return compute_volume_ratio(cand.volume, minute, profile)

    tweets: list[Tweet] = []
    for cand in candidates:
        if not ignore_dedup and _state.already_detected(cand.code):
            continue  # kyutou側の当日重複抑制
        result = evaluate_candidate(cand, market_strong, volume_ratio_fn, _settings)
        if not result.passed:
            continue
        if not ignore_dedup:
            # 材料が出るまで持ち越し（テストモード ignore_dedup=True では適用しない）
            from materials import fetch_materials
            day = now.strftime("%Y-%m-%d")
            mats = fetch_materials(cand.code, day)
            has_materials = bool(mats["tdnet"] or mats["news"])
            # ニュース源(株探)の取得が健全なときだけ「空＝まだ材料なし」を信頼して持ち越す。
            # 材料APIが不通なら待っても無駄＆銘柄を塩漬けにするので持ち越さず続行する。
            sources_healthy = mats.get("news_ok", True)
            effective = has_materials or not sources_healthy
            if _should_defer_for_materials(cand.code, day, effective):
                n = _defer_counts[(day, cand.code)]
                print(f"  [kyutou] {cand.code} {cand.name}: 材料未出のため持ち越し "
                      f"({n}/{MATERIAL_MAX_DEFER}) — 次ティックで再評価")
                continue
            if not has_materials:
                reason = "材料なし(API不通)" if not sources_healthy else "持ち越し上限到達"
                print(f"  [kyutou] {cand.code} {cand.name}: {reason}、材料なしで続行")
            _state.mark_detected(cand.code)
        tweets.append(_to_tweet(cand, result, now))
        if limit is not None and len(tweets) >= limit:
            break

    if tweets:
        print(f"  [kyutou] 新規急騰検知 {len(tweets)} 件: "
              + ", ".join(t.stock_codes[0] for t in tweets))
    else:
        print("  [kyutou] 新規急騰検知なし")
    return tweets
