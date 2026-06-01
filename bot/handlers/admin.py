"""Admin panel: approve/reject, per-device management, stats."""
import json
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
    back_to_admin_kb,
)
from bot.keyboards.user_kb import main_menu_kb, agreement_start_kb
from bot.config import ADMIN_CHAT_ID, SERVER_IP

router = Router()

PLATFORM_LABELS = {
    "iphone": "📱 iPhone (Happ)",
    "android": "📱 Android (Happ)",
    "windows": "💻 Windows (Hiddify)",
    "macos": "🖥 macOS (sing-box VT)",
}
PLATFORM_NAMES = {"iphone": "iPhone", "android": "Android", "windows": "Windows", "macos": "macOS"}


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

def _build_device_link_block(dd: dict) -> str:
    """Single-device rendered link block for approval / add-device messages."""
    platform = dd["platform"]
    label = PLATFORM_LABELS.get(platform, f"📱 {platform}")
    if platform == "macos":
        return f"{label}:\n<code>{dd['sub_url']}</code>\n"
    elif platform in ("iphone", "android") and dd.get("sub_id"):
        url = f"http://{SERVER_IP}:8080/connect/{dd['sub_id']}.html"
        return (
            f"{label}:\n"
            f"Нажмите — ссылка скопируется автоматически:\n"
            f"{url}\n"
        )
    else:
        return f"{label}:\n<code>{dd['vless']}</code>\n"


def _build_approval_text(devices_data: list[dict]) -> str:
    """Build per-platform links text for approval message."""
    n = len(devices_data)
    dev_word = "устройство" if n == 1 else "устройства"
    lines = [
        f"✅ <b>Доступ одобрен!</b> Вам выданы ссылки на {n} {dev_word}.\n",
        "⚠️ Каждая ссылка работает строго на <b>ОДНОМ</b> устройстве. "
        "При использовании на двух устройствах одновременно — ссылка блокируется автоматически.\n",
    ]

    has_mobile = False
    for dd in devices_data:
        platform = dd["platform"]
        label = PLATFORM_LABELS.get(platform, f"📱 {platform}")
        if platform == "macos":
            lines.append(f"{label}:\n<code>{dd['sub_url']}</code>\n")
        elif platform in ("iphone", "android") and dd.get("sub_id"):
            url = f"http://{SERVER_IP}:8080/connect/{dd['sub_id']}.html"
            lines.append(
                f"{label}:\n"
                f"Нажмите — ссылка скопируется автоматически:\n"
                f"{url}\n"
            )
            has_mobile = True
        else:
            lines.append(f"{label}:\n<code>{dd['vless']}</code>\n")

    if has_mobile:
        lines.append(
            f"🔧 <b>Настройка маршрутизации (один раз):</b>\n"
            f"Нажмите ссылку ниже — Happ импортирует правила:\n"
            f"http://{SERVER_IP}:8080/route.html\n\n"
            "✅ Теперь не нужно отключать сервис для использования российских "
            "приложений — Сбербанк, Яндекс, Госуслуги и другие работают без переключений.\n"
        )

    return "\n".join(lines)


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
        sub_url = f"http://{SERVER_IP}:8080/profiles/{client['subId']}.json"
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

    user_text = _build_approval_text(devices_data)
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

    lines = [f"🔗 <b>Ссылки {user['fio']}:</b>\n"]
    for d in devices:
        platform = d.get("platform", "")
        label = PLATFORM_LABELS.get(platform, f"Устройство {d['device_number']}")
        status = "✅" if d["status"] == "active" else "🔴"
        lines.append(f"{label} {status} ({d['email']}):")
        if d["status"] == "active":
            if platform == "macos":
                lines.append(f"<code>{d.get('subscription_url', '')}</code>")
            elif platform in ("iphone", "android") and d.get("sub_id"):
                url = f"http://{SERVER_IP}:8080/connect/{d['sub_id']}.html"
                lines.append(f"{url}")
            else:
                lines.append(f"<code>{d.get('vless_link', '')}</code>")
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
    sub_url = f"http://{SERVER_IP}:8080/profiles/{client['subId']}.json"
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

    dd = {
        "platform": platform,
        "email": client["email"],
        "vless": vless,
        "sub_url": sub_url,
        "sub_id": client["subId"],
    }
    user_text = (
        f"✅ <b>Доп. устройство одобрено!</b>\n\n"
        f"{_build_device_link_block(dd)}\n"
        f"⚠️ Ссылка работает строго на <b>ОДНОМ</b> устройстве. "
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
    if platform == "macos":
        body = (
            f"{label}:\n<code>{sub_url}</code>\n\n"
            "v2RayTun → Subscription → Add → вставить URL → Update subscription → Connect."
        )
    elif platform in ("iphone", "android"):
        url = f"http://{SERVER_IP}:8080/connect/{sub_id}.html"
        body = (
            f"{label}:\nНажмите — ссылка скопируется автоматически:\n{url}\n\n"
            "v2RayTun → + → «Импорт из буфера обмена» → Connect."
        )
    else:  # windows
        body = (
            f"{label}:\n<code>{vless_link}</code>\n\n"
            "v2rayN → Import → From clipboard → Connect."
        )
    return body


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
    sub_url = f"http://{SERVER_IP}:8080/profiles/{client['subId']}.json"
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
