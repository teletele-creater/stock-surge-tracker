-- 急落イベント用テーブル（急騰の surge_events とは完全に別テーブル）
--
-- 分離する理由:
--   1. 既存の分析コード（surge_outcome_analysis / dip_rebound_analysis /
--      notify_analysis_slack など）はすべて surge_events の列構成を前提にしている。
--      同じテーブルに direction 列を足すと、それら全部が「上昇イベントだけ」を
--      選ぶよう直す必要が出て、既存の集計を壊すリスクが高い。
--   2. 下落側は保持する項目が違う（disclosure / nature / priced など）。
--   3. 答え合わせの向きが逆（下落後は「戻ったか」を見る）。
--
-- Supabase の SQL Editor でそのまま実行する。

create table if not exists drop_events (
  id            bigserial primary key,

  -- ── 検知 ────────────────────────────────────────────
  stock_code    text        not null,
  stock_name    text,
  drop_date     date        not null,
  detected_at   timestamptz not null default now(),
  detect_mode   text,                    -- 'intraday' | 'eod'
  drop_pct      numeric,                 -- 当日リターン（負の値）
  close         numeric,

  -- ── 出来高（検知条件ではなく分析用の記録） ─────────────
  -- 2026-09-02 に「出来高3倍以上」を検知条件から外した。出来高を伴わない
  -- 急落も後から見比べたいため、倍率は落とさず記録だけ続ける。
  volume        bigint,                  -- 当日出来高（株）
  vol20         bigint,                  -- 前20日平均の出来高（株）
  vol5          bigint,                  -- 前5日平均の出来高（株）
  vol_ratio     numeric,                 -- 当日出来高 ÷ 前20日平均
  vol_ratio5    numeric,                 -- 当日出来高 ÷ 前5日平均（直近比）
  turnover      bigint,                  -- 当日の売買代金（円）
  turnover20    bigint,                  -- 前20日平均の売買代金（円）

  -- ── 原因を価格の形から推定する軸（米国5.5年の検証で最も効いた2軸） ──
  gap_pct       numeric,                 -- 寄り付きギャップ
  gap_share     numeric,                 -- 下落のうちギャップが占める割合。
                                         -- 1に近い = 引け後の個別ニュース起因
  breadth_dn3   numeric,                 -- その日3%以上下げた銘柄の割合（地合い）
  mkt_median    numeric,                 -- 全銘柄の当日リターン中央値
  sector_median numeric,                 -- 同業33業種の当日リターン中央値
  excess_drop   numeric,                 -- drop_pct - sector_median（自分だけか）

  -- ── 属性 ────────────────────────────────────────────
  sector33      text,
  scale         text,
  market        text,
  market_cap    numeric,                 -- 時価総額（円）

  -- ── 一次情報にもとづく判定（drop_analyzer.py） ──────────
  reason        text,
  disclosure    text,                    -- 下方修正/決算/特別損失/増資希薄化/減配無配/
                                         -- 業務提携解消/訴訟不祥事/上場廃止関連/
                                         -- その他開示/開示なし
  evidence      text,                    -- 根拠にした開示・見出しの原文（" / "連結）
  nature        text,                    -- 一過性 / 構造的 / 不明
  scope         text,                    -- 個別事情 / 業界全体 / 市場全体 / 不明
  priced        text,                    -- 過剰反応 / 妥当 / 織り込み不足 / 不明
  next_catalyst text,                    -- 近い / 遠い / 不明
  note          text,
  backgrounds   text,                    -- 背景（「・」区切り、最大4件）
  related_stocks text,                   -- 同日・同業種で急落した銘柄（推測ではなく観測）
  n_tdnet       int,                     -- 取得できたTDnet開示の件数
  n_news        int,
  tdnet_ok      boolean,                 -- 取得に成功したか（falseなら「開示なし」と
  news_ok       boolean,                 -- 断定できない。塩漬け防止用）

  -- ── 答え合わせ（後から埋める） ────────────────────────
  fwd_ret_5d    numeric,
  fwd_ret_10d   numeric,
  fwd_ret_20d   numeric,
  recovery_20d  numeric,                 -- (20日後 - 急落前) / (急落日 - 急落前)
                                         -- 0以下 = 急落前まで戻した
  outcome_20d   text,                    -- 全戻し / 半戻し / 未回復
  judged_at     timestamptz,

  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),

  unique (stock_code, drop_date)
);

create index if not exists drop_events_date_idx        on drop_events (drop_date desc);
create index if not exists drop_events_code_idx        on drop_events (stock_code);
create index if not exists drop_events_disclosure_idx  on drop_events (disclosure);
create index if not exists drop_events_unjudged_idx    on drop_events (drop_date)
  where fwd_ret_20d is null;

-- updated_at の自動更新
create or replace function drop_events_touch() returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

drop trigger if exists drop_events_touch_trg on drop_events;
create trigger drop_events_touch_trg before update on drop_events
  for each row execute function drop_events_touch();

-- RLS: service_role キーで書き込む前提。匿名の読み取りだけ許可する場合は下を有効に。
-- alter table drop_events enable row level security;
-- create policy drop_events_read on drop_events for select using (true);
