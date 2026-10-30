# -*- coding: utf-8 -*-
"""急落理由アナライザー — analyzer.py の下落版

analyzer.py(急騰) と対になる。materials.fetch_materials() が返す一次情報
(TDnet適時開示 / 株探・Yahooのニュース見出し) をそのまま渡し、Claude に
「なぜ落ちたか」を構造化して出させる。

急騰版との設計上の違い:

1. **開示の有無を最重要項目として明示的に出させる**
   米国5.5年・急落2,553件の検証で、その後の値動きを最も分けたのは
   「寄り付きギャップ(=引け後の個別ニュース起因)かどうか」だった。
       ほぼ全部ギャップ    期待値+0.00R / 6年中2年しかプラスにならず(戻らない)
       ザラ場型×市場連動   期待値+0.07R / 6年中5年プラス(戻る)
   TDnetを引けばこれを推定でなく事実として判定できる。したがって
   「開示なし」が正しく出ることがこのモジュールの一番の仕事。

2. **解説文(backgrounds)を主役にしない**
   急騰版の backgrounds は「なぜ業績が良いか・何が強みか」という
   Claudeの解説文で、事実の列挙ではない。押し目予測でこれを特徴量にした
   ところウォークフォワードのskillはマイナスだった。そこで下落版では
   「TDnetにあった開示タイトルそのもの」を evidence として残す。
   後から人が検証でき、LLMの作文が混ざらない。

3. **一過性/構造的の区別を出させる**
   特別損失の一括計上のような一過性の下げと、稼ぐ力そのものが落ちた
   下げでは戻り方が違うはず、という仮説を検証可能にするため。
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, asdict, field

import anthropic

from config import ANTHROPIC_API_KEY

MODEL = "claude-haiku-4-5-20251001"

# 出力の許容値。ここに無い値が返ったら欠損扱いにする(自由記述を許すと集計できない)
DISCLOSURE = ["下方修正", "決算", "特別損失", "増資希薄化", "減配無配",
              "業務提携解消", "訴訟不祥事", "上場廃止関連", "その他開示", "開示なし"]
NATURE = ["一過性", "構造的", "不明"]
SCOPE = ["個別事情", "業界全体", "市場全体", "不明"]
PRICED = ["過剰反応", "妥当", "織り込み不足", "不明"]
NEXTC = ["近い", "遠い", "不明"]


@dataclass
class DropAnalysis:
    stock_code: str
    stock_name: str
    drop_date: str
    reason: str = ""              # 20〜40字の要約
    disclosure: str = ""          # DISCLOSURE のいずれか
    evidence: list[str] = field(default_factory=list)   # 根拠にした開示/見出しの原文
    nature: str = ""              # NATURE
    scope: str = ""               # SCOPE
    priced: str = ""              # PRICED
    next_catalyst: str = ""       # NEXTC
    note: str = ""                # 一言所見
    backgrounds: list[str] = field(default_factory=list)  # 背景(最大4件)
    raw_output: str = ""

    def to_row(self) -> dict:
        d = asdict(self)
        d["evidence"] = " / ".join(self.evidence)
        d["backgrounds"] = "\n".join(f"・{b}" for b in self.backgrounds)
        return d


SYSTEM_PROMPT = f"""あなたは日本株の株価下落要因を分析するアナライザーです。
与えられた一次情報(TDnet適時開示のタイトル、ニュース見出し、企業の正式情報)だけを
根拠に、その日の急落の要因を判定します。

最重要のポリシー:
- **一次情報に無いことを書いてはならない。** 開示もニュースも見当たらない場合は、
  推測で下方修正や不祥事をでっち上げず、disclosure に「開示なし」と明記すること。
  「開示なし」は失敗ではなく、それ自体が価値のある観測結果である。
- evidence には、判断の根拠にした開示タイトル/見出しを**原文のまま**入れる。
  要約・言い換えをしない。根拠が無ければ空配列にする。
- 銘柄名・事業内容は与えられた正式情報に従う。社名からの連想で事業を創作しない。

出力は次のJSONだけ。前置き・注記・コードブロック記号は一切書かない。

{{"reason": "40字以内の要約",
 "disclosure": "{' / '.join(DISCLOSURE)} のいずれか",
 "evidence": ["根拠にした開示やニュースの見出し(原文)", "..."],
 "nature": "{' / '.join(NATURE)}",
 "scope": "{' / '.join(SCOPE)}",
 "priced": "{' / '.join(PRICED)}",
 "next_catalyst": "{' / '.join(NEXTC)}",
 "note": "60字以内の所見",
 "backgrounds": ["背景1", "背景2"]}}

各項目の判定基準:
- disclosure: その日(または前営業日引け後)に出た開示・ニュースの種別。
    下方修正      = 業績予想の下方修正
    決算          = 決算短信の内容自体が失望された
    特別損失      = 減損・特別損失・引当の計上
    増資希薄化    = 公募増資・新株予約権・転換社債など希薄化を伴うもの
    減配無配      = 配当予想の引き下げ・無配転落
    業務提携解消  = 契約解消・取引先喪失・受注取消
    訴訟不祥事    = 訴訟・行政処分・不適切会計・役員の不祥事
    上場廃止関連  = 監理/整理銘柄・上場廃止の可能性
    その他開示    = 上記以外の開示が下落要因とみられる場合
    開示なし      = 該当する開示もニュースも見当たらない
- nature: 一過性 = 今回限りの損失やイベントで、稼ぐ力自体は変わっていない
          構造的 = 需要・シェア・採算など継続的な収益力が落ちた
- scope:  個別事情 / 業界全体(同業も同時に下落) / 市場全体(地合い) / 不明
- priced: 下げ幅が材料の重さに対して 過剰反応 / 妥当 / 織り込み不足 のどれか
- next_catalyst: 次に答え合わせが来るのが1ヶ月以内なら「近い」、四半期以上先なら「遠い」
- backgrounds: その材料がなぜ売られたのかを補う文脈を、1項目40字以内で最大4件。
    例: 事業の内容や収益構造 / その材料が効いた理由 / 同業や業界の状況 /
        直前の株価位置(高値圏だった等) / 今後の注目点
    **一次情報と企業の正式情報から言えることだけを書く。** 材料が無い場合は
    事業内容や地合いなど、確認できた事実だけを書き、憶測で材料を作らない。
    書けることが無ければ空配列にする。
"""


def _build_user_message(code: str, name: str, day: str, drop_pct: float,
                        vol_ratio: float, materials: dict, profile: str) -> str:
    tdnet = materials.get("tdnet") or []
    news = materials.get("news") or []
    lines = [f"【銘柄】{code} {name}",
             f"【下落日】{day}",
             f"【下落率】{drop_pct*100:.1f}%   【出来高】前20日平均の{vol_ratio:.1f}倍",
             f"【企業の正式情報】{profile or '(取得できず)'}",
             "",
             "【TDnet適時開示】" + (f"({len(tdnet)}件)" if tdnet else "なし")]
    lines += [f"  - {t}" for t in tdnet[:20]] or ["  (この窓に開示なし)"]
    lines += ["", "【ニュース見出し】" + (f"({len(news)}件)" if news else "なし")]
    lines += [f"  - {t}" for t in news[:25]] or ["  (この窓にニュースなし)"]
    if not materials.get("tdnet_ok", True) or not materials.get("news_ok", True):
        lines += ["", "※注意: 一部ソースの取得に失敗している。"
                  "『開示なし』と断定せず disclosure は『不明』寄りに扱うこと"]
    return "\n".join(lines)


def _parse(text: str) -> dict:
    """JSONを取り出す。前後にゴミが付いていても拾えるようにする。"""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {}
    try:
        d = json.loads(m.group(0))
    except Exception:
        return {}
    if d.get("disclosure") not in DISCLOSURE:
        d["disclosure"] = ""
    if d.get("nature") not in NATURE:
        d["nature"] = ""
    if d.get("scope") not in SCOPE:
        d["scope"] = ""
    if d.get("priced") not in PRICED:
        d["priced"] = ""
    if d.get("next_catalyst") not in NEXTC:
        d["next_catalyst"] = ""
    ev = d.get("evidence")
    d["evidence"] = [str(x) for x in ev][:8] if isinstance(ev, list) else []
    bg = d.get("backgrounds")
    d["backgrounds"] = [str(x)[:60] for x in bg][:4] if isinstance(bg, list) else []
    return d


def analyze(code: str, name: str, day: str, drop_pct: float, vol_ratio: float,
            materials: dict, profile: str = "",
            api_key: str = "", retries: int = 2) -> DropAnalysis:
    """1銘柄ぶんの急落理由を判定する。失敗しても例外を投げず空で返す。"""
    out = DropAnalysis(stock_code=code, stock_name=name, drop_date=day)
    key = api_key or ANTHROPIC_API_KEY
    if not key:
        out.note = "APIキー未設定"
        return out
    client = anthropic.Anthropic(api_key=key)
    msg = _build_user_message(code, name, day, drop_pct, vol_ratio, materials, profile)
    for attempt in range(retries + 1):
        try:
            resp = client.messages.create(
                model=MODEL, max_tokens=1200, system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": msg}])
            text = resp.content[0].text
            break
        except Exception as e:
            if attempt >= retries:
                out.note = f"LLM失敗: {e}"
                return out
            time.sleep(2 * (attempt + 1))
    out.raw_output = text
    d = _parse(text)
    out.reason = str(d.get("reason", ""))[:60]
    out.disclosure = d.get("disclosure", "")
    out.evidence = d.get("evidence", [])
    out.nature = d.get("nature", "")
    out.scope = d.get("scope", "")
    out.priced = d.get("priced", "")
    out.next_catalyst = d.get("next_catalyst", "")
    out.note = str(d.get("note", ""))[:80]
    out.backgrounds = d.get("backgrounds", [])
    return out
