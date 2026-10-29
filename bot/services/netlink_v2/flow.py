"""Employee resolution + Telegram name building for the v2 enrollment flow.

Resolution algorithm (explicit, auditable; no server-side upsert):
1. GET /v1/employees/by-telegram/{id}
2. 404 -> POST /v1/employees (create once with the Telegram-derived name)
3. duplicate-create race (409) -> one GET retry
4. anything else propagates to the caller
"""
from __future__ import annotations

from typing import Any

from bot.services.netlink_v2.client import (
    ControlPlaneClient,
    ControlPlaneConflict,
    EmployeeNotFound,
)

FALLBACK_NAME_TEMPLATE = "Telegram user {telegram_user_id}"


def build_full_name(
    first_name: str | None, last_name: str | None, telegram_user_id: int
) -> str:
    """First + last name from Telegram; safe fallback; never the username."""
    parts = [(first_name or "").strip(), (last_name or "").strip()]
    name = " ".join(part for part in parts if part)
    if not name:
        return FALLBACK_NAME_TEMPLATE.format(telegram_user_id=telegram_user_id)
    return name[:200]


async def resolve_employee(
    client: ControlPlaneClient, telegram_user_id: int, full_name: str
) -> dict[str, Any]:
    try:
        return await client.get_employee_by_telegram(telegram_user_id)
    except EmployeeNotFound:
        pass
    try:
        return await client.create_employee(telegram_user_id, full_name)
    except ControlPlaneConflict:
        # concurrent create won the race: re-read once
        return await client.get_employee_by_telegram(telegram_user_id)
