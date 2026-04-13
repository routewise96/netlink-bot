"""Approved user handlers: link, instruction, devices, questions."""
import json
import time

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import CallbackQuery, Message

from bot.db import queries as db
from bot.services import deepseek
from bot.keyboards.user_kb import main_menu_kb, back_to_menu_kb, link_and_back_kb
from bot.config import ADMIN_CHAT_ID, SERVER_IP

router = Router()

# Rate limit: track last question timestamps per user
_rate_limit: dict[int, list[float]] = {}
RATE_LIMIT_MAX = 5
RATE_LIMIT_WINDOW = 60


class AskQuestion(StatesGroup):
    waiting_question = State()


def _build_devices_text(devices: list[dict]) -> str:
    """Build per-device links message."""
    lines = ["🔗 <b>Ваши ссылки для подключения:</b>\n"]
    lines.append(
        "⚠️ Каждая ссылка работает строго на <b>ОДНОМ</b> устройстве. "
        "При использовании на двух устройствах одновременно — ссылка блокируется автоматически.\n"
    )

    vless_lines = []
    sub_lines = []

    for d in devices:
        num = d["device_number"]
        if d["status"] == "active":
            vless_lines.append(f"📱 Устройство {num}:\n<code>{d['vless_link']}</code>")
            if d.get("subscription_url"):
                sub_lines.append(f"Устройство {num}: <code>{d['subscription_url']}</code>")
        else:
            vless_lines.append(f"📱 Устройство {num}: 🔴 заблокировано")

    lines.extend(vless_lines)

    if sub_lines:
        lines.append(f"\n🖥 <b>Для macOS</b> (sing-box VT):")
        lines.extend(sub_lines)

    lines.append("\nНажмите 📖 Инструкция для пошаговой настройки.")
    return "\n".join(lines)


INSTRUCTIONS = {
    "iphone": """📱 <b>Установка на iPhone (Streisand)</b>

1. Откройте App Store → найдите "Streisand"
2. Скачайте и откройте
3. Нажмите "+" в правом верхнем углу
4. Выберите "Импорт из буфера обмена"
5. Скопируйте вашу ссылку (vless://...) выше
6. Вернитесь в Streisand — профиль добавится автоматически
7. Нажмите переключатель вверху для подключения

✅ Готово!""",

    "android": """📱 <b>Установка на Android (Streisand)</b>

1. Откройте Google Play → найдите "Streisand"
2. Скачайте и откройте
3. Нажмите "+" в правом верхнем углу
4. Выберите "Импорт из буфера обмена"
5. Скопируйте вашу ссылку (vless://...) выше
6. Вернитесь в Streisand — профиль добавится автоматически
7. Нажмите переключатель вверху для подключения

✅ Готово!""",

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

✅ Готово!""",
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

    platforms = json.loads(user["platforms"] or "[]")
    texts = []
    for p in platforms:
        if p in INSTRUCTIONS:
            texts.append(INSTRUCTIONS[p])

    if not texts:
        texts.append("Инструкции для ваших платформ не найдены.")

    full_text = "\n\n".join(texts)
    if len(full_text) > 4000:
        for text in texts:
            await callback.message.answer(text, parse_mode="HTML")
    else:
        await callback.message.answer(
            full_text,
            parse_mode="HTML",
        )
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
    platforms = json.loads(user["platforms"] or "[]")
    platform_names = {"iphone": "iPhone", "android": "Android", "windows": "Windows", "macos": "macOS"}
    platforms_str = ", ".join(platform_names.get(p, p) for p in platforms)
    approved_date = (user["approved_at"] or "")[:10]

    lines = [
        f"📊 <b>Ваш профиль</b>\n",
        f"👤 {user['fio']}",
        f"💻 Платформы: {platforms_str}",
        f"📅 Доступ с: {approved_date}\n",
    ]

    active = sum(1 for d in devices if d["status"] == "active")
    banned = sum(1 for d in devices if d["status"] == "banned")
    for d in devices:
        num = d["device_number"]
        status_icon = "✅" if d["status"] == "active" else "🔴"
        status_text = "активно" if d["status"] == "active" else "заблокировано"
        lines.append(f"📱 Устройство {num}: {status_icon} {status_text}")

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

    # Rate limit
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
        # Escalate to admin
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
