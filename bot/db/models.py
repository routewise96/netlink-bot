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
    app_choice TEXT,
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
    request_type TEXT DEFAULT 'initial',
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

CREATE TABLE IF NOT EXISTS violations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL,
    ips_json TEXT NOT NULL,
    minute_bucket TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    alerted INTEGER DEFAULT 0,
    ignored INTEGER DEFAULT 0
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_violations_email_minute
    ON violations(email, minute_bucket);

CREATE TABLE IF NOT EXISTS recovery_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token TEXT NOT NULL UNIQUE,
    user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used INTEGER DEFAULT 0,
    ip TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_recovery_tokens_token
    ON recovery_tokens(token);
"""


async def init_db():
    async with aiosqlite.connect(NETLINK_DB_PATH) as db:
        await db.executescript(SCHEMA)
        # migrations
        try:
            await db.execute("ALTER TABLE user_devices ADD COLUMN app_choice TEXT")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE requests ADD COLUMN request_type TEXT DEFAULT 'initial'")
        except Exception:
            pass
        try:
            await db.execute(
                "ALTER TABLE user_devices ADD COLUMN is_admin_device INTEGER DEFAULT 0"
            )
        except Exception:
            pass
        try:
            await db.execute(
                "ALTER TABLE user_devices ADD COLUMN is_temp INTEGER DEFAULT 0"
            )
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE user_devices ADD COLUMN expires_at TEXT")
        except Exception:
            pass
        await db.commit()


def get_db():
    return aiosqlite.connect(NETLINK_DB_PATH)
