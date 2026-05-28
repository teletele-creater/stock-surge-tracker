import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(override=True)

# Anthropic API
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# Notion API
NOTION_TOKEN = os.getenv("NOTION_TOKEN", "")
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID", "")

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
