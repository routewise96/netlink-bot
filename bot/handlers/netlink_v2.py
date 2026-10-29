"""NetLink 2.0 pilot enrollment flow (pilot users only, disabled by default).

Gate order (defense in depth):
1. global feature flag + configuration completeness;
2. pilot allowlist by telegram id;
3. legacy bot approval status (unchanged legacy rules).

On Control Plane failure the pilot user gets a safe message; the legacy bot
keeps working untouched. Enrollment codes are used exactly once: sent to the
user and never stored, logged, or cached.
"""
from __future__ import annotations

import html
import logging
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.types import CallbackQuery

from bot.db import queries as db
from bot.services.netlink_v2 import settings as v2_settings
from bot.services.netlink_v2.client import ControlPlaneClient, ControlPlaneError
from bot.services.netlink_v2.enrollment_uri import build as build_enrollment_uri
from bot.services.netlink_v2.flow import build_full_name, resolve_employee

router = Router(name="netlink_v2")
logger = logging.getLogger(__name__)

UNAVAILABLE_TEXT = (
    "⚠️ Сейчас новое подключение временно недоступно.\n"
    "Действующие подключения NetLink продолжают работать."
)


def _minutes_left(expires_at: object) -> int:
    """Remaining validity from the Control Plane response (never hardcoded)."""
    if not isinstance(expires_at, str):
        return 0
    try:
        expires = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        remaining = (expires - datetime.now(timezone.utc)).total_seconds() / 60
        return max(1, round(remaining))
    except ValueError:
        return 0


@router.callback_query(F.data == "v2_enroll")
async def v2_enroll(callback: CallbackQuery) -> None:
    telegram_id = callback.from_user.id

    if not v2_settings.is_enabled() or not v2_settings.is_pilot_user(telegram_id):
        await callback.answer("Недоступно", show_alert=True)
        return

    user = await db.get_user(telegram_id)
    if not user or user["status"] != "approved":
        await callback.answer("Доступ не активен", show_alert=True)
        return

    await callback.answer()

    config = v2_settings.load_v2_settings()
    client: ControlPlaneClient | None = None
    try:
        # Construction happens inside the error boundary: an invalid
        # configuration or transport failure degrades to the safe message,
        # never an unhandled handler exception.
        client = ControlPlaneClient(
            base_url=config.base_url, internal_api_key=config.internal_api_key
        )
        employee = await resolve_employee(
            client,
            telegram_id,
            build_full_name(
                callback.from_user.first_name,
                callback.from_user.last_name,
                telegram_id,
            ),
        )
        enrollment = await client.create_enrollment(employee["id"])
    except ControlPlaneError as exc:
        # safe message only: typed exceptions never carry the api key or codes
        logger.warning("v2 enrollment failed (telegram_id=%s): %s", telegram_id, exc)
        await callback.message.answer(UNAVAILABLE_TEXT)
        return
    except Exception as exc:  # noqa: BLE001 - defensive boundary for the pilot flow
        logger.error(
            "v2 enrollment unexpected error (telegram_id=%s): %s",
            telegram_id,
            type(exc).__name__,
        )
        await callback.message.answer(UNAVAILABLE_TEXT)
        return
    finally:
        if client is not None:
            await client.close()

    code = enrollment.get("code")
    if not isinstance(code, str) or not code:
        # safe identifier only; never the code itself
        logger.error(
            "v2 enrollment response missing code (enrollment_id=%s)",
            enrollment.get("id"),
        )
        await callback.message.answer(UNAVAILABLE_TEXT)
        return

    minutes = _minutes_left(enrollment.get("expires_at"))
    text = (
        "🚀 <b>NetLink 2.0 — новое подключение</b>\n\n"
        "Ваш одноразовый код подключения:\n"
        f"<code>{html.escape(code)}</code>\n\n"
        f"⏳ Код действует ещё {minutes} мин.\n"
        "Введите его в приложении NetLink при первом запуске.\n\n"
        "Или используйте ссылку:\n"
        f"<code>{html.escape(build_enrollment_uri(code))}</code>"
    )
    try:
        await callback.message.answer(text, parse_mode="HTML")
    except Exception as exc:  # noqa: BLE001 - Telegram delivery failure
        # no retry loop: the enrollment simply expires by its TTL
        logger.error(
            "failed to deliver v2 enrollment code (enrollment_id=%s): %s",
            enrollment.get("id"),
            type(exc).__name__,
        )
