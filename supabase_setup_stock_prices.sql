-- ============================================================
-- stock_prices テーブル: 現在株価の保存先
-- Supabase ダッシュボード → SQL Editor に貼り付けて1回実行
-- ============================================================

-- 1) テーブル本体 ---------------------------------------------
create table if not exists stock_prices (
  stock_code   text        primary key,         -- 例 "7203" (素の4桁、.T無し。surge_events.stock_code と一致)
  price        numeric     not null,            -- 直近の現在値 (regularMarketPrice)
  prev_close   numeric,                         -- 前日終値 (chartPreviousClose)
  updated_at   timestamptz not null default now()
);

create index if not exists stock_prices_updated_at_idx
  on stock_prices (updated_at desc);


-- 2) Realtime を有効化 (任意。サイト側が即時反映したい場合) ----------
-- 既に有効ならエラーになるが無害なので無視してOK
alter publication supabase_realtime add table stock_prices;


-- 3) Row Level Security: 「公開閲覧 / 書き込みは service_role のみ」 ----
alter table stock_prices enable row level security;

drop policy if exists "public read stock_prices" on stock_prices;
create policy "public read stock_prices"
  on stock_prices for select
  using (true);

-- ※ INSERT/UPDATE/DELETE のポリシーは作らない。
--    GitHub Actions の cron は service_role キーで RLS をバイパスして書き込む。
