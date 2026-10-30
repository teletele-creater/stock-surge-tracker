import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(override=True)

# Anthropic API
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# Notion API
NOTION_TOKEN = os.getenv("NOTION_TOKEN", "")
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID", "")
# 急落用の別DB（毎日急落分析DB）。急騰DBとは列構成が違うので分けている。
NOTION_DROP_DATABASE_ID = os.getenv("NOTION_DROP_DATABASE_ID", "")

# 設定
TARGET_USERNAME = os.getenv("TARGET_USERNAME", "shodousan")
MAX_TWEETS = int(os.getenv("MAX_TWEETS", "20"))

BASE_DIR = Path(__file__).parent
NOTES_DIR = BASE_DIR / os.getenv("NOTES_DIR", "notes")
NOTES_DIR.mkdir(exist_ok=True)

AUTH_STATE_FILE = BASE_DIR / ".auth_state.json"

# Slack API
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL = os.getenv("SLACK_CHANNEL", "stock-alert")

# 急落検知(drop_events)の通知先。急騰のX投稿承認メッセージと混ざるのが嫌な場合に
# .env で別チャンネルを指定する。未設定なら急騰と同じチャンネルに流す。
SLACK_DROP_CHANNEL = os.getenv("SLACK_DROP_CHANNEL", "") or SLACK_CHANNEL

# 急騰要因メモサイト（Xポスト・画像に銘柄ページへのリンクを載せる）
SITE_BASE_URL = os.getenv("SITE_BASE_URL", "https://kyuutoumemo.com").rstrip("/")

# X ポスト形式。既定はカード方式: 画像を添付せず URL だけ載せ、URL 先の OGP
# (og:image=チャート画像) で「画像全体がリンクになるカード」を X に描画させる。
# 画像添付があるとカードは表示されないため排他。旧来の添付に戻すなら X_ATTACH_IMAGE=1
X_ATTACH_IMAGE = os.getenv("X_ATTACH_IMAGE", "0") == "1"
