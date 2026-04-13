"""Registration flow: /start → agreement → FIO → platforms → request."""
import json
from datetime import datetime

from aiogram import Router, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import Message, CallbackQuery

from bot.db import queries as db
from bot.keyboards.user_kb import (
    agreement_start_kb,
    agreement_accept_kb,
    platforms_kb,
    main_menu_kb,
)
from bot.keyboards.admin_kb import approve_reject_kb
from bot.config import ADMIN_CHAT_ID

router = Router()

PLATFORM_NAMES = {"iphone": "iPhone", "android": "Android", "windows": "Windows", "macos": "macOS"}
MAX_DEVICES = 3

AGREEMENT_TEXT = """📜 <b>УСЛОВИЯ ИСПОЛЬЗОВАНИЯ СЕРВИСА NETLINK</b>

1. Сервис предназначен исключительно для обеспечения рабочего доступа к корпоративным ресурсам и сервисам.

2. <b>ЗАПРЕЩЕНО:</b>
• Использование сервиса для любой противоправной деятельности
• Передача ссылки доступа третьим лицам
• Попытки обхода ограничений сервиса

3. Каждая ссылка выдаётся строго на <b>ОДНО</b> устройство. При обнаружении использования одной ссылки на двух и более устройствах одновременно — ссылка блокируется автоматически.

4. Сервис фиксирует дату, время подключений и адреса запрашиваемых ресурсов для обеспечения стабильной работы и безопасности. Содержимое трафика (переписки, пароли, передаваемые данные) не отслеживается и не сохраняется.

5. В случае нарушения условий использования доступ к сервису может быть ограничен.

6. Пользователь несёт персональную ответственность за все действия, совершённые через предоставленный доступ.

7. Принимая данные условия, вы подтверждаете, что ознакомлены с правилами и обязуетесь их соблюдать."""


class Registration(StatesGroup):
    waiting_fio = State()
    waiting_platforms = State()


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    telegram_id = message.from_user.id

    data = await state.get_data()
    if not data.get("test_mode"):
        await state.clear()

        if telegram_id == ADMIN_CHAT_ID:
            from bot.db.queries import get_stats
            stats = await get_stats()
            from bot.keyboards.admin_kb import admin_panel_kb
            await message.answer(
                "🔧 <b>Админ-панель NetLink</b>",
                reply_markup=admin_panel_kb(stats["pending"]),
                parse_mode="HTML",
            )
            return

    user = await db.get_user(telegram_id)

    if user and user["status"] == "approved":
        await message.answer(
            "🔒 <b>NetLink</b> — защищённый доступ\n\nВыберите действие:",
            reply_markup=main_menu_kb(),
            parse_mode="HTML",
        )
        return

    if user and user["status"] == "pending":
        await message.answer(
            "⏳ Ваша заявка ожидает рассмотрения. Пожалуйста, дождитесь ответа администратора."
        )
        return

    if user and user["status"] == "blocked":
        await message.answer("🚫 Ваш доступ заблокирован. Обратитесь к администратору.")
        return

    await message.answer(
        "🔒 <b>NetLink</b> — корпоративный сервис защищённого доступа.\n\n"
        "Для получения доступа необходимо принять условия использования.",
        reply_markup=agreement_start_kb(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "show_agreement")
async def show_agreement(callback: CallbackQuery):
    await callback.message.edit_text(
        AGREEMENT_TEXT,
        reply_markup=agreement_accept_kb(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "accept_agreement")
async def accept_agreement(callback: CallbackQuery, state: FSMContext):
    telegram_id = callback.from_user.id
    now = datetime.now().isoformat()
    await db.create_user(telegram_id, callback.from_user.username)
    await db.update_user(telegram_id, agreement_accepted_at=now)
    await callback.message.edit_text("✅ Условия приняты.\n\nВведите ваше ФИО (полностью):")
    await state.set_state(Registration.waiting_fio)
    await callback.answer()


@router.callback_query(F.data == "reject_agreement")
async def reject_agreement(callback: CallbackQuery):
    await callback.message.edit_text(
        "❌ Вы отклонили условия использования. Доступ не может быть предоставлен.\n\n"
        "Если передумаете — нажмите /start"
    )
    await callback.answer()


@router.message(Registration.waiting_fio)
async def process_fio(message: Message, state: FSMContext):
    fio = message.text.strip()
    if len(fio) < 3 or len(fio.split()) < 2:
        await message.answer("Пожалуйста, введите полное ФИО (минимум имя и фамилия):")
        return
    await state.update_data(fio=fio, selected_platforms=set())
    await db.update_user(message.from_user.id, fio=fio)
    await message.answer(
        "📱 На каких устройствах будете использовать?\n"
        "(выберите все нужные, затем нажмите «Готово»)",
        reply_markup=platforms_kb(),
    )
    await state.set_state(Registration.waiting_platforms)


@router.callback_query(F.data.startswith("platform_"), Registration.waiting_platforms)
async def toggle_platform(callback: CallbackQuery, state: FSMContext):
    platform = callback.data.split("_", 1)[1]
    data = await state.get_data()
    selected = data.get("selected_platforms", set())

    if platform in selected:
        selected.discard(platform)
    else:
        if len(selected) >= MAX_DEVICES:
            await callback.answer(
                f"⚠️ Максимум {MAX_DEVICES} устройства. Уберите одно.",
                show_alert=True,
            )
            return
        selected.add(platform)

    await state.update_data(selected_platforms=selected)
    await callback.message.edit_reply_markup(reply_markup=platforms_kb(selected))
    await callback.answer()


@router.callback_query(F.data == "platforms_done", Registration.waiting_platforms)
async def platforms_done(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = data.get("selected_platforms", set())
    if not selected:
        await callback.answer("Выберите хотя бы одну платформу", show_alert=True)
        return

    fio = data["fio"]
    platforms_list = sorted(selected)
    devices_count = len(platforms_list)
    platforms_json = json.dumps(platforms_list)

    telegram_id = callback.from_user.id
    username = callback.from_user.username

    await db.update_user(
        telegram_id,
        devices_count=devices_count,
        platforms=platforms_json,
    )

    request_id = await db.create_request(
        telegram_id, fio, devices_count, platforms_json
    )

    platforms_str = ", ".join(PLATFORM_NAMES.get(p, p) for p in platforms_list)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    username_str = f"@{username}" if username else "нет username"

    admin_text = (
        f"📋 <b>Новая заявка #{request_id}</b>\n\n"
        f"👤 ФИО: {fio}\n"
        f"📱 Telegram: {username_str} (ID: {telegram_id})\n"
        f"💻 Платформы: {platforms_str} ({devices_count} "
        f"{'устройство' if devices_count == 1 else 'устройства'})\n"
        f"🕐 Дата: {now}"
    )

    bot = callback.bot
    admin_msg = await bot.send_message(
        ADMIN_CHAT_ID,
        admin_text,
        reply_markup=approve_reject_kb(request_id),
        parse_mode="HTML",
    )
    await db.update_request(request_id, admin_message_id=admin_msg.message_id)

    await callback.message.edit_text(
        "✅ Заявка отправлена администратору.\n\nОжидайте одобрения — вам придёт уведомление."
    )
    await state.clear()
    await callback.answer()
