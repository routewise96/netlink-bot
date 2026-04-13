"""Admin panel: approve/reject requests, manage users, stats."""
import json
import subprocess
from datetime import datetime

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from bot.db import queries as db
from bot.services.proxy import get_free_uuids, generate_vless_link, update_client_limit_ip
from bot.keyboards.admin_kb import (
    admin_panel_kb,
    approve_reject_kb,
    user_detail_kb,
    unblock_kb,
    back_to_admin_kb,
)
from bot.keyboards.user_kb import main_menu_kb, agreement_start_kb
from bot.config import ADMIN_CHAT_ID, SERVER_IP

router = Router()


def exit_test_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Выйти из тест-режима", callback_data="exit_test_mode")]
    ])


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_CHAT_ID


@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if not is_admin(message.from_user.id):
        return
    stats = await db.get_stats()
    await message.answer(
        "🔧 <b>Админ-панель NetLink</b>",
        reply_markup=admin_panel_kb(stats["pending"]),
        parse_mode="HTML",
    )


@router.message(F.text.lower().contains("админ") | F.text.lower().contains("admin"))
async def admin_keyword(message: Message):
    if not is_admin(message.from_user.id):
        return
    stats = await db.get_stats()
    await message.answer(
        "🔧 <b>Админ-панель NetLink</b>",
        reply_markup=admin_panel_kb(stats["pending"]),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "admin_panel")
async def admin_panel(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    stats = await db.get_stats()
    await callback.message.edit_text(
        "🔧 <b>Админ-панель NetLink</b>",
        reply_markup=admin_panel_kb(stats["pending"]),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "admin_test_mode")
async def admin_test_mode(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return

    # Clean any previous test data for admin
    await _cleanup_test_data(callback.from_user.id)

    # Set test mode flag in FSM
    await state.clear()
    await state.update_data(test_mode=True)

    await callback.message.edit_text(
        "🧪 <b>Тест-режим активирован</b>\n\n"
        "Вы увидите бота глазами нового сотрудника.\n"
        "После теста данные будут удалены.",
        parse_mode="HTML",
    )

    # Show the welcome screen as a new user would see it
    await callback.message.answer(
        "🔒 <b>NetLink</b> — корпоративный сервис защищённого доступа.\n\n"
        "Для получения доступа необходимо принять условия использования.",
        reply_markup=agreement_start_kb(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "exit_test_mode")
async def exit_test_mode(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return

    await _cleanup_test_data(callback.from_user.id)
    await state.clear()

    stats = await db.get_stats()
    await callback.message.edit_text(
        "🔧 <b>Админ-панель NetLink</b>\n\n🧪 Тест-режим завершён. Данные очищены.",
        reply_markup=admin_panel_kb(stats["pending"]),
        parse_mode="HTML",
    )
    await callback.answer()


async def _cleanup_test_data(telegram_id: int):
    """Remove admin's test user/request records from bot DB."""
    from bot.db.models import get_db
    async with get_db() as conn:
        await conn.execute("DELETE FROM users WHERE telegram_id = ?", (telegram_id,))
        await conn.execute("DELETE FROM requests WHERE telegram_id = ?", (telegram_id,))
        await conn.commit()


@router.callback_query(F.data == "admin_requests")
async def admin_requests(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    requests = await db.get_pending_requests()
    if not requests:
        await callback.message.edit_text(
            "📋 Нет ожидающих заявок.",
            reply_markup=back_to_admin_kb(),
            parse_mode="HTML",
        )
        await callback.answer()
        return

    for req in requests[:10]:
        platforms = json.loads(req["platforms"] or "[]")
        platform_names = {"iphone": "iPhone", "android": "Android", "windows": "Windows", "macos": "macOS"}
        platforms_str = ", ".join(platform_names.get(p, p) for p in platforms)
        text = (
            f"📋 <b>Заявка #{req['id']}</b>\n\n"
            f"👤 ФИО: {req['fio']}\n"
            f"📊 Устройств: {req['devices_count']}\n"
            f"💻 Платформы: {platforms_str}\n"
            f"🕐 {req['created_at']}"
        )
        await callback.message.answer(
            text,
            reply_markup=approve_reject_kb(req["id"]),
            parse_mode="HTML",
        )
    await callback.answer()


@router.callback_query(F.data.startswith("approve_"))
async def approve_request(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return

    request_id = int(callback.data.split("_")[1])
    req = await db.get_request(request_id)
    if not req or req["status"] != "pending":
        await callback.answer("Заявка уже обработана", show_alert=True)
        return

    telegram_id = req["telegram_id"]
    user = await db.get_user(telegram_id)
    if not user:
        await callback.answer("Пользователь не найден", show_alert=True)
        return

    # Check if this is a test mode approval (admin approving their own test request)
    is_test = telegram_id == ADMIN_CHAT_ID
    fsm_data = await state.get_data()
    is_test = is_test and fsm_data.get("test_mode", False)

    # Get used emails
    approved_users = await db.get_users_by_status("approved")
    used_emails = {u["email"] for u in approved_users if u.get("email")}

    # Get free UUID
    free = get_free_uuids(used_emails)
    if not free:
        await callback.answer("Нет свободных UUID в пуле!", show_alert=True)
        return

    client = free[0]
    uuid = client["id"]
    email = client["email"]
    sub_id = client["subId"]
    devices_count = req["devices_count"]

    # Generate VLESS link
    vless_link = generate_vless_link(uuid, "NetLink")

    if not is_test:
        # Real approval: update x-ui DB and restart
        update_client_limit_ip(email, devices_count)
        try:
            subprocess.run(["systemctl", "restart", "x-ui"], timeout=10)
        except Exception:
            pass

    # Update bot DB
    now = datetime.now().isoformat()
    await db.update_user(
        telegram_id,
        uuid=uuid,
        email=email,
        sub_id=sub_id,
        vless_link=vless_link,
        status="approved",
        approved_at=now,
        devices_count=devices_count,
    )
    await db.update_request(
        request_id, status="approved", resolved_at=now
    )

    # Build dual-link text
    sub_url = f"http://{SERVER_IP}:8080/profiles/{sub_id}.json" if sub_id else ""
    links_lines = []
    if sub_url:
        links_lines.append(f"📎 <b>Для macOS</b> (sing-box VT):\n<code>{sub_url}</code>")
    if vless_link:
        links_lines.append(f"📎 <b>Для iPhone / Android / Windows:</b>\n<code>{vless_link}</code>")
    links_text = "\n\n".join(links_lines)

    if is_test:
        # Test mode: show result to admin directly with exit button
        await callback.message.edit_text(
            callback.message.text + f"\n\n✅ Одобрено (тест) → {email}",
            parse_mode="HTML",
        )
        await callback.bot.send_message(
            telegram_id,
            f"🧪 <b>Тест: Доступ одобрен!</b>\n\n"
            f"🔗 <b>Ваши ссылки для подключения:</b>\n\n"
            f"{links_text}\n\n"
            f"Это тестовые ссылки. Нажмите кнопку ниже для просмотра инструкций,\n"
            f"или выйдите из тест-режима.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📖 Инструкция", callback_data="instruction")],
                [InlineKeyboardButton(text="⚙️ Мои устройства", callback_data="my_devices")],
                [InlineKeyboardButton(text="🔙 Выйти из тест-режима", callback_data="exit_test_mode")],
            ]),
            parse_mode="HTML",
        )
    else:
        # Real approval: notify user
        await callback.bot.send_message(
            telegram_id,
            f"✅ <b>Доступ одобрен!</b>\n\n"
            f"🔗 <b>Ваши ссылки для подключения:</b>\n\n"
            f"{links_text}\n\n"
            f"Нажмите на ссылку чтобы скопировать, затем следуйте 📖 Инструкции.",
            reply_markup=main_menu_kb(),
            parse_mode="HTML",
        )
        await callback.message.edit_text(
            callback.message.text + f"\n\n✅ Одобрено → {email}",
            parse_mode="HTML",
        )

    await callback.answer(f"Одобрено: {email}")


@router.callback_query(F.data.startswith("reject_"))
async def reject_request(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return

    request_id = int(callback.data.split("_")[1])
    req = await db.get_request(request_id)
    if not req or req["status"] != "pending":
        await callback.answer("Заявка уже обработана", show_alert=True)
        return

    telegram_id = req["telegram_id"]
    fsm_data = await state.get_data()
    is_test = telegram_id == ADMIN_CHAT_ID and fsm_data.get("test_mode", False)

    now = datetime.now().isoformat()
    await db.update_request(request_id, status="rejected", resolved_at=now)
    await db.update_user(telegram_id, status="rejected")

    if is_test:
        await callback.message.edit_text(
            callback.message.text + "\n\n❌ Отклонено (тест)",
            parse_mode="HTML",
        )
        await callback.bot.send_message(
            telegram_id,
            "🧪 Тест: заявка отклонена.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔙 Выйти из тест-режима", callback_data="exit_test_mode")]
            ]),
        )
    else:
        await callback.bot.send_message(
            telegram_id,
            "❌ Ваша заявка отклонена. Обратитесь к администратору лично.",
        )
        await callback.message.edit_text(
            callback.message.text + "\n\n❌ Отклонено",
            parse_mode="HTML",
        )
    await callback.answer("Отклонено")


@router.callback_query(F.data == "admin_users")
async def admin_users(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    users = await db.get_users_by_status("approved")
    if not users:
        await callback.message.edit_text(
            "👥 Нет активных пользователей.",
            reply_markup=back_to_admin_kb(),
            parse_mode="HTML",
        )
        await callback.answer()
        return

    text = "👥 <b>Активные пользователи:</b>\n\n"
    for u in users[:20]:
        text += f"• {u['fio']} — {u['email']}\n"
    if len(users) > 20:
        text += f"\n...и ещё {len(users) - 20}"

    # Show first user details
    if users:
        first = users[0]
        text += (
            f"\n\n<b>Детали: {first['fio']}</b>\n"
            f"📱 Telegram ID: {first['telegram_id']}\n"
            f"🔑 {first['email']} ({first['uuid'][:12]}...)\n"
            f"📊 Устройств: {first['devices_count']}\n"
            f"📅 Доступ с: {(first['approved_at'] or '')[:10]}"
        )
        await callback.message.edit_text(
            text,
            reply_markup=user_detail_kb(first["telegram_id"]),
            parse_mode="HTML",
        )
    else:
        await callback.message.edit_text(text, reply_markup=back_to_admin_kb(), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("block_"))
async def block_user(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    telegram_id = int(callback.data.split("_")[1])
    now = datetime.now().isoformat()
    await db.update_user(telegram_id, status="blocked", blocked_at=now)

    user = await db.get_user(telegram_id)
    await callback.bot.send_message(
        telegram_id,
        "🚫 Ваш доступ к сервису заблокирован. Обратитесь к администратору.",
    )

    await callback.message.edit_text(
        f"🔴 Пользователь {user['fio']} заблокирован.",
        reply_markup=back_to_admin_kb(),
        parse_mode="HTML",
    )
    await callback.answer("Заблокирован")


@router.callback_query(F.data.startswith("unblock_"))
async def unblock_user(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    telegram_id = int(callback.data.split("_")[1])
    await db.update_user(telegram_id, status="approved", blocked_at=None)

    user = await db.get_user(telegram_id)
    await callback.bot.send_message(
        telegram_id,
        "🟢 Ваш доступ восстановлен!",
        reply_markup=main_menu_kb(),
    )

    await callback.message.edit_text(
        f"🟢 Пользователь {user['fio']} разблокирован.",
        reply_markup=back_to_admin_kb(),
        parse_mode="HTML",
    )
    await callback.answer("Разблокирован")


@router.callback_query(F.data.startswith("userlink_"))
async def show_user_link(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    telegram_id = int(callback.data.split("_")[1])
    user = await db.get_user(telegram_id)
    if not user or not user["vless_link"]:
        await callback.answer("Ссылка не найдена", show_alert=True)
        return

    sub_id = user.get("sub_id", "")
    sub_url = f"http://{SERVER_IP}:8080/profiles/{sub_id}.json" if sub_id else ""
    lines = [f"🔗 <b>Ссылки {user['fio']}:</b>"]
    if sub_url:
        lines.append(f"\n📎 macOS: <code>{sub_url}</code>")
    lines.append(f"\n📎 VLESS: <code>{user['vless_link']}</code>")

    await callback.message.answer(
        "\n".join(lines),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "admin_blocked")
async def admin_blocked(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    users = await db.get_users_by_status("blocked")
    if not users:
        await callback.message.edit_text(
            "🚫 Нет заблокированных пользователей.",
            reply_markup=back_to_admin_kb(),
            parse_mode="HTML",
        )
        await callback.answer()
        return

    text = "🚫 <b>Заблокированные:</b>\n\n"
    for u in users:
        text += f"• {u['fio']} — заблокирован {(u['blocked_at'] or '')[:10]}\n"

    if users:
        first = users[0]
        await callback.message.edit_text(
            text,
            reply_markup=unblock_kb(first["telegram_id"]),
            parse_mode="HTML",
        )
    else:
        await callback.message.edit_text(text, reply_markup=back_to_admin_kb(), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    stats = await db.get_stats()

    # Count free UUIDs
    approved_users = await db.get_users_by_status("approved")
    used_emails = {u["email"] for u in approved_users if u.get("email")}
    free_uuids = len(get_free_uuids(used_emails))

    text = (
        f"📊 <b>Статистика NetLink</b>\n\n"
        f"👥 Всего пользователей: {stats['total']}\n"
        f"✅ Активных: {stats['approved']}\n"
        f"🚫 Заблокированных: {stats['blocked']}\n"
        f"📋 Ожидают одобрения: {stats['pending']}\n"
        f"🔑 Свободных UUID: {free_uuids}\n"
        f"📅 Сегодня новых: {stats['today_new']}"
    )

    await callback.message.edit_text(
        text,
        reply_markup=back_to_admin_kb(),
        parse_mode="HTML",
    )
    await callback.answer()
