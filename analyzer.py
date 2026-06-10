"""
Claude API アナライザー
ツイートから株価上昇理由をテンプレートに沿って分析する
"""

import anthropic
import time
from dataclasses import dataclass
from datetime import timezone, timedelta
from fetcher import Tweet
from config import ANTHROPIC_API_KEY


@dataclass
class Analysis:
    tweet: Tweet
    stock_code: str        # 主な銘柄コード
    stock_name: str        # 銘柄名（推定）
    reason: str            # 理由1 (例: 好決算)
    business: str          # 事業内容
    keywords: str          # キーワード
    backgrounds: list[str] # 背景リスト (最大5件)
    related_stocks: str    # 関連銘柄
    raw_output: str        # Claudeの生出力


SYSTEM_PROMPT = """あなたは日本株の株価上昇理由を分析するアナライザーです。
Xのポストから銘柄を特定し、株価上昇の理由を簡潔・的確にレポートします。

分析のポリシー:
- レポート口調（「〜である」「〜とみられる」「〜が追い風となっている」等）
- 文章は簡潔に。ただし重要な箇所は丁寧に説明する
- 理由1は「好決算」「上方修正」「株主優待導入」など、実際の株価急騰のきっかけとなった具体的な材料を記載する。「初動検知」「シグナル検知」のようなシグナル自体を理由にしてはならない。ポストから具体的な材料が読み取れない場合は、その銘柄の直近IRや業績動向から一般的に考えられる材料を推測して記載する
- 背景は「なぜ業績が良いのか」「何が強みか」という実質的な内容
- 関連銘柄は同様の恩恵が期待できる具体的な銘柄コードと銘柄名を挙げる
- 注記・免責・補足説明は一切出力しない"""


RETRYABLE_ANTHROPIC_EXCEPTIONS = (
    anthropic.RateLimitError,
    anthropic.InternalServerError,
    anthropic.APIConnectionError,
    anthropic.APITimeoutError,
)

RETRYABLE_STATUS_CODES = {408, 409, 429, 500, 502, 503, 504}


def _extract_api_error_type(exc: Exception) -> str:
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            return str(error.get("type", "") or "")
    return ""


def _is_retryable_anthropic_error(exc: Exception) -> bool:
    if isinstance(exc, RETRYABLE_ANTHROPIC_EXCEPTIONS):
        return True

    if isinstance(exc, anthropic.APIStatusError):
        if getattr(exc, "status_code", None) in RETRYABLE_STATUS_CODES:
            return True

    error_type = _extract_api_error_type(exc).lower()
    if error_type in {"overloaded_error", "rate_limit_error"}:
        return True

    return "overloaded" in str(exc).lower()


def _stream_analysis_with_retry(client: anthropic.Anthropic, user_prompt: str, max_attempts: int = 4):
    last_error = None

    for attempt in range(1, max_attempts + 1):
        try:
            with client.messages.stream(
                model="claude-sonnet-4-6",
                max_tokens=2048,
                system=SYSTEM_PROMPT,
                messages=[
                    {"role": "user", "content": user_prompt}
                ]
            ) as stream:
                return stream.get_final_message()
        except Exception as exc:
            last_error = exc
            if not _is_retryable_anthropic_error(exc) or attempt == max_attempts:
                raise

            delay_sec = min(30, 3 * (2 ** (attempt - 1)))
            print(
                f"  [Claude] transient API error: {type(exc).__name__}. "
                f"Retrying in {delay_sec}s ({attempt}/{max_attempts})"
            )
            time.sleep(delay_sec)

    raise last_error


def _build_materials_section(materials: dict | None) -> str:
    """当日の一次情報（TDnet開示・ニュース見出し）をプロンプト追記用に整形する。

    materials が None（取得失敗・未取得）の場合は空文字を返し、
    プロンプトは従来と完全に同一になる（グラウンディングなしで続行）。
    """
    if not materials:
        return ""
    tdnet = materials.get("tdnet") or []
    news = materials.get("news") or []
    if not tdnet and not news:
        return ""

    tdnet_block = "\n".join(f"・{t}" for t in tdnet) if tdnet else "（該当なし）"
    news_block = "\n".join(f"・{t}" for t in news) if news else "（該当なし）"
    return f"""

【参考: 当日の一次情報（前営業日15時以降〜当日）】
■ TDnet適時開示:
{tdnet_block}
■ ニュース見出し（株探）:
{news_block}

上記に急騰の材料となる開示・報道があれば、それを(理由1)として最優先で採用すること。
複数ある場合は株価インパクトが最も大きいものを選ぶこと。
「ストップ高」「動意株」など結果を伝えるだけの見出しは理由にしないこと。"""


def _build_analysis_prompt(tweet: Tweet, materials: dict | None = None) -> str:
    codes_str = "、".join(tweet.stock_codes) if tweet.stock_codes else "不明"
    materials_section = _build_materials_section(materials)
    return f"""以下のポストを分析し、株価上昇理由をテンプレートに沿って出力してください。

【ポスト内容】
{tweet.text}

【抽出済み銘柄コード】
{codes_str}

【出力テンプレート（()内のみ記入し、それ以外は変更しないこと）】
銘柄: (銘柄コード) (銘柄名)

(理由1)によるもの
事業内容:(事業内容)
キーワード:(キーワード)
・(背景1)
・(背景2)
・(背景3)

関連銘柄
(銘柄コード: 銘柄名 — 理由)

【記入ルール】
1. ()の中に入力し、それ以外のテキストは変更しない
2. (理由1)は「好決算」「上方修正」「株主優待導入」など、実際に株価が動いたきっかけとなる具体的な材料を記載する。「初動検知」「シグナル」などシグナル自体を理由にしない。ポストから読み取れない場合は直近IRや業績動向から推測する
3. (事業内容)はその企業が何をしている会社かを1文で簡潔にまとめる
4. (背景)は好業績の仕組み・独自の強み・市場環境など。情報が多ければ4〜5個記載してもよい
5. (キーワード)はテーマや業界トレンド（例: AI需要、インバウンド、防衛費増額）
6. 関連銘柄は同様の恩恵が期待できる具体的な銘柄を2〜4社記載
7. ポストだけでは情報が不足している場合、一般的な知識を補って分析する
8. テンプレート以外のテキスト（注記・免責・補足説明など）は一切出力しない"""\
        + materials_section


def _strip_code_parens(text: str) -> str:
    """テンプレート由来の括弧（半角・全角）を剥がす。

    Claudeが「銘柄: (5982) (マルゼン)」のように括弧ごと返すことがあり、
    そのままだと重複防止・Notion・Supabase・ファイル名すべてに波及するため、
    パース時点で正規化する。
    """
    t = (text or "").strip()
    while len(t) >= 2 and t[0] in "(（" and t[-1] in ")）":
        t = t[1:-1].strip()
    return t


def _parse_output(text: str, tweet: Tweet) -> Analysis:
    """Claudeの出力をパースして Analysis オブジェクトに変換する"""
    lines = text.strip().split("\n")

    stock_code = tweet.stock_codes[0] if tweet.stock_codes else "不明"
    stock_name = ""
    reason = ""
    business = ""
    keywords = ""
    backgrounds = []
    related_stocks = ""
    in_related = False

    for line in lines:
        line = line.strip()
        if not line:
            continue

        if line.startswith("銘柄:"):
            parts = line.replace("銘柄:", "").strip().split(" ", 1)
            if parts:
                stock_code = parts[0].strip()
            if len(parts) > 1:
                stock_name = parts[1].strip()

        elif "によるもの" in line and not line.startswith("関連"):
            reason = line.replace("によるもの", "").strip()

        elif line.startswith("事業内容:"):
            business = line.replace("事業内容:", "").strip()

        elif line.startswith("キーワード:"):
            keywords = line.replace("キーワード:", "").strip()

        elif line.startswith("・"):
            backgrounds.append(line[1:].strip())

        elif line.startswith("関連銘柄"):
            in_related = True

        elif in_related and line:
            related_stocks += line + "\n"

    return Analysis(
        tweet=tweet,
        stock_code=_strip_code_parens(stock_code),
        stock_name=_strip_code_parens(stock_name),
        reason=reason,
        business=business,
        keywords=keywords,
        backgrounds=backgrounds,
        related_stocks=related_stocks.strip(),
        raw_output=text
    )


def analyze_tweet(tweet: Tweet) -> Analysis:
    """
    1件のツイートをClaudeで分析する

    Args:
        tweet: 分析対象のツイート

    Returns:
        Analysis オブジェクト
    """
    if not ANTHROPIC_API_KEY:
        raise ValueError(
            "ANTHROPIC_API_KEY が設定されていません。\n"
            ".env ファイルに ANTHROPIC_API_KEY を設定してください。"
        )

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # 当日の一次情報（TDnet開示・株探ニュース見出し）を取得して根拠付け。
    # 取得失敗しても従来どおり材料なしで分析を続行する。
    materials = None
    if tweet.stock_codes:
        try:
            from materials import fetch_materials
            day_str = tweet.created_at.astimezone(
                timezone(timedelta(hours=9))).strftime("%Y-%m-%d")
            materials = fetch_materials(tweet.stock_codes[0], day_str)
            print(f"  [材料] TDnet開示 {len(materials['tdnet'])}件 / "
                  f"ニュース見出し {len(materials['news'])}件")
        except Exception as e:
            print(f"  [WARN] 材料取得失敗（材料なしで分析続行）: {e}")
            materials = None

    print(f"  [Claude] 分析中: {tweet.text[:50]}...")

    response = _stream_analysis_with_retry(client, _build_analysis_prompt(tweet, materials))

    # テキストブロックを抽出
    output_text = ""
    for block in response.content:
        if block.type == "text":
            output_text += block.text

    return _parse_output(output_text, tweet)


def analyze_tweets(tweets: list[Tweet]) -> list[Analysis]:
    """複数ツイートをまとめて分析する"""
    analyses = []
    for i, tweet in enumerate(tweets, 1):
        print(f"[分析 {i}/{len(tweets)}] {tweet.stock_codes}")
        try:
            analysis = analyze_tweet(tweet)
            analyses.append(analysis)
        except Exception as e:
            print(f"  [ERROR] 分析失敗: {e}")
    return analyses
