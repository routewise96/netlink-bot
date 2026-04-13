import json
from datetime import datetime
from bot.db.models import get_db


async def get_user(telegram_id: int) -> dict | None:
    async with get_db() as db:
        db.row_factory = aiosqlite_row_factory
        cursor = await db.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        )
        row = await cursor.fetchone()
        return row


async def create_user(telegram_id: int, username: str | None) -> None:
    async with get_db() as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (telegram_id, username) VALUES (?, ?)",
            (telegram_id, username),
        )
        await db.commit()


async def update_user(telegram_id: int, **kwargs) -> None:
    if not kwargs:
        return
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [telegram_id]
    async with get_db() as db:
        await db.execute(
            f"UPDATE users SET {sets} WHERE telegram_id = ?", values
        )
        await db.commit()


async def create_request(
    telegram_id: int, fio: str, devices_count: int, platforms: str
) -> int:
    async with get_db() as db:
        cursor = await db.execute(
            "INSERT INTO requests (telegram_id, fio, devices_count, platforms) VALUES (?, ?, ?, ?)",
            (telegram_id, fio, devices_count, platforms),
        )
        await db.commit()
        return cursor.lastrowid


async def get_request(request_id: int) -> dict | None:
    async with get_db() as db:
        db.row_factory = aiosqlite_row_factory
        cursor = await db.execute(
            "SELECT * FROM requests WHERE id = ?", (request_id,)
        )
        return await cursor.fetchone()


async def get_request_by_message_id(admin_message_id: int) -> dict | None:
    async with get_db() as db:
        db.row_factory = aiosqlite_row_factory
        cursor = await db.execute(
            "SELECT * FROM requests WHERE admin_message_id = ?",
            (admin_message_id,),
        )
        return await cursor.fetchone()


async def update_request(request_id: int, **kwargs) -> None:
    if not kwargs:
        return
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [request_id]
    async with get_db() as db:
        await db.execute(
            f"UPDATE requests SET {sets} WHERE id = ?", values
        )
        await db.commit()


async def get_pending_requests() -> list[dict]:
    async with get_db() as db:
        db.row_factory = aiosqlite_row_factory
        cursor = await db.execute(
            "SELECT * FROM requests WHERE status = 'pending' ORDER BY created_at DESC"
        )
        return await cursor.fetchall()


async def get_users_by_status(status: str) -> list[dict]:
    async with get_db() as db:
        db.row_factory = aiosqlite_row_factory
        cursor = await db.execute(
            "SELECT * FROM users WHERE status = ? ORDER BY created_at DESC",
            (status,),
        )
        return await cursor.fetchall()


async def get_stats() -> dict:
    async with get_db() as db:
        total = (await (await db.execute("SELECT COUNT(*) FROM users WHERE fio IS NOT NULL")).fetchone())[0]
        approved = (await (await db.execute("SELECT COUNT(*) FROM users WHERE status = 'approved'")).fetchone())[0]
        blocked = (await (await db.execute("SELECT COUNT(*) FROM users WHERE status = 'blocked'")).fetchone())[0]
        pending = (await (await db.execute("SELECT COUNT(*) FROM requests WHERE status = 'pending'")).fetchone())[0]
        today = datetime.now().strftime("%Y-%m-%d")
        today_new = (await (await db.execute("SELECT COUNT(*) FROM users WHERE created_at LIKE ?", (f"{today}%",))).fetchone())[0]
        return {
            "total": total,
            "approved": approved,
            "blocked": blocked,
            "pending": pending,
            "today_new": today_new,
        }


async def save_ai_conversation(
    telegram_id: int, user_message: str, ai_response: str, escalated: bool = False
) -> None:
    async with get_db() as db:
        await db.execute(
            "INSERT INTO ai_conversations (telegram_id, user_message, ai_response, escalated) VALUES (?, ?, ?, ?)",
            (telegram_id, user_message, ai_response, 1 if escalated else 0),
        )
        await db.commit()


def aiosqlite_row_factory(cursor, row):
    columns = [d[0] for d in cursor.description]
    return dict(zip(columns, row))


import aiosqlite
aiosqlite.Row = aiosqlite_row_factory
