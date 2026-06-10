"""
Notion ストレージ
分析結果を Notion データベースに保存する
"""

from notion_client import Client
from analyzer import Analysis
from config import NOTION_TOKEN, NOTION_DATABASE_ID


PRICE_PROPERTY_NAME = "株価"


def _rt(text: str) -> list[dict]:
    """rich_text ブロックを生成するヘルパー"""
    return [{"type": "rich_text", "rich_text": [{"type": "text", "text": {"content": text[:2000]}}]}]


def save_to_notion(analysis: Analysis) -> str | None:
    """
    分析結果を Notion DB に1行追加する

    Returns:
        作成したページのID。失敗時は None。
    """
    if not NOTION_TOKEN or not NOTION_DATABASE_ID:
        return None

    client = Client(auth=NOTION_TOKEN)
    tweet = analysis.tweet
    # created_at は @shodousan経由=UTC / kyutou経由=JST。JSTに変換してから書き出す
    # （変換せずに +09:00 を付けると9時間ズレる）
    from storage import _to_jst
    jst_datetime = _to_jst(tweet.created_at).strftime("%Y-%m-%dT%H:%M:%S+09:00")

    backgrounds_text = "\n".join(f"・{bg}" for bg in analysis.backgrounds) if analysis.backgrounds else ""

    properties = {
        "銘柄名": {
            "title": [{"type": "text", "text": {"content": analysis.stock_name or analysis.stock_code}}]
        },
        "銘柄コード": {
            "rich_text": [{"type": "text", "text": {"content": analysis.stock_code}}]
        },
        "日時": {
            "date": {"start": jst_datetime}
        },
        "上昇理由": {
            "rich_text": [{"type": "text", "text": {"content": analysis.reason}}]
        },
        "事業内容": {
            "rich_text": [{"type": "text", "text": {"content": analysis.business[:2000]}}]
        },
        "キーワード": {
            "multi_select": [{"name": kw.strip()} for kw in analysis.keywords.split("、") if kw.strip()]
        },
        "背景": {
            "rich_text": [{"type": "text", "text": {"content": backgrounds_text[:2000]}}]
        },
        "関連銘柄": {
            "rich_text": [{"type": "text", "text": {"content": analysis.related_stocks[:2000]}}]
        },
    }

    # 株価 (Number プロパティ) を取得して追加 (取得失敗時はスキップ)
    try:
        from image_generator import fetch_current_price
        price = fetch_current_price(analysis.stock_code)
    except Exception as e:
        print(f"  [Notion] 株価取得失敗(スキップ): {e}")
        price = None
    if price is not None:
        properties[PRICE_PROPERTY_NAME] = {"number": round(price, 2)}

    try:
        response = client.pages.create(
            parent={"database_id": NOTION_DATABASE_ID},
            properties=properties,
        )
        page_id = response["id"]
        price_log = f", price=¥{price:,.0f}" if price is not None else ""
        print(f"  [Notion] 保存完了: {analysis.stock_code} {analysis.stock_name} (page_id={page_id}{price_log})")
        return page_id
    except Exception as e:
        # 株価プロパティが Notion DB に未定義だと validation error になるため、
        # その場合は株価を外して再試行する
        if PRICE_PROPERTY_NAME in properties:
            print(f"  [Notion] 株価プロパティで保存失敗、株価を除外して再試行: {e}")
            properties.pop(PRICE_PROPERTY_NAME, None)
            try:
                response = client.pages.create(
                    parent={"database_id": NOTION_DATABASE_ID},
                    properties=properties,
                )
                page_id = response["id"]
                print(f"  [Notion] 保存完了(株価なし): {analysis.stock_code} {analysis.stock_name} (page_id={page_id})")
                print(f"  [Notion] ヒント: Notion DB に Number型の「{PRICE_PROPERTY_NAME}」プロパティを追加すると株価が記録されます")
                return page_id
            except Exception as e2:
                print(f"  [Notion] 保存失敗: {e2}")
                return None
        print(f"  [Notion] 保存失敗: {e}")
        return None
