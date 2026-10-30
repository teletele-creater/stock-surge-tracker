"""急騰の中身を全文読ませて押し目の成否を判断させる
============================================================
これまで LLM には「5軸のラベル付け」しかさせておらず、事業内容・強材料ポイント
・キーワード・関連銘柄を通して読んで判断させたことが無かった。ここではそれをやる。

この方式の重要な性質:
  LLM の判断は結果データ(outcome_v2)を一切見ない。したがって当てはめによる
  過学習が原理的に起こらず、**判定済み全件が out-of-sample** になる。
  ロジスティック回帰の候補が126件しか評価できなかったのに対し、こちらは
  235件すべてで評価できる。パラメータ数0の予測器の利点。

守っている規律:
  プロンプトを結果に合わせて調整しない。ドメインの理屈で一度書き、出た数字を
  そのまま採用する。プロンプトを回して数字が良くなるまで直すのは、これまで
  機械側で潰してきた過学習を人間の手で再導入する行為なので禁止。

  ベース率(約35%)だけはプロンプトに書いている。これは集計値1つの開示であり、
  校正のために必要。それ以外の結果情報は渡していない。

判定の定義もプロンプトに明記する(翌営業日始値で買い、-8%で損切り、10営業日):
  何を予測しているのかを曖昧にすると、LLM は「良い会社か」を答えてしまう。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

import pandas as pd

CACHE_NAME = "reason_llm_judge.json"
MODEL = "claude-haiku-4-5-20251001"
BATCH = 8

RANKS = ["S", "A", "B", "C"]

SYSTEM = """あなたは日本株の押し目買いを判断するアナリストです。

【予測してほしいこと】
急騰した銘柄が25日移動平均線まで押した。この押し目を次の条件で買った場合に
「上昇」で終わるか「下落」で終わるかを予測してください。
  - 25日線タッチ日の翌営業日の始値で買う
  - 10営業日以内に日中安値が -8% に触れたらその時点で損切り(=下落)
  - 触れなければ10営業日後の終値がエントリーより上なら上昇、下なら下落
  - 利確はしない(上限を置かず10営業日持つ)

この条件での過去の実績は「上昇」が約35%です。つまり大半は下落します。
安易に高い確率を出さないでください。35%が基準で、そこから上下に振ってください。

【判断材料】
急騰の理由・事業内容・強材料ポイント・キーワード・関連銘柄を渡します。
株価や日付は渡しません。materialの中身だけで判断してください。

【考慮すべき観点】
- 材料が既に消費されたか、まだ続報や上振れの余地が残っているか
- 数字の裏付けが確定実績か、単なる期待・思惑・推測か
- 需要が構造的に続くのか、一過性のイベントなのか
- そのテーマに買い手が継続的につくか、単発の人気で終わるか
- 押し目で拾う買い手がいる規模・知名度の材料か

【出力形式】
1行1件。番号|確率|ランク|20字以内の根拠
  確率: 0〜100の整数(この押し目が「上昇」で終わる確率)
  ランク: S(最有望) / A / B / C(見送り)
説明や前置きは書かないこと。

例:
1|48|A|上方修正未消費・半導体需要継続"""


def _key(payload: str) -> str:
    return hashlib.md5(payload.encode()).hexdigest()


def _payload(row: pd.Series) -> str:
    """LLM に見せる中身。株価・日付・結果は一切入れない。"""
    def g(c, default=""):
        v = row.get(c, default)
        return "" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v)

    kw = g("keywords")
    parts = [
        f"銘柄: {g('stock_name')}",
        f"急騰理由: {g('reason')}",
        f"事業内容: {g('business')}",
        f"キーワード: {kw}",
        f"強材料ポイント: {g('backgrounds_text')}",
        f"関連銘柄: {g('related_stocks')[:200]}",
    ]
    return "\n".join(p for p in parts if not p.endswith(": "))


def _parse(text: str, batch: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for line in text.strip().splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3:
            continue
        m = re.match(r"\s*(\d+)", parts[0])
        if not m:
            continue
        i = int(m.group(1)) - 1
        if not (0 <= i < len(batch)):
            continue
        try:
            prob = float(re.sub(r"[^\d.]", "", parts[1]))
        except ValueError:
            continue
        if not (0 <= prob <= 100):
            continue
        rank = parts[2] if parts[2] in RANKS else "B"
        note = parts[3][:40] if len(parts) > 3 else ""
        out[batch[i]] = {"prob": prob / 100.0, "rank": rank, "note": note}
    return out


def add_judgement(df: pd.DataFrame, cache_dir: Path, api_key: str = "",
                  use_llm: bool = True) -> pd.DataFrame:
    """llm_prob / llm_rank / llm_note 列を足して返す。キャッシュ優先。"""
    cache_file = cache_dir / CACHE_NAME
    cache: dict[str, dict] = {}
    if cache_file.exists():
        try:
            cache = json.loads(cache_file.read_text(encoding="utf-8"))
        except Exception:
            cache = {}

    payloads = df.apply(_payload, axis=1)
    uniq = sorted(set(payloads))
    pending = [p for p in uniq if _key(p) not in cache]

    if pending and use_llm and api_key:
        print(f"中身の読み込み判断: 未キャッシュ {len(pending)}件 → LLM")
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        for i in range(0, len(pending), BATCH):
            batch = pending[i:i + BATCH]
            numbered = "\n\n".join(
                f"--- {j+1} ---\n{p}" for j, p in enumerate(batch))
            try:
                resp = client.messages.create(
                    model=MODEL, max_tokens=1200, system=SYSTEM,
                    messages=[{"role": "user", "content": numbered}])
                got = _parse(resp.content[0].text, batch)
            except Exception as e:
                print(f"  [WARN] このバッチは判断できず欠損扱い: {e}")
                got = {}
            for p, rec in got.items():
                cache[_key(p)] = rec
            print(f"  判断: {min(i+BATCH, len(pending))}/{len(pending)}")
            time.sleep(0.4)
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(cache, ensure_ascii=False, indent=1),
                             encoding="utf-8")
    elif pending:
        print(f"注意: {len(pending)}件は中身の判断ができず欠損"
              "(APIキー未設定か use_llm=False)")

    recs = [cache.get(_key(p), {}) for p in payloads]
    out = df.copy()
    out["llm_prob"] = [r.get("prob") for r in recs]
    out["llm_rank"] = [r.get("rank") for r in recs]
    out["llm_note"] = [r.get("note") for r in recs]
    return out


def coverage(df: pd.DataFrame) -> str:
    if "llm_prob" not in df.columns:
        return "中身判断: 未実行"
    ok = df["llm_prob"].notna().sum()
    return (f"中身判断カバレッジ {ok}/{len(df)} ({100*ok/max(len(df),1):.0f}%) "
            f"平均確率 {100*df['llm_prob'].mean():.1f}%"
            if ok else "中身判断: 全件欠損")
