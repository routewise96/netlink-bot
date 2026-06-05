"""Admin panel: approve/reject, per-device management, stats."""
import asyncio
import json
import logging
from datetime import datetime

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from bot.db import queries as db
from bot.services.proxy import (
    get_free_uuids, generate_vless_link, update_clients_limit_ip,
    set_client_enabled,
)
from bot.keyboards.admin_kb import (
    admin_panel_kb,
    approve_reject_kb,
    user_detail_kb,
    back_to_admin_kb,
)
from bot.keyboards.user_kb import main_menu_kb, agreement_start_kb
from bot.handlers.user import _subscription_url, UNIVERSAL_INSTRUCTION, render_subscription_block
from bot.config import ADMIN_CHAT_ID, SERVER_IP

router = Router()

PLATFORM_LABELS = {
    "iphone": "📱 iPhone",
    "android": "📱 Android",
    "windows": "💻 Windows",
    "macos": "🖥 macOS",
}
PLATFORM_NAMES = {"iphone": "iPhone", "android": "Android", "windows": "Windows", "macos": "macOS"}

broadcast_log = logging.getLogger("netlink.broadcast")
if not broadcast_log.handlers:
    _bcast_h = logging.FileHandler("/var/log/netlink-bot-broadcast.log")
    _bcast_h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    broadcast_log.addHandler(_bcast_h)
    broadcast_log.setLevel(logging.INFO)
    broadcast_log.propagate = False


def _device_message(device_number: int, sub_id: str) -> str:
    """Universal message for a single newly-handed-out device."""
    return (
        f"✅ Устройство #{device_number} добавлено.\n\n"
        + render_subscription_block(sub_id)
    )


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_CHAT_ID


# ── Admin entry ──

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
        platforms_str = ", ".join(PLATFORM_NAMES.get(p, p) for p in platforms)
        n = len(platforms)
        dev_word = "устройство" if n == 1 else "устройства"
        text = (
            f"📋 <b>Заявка #{req['id']}</b>\n\n"
            f"👤 ФИО: {req['fio']}\n"
            f"💻 Платформы: {platforms_str} ({n} {dev_word})\n"
            f"🕐 {req['created_at']}"
        )
        await callback.message.answer(
            text,
            reply_markup=approve_reject_kb(req["id"]),
            parse_mode="HTML",
        )
    await callback.answer()


# ── Approve: per-platform UUID assignment ──

def _build_approval_messages(devices_data: list[dict]) -> list[str]:
    """Header message + per-device rendered subscription/instruction blocks."""
    n = len(devices_data)
    dev_word = "устройство" if n == 1 else "устройства" if n < 5 else "устройств"
    header = (
        f"✅ <b>Доступ одобрен!</b> Вам выданы подписки на {n} {dev_word}.\n\n"
        "⚠️ Каждая подписка работает строго на <b>ОДНОМ</b> устройстве. "
        "При использовании на двух устройствах одновременно — подписка блокируется автоматически."
    )
    msgs = [header]
    for i, dd in enumerate(devices_data, 1):
        label = PLATFORM_LABELS.get(dd["platform"], f"📱 {dd['platform']}")
        msgs.append(f"<b>{label}</b> — устройство #{i}\n\n" + render_subscription_block(dd["sub_id"]))
    return msgs


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

    platforms = json.loads(req["platforms"] or "[]")
    devices_count = len(platforms)

    is_test = telegram_id == ADMIN_CHAT_ID
    fsm_data = await state.get_data()
    is_test = is_test and fsm_data.get("test_mode", False)

    used_emails = await db.get_all_used_emails()
    free = get_free_uuids(used_emails)
    if len(free) < devices_count:
        await callback.answer(
            f"Недостаточно UUID! Нужно {devices_count}, доступно {len(free)}",
            show_alert=True,
        )
        return

    assigned = free[:devices_count]
    emails_to_update = [c["email"] for c in assigned]

    if not is_test:
        update_clients_limit_ip(emails_to_update, 1)

    now = datetime.now().isoformat()
    first = assigned[0]
    await db.update_user(
        telegram_id,
        uuid=first["id"],
        email=first["email"],
        sub_id=first["subId"],
        vless_link=generate_vless_link(first["id"], f"NetLink-{PLATFORM_NAMES.get(platforms[0], '1')}"),
        status="approved",
        approved_at=now,
        devices_count=devices_count,
    )
    await db.update_request(request_id, status="approved", resolved_at=now)

    user_row = await db.get_user(telegram_id)
    user_id = user_row["id"]

    devices_data = []
    for i, (client, platform) in enumerate(zip(assigned, platforms), 1):
        plat_name = PLATFORM_NAMES.get(platform, str(i))
        vless = generate_vless_link(client["id"], f"NetLink-{plat_name}")
        sub_url = _subscription_url(client["subId"])
        app = "happ" if platform in ("android", "iphone") else ""
        await db.create_user_device(
            user_id=user_id,
            device_number=i,
            uuid=client["id"],
            email=client["email"],
            sub_id=client["subId"],
            vless_link=vless,
            subscription_url=sub_url,
            platform=platform,
            app_choice=app,
        )
        devices_data.append({
            "platform": platform, "email": client["email"],
            "vless": vless, "sub_url": sub_url,
            "sub_id": client["subId"],
        })

    messages = _build_approval_messages(devices_data)
    emails_str = ", ".join(emails_to_update)

    if is_test:
        await callback.message.edit_text(
            callback.message.text + f"\n\n✅ Одобрено (тест) → {emails_str}",
            parse_mode="HTML",
        )
        await callback.bot.send_message(
            telegram_id,
            "🧪 <b>Тест:</b> " + messages[0],
            parse_mode="HTML",
        )
        for m in messages[1:]:
            await callback.bot.send_message(telegram_id, m, parse_mode="HTML")
            await asyncio.sleep(0.3)
        await callback.bot.send_message(
            telegram_id,
            "Выберите действие:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔗 Моя ссылка", callback_data="my_link")],
                [InlineKeyboardButton(text="📖 Инструкция", callback_data="instruction")],
                [InlineKeyboardButton(text="⚙️ Мои устройства", callback_data="my_devices")],
                [InlineKeyboardButton(text="🔙 Выйти из тест-режима", callback_data="exit_test_mode")],
            ]),
        )
    else:
        for i, m in enumerate(messages):
            await callback.bot.send_message(
                telegram_id, m,
                reply_markup=main_menu_kb() if i == len(messages) - 1 else None,
                parse_mode="HTML",
            )
            await asyncio.sleep(0.3)
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
    for u in users[:20]:
        devices = await db.get_user_devices(u["telegram_id"])
        active_d = sum(1 for d in devices if d["status"] == "active")
        total_d = len(devices)
        text += f"• {u['fio']} — {active_d}/{total_d} устройств\n"
    if len(users) > 20:
        text += f"\n...и ещё {len(users) - 20}"

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


# ── User detail ──

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
    text += f"📱 Telegram: {user.get('username') or user['telegram_id']}\n"
    text += f"📅 Доступ с: {(user['approved_at'] or '')[:10]}\n\n"

    for d in devices:
        platform = d.get("platform", "")
        plat_label = PLATFORM_NAMES.get(platform, f"#{d['device_number']}")
        if d["status"] == "active":
            text += f"📱 {plat_label}: {d['email']} ✅ активно\n"
        else:
            text += f"📱 {plat_label}: {d['email']} 🔴 забанено\n"

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
    update_clients_limit_ip([device["email"]], 0)

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
    update_clients_limit_ip([device["email"]], 1)

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

    devices = await db.get_user_devices(telegram_id)
    await db.ban_all_devices(telegram_id)
    emails = [d["email"] for d in devices if d["status"] == "active"]
    if emails:
        update_clients_limit_ip(emails, 0)

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

    devices = await db.get_user_devices(telegram_id)
    for d in devices:
        if d["status"] == "banned":
            await db.unban_device(d["id"])
    emails = [d["email"] for d in devices]
    if emails:
        update_clients_limit_ip(emails, 1)

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

    lines = [f"🔗 <b>Подписки {user['fio']}:</b>\n"]
    for d in devices:
        platform = d.get("platform", "")
        label = PLATFORM_LABELS.get(platform, f"Устройство {d['device_number']}")
        status = "✅" if d["status"] == "active" else "🔴"
        lines.append(f"{label} {status} ({d['email']}):")
        if d["status"] == "active":
            lines.append(f"<code>{_subscription_url(d['sub_id'])}</code>")
        else:
            lines.append("(заблокировано)")
        lines.append("")

    await callback.message.answer("\n".join(lines), parse_mode="HTML")
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


# ── Add-device approval ──

MAX_DEVICES_PER_USER = 3


@router.callback_query(F.data.startswith("addapprove_"))
async def add_device_approve(callback: CallbackQuery):
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

    platforms = json.loads(req["platforms"] or "[]")
    if not platforms:
        await callback.answer("Платформа не указана", show_alert=True)
        return
    platform = platforms[0]

    existing = await db.get_user_devices(telegram_id)
    active = [d for d in existing if d["status"] == "active"]
    if len(active) >= MAX_DEVICES_PER_USER:
        await callback.answer(
            f"У пользователя уже {MAX_DEVICES_PER_USER} активных устройств",
            show_alert=True,
        )
        return

    used_emails = await db.get_all_used_emails()
    free = get_free_uuids(used_emails)
    if not free:
        await callback.answer("Нет свободных UUID в пуле!", show_alert=True)
        return

    client = free[0]
    update_clients_limit_ip([client["email"]], 1)

    next_num = max([d["device_number"] for d in existing], default=0) + 1
    plat_name = PLATFORM_NAMES.get(platform, str(next_num))
    vless = generate_vless_link(client["id"], f"NetLink-{plat_name}")
    sub_url = _subscription_url(client["subId"])
    app = "happ" if platform in ("iphone", "android") else ""

    await db.create_user_device(
        user_id=user["id"],
        device_number=next_num,
        uuid=client["id"],
        email=client["email"],
        sub_id=client["subId"],
        vless_link=vless,
        subscription_url=sub_url,
        platform=platform,
        app_choice=app,
    )
    await db.update_user(telegram_id, devices_count=len(active) + 1)
    now = datetime.now().isoformat()
    await db.update_request(request_id, status="approved", resolved_at=now)

    user_text = (
        f"✅ <b>Доп. устройство одобрено!</b>\n\n"
        f"{_device_message(next_num, client['subId'])}\n\n"
        f"⚠️ Подписка работает строго на <b>ОДНОМ</b> устройстве. "
        f"При использовании на двух устройствах одновременно — блокируется автоматически."
    )
    try:
        await callback.bot.send_message(
            telegram_id, user_text,
            reply_markup=main_menu_kb(),
            parse_mode="HTML",
        )
    except Exception:
        pass

    await callback.message.edit_text(
        callback.message.text + f"\n\n✅ Одобрено → {client['email']}",
        parse_mode="HTML",
    )
    await callback.answer(f"Одобрено: {client['email']}")


@router.callback_query(F.data.startswith("addreject_"))
async def add_device_reject(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    request_id = int(callback.data.split("_")[1])
    req = await db.get_request(request_id)
    if not req or req["status"] != "pending":
        await callback.answer("Заявка уже обработана", show_alert=True)
        return

    now = datetime.now().isoformat()
    await db.update_request(request_id, status="rejected", resolved_at=now)

    try:
        await callback.bot.send_message(
            req["telegram_id"],
            "❌ Ваша заявка на добавление устройства отклонена. "
            "Обратитесь к администратору.",
        )
    except Exception:
        pass

    await callback.message.edit_text(
        callback.message.text + "\n\n❌ Отклонено",
        parse_mode="HTML",
    )
    await callback.answer("Отклонено")


# ── Admin: reset user (delete all data, release UUIDs) ──

@router.callback_query(F.data.startswith("resetuser_"))
async def admin_reset_user(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    telegram_id = int(callback.data.split("_")[1])
    user = await db.get_user(telegram_id)
    if not user:
        await callback.answer("Пользователь не найден", show_alert=True)
        return

    fio = user.get("fio", "?")

    # Release UUIDs — set limitIp=0 for all device emails
    devices = await db.get_user_devices(telegram_id)
    active_emails = [d["email"] for d in devices if d.get("email")]
    if active_emails:
        try:
            update_clients_limit_ip(active_emails, 0)
        except Exception:
            pass

    # Delete all user data
    await db.delete_user_devices(telegram_id)
    from bot.db.models import get_db as _get_db
    async with _get_db() as conn:
        await conn.execute("DELETE FROM requests WHERE telegram_id = ?", (telegram_id,))
        await conn.execute("DELETE FROM users WHERE telegram_id = ?", (telegram_id,))
        await conn.commit()

    # Notify user
    try:
        await callback.bot.send_message(
            telegram_id,
            "🔄 Ваш профиль был сброшен администратором.\n"
            "Нажмите /start чтобы начать регистрацию заново.",
        )
    except Exception:
        pass

    await callback.message.edit_text(
        f"🔄 Пользователь {fio} (ID: {telegram_id}) сброшен.\n"
        f"UUID освобождены: {', '.join(active_emails) if active_emails else 'нет устройств'}",
        reply_markup=back_to_admin_kb(),
        parse_mode="HTML",
    )
    await callback.answer(f"Сброшен: {fio}")


# ── Admin: self-registration (unlimited devices) ──

ADMIN_PLATFORM_ORDER = ["iphone", "android", "windows", "macos"]
ADMIN_PLATFORM_EMOJI = {
    "iphone": "📱 iPhone",
    "android": "🤖 Android",
    "windows": "💻 Windows",
    "macos": "🖥 macOS",
}


async def _ensure_admin_user(tg_id: int, username: str | None) -> dict:
    """Ensure a users row exists for the admin; create if missing."""
    user = await db.get_user(tg_id)
    if user:
        return user
    await db.create_user(tg_id, username)
    now = datetime.now().isoformat()
    await db.update_user(
        tg_id,
        fio="Daniel Trofimov (admin)",
        status="approved",
        approved_at=now,
        agreement_accepted_at=now,
    )
    return await db.get_user(tg_id)


def _admin_device_link_text(platform: str, email: str, sub_id: str,
                            vless_link: str, sub_url: str) -> str:
    """Build the link + install instruction block for one admin device."""
    label = PLATFORM_LABELS.get(platform, f"📱 {platform}")
    return f"<b>{label}</b>\n\n" + render_subscription_block(sub_id)


def _admin_add_platform_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=ADMIN_PLATFORM_EMOJI[p], callback_data=f"asd_pick_{p}")]
        for p in ADMIN_PLATFORM_ORDER
    ]
    rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "admin_add_self")
async def admin_add_self_start(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    await callback.message.edit_text(
        "➕ <b>Добавить устройство админу</b>\n\nВыберите платформу:",
        reply_markup=_admin_add_platform_kb(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("asd_pick_"))
async def admin_add_self_pick(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    platform = callback.data.split("_", 2)[2]
    if platform not in ADMIN_PLATFORM_ORDER:
        await callback.answer()
        return

    user = await _ensure_admin_user(
        callback.from_user.id, callback.from_user.username
    )

    existing = await db.get_user_devices(callback.from_user.id)

    used_emails = await db.get_all_used_emails()
    free = get_free_uuids(used_emails)
    if not free:
        await callback.answer("В пуле нет свободных UUID", show_alert=True)
        return
    client = free[0]

    next_num = max([d["device_number"] for d in existing], default=0) + 1

    update_clients_limit_ip([client["email"]], 1)

    plat_name = PLATFORM_NAMES.get(platform, platform)
    vless = generate_vless_link(client["id"], f"NetLink-{plat_name}")
    sub_url = _subscription_url(client["subId"])
    app = "happ" if platform in ("iphone", "android") else ""

    device_id = await db.create_user_device(
        user_id=user["id"],
        device_number=next_num,
        uuid=client["id"],
        email=client["email"],
        sub_id=client["subId"],
        vless_link=vless,
        subscription_url=sub_url,
        platform=platform,
        app_choice=app,
        is_admin_device=True,
    )

    link_block = _admin_device_link_text(
        platform, client["email"], client["subId"], vless, sub_url
    )
    await callback.message.answer(
        f"✅ Устройство #{next_num} добавлено ({client['email']}).\n\n{link_block}",
        reply_markup=back_to_admin_kb(),
        parse_mode="HTML",
    )
    await callback.answer(f"Добавлено: {client['email']}")


@router.callback_query(F.data == "admin_my_devices")
async def admin_my_devices_list(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    user = await _ensure_admin_user(
        callback.from_user.id, callback.from_user.username
    )
    devices = await db.get_user_devices(callback.from_user.id)
    if not devices:
        await callback.message.edit_text(
            "📱 У вас пока нет устройств.\n\nНажмите «➕ Добавить себе устройство».",
            reply_markup=back_to_admin_kb(),
        )
        await callback.answer()
        return

    lines = [f"📱 <b>Ваши устройства ({len(devices)})</b>\n"]
    rows = []
    for d in devices:
        plat = d.get("platform") or "?"
        plat_lbl = ADMIN_PLATFORM_EMOJI.get(plat, plat)
        email = d.get("email", "?")
        status_mark = "✅" if d.get("status") == "active" else "🔴"
        adm_mark = " 🔧" if d.get("is_admin_device") else ""
        lines.append(f"{d['device_number']}. {plat_lbl} ({email}) {status_mark}{adm_mark}")
        rows.append([InlineKeyboardButton(
            text=f"#{d['device_number']} {plat_lbl} ({email})",
            callback_data=f"asd_view_{d['id']}",
        )])
    rows.append([InlineKeyboardButton(text="➕ Добавить ещё", callback_data="admin_add_self")])
    rows.append([InlineKeyboardButton(text="🔙 В админ-панель", callback_data="admin_panel")])
    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("asd_view_"))
async def admin_my_device_detail(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    try:
        device_id = int(callback.data.split("_", 2)[2])
    except ValueError:
        await callback.answer()
        return
    device = await db.get_device(device_id)
    if not device:
        await callback.answer("Устройство не найдено", show_alert=True)
        return

    link_block = _admin_device_link_text(
        device.get("platform") or "",
        device["email"],
        device["sub_id"],
        device.get("vless_link", ""),
        device.get("subscription_url", ""),
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Удалить это устройство",
                              callback_data=f"asd_del_{device_id}")],
        [InlineKeyboardButton(text="🔙 К списку",
                              callback_data="admin_my_devices")],
    ])
    await callback.message.edit_text(
        f"📱 <b>Устройство #{device['device_number']}</b> ({device['email']})\n\n"
        f"{link_block}",
        reply_markup=kb,
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("asd_del_"))
async def admin_delete_confirm(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    try:
        device_id = int(callback.data.split("_", 2)[2])
    except ValueError:
        await callback.answer()
        return
    device = await db.get_device(device_id)
    if not device:
        await callback.answer("Устройство не найдено", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, удалить",
                              callback_data=f"asd_delyes_{device_id}")],
        [InlineKeyboardButton(text="❌ Отмена",
                              callback_data=f"asd_view_{device_id}")],
    ])
    await callback.message.edit_text(
        f"🗑 Удалить устройство #{device['device_number']} ({device['email']})?\n\n"
        f"UUID вернётся в свободный пул, x-ui будет перезапущен.",
        reply_markup=kb,
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("asd_delyes_"))
async def admin_delete_execute(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    try:
        device_id = int(callback.data.split("_", 2)[2])
    except ValueError:
        await callback.answer()
        return
    device = await db.get_device(device_id)
    if not device:
        await callback.answer("Устройство не найдено", show_alert=True)
        return

    email = device["email"]
    await db.delete_user_device(device_id)
    try:
        update_clients_limit_ip([email], 0)
    except Exception:
        pass

    # Re-render the list view
    callback.data = "admin_my_devices"
    await admin_my_devices_list(callback)
    await callback.answer(f"Удалено: {email}")


# ── Broadcast: notify all active users about the server migration ──

BROADCAST_TEXT = """Привет! Корпоративный VPN NetLink переехал на новый сервер. Старая ссылка больше не работает.

📲 Твоя новая подписка:
<code>{subscription_url}</code>

📖 Как подключить:

1. Удали из своего VPN-клиента старый профиль NetLink.

2. Установи Hiddify (если ещё не стоит):
   • iOS: App Store → «Hiddify»
   • Android: Google Play → «Hiddify»
   • macOS: App Store → «Hiddify», или hiddify.com
   • Windows: hiddify.com

3. В Hiddify нажми «+» → «Добавить профиль» / «Add Profile from URL» → вставь ссылку выше → Save.

4. Подключи VPN.

Российские сервисы (Яндекс, Госуслуги, банки, маркетплейсы) работают сами — без настроек.

Если у тебя несколько устройств — для каждого приходит отдельная подписка.

По вопросам пиши @QuentinCostello."""


async def _count_broadcast_targets() -> tuple[int, int]:
    users = await db.get_users_by_status("approved")
    user_count = 0
    device_count = 0
    for u in users:
        devices = await db.get_user_devices(u["telegram_id"])
        active = [d for d in devices if d["status"] == "active"]
        if active:
            user_count += 1
            device_count += len(active)
    return user_count, device_count


@router.message(Command("broadcast_new_subscription"))
async def cmd_broadcast_new_subscription(message: Message):
    if not is_admin(message.from_user.id):
        return
    user_count, device_count = await _count_broadcast_targets()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"✅ Разослать всем {user_count} пользователям ({device_count} устройств)?",
            callback_data="broadcast_confirm",
        )],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="broadcast_cancel")],
    ])
    await message.answer(
        f"⚠️ <b>Массовая рассылка о переезде на новый сервер</b>\n\n"
        f"Пользователей с активными устройствами: <b>{user_count}</b>\n"
        f"Отдельных сообщений (по устройствам): <b>{device_count}</b>\n\n"
        f"Между отправками задержка 0.5 сек. Логи: <code>/var/log/netlink-bot-broadcast.log</code>",
        reply_markup=kb,
        parse_mode="HTML",
    )


@router.callback_query(F.data == "broadcast_cancel")
async def cb_broadcast_cancel(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    await callback.message.edit_text("❌ Рассылка отменена.", parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "broadcast_confirm")
async def cb_broadcast_confirm(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    await callback.message.edit_text(
        "⏳ Рассылка запущена. Отчёт придёт в этот чат по завершении.",
        parse_mode="HTML",
    )
    await callback.answer()
    asyncio.create_task(_broadcast_and_report(callback.bot, callback.from_user.id))


async def _broadcast_and_report(bot, admin_id: int) -> None:
    try:
        sent, errors = await _run_broadcast(bot)
        await bot.send_message(
            admin_id,
            f"✅ Рассылка завершена.\n\n"
            f"Отправлено: <b>{sent}</b>, ошибок: <b>{errors}</b> "
            f"(детали в <code>/var/log/netlink-bot-broadcast.log</code>)",
            parse_mode="HTML",
        )
    except Exception as e:
        broadcast_log.exception("broadcast crashed")
        await bot.send_message(admin_id, f"❌ Ошибка рассылки: {e}")


async def _run_broadcast(bot) -> tuple[int, int]:
    sent = 0
    errors = 0
    users = await db.get_users_by_status("approved")
    for u in users:
        devices = await db.get_user_devices(u["telegram_id"])
        active = [d for d in devices if d["status"] == "active"]
        for d in active:
            text = BROADCAST_TEXT.format(subscription_url=_subscription_url(d["sub_id"]))
            try:
                await bot.send_message(u["telegram_id"], text, parse_mode="HTML")
                sent += 1
                broadcast_log.info(
                    "sent tg_id=%s device_id=%s sub_id=%s",
                    u["telegram_id"], d["id"], d["sub_id"],
                )
            except Exception as e:
                errors += 1
                broadcast_log.error(
                    "failed tg_id=%s device_id=%s sub_id=%s err=%s",
                    u["telegram_id"], d["id"], d["sub_id"], e,
                )
            await asyncio.sleep(0.5)
    broadcast_log.info("broadcast finished sent=%d errors=%d", sent, errors)
    return sent, errors


# ── IP-sharing monitor: block / ignore / list ──

def _violation_kb(device_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚫 Заблокировать UUID",
                              callback_data=f"block_uuid:{device_id}")],
        [InlineKeyboardButton(text="✅ Игнорировать",
                              callback_data=f"ignore_violation:{device_id}")],
    ])


@router.callback_query(F.data.startswith("block_uuid:"))
async def cb_block_uuid(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    try:
        device_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer()
        return

    device = await db.get_device(device_id)
    if not device:
        await callback.answer("Устройство не найдено", show_alert=True)
        return

    now = datetime.now().isoformat()
    from bot.db.models import get_db as _get_db
    async with _get_db() as conn:
        await conn.execute(
            "UPDATE user_devices SET status='banned', banned_at=? WHERE id=?",
            (now, device_id),
        )
        await conn.commit()

    try:
        set_client_enabled(device["email"], False)
    except Exception as e:
        await callback.answer(f"x-ui write failed: {e}", show_alert=True)

    user_row = None
    async with _get_db() as conn:
        cursor = await conn.execute(
            "SELECT telegram_id FROM users WHERE id=?",
            (device["user_id"],),
        )
        user_row = await cursor.fetchone()
    if user_row:
        try:
            await callback.bot.send_message(
                user_row[0],
                f"Ваше устройство #{device['device_number']} отключено за нарушение "
                f"правил (общий доступ к подписке). По вопросам пишите @routewise96.",
            )
        except Exception:
            pass

    try:
        await callback.message.edit_text(
            (callback.message.html_text or callback.message.text or "")
            + "\n\n🚫 <b>Заблокировано админом.</b>",
            parse_mode="HTML",
        )
    except Exception:
        pass
    await callback.answer(f"Заблокировано: {device['email']}")


@router.callback_query(F.data.startswith("ignore_violation:"))
async def cb_ignore_violation(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    try:
        device_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer()
        return

    device = await db.get_device(device_id)
    if not device:
        await callback.answer("Устройство не найдено", show_alert=True)
        return

    from bot.db.models import get_db as _get_db
    async with _get_db() as conn:
        await conn.execute(
            """UPDATE violations SET ignored=1
               WHERE email=? AND created_at >= datetime('now','-24 hours')""",
            (device["email"],),
        )
        await conn.commit()

    try:
        await callback.message.edit_text(
            (callback.message.html_text or callback.message.text or "")
            + "\n\n✅ <b>Игнорируется 24 часа.</b>",
            parse_mode="HTML",
        )
    except Exception:
        pass
    await callback.answer(f"Игнор 24ч: {device['email']}")


@router.message(Command("violations"))
async def cmd_violations(message: Message):
    if not is_admin(message.from_user.id):
        return
    from bot.db.models import get_db as _get_db
    async with _get_db() as conn:
        cursor = await conn.execute(
            """SELECT v.email,
                      COUNT(DISTINCT v.minute_bucket) AS windows,
                      MAX(v.created_at) AS last_seen,
                      MAX(v.ignored) AS is_ignored,
                      MAX(v.alerted) AS is_alerted,
                      ud.id AS device_id, ud.device_number,
                      u.fio, u.telegram_id
               FROM violations v
               LEFT JOIN user_devices ud ON ud.email = v.email
               LEFT JOIN users u ON u.id = ud.user_id
               WHERE v.created_at >= datetime('now','-24 hours')
               GROUP BY v.email
               ORDER BY windows DESC, last_seen DESC""",
        )
        rows = await cursor.fetchall()

    if not rows:
        await message.answer("📊 За последние 24 часа нарушений не зафиксировано.")
        return

    await message.answer(
        f"📊 <b>Нарушения за 24 часа</b> ({len(rows)} email)",
        parse_mode="HTML",
    )
    for r in rows:
        email, windows, last_seen, is_ignored, is_alerted = r[0], r[1], r[2], r[3], r[4]
        device_id, device_number, fio, tg_id = r[5], r[6], r[7], r[8]
        flag = "🛑" if is_ignored else ("🔔" if is_alerted else "•")
        head = (
            f"{flag} <b>{fio or '?'}</b> (tg_id {tg_id})\n"
            f"Email: <code>{email}</code> · устройство #{device_number}\n"
            f"Окон: <b>{windows}</b> · последнее: {last_seen[:16] if last_seen else '?'}"
        )
        if device_id is None:
            await message.answer(head + "\n\n⚠️ Устройство не найдено в user_devices.",
                                 parse_mode="HTML")
            continue
        await message.answer(head, parse_mode="HTML", reply_markup=_violation_kb(device_id))
