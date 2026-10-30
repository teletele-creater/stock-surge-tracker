"""
急騰後アウトカム分析 — 維持型 vs 全戻し型の特徴量比較
============================================================
surge_events(Supabase) の急騰イベントについて、急騰5/20営業日後の
「保持率」を計算し、材料の質・出来高・サイズなどの特徴量別に
維持率を比較するレポートを生成する。

保持率 = (N日後終値 - 急騰前終値) / (急騰日終値 - 急騰前終値)
  維持型   : 保持率 >= 0.5
  半戻し型 : 0 < 保持率 < 0.5
  全戻し型 : 保持率 <= 0

理由カテゴリ(6分類):
  決算・業績 / 株主還元 / 決算+還元 / 個別材料 / テーマ物色 / 不明
  正規表現ルールで分類し、曖昧なものだけ Claude API (Haiku) で補完。

使い方(stock_memo ディレクトリで実行、.env は既存のものを流用):
  py surge_outcome_analysis.py               # 全パイプライン実行
  py surge_outcome_analysis.py --limit 30    # 動作確認(最新30件のみ)
  py surge_outcome_analysis.py --no-llm      # LLM補完なし(ルールのみ)
  py surge_outcome_analysis.py --with-news   # ニュース件数も取得(約20分)

出力:
  analysis_out/surge_outcome_features.csv    # 全イベントの特徴量+判定
  analysis_out/surge_outcome_report.md       # 集計レポート
  analysis_out/cache/                        # 株価・分類などのキャッシュ

既存パイプライン(main.py 等)には一切依存されない独立スクリプト。
"""

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

import pandas as pd

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
EVENTS_TABLE = os.getenv("SUPABASE_TABLE", "surge_events")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

JST = timezone(timedelta(hours=9))
TIMEOUT = 30

OUT_DIR = Path(__file__).parent / "analysis_out"
CACHE_DIR = OUT_DIR / "cache"
OHLC_CACHE = CACHE_DIR / "ohlc"

# 株価取得の開始日(最古の検知の75営業日以上前から: 75日移動平均の計算に必要)
PRICE_START = "2025-10-01"

CAT_EARN = "決算・業績"
CAT_RETURN = "株主還元"
CAT_EARN_RETURN = "決算+還元"
CAT_MATERIAL = "個別材料"
CAT_THEME = "テーマ物色"
CAT_UNKNOWN = "不明"
CATEGORIES = [CAT_EARN, CAT_RETURN, CAT_EARN_RETURN, CAT_MATERIAL, CAT_THEME, CAT_UNKNOWN]


def _headers():
    return {"Authorization": f"Bearer {SUPABASE_KEY}", "apikey": SUPABASE_KEY}


# ─────────────────────────────────────────────
# 1) イベント取得
# ─────────────────────────────────────────────
def fetch_events(limit: int | None = None) -> pd.DataFrame:
    rows, offset, page = [], 0, 1000
    while True:
        url = (
            f"{SUPABASE_URL}/rest/v1/{EVENTS_TABLE}"
            # business / backgrounds / related_stocks / raw_post は長らく未取得
            # だったが、reason(20字程度の要約)より情報量が桁違いに多いので
            # 予測特徴の材料として引く。
            f"?select=id,tweet_id,stock_code,stock_name,detected_at,reason,"
            f"surge_price,keywords,business,backgrounds,related_stocks,raw_post"
            f"&order=detected_at.desc&offset={offset}&limit={page}"
        )
        r = requests.get(url, headers=_headers(), timeout=TIMEOUT)
        r.raise_for_status()
        batch = r.json()
        rows.extend(batch)
        if len(batch) < page or (limit and len(rows) >= limit):
            break
        offset += page
    if limit:
        rows = rows[:limit]

    df = pd.DataFrame(rows)
    df["detected_at"] = pd.to_datetime(df["detected_at"], utc=True, format="ISO8601")
    df["surge_date"] = df["detected_at"].dt.tz_convert(JST).dt.date
    df["stock_code"] = df["stock_code"].astype(str).str.strip()
    df["reason"] = df["reason"].fillna("").astype(str).str.strip()
    # 検知ソース: 自前検知は tweet_id が "kyutou-..."、@shodousan は数値ID
    df["source"] = df["tweet_id"].astype(str).apply(
        lambda t: "kyutou" if t.startswith("kyutou") else "shodousan"
    )

    def _kwset(v) -> frozenset:
        if isinstance(v, list):
            return frozenset(str(k).strip() for k in v if str(k).strip())
        return frozenset()

    df["kwset"] = df["keywords"].apply(_kwset)

    # ── リッチフィールドから構造的な特徴を作る ─────────────────
    # 強材料ポイント(backgrounds)は「材料がどれだけ厚いか」、related_stocks は
    # 「テーマの裾野の広さ」の代理変数になる。いずれも急騰検知時点で確定して
    # いるので押し目の予測に使っても先読みにならない。
    def _as_list(v) -> list:
        if isinstance(v, list):
            return [str(x) for x in v if str(x).strip()]
        if isinstance(v, str) and v.strip():
            return [v]
        return []

    bg = df["backgrounds"].apply(_as_list)
    df["n_backgrounds"] = bg.apply(len)
    df["len_backgrounds"] = bg.apply(lambda L: sum(len(x) for x in L))
    df["backgrounds_text"] = bg.apply(lambda L: " / ".join(L))
    df["n_keywords"] = df["kwset"].apply(len)
    df["business"] = df["business"].fillna("").astype(str).str.strip()
    df["len_business"] = df["business"].str.len()

    rel = df["related_stocks"].fillna("").astype(str)
    # 「4183: 三井化学 — ...」の形で列挙されるので銘柄コードの出現数を数える
    df["n_related"] = rel.apply(lambda s: len(re.findall(r"\b\d{4}[A-Z]?\s*[:：]", s)))
    # 「省略」と書かれた埋め草を除いた実質的な関連銘柄数
    df["n_related_real"] = rel.apply(
        lambda s: sum(1 for ln in re.split(r"[/\n]", s)
                      if re.search(r"\d{4}[A-Z]?\s*[:：]", ln) and "省略" not in ln))

    # raw_post の数値(自前検知 kyutou のみ。shodousan は銘柄名とURLだけ)
    rp = df["raw_post"].fillna("").astype(str)

    def _num(pat):
        return pd.to_numeric(rp.str.extract(pat, expand=False), errors="coerce")

    df["post_surge_pct"] = _num(r"前日比\s*\+?(-?[\d.]+)\s*%")
    df["post_vol_ratio"] = _num(r"出来高比\s*([\d.]+)\s*倍")
    df["post_turnover_oku"] = _num(r"売買代金\s*([\d.]+)\s*億円")
    # 同一銘柄・同一日の重複は最初の1件に絞る
    before = len(df)
    df = df.sort_values("detected_at").drop_duplicates(
        subset=["stock_code", "surge_date"], keep="first"
    ).reset_index(drop=True)
    print(f"イベント取得: {before}件 → 重複除去後 {len(df)}件")
    return df


# ─────────────────────────────────────────────
# 2) 理由の分類(ルール + LLM補完)
# ─────────────────────────────────────────────
RE_EARN = re.compile(
    r"決算|増益|増収|上方修正|営業益|経常|利益|黒字|最高益|業績予想|業績見通し|着地|進捗率|増額修正"
)
RE_RETURN = re.compile(
    r"増配|復配|配当|自社株買い|自己株式|株主優待|優待|株式分割|累進配当|株主還元"
)
RE_THEME_STRONG = re.compile(r"としての物色|関連銘柄|物色|思惑")
RE_THEME_WEAK = re.compile(r"期待|見直し|再評価|テーマ|関連|人気化|リバウンド|連想")
RE_MATERIAL = re.compile(
    r"提携|受注|採択|契約|承認|認可|出資|買収|TOB|子会社|設立|協業|共同|開発|発売|"
    r"特許|合意|ライセンス|上市|治験|導出|資本業務|新製品|新サービス|参入|展開|"
    r"IR|開示|発表|取得|供給|納入|指定|選定"
)


def classify_by_rule(reason: str) -> str | None:
    """ルールで確定できればカテゴリを返す。曖昧なら None(→LLM行き)。"""
    if not reason:
        return CAT_UNKNOWN
    e = bool(RE_EARN.search(reason))
    r = bool(RE_RETURN.search(reason))
    theme_strong = bool(RE_THEME_STRONG.search(reason))
    if theme_strong and not (e or r):
        return CAT_THEME
    if e and r:
        return CAT_EARN_RETURN
    if e:
        # 「黒字転換期待」のように実績でなく思惑の場合は LLM に回す
        if RE_THEME_WEAK.search(reason) or theme_strong:
            return None
        return CAT_EARN
    if r:
        return CAT_RETURN
    if RE_MATERIAL.search(reason):
        return CAT_MATERIAL
    if RE_THEME_WEAK.search(reason):
        return CAT_THEME
    return None


LLM_SYSTEM = """あなたは日本株の株価急騰理由を分類する分類器です。
与えられた理由テキストを以下の6カテゴリのいずれか1つに分類し、
1行に「番号. カテゴリ名」の形式で、入力と同じ順序・同じ行数で出力してください。
カテゴリ:
1. 決算・業績 (実際に発表された決算・上方修正・業績数値が主因)
2. 株主還元 (増配・自社株買い・株主優待・株式分割が主因)
3. 決算+還元 (決算・業績と株主還元の両方が発表された)
4. 個別材料 (提携・受注・採択・新製品など、その企業固有の具体的発表)
5. テーマ物色 (「〜関連としての物色」「〜期待」「思惑」など、具体的発表を伴わない)
6. 不明 (判断材料がない)
出力例:
1. 決算・業績
2. テーマ物色"""

_NUM2CAT = {1: CAT_EARN, 2: CAT_RETURN, 3: CAT_EARN_RETURN,
            4: CAT_MATERIAL, 5: CAT_THEME, 6: CAT_UNKNOWN}


def classify_by_llm(reasons: list[str]) -> dict[str, str]:
    """曖昧な理由テキストを Claude (Haiku) でまとめて分類する。"""
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    result: dict[str, str] = {}
    batch_size = 25
    for i in range(0, len(reasons), batch_size):
        batch = reasons[i:i + batch_size]
        numbered = "\n".join(f"{j+1}. {t}" for j, t in enumerate(batch))
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1500,
            system=LLM_SYSTEM,
            messages=[{"role": "user", "content": numbered}],
        )
        text = resp.content[0].text
        for line in text.strip().splitlines():
            m = re.match(r"\s*(\d+)\s*[.．]\s*(\S+)", line)
            if not m:
                continue
            idx = int(m.group(1)) - 1
            cat = m.group(2).strip()
            if 0 <= idx < len(batch) and cat in CATEGORIES:
                result[batch[idx]] = cat
        print(f"  LLM分類: {min(i+batch_size, len(reasons))}/{len(reasons)}")
        time.sleep(0.5)
    return result


def classify_reasons(df: pd.DataFrame, use_llm: bool) -> pd.DataFrame:
    cache_file = CACHE_DIR / "reason_categories.json"
    cache: dict[str, str] = {}
    if cache_file.exists():
        cache = json.loads(cache_file.read_text(encoding="utf-8"))

    cats, methods = [], []
    pending: list[str] = []
    for reason in df["reason"]:
        key = hashlib.md5(reason.encode()).hexdigest()
        if key in cache:
            cats.append(cache[key]); methods.append("cache")
            continue
        rule_cat = classify_by_rule(reason)
        if rule_cat is not None:
            cache[key] = rule_cat
            cats.append(rule_cat); methods.append("rule")
        else:
            cats.append(None); methods.append("llm")
            pending.append(reason)

    pending = sorted(set(pending))
    if pending and use_llm and ANTHROPIC_API_KEY:
        print(f"ルールで確定できない理由 {len(pending)}件 → LLM分類")
        llm_result = classify_by_llm(pending)
        for reason, cat in llm_result.items():
            cache[hashlib.md5(reason.encode()).hexdigest()] = cat
    elif pending:
        print(f"注意: 曖昧な理由 {len(pending)}件は LLM 未使用のため「不明」扱い")

    # cats を確定(LLM結果を反映、未解決は不明)
    final = []
    for reason, c in zip(df["reason"], cats):
        if c is not None:
            final.append(c)
        else:
            key = hashlib.md5(reason.encode()).hexdigest()
            final.append(cache.get(key, CAT_UNKNOWN))
    df["category"] = final
    df["category_method"] = methods

    cache_file.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    print("カテゴリ内訳:", dict(df["category"].value_counts()))
    return df


# ─────────────────────────────────────────────
# 2b) 継続性分類(ルールのみ・LLM不使用)と後続シグナル
# ─────────────────────────────────────────────
# 「後続材料の出やすさ」を材料の構造から機械的に判定する4分類。
# 優先順: 続報期待 > 定期イベント > 一過性完結 > 外部依存
CONT_FOLLOWUP = "続報期待型"    # 同一案件の続報が構造的に想定される
CONT_PERIODIC = "定期イベント型"  # 次の四半期決算という確定カタリスト持ち
CONT_ONESHOT = "一過性完結型"   # 続報の出ようがない単発イベント
CONT_EXTERNAL = "外部依存型"    # 自社発ニュースなし。テーマの外部フロー次第
CONT_BINARY = "バイオ・イベント型"  # 治験結果などバイナリーな一発イベント頼み。押し目反発が弱いため続報期待型と分離
CONT_UNKNOWN = "分類不能"

RE_CONT_ONESHOT = re.compile(
    r"優待|記念|周年|株式分割|自社株買い|自己株式|増配|復配|配当|累進"
)

# ── 続報期待スコア ルーブリック ────────────────────────────────
# 「同じ案件の続報がこの先も出そうか」を材料の構造から加点・減点で採点する。
# ここ(配点とキーワード)を書き換えるだけで自由に調整できる。LLM不使用・再現可能。
#   "max"  : マッチした中で最高配点の1件だけ採用(材料タイプの排他選択)
#   "each" : マッチした各行の配点をすべて加算
FOLLOWUP_RUBRIC = [
    ("材料タイプ", "max", [
        (3, r"治験|臨床|フェーズ|第[IVXｰ0-9]+相|承認申請|薬事|導出|パイプライン"),
        (2, r"提携|協業|共同|資本業務提携|アライアンス"),
        (2, r"受注|契約|採択|選定|指定|落札|受託"),
        (1, r"買収|TOB|M&A|出資|子会社|設立|買付"),
        (1, r"新製品|新サービス|発売|上市|参入|開発|実用化"),
    ]),
    ("段階性", "each", [
        (2, r"第[1１一]弾|基本合意|MOU|一部|着手|協議|交渉|予定|計画|見込|開始"),
        (-2, r"完了|最終|満了|解消|終了|撤回|中止"),
    ]),
    ("具体性", "each", [
        # 「」で括られた固有名、または4文字以上のカタカナ語(製品名・社名の目安)
        (1, r"[「『][^」』]{2,}[」』]|[ァ-ヺー]{4,}"),
    ]),
    ("一過性・減点", "each", [
        (-3, r"優待|記念|周年|株式分割"),
        (-2, r"増配|復配|配当|自社株買い|自己株式|累進"),
        (-2, r"としての物色|関連株|関連銘柄"),
    ]),
]
FOLLOWUP_THRESHOLD = 2  # このスコア以上を「続報期待型」とする
# 注: 現データでは閾値3(治験/パイプライン等の高確度材料)は押し目反発が弱く、
# 閾値2(提携/受注など中程度材料を含む)の方が押し目シグナルが良好だった。

_FOLLOWUP_COMPILED = [
    (name, mode, [(pt, re.compile(pat)) for pt, pat in items])
    for name, mode, items in FOLLOWUP_RUBRIC
]

# バイオ/創薬: 治験結果というバイナリーイベント頼みで、押し目に継続買いが入りにくい。
# 続報期待型(提携・受注の"じわじわ続く再評価")とは仕組みが違うため別枠に隔離する。
RE_BIOTECH = re.compile(
    r"治験|臨床試験|臨床開発|フェーズ|第[IVXｰ0-9]+相|承認申請|薬事承認|"
    r"導出|パイプライン|創薬|抗体医薬|治療薬|ワクチン|再生医療|遺伝子治療|"
    r"希少疾患|バイオ医薬|新薬|治験薬"
)


def followup_score(reason: str, breakdown: bool = False):
    """続報期待スコアを計算する。breakdown=True なら (score, 内訳dict) を返す。"""
    if not reason:
        return (0, {}) if breakdown else 0
    total = 0
    detail = {}
    for name, mode, items in _FOLLOWUP_COMPILED:
        hits = [pt for pt, rx in items if rx.search(reason)]
        if not hits:
            continue
        pt = max(hits) if mode == "max" else sum(hits)
        total += pt
        detail[name] = pt
    return (total, detail) if breakdown else total


# 「未確定」= 材料がまだ期待・思惑の段階で終わっている(=買う理由が消費されていない)。
# 具体材料(続報期待型)と掛け合わせたときに押し目反発が跳ね上がる中核シグナル。
RE_UNRESOLVED_TAIL = re.compile(r"(期待|思惑|観測|憶測|見通し|期待感|人気化)$")


def is_unresolved(reason: str) -> bool:
    return bool(RE_UNRESOLVED_TAIL.search((reason or "").strip()))


def classify_continuation(reason: str) -> str:
    if not reason:
        return CONT_UNKNOWN
    if RE_BIOTECH.search(reason):
        return CONT_BINARY
    if followup_score(reason) >= FOLLOWUP_THRESHOLD:
        return CONT_FOLLOWUP
    if RE_EARN.search(reason):
        return CONT_PERIODIC
    if RE_CONT_ONESHOT.search(reason):
        return CONT_ONESHOT
    if RE_THEME_STRONG.search(reason) or RE_THEME_WEAK.search(reason):
        return CONT_EXTERNAL
    return CONT_UNKNOWN


THEME_HEAT_WINDOW = 14  # 過熱度の観測窓(暦日)
REDETECT_WINDOW = 30    # 再検知の観測窓(暦日)


def add_followup_signals(df: pd.DataFrame) -> pd.DataFrame:
    """継続性タイプ・続報期待スコア・テーマ過熱度・再検知フラグを付与する(全て実測/ルールのみ)。"""
    df["followup_score"] = df["reason"].apply(followup_score)
    df["unresolved"] = df["reason"].apply(is_unresolved)
    df["continuation_type"] = df["reason"].apply(classify_continuation)

    # テーマ過熱度: 直近14日(当日含む)に同一キーワードを1つ以上共有する
    # 他銘柄の急騰が何件あったか
    events = df[["stock_code", "surge_date", "kwset"]].to_dict("records")
    heats = []
    for me in events:
        if not me["kwset"]:
            heats.append(None)
            continue
        cnt = 0
        for other in events:
            if other["stock_code"] == me["stock_code"]:
                continue
            delta = (me["surge_date"] - other["surge_date"]).days
            if 0 <= delta <= THEME_HEAT_WINDOW and me["kwset"] & other["kwset"]:
                cnt += 1
        heats.append(cnt)
    df["theme_heat"] = heats

    # 再検知: 同一銘柄が30日以内に再度検知されたか。
    # データ末尾30日は観測期間が足りないので None(判定不能)にする
    df = df.sort_values(["stock_code", "surge_date"])
    next_date = df.groupby("stock_code")["surge_date"].shift(-1)
    gap = (pd.to_datetime(next_date) - pd.to_datetime(df["surge_date"])).dt.days
    cutoff = df["surge_date"].max() - timedelta(days=REDETECT_WINDOW)
    redetect = (gap <= REDETECT_WINDOW).astype(object)
    redetect.loc[(df["surge_date"] > cutoff).values] = None
    df["redetect_30d"] = redetect
    return df.sort_values("detected_at").reset_index(drop=True)


# ─────────────────────────────────────────────
# 3) 株価データ(yfinance、銘柄ごとにCSVキャッシュ)
# ─────────────────────────────────────────────
def load_price_data(codes: list[str]) -> dict[str, pd.DataFrame]:
    import yfinance as yf
    OHLC_CACHE.mkdir(parents=True, exist_ok=True)
    data: dict[str, pd.DataFrame] = {}
    missing: list[str] = []
    now = datetime.now(JST)
    # yf.download の end は排他的なので、当日の足を含めるには翌日を指定する。
    # end=今日 だと大引け後(16:33)の実行でも前日までしか入らず、
    # 押し目接近の検知が丸1日遅れる(2026-08-20 のセリアで実害)。
    end = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    for code in codes:
        f = OHLC_CACHE / f"{code}.csv"
        # キャッシュが当日取得済みならそれを使う。日次ジョブは毎日ほぼ同時刻に
        # 走るので、閾値24hだと前日分(≒24h前)を「新しい」と誤判定する日が出る。
        if f.exists():
            age_h = (time.time() - f.stat().st_mtime) / 3600
            if age_h < 6:
                try:
                    df = pd.read_csv(f, index_col=0, parse_dates=True)
                    # PRICE_START を広げた場合は古いキャッシュを使わない
                    # (上場が浅い銘柄は1日1回だけ再取得される)
                    covered = df.index.min() <= pd.Timestamp(PRICE_START) + pd.Timedelta(days=45)
                    if len(df) > 30 and covered:
                        data[code] = df
                        continue
                except Exception:
                    pass
        missing.append(code)

    print(f"株価: キャッシュ {len(data)}銘柄 / 取得必要 {len(missing)}銘柄")
    chunk = 50
    for i in range(0, len(missing), chunk):
        batch = missing[i:i + chunk]
        tickers = [f"{c}.T" for c in batch]
        try:
            raw = yf.download(
                tickers, start=PRICE_START, end=end, interval="1d",
                group_by="ticker", auto_adjust=False, threads=True, progress=False,
            )
        except Exception as e:
            print(f"  yf.download 失敗({batch[0]}〜): {e}")
            continue
        for code, tic in zip(batch, tickers):
            try:
                # group_by="ticker" は1銘柄バッチでも MultiIndex 列を返すことがある
                sub = raw[tic] if isinstance(raw.columns, pd.MultiIndex) else raw
                df = sub.dropna(subset=["Close"])
                if len(df) > 30:
                    df = df[["Open", "High", "Low", "Close", "Volume"]]
                    df.to_csv(OHLC_CACHE / f"{code}.csv")
                    data[code] = df
            except Exception:
                pass
        print(f"  取得: {min(i+chunk, len(missing))}/{len(missing)}")
        time.sleep(1.0)
    return data


def load_market_caps(codes: list[str]) -> dict[str, float]:
    import yfinance as yf
    from concurrent.futures import ThreadPoolExecutor
    cache_file = CACHE_DIR / "market_caps.json"
    caps: dict[str, float] = {}
    if cache_file.exists():
        caps = json.loads(cache_file.read_text(encoding="utf-8"))
    missing = [c for c in codes if c not in caps]
    print(f"時価総額: キャッシュ {len(codes)-len(missing)}銘柄 / 取得必要 {len(missing)}銘柄")

    def _get(code: str):
        try:
            mc = yf.Ticker(f"{code}.T").fast_info.get("marketCap")
            return code, float(mc) if mc else None
        except Exception:
            return code, None

    if missing:
        with ThreadPoolExecutor(max_workers=8) as ex:
            for n, (code, mc) in enumerate(ex.map(_get, missing), 1):
                caps[code] = mc
                if n % 100 == 0:
                    print(f"  時価総額: {n}/{len(missing)}")
        cache_file.write_text(json.dumps(caps), encoding="utf-8")
    return caps


# ─────────────────────────────────────────────
# 4) 東証の値幅制限(ストップ高判定)
# ─────────────────────────────────────────────
_LIMIT_TABLE = [
    (100, 30), (200, 50), (500, 80), (700, 100), (1000, 150),
    (1500, 300), (2000, 400), (3000, 500), (5000, 700), (7000, 1000),
    (10000, 1500), (15000, 3000), (20000, 4000), (30000, 5000),
    (50000, 7000), (70000, 10000), (100000, 15000), (150000, 30000),
    (200000, 40000), (300000, 50000), (500000, 70000), (700000, 100000),
    (1000000, 150000),
]


def price_limit_width(base_price: float) -> float:
    for threshold, width in _LIMIT_TABLE:
        if base_price < threshold:
            return width
    return 150000


# ─────────────────────────────────────────────
# 5) ニュース件数(オプション、Google News RSS)
# ─────────────────────────────────────────────
def fetch_news_counts(df: pd.DataFrame) -> dict[str, int | None]:
    cache_file = CACHE_DIR / "news_counts.json"
    cache: dict[str, int | None] = {}
    if cache_file.exists():
        cache = json.loads(cache_file.read_text(encoding="utf-8"))

    targets = []
    for _, row in df.iterrows():
        key = f"{row['stock_code']}_{row['surge_date']}"
        if key not in cache:
            targets.append((key, row["stock_name"], row["surge_date"]))

    print(f"ニュース件数: キャッシュ {len(df)-len(targets)}件 / 取得必要 {len(targets)}件")
    for n, (key, name, d) in enumerate(targets, 1):
        after = (d - timedelta(days=2)).isoformat()
        before = (d + timedelta(days=1)).isoformat()
        q = urllib.parse.quote(f'"{name}" after:{after} before:{before}')
        url = f"https://news.google.com/rss/search?q={q}&hl=ja&gl=JP&ceid=JP:ja"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                xml = resp.read().decode("utf-8", errors="replace")
            cache[key] = xml.count("<item>")
        except Exception:
            cache[key] = None
        if n % 25 == 0:
            print(f"  ニュース: {n}/{len(targets)}")
            cache_file.write_text(json.dumps(cache), encoding="utf-8")
        time.sleep(1.2)
    cache_file.write_text(json.dumps(cache), encoding="utf-8")
    return cache


# ─────────────────────────────────────────────
# 6) 特徴量とアウトカムの計算
# ─────────────────────────────────────────────
MIN_SURGE_RET = 0.02   # 急騰日リターンがこれ未満なら保持率の分母が不安定なので除外
HOLD_THRESHOLD = 0.5   # 保持率がこれ以上なら維持型


def outcome_class(retention: float | None) -> str | None:
    if retention is None or (isinstance(retention, float) and math.isnan(retention)):
        return None
    if retention >= HOLD_THRESHOLD:
        return "維持型"
    if retention > 0:
        return "半戻し型"
    return "全戻し型"


def compute_features(df: pd.DataFrame, prices: dict[str, pd.DataFrame],
                     caps: dict[str, float]) -> pd.DataFrame:
    recs = []
    skip_no_price, skip_no_date, skip_small = 0, 0, 0
    for _, ev in df.iterrows():
        code = ev["stock_code"]
        px = prices.get(code)
        if px is None:
            skip_no_price += 1
            continue
        dates = px.index.date
        d = ev["surge_date"]
        # 検知日が取引日でなければ3日以内の次の取引日に合わせる
        pos = None
        for offset in range(0, 4):
            cand = d + timedelta(days=offset)
            hit = (dates == cand)
            if hit.any():
                pos = int(hit.argmax())
                break
        if pos is None or pos < 6:
            skip_no_date += 1
            continue

        close = px["Close"].values
        high = px["High"].values
        vol = px["Volume"].values

        pre_close = close[pos - 1]
        surge_close = close[pos]
        surge_ret = surge_close / pre_close - 1
        if surge_ret < MIN_SURGE_RET:
            skip_small += 1
            continue

        base_start = max(0, pos - 20)
        vol_base = vol[base_start:pos]
        vol_ratio = (vol[pos] / vol_base.mean()) if vol_base.mean() > 0 else None
        value_traded = float((close[base_start:pos] * vol[base_start:pos]).mean())

        prev_rets = pd.Series(close[max(0, pos - 21):pos]).pct_change().dropna()
        max_prev = float(prev_rets.max()) if len(prev_rets) else None

        limit = price_limit_width(pre_close)
        eps = limit * 0.005 + 0.5
        stop_touch = bool(high[pos] >= pre_close + limit - eps)
        stop_close = bool(surge_close >= pre_close + limit - eps)

        def _retention(n: int):
            if pos + n >= len(close):
                return None, None
            p = close[pos + n]
            ret = (p - pre_close) / (surge_close - pre_close)
            fwd = p / surge_close - 1
            return float(ret), float(fwd)

        r5, fwd5 = _retention(5)
        r20, fwd20 = _retention(20)

        recs.append({
            "stock_code": code,
            "stock_name": ev["stock_name"],
            "surge_date": d,
            "category": ev["category"],
            "continuation_type": ev["continuation_type"],
            "followup_score": ev["followup_score"],
            "unresolved": ev["unresolved"],
            "theme_heat": ev["theme_heat"],
            "redetect_30d": ev["redetect_30d"],
            "reason": ev["reason"],
            "source": ev["source"],
            "pre_close": round(float(pre_close), 1),
            "surge_close": round(float(surge_close), 1),
            "surge_ret": round(float(surge_ret), 4),
            "vol_ratio": round(float(vol_ratio), 2) if vol_ratio else None,
            "value_traded_avg20": round(value_traded),
            "max_prev_1mo": round(max_prev, 4) if max_prev is not None else None,
            "stop_high_touch": stop_touch,
            "stop_high_close": stop_close,
            "market_cap": caps.get(code),
            "retention_5d": round(r5, 3) if r5 is not None else None,
            "retention_20d": round(r20, 3) if r20 is not None else None,
            "fwd_ret_5d": round(fwd5, 4) if fwd5 is not None else None,
            "fwd_ret_20d": round(fwd20, 4) if fwd20 is not None else None,
            "class_5d": outcome_class(r5),
            "class_20d": outcome_class(r20),
        })

    out = pd.DataFrame(recs)
    print(
        f"特徴量計算: {len(out)}件 "
        f"(除外: 株価なし{skip_no_price} / 日付不一致{skip_no_date} / 急騰弱{skip_small})"
    )
    return out


# ─────────────────────────────────────────────
# 7) 統計とレポート
# ─────────────────────────────────────────────
def two_prop_z(k1, n1, k2, n2) -> float | None:
    """2群の維持率の差の両側p値(正規近似)。scipyなしで動くよう手実装。"""
    if n1 < 10 or n2 < 10:
        return None
    p1, p2 = k1 / n1, k2 / n2
    p = (k1 + k2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    if se == 0:
        return None
    z = (p1 - p2) / se
    return math.erfc(abs(z) / math.sqrt(2))


def group_table(df: pd.DataFrame, key: str, cls_col: str = "class_20d",
                ret_col: str = "retention_20d", fwd_col: str = "fwd_ret_20d") -> pd.DataFrame:
    sub = df.dropna(subset=[cls_col])
    rows = []
    for g, gd in sub.groupby(key, dropna=False):
        n = len(gd)
        if n == 0:
            continue
        row = {
            key: g,
            "n": n,
            "維持型%": round(100 * (gd[cls_col] == "維持型").mean(), 1),
            "半戻し%": round(100 * (gd[cls_col] == "半戻し型").mean(), 1),
            "全戻し%": round(100 * (gd[cls_col] == "全戻し型").mean(), 1),
            "保持率中央値": round(gd[ret_col].median(), 2),
            "リターン中央値%": round(100 * gd[fwd_col].median(), 1),
        }
        if "redetect_30d" in gd.columns:
            rd = gd["redetect_30d"].dropna()
            row["再検知率%"] = round(100 * rd.astype(float).mean(), 1) if len(rd) >= 5 else None
        rows.append(row)
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    return out.sort_values("維持型%", ascending=False)


def md_table(df: pd.DataFrame) -> str:
    """tabulate 非依存の Markdown テーブル整形。"""
    if df.empty:
        return "(該当なし)\n"
    cols = [str(c) for c in df.columns]
    lines = ["| " + " | ".join(cols) + " |",
             "|" + "|".join("---" for _ in cols) + "|"]
    for _, row in df.iterrows():
        cells = ["" if pd.isna(v) else str(v) for v in row.tolist()]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def quartile_col(s: pd.Series, labels: list[str]) -> pd.Series:
    try:
        return pd.qcut(s, q=len(labels), labels=labels, duplicates="drop")
    except Exception:
        return pd.Series([None] * len(s), index=s.index)


def build_report(feat: pd.DataFrame, news: dict | None) -> str:
    sub20 = feat.dropna(subset=["class_20d"]).copy()
    sub5 = feat.dropna(subset=["class_5d"]).copy()

    L = []
    L.append("# 急騰後アウトカム分析レポート\n")
    L.append(f"生成: {datetime.now(JST).strftime('%Y-%m-%d %H:%M')} JST\n")
    L.append(f"- 分析対象イベント: {len(feat)}件"
             f" (20日判定可能: {len(sub20)}件 / 5日判定可能: {len(sub5)}件)")
    L.append(f"- 期間: {feat['surge_date'].min()} 〜 {feat['surge_date'].max()}")
    L.append(f"- 判定基準: 保持率 = (N日後終値-急騰前終値)/(急騰日終値-急騰前終値)。"
             f" 維持型>= {HOLD_THRESHOLD} / 半戻し 0〜{HOLD_THRESHOLD} / 全戻し<=0\n")

    L.append("## 全体分布(20営業日後)\n")
    total = len(sub20)
    if total == 0:
        L.append("(20日後判定が可能なイベントがまだありません。--limit を外すか期間を広げてください)\n")
    else:
        for c in ["維持型", "半戻し型", "全戻し型"]:
            k = (sub20["class_20d"] == c).sum()
            L.append(f"- {c}: {k}件 ({100*k/total:.1f}%)")
        L.append(f"- 保持率中央値: {sub20['retention_20d'].median():.2f}")
        L.append(f"- 急騰日終値から20日後のリターン中央値: {100*sub20['fwd_ret_20d'].median():.1f}%\n")

    L.append("## 1. 材料の質(カテゴリ別) — 本命の比較\n")
    L.append(md_table(group_table(sub20, "category")))
    # 決算系 vs テーマ物色の検定
    earn_like = sub20[sub20["category"].isin([CAT_EARN, CAT_EARN_RETURN])]
    theme = sub20[sub20["category"] == CAT_THEME]
    if len(earn_like) >= 10 and len(theme) >= 10:
        p = two_prop_z(
            (earn_like["class_20d"] == "維持型").sum(), len(earn_like),
            (theme["class_20d"] == "維持型").sum(), len(theme),
        )
        L.append(f"\n**決算系(決算・業績+決算+還元) vs テーマ物色** の維持率差: "
                 f"{100*(earn_like['class_20d']=='維持型').mean():.1f}% vs "
                 f"{100*(theme['class_20d']=='維持型').mean():.1f}%"
                 f" (両側p値 {p:.4f})\n" if p is not None else "\n")
    L.append("> 文献の予測: 決算・業績起点は継続(PEAD; Bernard&Thomas 1989, "
             "Pritamani&Singal 2001)、具体的ニュースを伴わない物色は反転(Chan 2003)。\n")

    L.append("## 2. 異常出来高(急騰日出来高÷前20日平均)\n")
    sub20["出来高倍率"] = quartile_col(sub20["vol_ratio"], ["Q1(小)", "Q2", "Q3", "Q4(大)"])
    L.append(md_table(group_table(sub20, "出来高倍率")))
    L.append("> 文献の予測: 大出来高を伴う急騰は継続しやすい(Pritamani&Singal 2001)。"
             "ただし小型株では継続・大型流動株では反転という属性依存あり(Llorente et al. 2002)。\n")

    L.append("## 3. サイズ(時価総額)と流動性(売買代金)\n")
    cap = sub20.dropna(subset=["market_cap"]).copy()
    if len(cap) > 30:
        cap["時価総額"] = quartile_col(cap["market_cap"], ["小型", "中型", "大型"])
        L.append(md_table(group_table(cap, "時価総額")))
    sub20["売買代金"] = quartile_col(sub20["value_traded_avg20"], ["低流動", "中流動", "高流動"])
    L.append(md_table(group_table(sub20, "売買代金")))
    L.append("> 文献の予測: 反転(全戻し)は小型・低流動性株に集中する"
             "(Avramov, Chordia & Goyal 2006; Chan 2003)。\n")

    L.append("## 4. 急騰の極端さ\n")
    sub20["急騰率"] = quartile_col(sub20["surge_ret"], ["Q1(小)", "Q2", "Q3", "Q4(大)"])
    L.append(md_table(group_table(sub20, "急騰率")))
    L.append("> 文献の予測: 極端な単日リターン(MAX)が大きいほどその後アンダーパフォーム"
             "(Bali, Cakici & Whitelaw 2011)。\n")

    L.append("## 5. ストップ高\n")
    sub20["S高"] = sub20.apply(
        lambda r: "張り付き" if r["stop_high_close"]
        else ("タッチ" if r["stop_high_touch"] else "なし"), axis=1)
    L.append(md_table(group_table(sub20, "S高")))
    L.append("> 文献の予測: 値幅制限は価格発見を翌日以降に持ち越す(Kim & Rhee 1997, 東証)。"
             "S高銘柄は短期(5日)では続伸しやすいが、その後の反動も大きい可能性。\n")

    L.append("## 6. 注目度(検知ソース)\n")
    L.append(md_table(group_table(sub20, "source")))
    L.append("> shodousan=有名アカウント経由(注目度・高)、kyutou=自前検知。"
             "個人の注目買いは一時的な価格圧力になりやすい(Barber & Odean 2008)。\n")

    if news:
        merged = sub20.copy()
        merged["news_count"] = merged.apply(
            lambda r: news.get(f"{r['stock_code']}_{r['surge_date']}"), axis=1)
        nn = merged.dropna(subset=["news_count"]).copy()
        if len(nn) > 30:
            L.append("## 6b. ニュース件数(検知日±2日, Google News)\n")
            nn["ニュース"] = pd.cut(nn["news_count"], bins=[-1, 0, 3, 10, 1e9],
                                    labels=["0件", "1-3件", "4-10件", "11件以上"])
            L.append(md_table(group_table(nn, "ニュース")))
            L.append("> 文献の予測: ニュースを伴う値動きは継続、ニュースなしは反転(Chan 2003)。\n")

    L.append("## 7. 継続性タイプ(続報期待スコア≥{}を続報期待型に再定義)\n".format(FOLLOWUP_THRESHOLD))
    L.append(md_table(group_table(sub20, "continuation_type")))

    L.append("### 続報期待スコア帯別\n")
    sc = sub20.dropna(subset=["followup_score"]).copy()
    sc["スコア帯"] = pd.cut(sc["followup_score"], bins=[-99, -1, 0, 2, 4, 99],
                            labels=["減点(<0)", "0", "1-2", "3-4", "5+"])
    L.append(md_table(group_table(sc, "スコア帯")))
    L.append("> スコアが高い(=続報が構造的に想定される)ほど維持されやすいかの検証。"
             "閾値はコード先頭 FOLLOWUP_RUBRIC / FOLLOWUP_THRESHOLD で調整可能。\n")
    L.append("> 仮説: 続報が構造的に想定される材料(続報期待型)や確定カタリスト持ち(定期イベント型)は"
             "維持されやすく、一過性完結型は恒久リプライシング(その水準で完結)、"
             "外部依存型はテーマのニュースフロー次第。再検知率%は実際に30日以内へ後続材料が出た割合。\n")

    L.append("### 再検知(実際に後続が出たか)別の成績\n")
    rd = sub20.dropna(subset=["redetect_30d"]).copy()
    rd["再検知"] = rd["redetect_30d"].map({True: "30日以内にあり", False: "なし"})
    L.append(md_table(group_table(rd, "再検知")))
    L.append("> 注意: 再検知は事後情報(結果として続報が出たか)。事前予測には上の継続性タイプと"
             "テーマ過熱度を使う。\n")

    L.append("## 8. テーマ過熱度(直近14日に同一キーワードの他銘柄急騰が何件あったか)\n")
    th = sub20.dropna(subset=["theme_heat"]).copy()
    if len(th) > 50:
        th["過熱度"] = pd.cut(th["theme_heat"], bins=[-1, 0, 3, 10, 1e9],
                              labels=["0件", "1-3件", "4-10件", "11件以上"])
        L.append(md_table(group_table(th, "過熱度")))
        ext = th[th["continuation_type"] == CONT_EXTERNAL]
        if len(ext) > 50:
            L.append("### 外部依存型(テーマ物色)に絞った過熱度別\n")
            ext = ext.copy()
            ext["過熱度"] = pd.cut(ext["theme_heat"], bins=[-1, 0, 3, 10, 1e9],
                                   labels=["0件", "1-3件", "4-10件", "11件以上"])
            L.append(md_table(group_table(ext, "過熱度")))
        L.append("> 実測のニュースフロー強度。過熱=後続が出やすい(継続)のか、"
                 "過熱=祭りの終盤(反転)なのかをデータで判定する。\n")

    L.append("## 短期(5営業日後)との比較\n")
    L.append(md_table(group_table(sub5, "category", cls_col="class_5d",
                                  ret_col="retention_5d", fwd_col="fwd_ret_5d")))

    L.append("## 注意事項\n")
    L.append("- 本サンプルはX上で注目された銘柄のみで構成され、市場全体を代表しない"
             "(注目バイアス)。カテゴリ間の**相対比較**として解釈すること。")
    L.append("- 急騰時価格は検知日の終値近似。ザラ場中の検知では実際の急騰起点とずれる。")
    L.append("- 検知期間が約4ヶ月と短く、この間の地合い(相場全体の方向)の影響を受ける。")
    L.append("- n<30 のセルは統計的に不安定。数字を鵜呑みにしない。")
    return "\n".join(L)


# ─────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="最新N件のみで動作確認")
    ap.add_argument("--no-llm", action="store_true", help="LLM補完を使わない")
    ap.add_argument("--with-news", action="store_true", help="ニュース件数も取得(時間がかかる)")
    args = ap.parse_args()

    if not SUPABASE_URL or not SUPABASE_KEY:
        print("SUPABASE_URL / SUPABASE_SERVICE_KEY が未設定です(.env を確認)")
        sys.exit(1)

    OUT_DIR.mkdir(exist_ok=True)
    CACHE_DIR.mkdir(exist_ok=True)

    df = fetch_events(args.limit)
    df = classify_reasons(df, use_llm=not args.no_llm)
    df = add_followup_signals(df)
    print("継続性内訳:", dict(df["continuation_type"].value_counts()))

    codes = sorted(df["stock_code"].unique())
    prices = load_price_data(codes)
    caps = load_market_caps(codes)

    news = fetch_news_counts(df) if args.with_news else None

    feat = compute_features(df, prices, caps)
    if feat.empty:
        print("特徴量を計算できたイベントがありません")
        sys.exit(1)

    csv_path = OUT_DIR / "surge_outcome_features.csv"
    feat.to_csv(csv_path, index=False, encoding="utf-8-sig")
    report = build_report(feat, news)
    md_path = OUT_DIR / "surge_outcome_report.md"
    md_path.write_text(report, encoding="utf-8")

    print(f"\n完了:\n  {csv_path}\n  {md_path}")


if __name__ == "__main__":
    main()
