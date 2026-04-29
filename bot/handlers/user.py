"""Approved user handlers: link, instruction, devices, questions, add-device."""
import json
import time

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import (
    CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton,
)

from bot.db import queries as db
from bot.services import deepseek
from bot.services.proxy import update_clients_limit_ip
from bot.keyboards.user_kb import (
    main_menu_kb, back_to_menu_kb, link_and_back_kb, add_device_platforms_kb,
)
from bot.config import ADMIN_CHAT_ID, SERVER_IP

router = Router()

_rate_limit: dict[int, list[float]] = {}
RATE_LIMIT_MAX = 5
RATE_LIMIT_WINDOW = 60

PLATFORM_LABELS = {
    "iphone": "📱 iPhone (Happ)",
    "android": "📱 Android (Happ)",
    "windows": "💻 Windows (Hiddify)",
    "macos": "🖥 macOS (sing-box VT)",
}


class AskQuestion(StatesGroup):
    waiting_question = State()


ROUTING_DONE = (
    "✅ Теперь не нужно отключать сервис для использования российских "
    "приложений — Сбербанк, Яндекс, Госуслуги и другие работают без переключений.\n"
)


def _routing_text() -> str:
    """Build routing instruction block for mobile platforms."""
    return (
        f"🔧 <b>Настройка маршрутизации (один раз):</b>\n"
        f"Нажмите ссылку ниже — Happ импортирует правила:\n"
        f"http://{SERVER_IP}:8080/route.html\n\n"
        + ROUTING_DONE
    )


def _connect_url(sub_id: str) -> str:
    """Build connect redirect URL for a device."""
    return f"http://{SERVER_IP}:8080/connect/{sub_id}.html"


def _build_devices_text(devices: list[dict]) -> str:
    """Build per-device links message with platform labels."""
    lines = ["🔗 <b>Ваши ссылки для подключения:</b>\n"]
    lines.append(
        "⚠️ Каждая ссылка работает строго на <b>ОДНОМ</b> устройстве. "
        "При использовании на двух устройствах одновременно — ссылка блокируется автоматически.\n"
    )

    has_routing = False
    for d in devices:
        platform = d.get("platform", "")
        sub_id = d.get("sub_id", "")
        label = PLATFORM_LABELS.get(platform, f"📱 Устройство {d['device_number']}")

        if d["status"] != "active":
            lines.append(f"{label}: 🔴 заблокировано\n")
            continue

        if platform == "macos":
            lines.append(f"{label}:\n<code>{d.get('subscription_url', '')}</code>\n")
        elif platform in ("iphone", "android") and sub_id:
            url = _connect_url(sub_id)
            lines.append(
                f"{label}:\n"
                f"Нажмите — ссылка скопируется автоматически:\n"
                f"{url}\n"
            )
            has_routing = True
        else:
            # windows or fallback
            lines.append(f"{label}:\n<code>{d.get('vless_link', '')}</code>\n")

    if has_routing:
        lines.append(_routing_text())

    lines.append("Нажмите 📖 Инструкция для пошаговой настройки.")
    return "\n".join(lines)


HAPP_ROUTING_INSTRUCTION = f"""
🔧 <b>Шаг 2 — Маршрутизация (один раз):</b>

Нажмите ссылку ниже — Happ импортирует правила:
http://{SERVER_IP}:8080/route.html

✅ Теперь не нужно отключать сервис для использования российских приложений — Сбербанк, Яндекс, Госуслуги и другие работают без переключений."""

INSTRUCTIONS = {
    "iphone": """📱 <b>Установка на iPhone (Happ)</b>

<b>Шаг 1 — Подключение:</b>
1. Откройте App Store → найдите "Happ - Proxy Utility" → скачайте
2. Вернитесь в этот чат → нажмите на ссылку выше
3. Ссылка скопируется автоматически
4. Откройте Happ → нажмите "+" → "Из буфера обмена"
5. Нажмите кнопку подключения

✅ Готово!
""" + HAPP_ROUTING_INSTRUCTION,

    "android": """📱 <b>Установка на Android (Happ)</b>

<b>Шаг 1 — Подключение:</b>
1. Откройте Google Play → найдите "Happ - Proxy Utility" → скачайте
2. Вернитесь в этот чат → нажмите на ссылку выше
3. Ссылка скопируется автоматически
4. Откройте Happ → нажмите "+" → "Из буфера обмена"
5. Нажмите кнопку подключения

✅ Готово!
""" + HAPP_ROUTING_INSTRUCTION,

    "windows": """💻 <b>Установка на Windows (Hiddify)</b>

1. Скачайте Hiddify: https://hiddify.com
2. Установите и запустите
3. Нажмите "+" → "Добавить из буфера обмена"
4. Скопируйте вашу ссылку (vless://...) выше
5. Вставьте — профиль добавится
6. Нажмите "Подключить"

⚠️ ВАЖНО: Зайдите в Настройки → отключите "Блокировать рекламу". Без этого не будут работать Яндекс Карты, Такси и другие сервисы Яндекса.

✅ Готово!""",

    "macos": """🖥 <b>Установка на macOS (sing-box VT)</b>

1. Откройте App Store → найдите "sing-box VT"
2. Скачайте и откройте
3. Перейдите в Profiles → New Profile
4. Type: выберите Remote
5. Name: NetLink
6. URL: вставьте ссылку подписки для macOS выше
7. Нажмите Create
8. Перейдите в Dashboard → выберите профиль → нажмите ▶
9. Разрешите добавление VPN-конфигурации если попросит

✅ Маршрутизация уже настроена в профиле — российские приложения работают автоматически.""",
}


@router.callback_query(F.data == "back_menu")
async def back_to_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer(
        "🔒 <b>NetLink</b> — защищённый доступ\n\nВыберите действие:",
        reply_markup=main_menu_kb(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "my_link")
async def show_link(callback: CallbackQuery):
    user = await db.get_user(callback.from_user.id)
    if not user or user["status"] != "approved":
        await callback.answer("Доступ не активен", show_alert=True)
        return

    devices = await db.get_user_devices(callback.from_user.id)
    if not devices:
        await callback.answer("Ссылки не найдены", show_alert=True)
        return

    await callback.message.answer(
        _build_devices_text(devices),
        reply_markup=link_and_back_kb(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "instruction")
async def show_instruction(callback: CallbackQuery):
    user = await db.get_user(callback.from_user.id)
    if not user or user["status"] != "approved":
        await callback.answer("Доступ не активен", show_alert=True)
        return

    devices = await db.get_user_devices(callback.from_user.id)
    if devices:
        platforms = [d["platform"] for d in devices if d.get("platform")]
    else:
        platforms = json.loads(user["platforms"] or "[]")

    texts = []
    seen = set()
    for p in platforms:
        if p in seen:
            continue
        seen.add(p)
        if p in INSTRUCTIONS:
            texts.append(INSTRUCTIONS[p])

    if not texts:
        texts.append("Инструкции для ваших платформ не найдены.")

    full_text = "\n\n".join(texts)
    if len(full_text) > 4000:
        for text in texts:
            await callback.message.answer(text, parse_mode="HTML")
    else:
        await callback.message.answer(full_text, parse_mode="HTML")
    await callback.message.answer(
        "Выберите действие:",
        reply_markup=main_menu_kb(),
    )
    await callback.answer()


@router.callback_query(F.data == "my_devices")
async def show_devices(callback: CallbackQuery):
    user = await db.get_user(callback.from_user.id)
    if not user or user["status"] != "approved":
        await callback.answer("Доступ не активен", show_alert=True)
        return

    devices = await db.get_user_devices(callback.from_user.id)
    approved_date = (user["approved_at"] or "")[:10]

    lines = [
        f"📊 <b>Ваш профиль</b>\n",
        f"👤 {user['fio']}",
        f"📅 Доступ с: {approved_date}\n",
    ]

    active = 0
    banned = 0
    for d in devices:
        platform = d.get("platform", "")
        label = PLATFORM_LABELS.get(platform, f"Устройство {d['device_number']}")
        if d["status"] == "active":
            lines.append(f"{label}: ✅ активно")
            active += 1
        else:
            lines.append(f"{label}: 🔴 заблокировано")
            banned += 1

    lines.append(f"\n📱 Активных: {active}" + (f" | 🔴 Забанено: {banned}" if banned else ""))

    await callback.message.answer(
        "\n".join(lines),
        reply_markup=main_menu_kb(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "ask_question")
async def start_question(callback: CallbackQuery, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    if not user or user["status"] != "approved":
        await callback.answer("Доступ не активен", show_alert=True)
        return

    await callback.message.answer(
        "💬 Задайте ваш вопрос о работе сервиса.",
        reply_markup=back_to_menu_kb(),
        parse_mode="HTML",
    )
    await state.set_state(AskQuestion.waiting_question)
    await callback.answer()


@router.message(AskQuestion.waiting_question)
async def process_question(message: Message, state: FSMContext):
    question = message.text
    if not question or not question.strip():
        return

    question = question.strip()
    user = await db.get_user(message.from_user.id)
    if not user or user["status"] != "approved":
        await state.clear()
        return

    uid = message.from_user.id
    now = time.time()
    timestamps = _rate_limit.get(uid, [])
    timestamps = [t for t in timestamps if now - t < RATE_LIMIT_WINDOW]
    if len(timestamps) >= RATE_LIMIT_MAX:
        await message.answer(
            "⏳ Слишком много вопросов. Подождите минуту.",
            reply_markup=back_to_menu_kb(),
        )
        return
    timestamps.append(now)
    _rate_limit[uid] = timestamps

    ai_response = await deepseek.ask(question)

    if ai_response:
        await db.save_ai_conversation(message.from_user.id, question, ai_response)
        await message.answer(
            ai_response + "\n\nЗадайте ещё вопрос или вернитесь в меню.",
            reply_markup=back_to_menu_kb(),
        )
    else:
        await db.save_ai_conversation(message.from_user.id, question, "", escalated=True)
        username = message.from_user.username
        username_str = f"@{username}" if username else str(message.from_user.id)
        fio = user.get("fio", "")
        await message.bot.send_message(
            ADMIN_CHAT_ID,
            f"❓ <b>Вопрос от {fio}</b> ({username_str}):\n\n{question}",
            parse_mode="HTML",
        )
        await message.answer(
            "Передаю ваш вопрос администратору. Он ответит вам лично.",
            reply_markup=back_to_menu_kb(),
        )


# ── Add device ──

MAX_DEVICES = 3
PLATFORM_ORDER = ["iphone", "android", "windows", "macos"]
PLATFORM_DISPLAY = {
    "iphone": "iPhone",
    "android": "Android",
    "windows": "Windows",
    "macos": "macOS",
}


@router.callback_query(F.data == "add_device")
async def add_device_start(callback: CallbackQuery):
    user = await db.get_user(callback.from_user.id)
    if not user or user["status"] != "approved":
        await callback.answer("Доступ не активен", show_alert=True)
        return

    devices = await db.get_user_devices(callback.from_user.id)
    active = [d for d in devices if d["status"] == "active"]
    count = len(active)

    if count >= MAX_DEVICES:
        await callback.message.answer(
            f"⚠️ У вас уже {count}/{MAX_DEVICES} устройств — это максимум.\n"
            f"Для добавления большего количества обратитесь к администратору.",
            reply_markup=back_to_menu_kb(),
        )
        await callback.answer()
        return

    owned = {d["platform"] for d in active if d.get("platform")}
    available = [p for p in PLATFORM_ORDER if p not in owned]

    if not available:
        await callback.message.answer(
            "⚠️ У вас уже есть устройства на всех поддерживаемых платформах.",
            reply_markup=back_to_menu_kb(),
        )
        await callback.answer()
        return

    owned_labels = ", ".join(PLATFORM_DISPLAY[p] for p in PLATFORM_ORDER if p in owned) or "—"
    await callback.message.answer(
        f"📱 <b>Добавление устройства</b>\n\n"
        f"Сейчас: {count}/{MAX_DEVICES} устройств\n"
        f"Уже подключены: {owned_labels}\n\n"
        f"Выберите платформу для нового устройства:",
        reply_markup=add_device_platforms_kb(available),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adddev_"))
async def add_device_pick_platform(callback: CallbackQuery):
    platform = callback.data.split("_", 1)[1]
    if platform not in PLATFORM_ORDER:
        await callback.answer()
        return

    user = await db.get_user(callback.from_user.id)
    if not user or user["status"] != "approved":
        await callback.answer("Доступ не активен", show_alert=True)
        return

    devices = await db.get_user_devices(callback.from_user.id)
    active = [d for d in devices if d["status"] == "active"]
    count = len(active)

    if count >= MAX_DEVICES:
        await callback.answer("Уже 3 устройства", show_alert=True)
        return

    owned = {d["platform"] for d in active if d.get("platform")}
    if platform in owned:
        await callback.answer("Эта платформа уже подключена", show_alert=True)
        return

    req_id = await db.create_request(
        telegram_id=callback.from_user.id,
        fio=user["fio"],
        devices_count=1,
        platforms=json.dumps([platform]),
        request_type="add",
    )

    plat_label = PLATFORM_DISPLAY[platform]
    admin_text = (
        f"📱 <b>Доп. устройство #{req_id}</b>\n\n"
        f"👤 {user['fio']}\n"
        f"💻 Платформа: {plat_label}\n"
        f"🔢 Сейчас: {count}/{MAX_DEVICES}"
    )
    admin_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Одобрить", callback_data=f"addapprove_{req_id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"addreject_{req_id}"),
    ]])
    await callback.bot.send_message(
        ADMIN_CHAT_ID, admin_text, reply_markup=admin_kb, parse_mode="HTML",
    )

    await callback.message.answer(
        f"✅ Заявка на добавление устройства отправлена администратору.\n\n"
        f"Платформа: <b>{plat_label}</b>\n"
        f"После одобрения вы получите ссылку для нового устройства.",
        reply_markup=back_to_menu_kb(),
        parse_mode="HTML",
    )
    await callback.answer()


# ── Change platform (instant, no admin approval, UUID unchanged) ──

PLATFORM_SHORT = {
    "iphone": "📱 iPhone",
    "android": "📱 Android",
    "windows": "💻 Windows",
    "macos": "🖥 macOS",
}


def _build_single_device_link(device: dict) -> str:
    """Build link block for a single device based on its platform."""
    platform = device.get("platform", "")
    label = PLATFORM_LABELS.get(platform, f"Устройство {device['device_number']}")
    sub_id = device.get("sub_id", "")
    if platform == "macos":
        return f"{label}:\n<code>{device.get('subscription_url', '')}</code>"
    elif platform in ("iphone", "android") and sub_id:
        return (
            f"{label}:\n"
            f"Нажмите — ссылка скопируется автоматически:\n"
            f"{_connect_url(sub_id)}"
        )
    else:
        return f"{label}:\n<code>{device.get('vless_link', '')}</code>"


@router.callback_query(F.data == "change_platform")
async def change_platform_start(callback: CallbackQuery):
    user = await db.get_user(callback.from_user.id)
    if not user or user["status"] != "approved":
        await callback.answer("Доступ не активен", show_alert=True)
        return

    devices = await db.get_user_devices(callback.from_user.id)
    active = [d for d in devices if d["status"] == "active"]
    if not active:
        await callback.message.answer(
            "У вас нет активных устройств.",
            reply_markup=back_to_menu_kb(),
        )
        await callback.answer()
        return

    lines = ["🔄 <b>Смена платформы</b>\n"]
    for d in active:
        plat = d.get("platform", "")
        short = PLATFORM_SHORT.get(plat, "?")
        plat_name = PLATFORM_DISPLAY.get(plat, plat or "—")
        lines.append(f"{short} Устройство {d['device_number']}: {plat_name}")
    lines.append("\nВыберите устройство для смены платформы:")

    rows = []
    for d in active:
        plat = d.get("platform", "")
        plat_name = PLATFORM_DISPLAY.get(plat, plat or "?")
        rows.append([InlineKeyboardButton(
            text=f"🔄 Устройство {d['device_number']} ({plat_name})",
            callback_data=f"cpdev_{d['id']}",
        )])
    rows.append([InlineKeyboardButton(text="🔙 Назад в меню", callback_data="back_menu")])

    await callback.message.answer(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("cpdev_"))
async def change_platform_pick(callback: CallbackQuery):
    try:
        device_id = int(callback.data.split("_", 1)[1])
    except (ValueError, IndexError):
        await callback.answer()
        return

    device = await db.get_device(device_id)
    if not device:
        await callback.answer("Устройство не найдено", show_alert=True)
        return

    user = await db.get_user(callback.from_user.id)
    if not user or user["status"] != "approved":
        await callback.answer("Доступ не активен", show_alert=True)
        return
    if device["user_id"] != user["id"]:
        await callback.answer("Это не ваше устройство", show_alert=True)
        return

    current = device.get("platform", "")
    current_label = PLATFORM_SHORT.get(current, "?")

    rows = []
    for p in PLATFORM_ORDER:
        mark = " ✓" if p == current else ""
        rows.append([InlineKeyboardButton(
            text=f"{PLATFORM_SHORT[p]}{mark}",
            callback_data=f"cpset_{device_id}_{p}",
        )])
    rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data="change_platform")])

    await callback.message.answer(
        f"🔄 <b>Устройство {device['device_number']}</b>\n"
        f"Текущая платформа: {current_label}\n\n"
        f"Выберите новую платформу:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("cpset_"))
async def change_platform_apply(callback: CallbackQuery):
    parts = callback.data.split("_", 2)
    if len(parts) != 3:
        await callback.answer()
        return
    try:
        device_id = int(parts[1])
    except ValueError:
        await callback.answer()
        return
    new_platform = parts[2]
    if new_platform not in PLATFORM_ORDER:
        await callback.answer()
        return

    device = await db.get_device(device_id)
    if not device:
        await callback.answer("Устройство не найдено", show_alert=True)
        return

    user = await db.get_user(callback.from_user.id)
    if not user or user["status"] != "approved":
        await callback.answer("Доступ не активен", show_alert=True)
        return
    if device["user_id"] != user["id"]:
        await callback.answer("Это не ваше устройство", show_alert=True)
        return

    if device.get("platform") == new_platform:
        await callback.answer("Уже на этой платформе", show_alert=True)
        return

    # Update DB: platform + app_choice (happ for mobile, empty otherwise).
    new_app = "happ" if new_platform in ("iphone", "android") else ""
    await db.update_device_platform(device_id, new_platform)
    from bot.db.models import get_db as _get_db
    async with _get_db() as conn:
        await conn.execute(
            "UPDATE user_devices SET app_choice = ? WHERE id = ?",
            (new_app, device_id),
        )
        await conn.commit()

    updated = await db.get_device(device_id)
    plat_name = PLATFORM_DISPLAY.get(new_platform, new_platform)
    link_block = _build_single_device_link(updated)
    instruction = INSTRUCTIONS.get(new_platform, "")

    msg = (
        f"✅ <b>Платформа устройства {updated['device_number']} изменена на {plat_name}</b>\n\n"
        f"{link_block}\n\n"
        f"{instruction}"
    )
    await callback.message.answer(msg, reply_markup=back_to_menu_kb(), parse_mode="HTML")
    await callback.answer(f"Платформа: {plat_name}")


# ── User self-reset ──

@router.callback_query(F.data == "user_reset")
async def user_reset_start(callback: CallbackQuery):
    user = await db.get_user(callback.from_user.id)
    if not user:
        await callback.answer("Пользователь не найден", show_alert=True)
        return

    confirm_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, сбросить", callback_data="user_reset_confirm")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="back_menu")],
    ])
    await callback.message.answer(
        "⚠️ <b>Вы уверены?</b>\n\n"
        "Все ваши ссылки будут удалены и нужно будет пройти регистрацию заново.",
        reply_markup=confirm_kb,
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "user_reset_confirm")
async def user_reset_confirm(callback: CallbackQuery, state: FSMContext):
    import subprocess
    telegram_id = callback.from_user.id
    user = await db.get_user(telegram_id)
    if not user:
        await callback.answer("Пользователь не найден", show_alert=True)
        return

    # Release UUIDs — set limitIp=0 for all device emails
    devices = await db.get_user_devices(telegram_id)
    active_emails = [d["email"] for d in devices if d.get("email")]
    if active_emails:
        try:
            update_clients_limit_ip(active_emails, 0)
            subprocess.run(["systemctl", "restart", "x-ui"], timeout=10)
        except Exception:
            pass

    # Delete all user data
    await db.delete_user_devices(telegram_id)
    from bot.db.models import get_db as _get_db
    async with _get_db() as conn:
        await conn.execute("DELETE FROM requests WHERE telegram_id = ?", (telegram_id,))
        await conn.execute("DELETE FROM users WHERE telegram_id = ?", (telegram_id,))
        await conn.commit()

    await state.clear()
    await callback.message.answer(
        "✅ Данные сброшены. Нажмите /start чтобы начать заново."
    )
    await callback.answer()
