"""Admin panel: approve/reject requests, manage users, per-device ban/unban, stats."""
import json
import subprocess
from datetime import datetime

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from bot.db import queries as db
from bot.services.proxy import (
    get_free_uuids, generate_vless_link, update_clients_limit_ip,
)
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


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_CHAT_ID


# ── Admin entry points ──

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


# ── Test mode ──

@router.callback_query(F.data == "admin_test_mode")
async def admin_test_mode(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return

    await _cleanup_test_data(callback.from_user.id)
    await state.clear()
    await state.update_data(test_mode=True)

    await callback.message.edit_text(
        "🧪 <b>Тест-режим активирован</b>\n\n"
        "Вы увидите бота глазами нового сотрудника.\n"
        "После теста данные будут удалены.",
        parse_mode="HTML",
    )
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
    """Remove admin's test user/request/device records from bot DB."""
    from bot.db.models import get_db
    async with get_db() as conn:
        await conn.execute(
            "DELETE FROM user_devices WHERE user_id IN (SELECT id FROM users WHERE telegram_id = ?)",
            (telegram_id,),
        )
        await conn.execute("DELETE FROM users WHERE telegram_id = ?", (telegram_id,))
        await conn.execute("DELETE FROM requests WHERE telegram_id = ?", (telegram_id,))
        await conn.commit()


# ── Requests ──

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


# ── Approve: N UUIDs, each limitIp=1 ──

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

    devices_count = req["devices_count"]

    # Check test mode
    is_test = telegram_id == ADMIN_CHAT_ID
    fsm_data = await state.get_data()
    is_test = is_test and fsm_data.get("test_mode", False)

    # Get used emails from user_devices + legacy users table
    used_emails = await db.get_all_used_emails()

    # Need N free UUIDs
    free = get_free_uuids(used_emails)
    if len(free) < devices_count:
        await callback.answer(
            f"Недостаточно свободных UUID! Нужно {devices_count}, доступно {len(free)}",
            show_alert=True,
        )
        return

    assigned = free[:devices_count]
    emails_to_update = [c["email"] for c in assigned]

    if not is_test:
        # Set limitIp=1 for each assigned UUID in x-ui DB
        update_clients_limit_ip(emails_to_update, 1)
        try:
            subprocess.run(["systemctl", "restart", "x-ui"], timeout=10)
        except Exception:
            pass

    # Update user status
    now = datetime.now().isoformat()
    first = assigned[0]
    await db.update_user(
        telegram_id,
        uuid=first["id"],
        email=first["email"],
        sub_id=first["subId"],
        vless_link=generate_vless_link(first["id"], "NetLink-1"),
        status="approved",
        approved_at=now,
        devices_count=devices_count,
    )
    await db.update_request(request_id, status="approved", resolved_at=now)

    # Create user_devices records
    user_row = await db.get_user(telegram_id)
    user_id = user_row["id"]

    device_records = []
    for i, client in enumerate(assigned, 1):
        vless = generate_vless_link(client["id"], f"NetLink-{i}")
        sub_url = f"http://{SERVER_IP}:8080/profiles/{client['subId']}.json"
        await db.create_user_device(
            user_id=user_id,
            device_number=i,
            uuid=client["id"],
            email=client["email"],
            sub_id=client["subId"],
            vless_link=vless,
            subscription_url=sub_url,
        )
        device_records.append({
            "num": i, "email": client["email"],
            "vless": vless, "sub_url": sub_url,
        })

    # Build message for user
    user_lines = [
        f"✅ <b>Доступ одобрен!</b> Вам выданы ссылки на {devices_count} "
        f"{'устройство' if devices_count == 1 else 'устройства'}.\n",
        "⚠️ Каждая ссылка работает строго на <b>ОДНОМ</b> устройстве. "
        "При использовании на двух устройствах одновременно — ссылка блокируется автоматически.\n",
    ]
    for dr in device_records:
        user_lines.append(f"📱 <b>Устройство {dr['num']}:</b>\n<code>{dr['vless']}</code>")

    # macOS subscription lines
    user_lines.append(f"\n🖥 <b>Для macOS</b> (sing-box VT):")
    for dr in device_records:
        user_lines.append(f"Устройство {dr['num']}: <code>{dr['sub_url']}</code>")

    user_text = "\n".join(user_lines)
    emails_str = ", ".join(emails_to_update)

    if is_test:
        await callback.message.edit_text(
            callback.message.text + f"\n\n✅ Одобрено (тест) → {emails_str}",
            parse_mode="HTML",
        )
        await callback.bot.send_message(
            telegram_id,
            "🧪 <b>Тест:</b> " + user_text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔗 Моя ссылка", callback_data="my_link")],
                [InlineKeyboardButton(text="📖 Инструкция", callback_data="instruction")],
                [InlineKeyboardButton(text="⚙️ Мои устройства", callback_data="my_devices")],
                [InlineKeyboardButton(text="🔙 Выйти из тест-режима", callback_data="exit_test_mode")],
            ]),
            parse_mode="HTML",
        )
    else:
        await callback.bot.send_message(
            telegram_id,
            user_text,
            reply_markup=main_menu_kb(),
            parse_mode="HTML",
        )
        await callback.message.edit_text(
            callback.message.text + f"\n\n✅ Одобрено → {emails_str}",
            parse_mode="HTML",
        )

    await callback.answer(f"Одобрено: {emails_str}")


# ── Reject ──

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


# ── Users list ──

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
    for i, u in enumerate(users[:20]):
        devices = await db.get_user_devices(u["telegram_id"])
        active_d = sum(1 for d in devices if d["status"] == "active")
        total_d = len(devices)
        text += f"• {u['fio']} — {active_d}/{total_d} устройств\n"
    if len(users) > 20:
        text += f"\n...и ещё {len(users) - 20}"

    # Build user selection buttons
    rows = []
    for u in users[:20]:
        rows.append([InlineKeyboardButton(
            text=f"👤 {u['fio']}",
            callback_data=f"userdetail_{u['telegram_id']}",
        )])
    rows.append([InlineKeyboardButton(text="🔙 Назад в админ-панель", callback_data="admin_panel")])

    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML",
    )
    await callback.answer()


# ── User detail with per-device info ──

@router.callback_query(F.data.startswith("userdetail_"))
async def user_detail(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    telegram_id = int(callback.data.split("_")[1])
    user = await db.get_user(telegram_id)
    if not user:
        await callback.answer("Пользователь не найден", show_alert=True)
        return

    devices = await db.get_user_devices(telegram_id)

    text = f"👤 <b>{user['fio']}</b>\n"
    text += f"📱 Telegram: {user.get('username', '') or user['telegram_id']}\n"
    text += f"📅 Доступ с: {(user['approved_at'] or '')[:10]}\n\n"

    for d in devices:
        num = d["device_number"]
        if d["status"] == "active":
            text += f"📱 Устройство {num}: {d['email']} ✅ активно\n"
        else:
            text += f"📱 Устройство {num}: {d['email']} 🔴 забанено\n"

    await callback.message.edit_text(
        text,
        reply_markup=user_detail_kb(telegram_id, devices),
        parse_mode="HTML",
    )
    await callback.answer()


# ── Per-device ban/unban ──

@router.callback_query(F.data.startswith("bandev_"))
async def ban_device(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    device_id = int(callback.data.split("_")[1])
    device = await db.get_device(device_id)
    if not device:
        await callback.answer("Устройство не найдено", show_alert=True)
        return

    await db.ban_device(device_id)

    # Disable in x-ui: set limitIp=0
    update_clients_limit_ip([device["email"]], 0)
    try:
        subprocess.run(["systemctl", "restart", "x-ui"], timeout=10)
    except Exception:
        pass

    # Find the user's telegram_id to refresh detail view
    from bot.db.models import get_db
    async with get_db() as conn:
        conn.row_factory = lambda c, r: dict(zip([d[0] for d in c.description], r))
        cur = await conn.execute(
            "SELECT u.telegram_id FROM users u JOIN user_devices ud ON ud.user_id = u.id WHERE ud.id = ?",
            (device_id,),
        )
        row = await cur.fetchone()

    if row:
        # Re-render detail
        callback.data = f"userdetail_{row['telegram_id']}"
        await user_detail(callback)
    else:
        await callback.answer(f"Устройство {device['email']} забанено")


@router.callback_query(F.data.startswith("unbandev_"))
async def unban_device(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    device_id = int(callback.data.split("_")[1])
    device = await db.get_device(device_id)
    if not device:
        await callback.answer("Устройство не найдено", show_alert=True)
        return

    await db.unban_device(device_id)

    # Re-enable in x-ui: set limitIp=1
    update_clients_limit_ip([device["email"]], 1)
    try:
        subprocess.run(["systemctl", "restart", "x-ui"], timeout=10)
    except Exception:
        pass

    from bot.db.models import get_db
    async with get_db() as conn:
        conn.row_factory = lambda c, r: dict(zip([d[0] for d in c.description], r))
        cur = await conn.execute(
            "SELECT u.telegram_id FROM users u JOIN user_devices ud ON ud.user_id = u.id WHERE ud.id = ?",
            (device_id,),
        )
        row = await cur.fetchone()

    if row:
        callback.data = f"userdetail_{row['telegram_id']}"
        await user_detail(callback)
    else:
        await callback.answer(f"Устройство {device['email']} разбанено")


# ── Block/unblock entire user ──

@router.callback_query(F.data.startswith("block_"))
async def block_user(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    telegram_id = int(callback.data.split("_")[1])
    now = datetime.now().isoformat()
    await db.update_user(telegram_id, status="blocked", blocked_at=now)

    # Ban all devices
    devices = await db.get_user_devices(telegram_id)
    await db.ban_all_devices(telegram_id)
    emails = [d["email"] for d in devices if d["status"] == "active"]
    if emails:
        update_clients_limit_ip(emails, 0)
        try:
            subprocess.run(["systemctl", "restart", "x-ui"], timeout=10)
        except Exception:
            pass

    user = await db.get_user(telegram_id)
    try:
        await callback.bot.send_message(
            telegram_id,
            "🚫 Ваш доступ к сервису заблокирован. Обратитесь к администратору.",
        )
    except Exception:
        pass

    await callback.message.edit_text(
        f"🔴 Пользователь {user['fio']} заблокирован (все устройства).",
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

    # Unban all devices
    devices = await db.get_user_devices(telegram_id)
    for d in devices:
        if d["status"] == "banned":
            await db.unban_device(d["id"])
    emails = [d["email"] for d in devices]
    if emails:
        update_clients_limit_ip(emails, 1)
        try:
            subprocess.run(["systemctl", "restart", "x-ui"], timeout=10)
        except Exception:
            pass

    user = await db.get_user(telegram_id)
    try:
        await callback.bot.send_message(
            telegram_id,
            "🟢 Ваш доступ восстановлен!",
            reply_markup=main_menu_kb(),
        )
    except Exception:
        pass

    await callback.message.edit_text(
        f"🟢 Пользователь {user['fio']} разблокирован (все устройства).",
        reply_markup=back_to_admin_kb(),
        parse_mode="HTML",
    )
    await callback.answer("Разблокирован")


# ── User links (admin view) ──

@router.callback_query(F.data.startswith("userlink_"))
async def show_user_link(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    telegram_id = int(callback.data.split("_")[1])
    user = await db.get_user(telegram_id)
    if not user:
        await callback.answer("Пользователь не найден", show_alert=True)
        return

    devices = await db.get_user_devices(telegram_id)
    if not devices:
        await callback.answer("Ссылки не найдены", show_alert=True)
        return

    lines = [f"🔗 <b>Ссылки {user['fio']}:</b>\n"]
    for d in devices:
        num = d["device_number"]
        status = "✅" if d["status"] == "active" else "🔴"
        lines.append(f"📱 Устройство {num} {status} ({d['email']}):")
        if d["status"] == "active":
            lines.append(f"<code>{d['vless_link']}</code>")
            if d.get("subscription_url"):
                lines.append(f"macOS: <code>{d['subscription_url']}</code>")
        else:
            lines.append("(заблокировано)")
        lines.append("")

    await callback.message.answer(
        "\n".join(lines),
        parse_mode="HTML",
    )
    await callback.answer()


# ── Blocked users ──

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

    rows = []
    for u in users[:10]:
        rows.append([InlineKeyboardButton(
            text=f"🟢 Разблокировать {u['fio']}",
            callback_data=f"unblock_{u['telegram_id']}",
        )])
    rows.append([InlineKeyboardButton(text="🔙 Назад в админ-панель", callback_data="admin_panel")])

    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML",
    )
    await callback.answer()


# ── Stats ──

@router.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    stats = await db.get_stats()

    used_emails = await db.get_all_used_emails()
    free_uuids = len(get_free_uuids(used_emails))

    text = (
        f"📊 <b>Статистика NetLink</b>\n\n"
        f"👥 Пользователей: {stats['approved']}\n"
        f"📱 Устройств активно: {stats['devices_active']}\n"
        f"🔴 Устройств забанено: {stats['devices_banned']}\n"
        f"📋 Ожидают одобрения: {stats['pending']}\n"
        f"🔑 Свободных UUID: {free_uuids}"
    )

    await callback.message.edit_text(
        text,
        reply_markup=back_to_admin_kb(),
        parse_mode="HTML",
    )
    await callback.answer()
