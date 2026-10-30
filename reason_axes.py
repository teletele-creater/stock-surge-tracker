"""急騰理由を「直交する複数の軸」に分解する
============================================================
なぜ1本の細かいラベルにしないか:
  判定済み235件に対し理由テキストは191種がユニーク。生テキストをラベルにすると
  ほぼ1件1ラベル = 暗記になる。「細分化」を素直にやると必ずこうなる。

そこで1本の高カーディナリティなラベルではなく、**低カーディナリティな軸を
複数**立てる。各軸が3〜9水準なら1水準あたり n=20〜80 を確保でき、しかも
情報は掛け合わせで細かく持てる(8×4×3×9×4 = 3456通りを5列で表現できる)。

軸は実際の理由テキストを読んで設計した。ほぼ全件が
「〜の受注拡大・業績上方修正期待」という同型の文なので、
そこで実際に差が出ている次元 = 材料種別 / 確度 / 主体 / テーマ / 持続性 を取る。

使い方は既存の classify_reasons と同じ思想: md5 キーの JSON キャッシュを持ち、
未知の理由だけ Claude Haiku にまとめて投げる。384種を一度払えば以降は無料。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

import pandas as pd

MISSING = "欠損"

# 各軸の許容水準。LLM の出力はここに載っているものだけ採用する。
AXES: dict[str, list[str]] = {
    # 何が材料か
    "ax_material": ["業績上振れ", "受注契約", "資本政策", "M&A資本異動",
                    "承認治験", "提携新製品", "政策規制", "需給思惑"],
    # どれだけ固まった話か(unresolved の細分化)
    "ax_certainty": ["確定発表", "会社観測", "第三者報道", "思惑のみ"],
    # 誰が出した話か
    "ax_agent": ["自社発", "他社発", "業界全体"],
    # どのテーマに属すか
    "ax_theme": ["半導体", "AIデータセンター", "防衛宇宙", "電力再エネ",
                 "医薬バイオ", "金融DX", "内需消費", "素材産業機械", "その他"],
    # 続報が続く構造か
    "ax_duration": ["単発完結", "四半期再確認", "継続受注フロー", "長期テーマ"],
}
AXIS_COLS = list(AXES)

_ORDER = ["ax_material", "ax_certainty", "ax_agent", "ax_theme", "ax_duration"]

LLM_SYSTEM = """あなたは日本株の急騰理由テキストを分類する専門家です。
各行の理由を、以下5つの軸で分類してください。

軸1 材料種別: 業績上振れ / 受注契約 / 資本政策 / M&A資本異動 / 承認治験 / 提携新製品 / 政策規制 / 需給思惑
  - 業績上振れ: 上方修正・好決算・増益など業績数値そのもの
  - 受注契約: 個別の受注・契約獲得・受注拡大
  - 資本政策: 増配・自己株買い・株式分割・優待
  - M&A資本異動: TOB・子会社化・親子解消・買収
  - 承認治験: 薬事承認・治験結果・特許
  - 提携新製品: 業務提携・新製品投入・技術開発
  - 政策規制: 政策・補助金・規制変更
  - 需給思惑: 具体材料がなく思惑・人気・需給のみ

軸2 確度: 確定発表 / 会社観測 / 第三者報道 / 思惑のみ
  - 確定発表: 会社が正式に発表済み(「発表」「開示」)
  - 会社観測: 会社の見通し・上振れ観測レベル(「観測」「見通し」)
  - 第三者報道: 報道・アナリスト・外部観測
  - 思惑のみ: 「期待」「思惑」だけで裏付けが無い

軸3 主体: 自社発 / 他社発 / 業界全体
  - 自社発: その会社自身のニュース
  - 他社発: 親会社・取引先・同業など他社起因
  - 業界全体: 業界・テーマ全体の流れ

軸4 テーマ: 半導体 / AIデータセンター / 防衛宇宙 / 電力再エネ / 医薬バイオ / 金融DX / 内需消費 / 素材産業機械 / その他

軸5 持続性: 単発完結 / 四半期再確認 / 継続受注フロー / 長期テーマ
  - 単発完結: それで終わる一発イベント
  - 四半期再確認: 次の決算で再確認される
  - 継続受注フロー: 受注が継続的に積み上がる構造
  - 長期テーマ: 数年単位のテーマ

出力は1行1件、必ず次の形式のみ。説明や前置きは書かないこと:
番号|材料種別|確度|主体|テーマ|持続性

例:
1|業績上振れ|思惑のみ|自社発|半導体|四半期再確認"""


def _key(reason: str) -> str:
    return hashlib.md5(reason.encode()).hexdigest()


def _blank() -> dict[str, str]:
    return {c: MISSING for c in AXIS_COLS}


def _parse(line: str, batch: list[str]) -> tuple[int, dict[str, str]] | None:
    parts = [p.strip() for p in line.split("|")]
    if len(parts) < 6:
        return None
    m = re.match(r"\s*(\d+)", parts[0])
    if not m:
        return None
    idx = int(m.group(1)) - 1
    if not (0 <= idx < len(batch)):
        return None
    rec = {}
    for col, val in zip(_ORDER, parts[1:6]):
        rec[col] = val if val in AXES[col] else MISSING
    # 全部弾かれた行は失敗扱い(形式崩れを黙って通さない)
    if all(v == MISSING for v in rec.values()):
        return None
    return idx, rec


def _label_by_llm(reasons: list[str], api_key: str) -> dict[str, dict[str, str]]:
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    out: dict[str, dict[str, str]] = {}
    batch_size = 20
    for i in range(0, len(reasons), batch_size):
        batch = reasons[i:i + batch_size]
        numbered = "\n".join(f"{j+1}. {t}" for j, t in enumerate(batch))
        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=2000,
                system=LLM_SYSTEM,
                messages=[{"role": "user", "content": numbered}],
            )
            text = resp.content[0].text
        except Exception as e:
            print(f"  [WARN] LLM軸分解に失敗(このバッチは欠損扱い): {e}")
            continue
        for line in text.strip().splitlines():
            p = _parse(line, batch)
            if p:
                out[batch[p[0]]] = p[1]
        print(f"  軸分解: {min(i+batch_size, len(reasons))}/{len(reasons)}")
        time.sleep(0.5)
    return out


def add_axes(df: pd.DataFrame, cache_dir: Path, api_key: str = "",
             use_llm: bool = True) -> pd.DataFrame:
    """df["reason"] を5軸に分解した列を足して返す。キャッシュ優先。"""
    cache_file = cache_dir / "reason_axes.json"
    cache: dict[str, dict[str, str]] = {}
    if cache_file.exists():
        try:
            cache = json.loads(cache_file.read_text(encoding="utf-8"))
        except Exception:
            cache = {}

    reasons = df["reason"].fillna("").astype(str)
    uniq = sorted({r for r in reasons if r.strip()})
    pending = [r for r in uniq if _key(r) not in cache]

    if pending and use_llm and api_key:
        print(f"理由の軸分解: 未キャッシュ {len(pending)}件 → LLM")
        got = _label_by_llm(pending, api_key)
        for r, rec in got.items():
            cache[_key(r)] = rec
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(cache, ensure_ascii=False, indent=1),
                             encoding="utf-8")
    elif pending:
        print(f"注意: 理由 {len(pending)}件は軸分解できず欠損扱い"
              "(APIキー未設定か use_llm=False)")

    recs = [cache.get(_key(r), _blank()) if r.strip() else _blank()
            for r in reasons]
    axes_df = pd.DataFrame(recs, index=df.index)
    out = df.copy()
    for c in AXIS_COLS:
        out[c] = axes_df[c]
    return out


# ---------------------------------------------------------------------------
# 強材料ポイント(backgrounds)から作る特徴
# ---------------------------------------------------------------------------
# backgrounds は reason(20字要約)と違い中央値4項目・280字前後の本文が入っている。
# ただし中身を読むと大半は「〜が期待される」「〜とみられる」というLLM生成の
# 定性的な強気ナラティブで、上昇例と下落例が読み分けられない。
#
# 差が出るのは「硬い数字が入っているか」だった:
#   定量的な数字あり 55.0%(n=20) vs なし 33.0%(n=215) 差+22.0pt (z=1.97)
# 確定した決算数値を伴う材料と、曖昧なナラティブだけの材料を分ける指標として
# 数字の個数・推測語の密度・確定語の有無を取る。

MAT_COLS = ["mat_n_num", "mat_n_pct", "mat_n_bai", "mat_n_yen",
            "mat_n_hedge", "mat_n_confirm", "mat_paren", "mat_n_items"]

_RE_PCT = re.compile(r"\d[\d.]*\s*[%％]")
_RE_BAI = re.compile(r"\d[\d.]*\s*倍")
_RE_YEN = re.compile(r"\d[\d.]*\s*(?:億|兆|万)?円")
_RE_HEDGE = re.compile(r"とみられる|期待される|見込まれる|だろう|可能性|思われる|考えられる")
_RE_CONFIRM = re.compile(r"発表|開示|決定|確定|公表")


def add_material_features(df: pd.DataFrame) -> pd.DataFrame:
    """backgrounds_text から材料の「硬さ」を表す特徴を作る。"""
    out = df.copy()
    if "backgrounds_text" not in out.columns:
        for c in MAT_COLS:
            out[c] = 0
        return out
    t = out["backgrounds_text"].fillna("").astype(str)
    out["mat_n_pct"] = t.apply(lambda s: len(_RE_PCT.findall(s)))
    out["mat_n_bai"] = t.apply(lambda s: len(_RE_BAI.findall(s)))
    out["mat_n_yen"] = t.apply(lambda s: len(_RE_YEN.findall(s)))
    # 定量的な数字の総数。これが1以上かどうかが今のところ最も差の出る境目。
    out["mat_n_num"] = out["mat_n_pct"] + out["mat_n_bai"] + out["mat_n_yen"]
    out["mat_n_hedge"] = t.apply(lambda s: len(_RE_HEDGE.findall(s)))
    out["mat_n_confirm"] = t.apply(lambda s: len(_RE_CONFIRM.findall(s)))
    # 全体が丸括弧で囲まれている = 生成側が推測モードで書いた印
    out["mat_paren"] = t.apply(
        lambda s: 1 if s.strip()[:1] in ("(", "（") else 0)
    out["mat_n_items"] = t.apply(
        lambda s: len([x for x in s.split(" / ") if x.strip()]))
    return out


def axis_breakdown(df: pd.DataFrame, min_n: int = 5) -> pd.DataFrame:
    """軸ごとの水準別実績。細分化が効かない理由を診断するための表。

    水準が偏っている(1水準に大半が入る)なら、細かく分けても情報は増えない。
    逆に幅が大きく見えても、その幅が n=5 の水準から出ているならノイズ。
    """
    if AXIS_COLS[0] not in df.columns or "outcome_v2" not in df.columns:
        return pd.DataFrame()
    j = df.dropna(subset=["outcome_v2"]).copy()
    if j.empty:
        return pd.DataFrame()
    j["_up"] = (j["outcome_v2"] == "上昇").astype(float)
    rows = []
    for c in AXIS_COLS:
        if c not in j.columns:
            continue
        g = j.groupby(c).agg(n=("_up", "size"), up=("_up", "mean"),
                             ret=("ret_v2", "mean"))
        g = g[g["n"] >= min_n]
        if g.empty:
            continue
        top = g["n"].idxmax()
        rows.append({
            "軸": c.replace("ax_", ""),
            "水準数": len(g),
            "最大水準": f"{top} ({int(g.loc[top,'n'])}件"
                        f"={100*g.loc[top,'n']/len(j):.0f}%)",
            "上昇率の幅pt": round(100 * (g["up"].max() - g["up"].min()), 0),
            "幅を作る最小水準n": int(g.loc[g["up"].idxmax(), "n"]),
        })
    return pd.DataFrame(rows)


def coverage(df: pd.DataFrame) -> str:
    if AXIS_COLS[0] not in df.columns:
        return "軸列なし"
    n = len(df)
    parts = []
    for c in AXIS_COLS:
        ok = (df[c] != MISSING).sum()
        parts.append(f"{c.replace('ax_','')}:{100*ok/n:.0f}%")
    return f"軸分解カバレッジ(n={n}) " + " ".join(parts)
