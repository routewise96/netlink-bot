import aiosqlite
from bot.config import NETLINK_DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER UNIQUE NOT NULL,
    username TEXT,
    fio TEXT,
    devices_count INTEGER DEFAULT 2,
    platforms TEXT,
    uuid TEXT,
    email TEXT,
    sub_id TEXT,
    vless_link TEXT,
    status TEXT DEFAULT 'pending',
    agreement_accepted_at TEXT,
    approved_at TEXT,
    blocked_at TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS user_devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    device_number INTEGER NOT NULL,
    uuid TEXT NOT NULL,
    email TEXT NOT NULL,
    sub_id TEXT NOT NULL,
    vless_link TEXT,
    platform TEXT,
    subscription_url TEXT,
    status TEXT DEFAULT 'active',
    banned_at TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL,
    fio TEXT,
    devices_count INTEGER,
    platforms TEXT,
    status TEXT DEFAULT 'pending',
    admin_message_id INTEGER,
    created_at TEXT DEFAULT (datetime('now')),
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS ai_conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL,
    user_message TEXT,
    ai_response TEXT,
    escalated INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);
"""


async def init_db():
    async with aiosqlite.connect(NETLINK_DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()


def get_db():
    return aiosqlite.connect(NETLINK_DB_PATH)
