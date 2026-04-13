import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_API_URL = os.getenv("DEEPSEEK_API_URL", "https://api.deepseek.com/v1/chat/completions")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
XUI_DB_PATH = os.getenv("XUI_DB_PATH", "/etc/x-ui/x-ui.db")
NETLINK_DB_PATH = os.getenv("NETLINK_DB_PATH", "/opt/netlink-bot/netlink.db")
SERVER_IP = os.getenv("SERVER_IP", "89.125.24.203")
SOCKS_PROXY = os.getenv("SOCKS_PROXY", "socks5://127.0.0.1:1080")
