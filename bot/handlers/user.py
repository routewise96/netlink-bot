"""Approved user handlers: link, instruction, devices, questions."""
import json

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import CallbackQuery, Message

from bot.db import queries as db
from bot.services import deepseek
from bot.keyboards.user_kb import main_menu_kb, back_to_menu_kb, link_and_back_kb
from bot.config import ADMIN_CHAT_ID

router = Router()


class AskQuestion(StatesGroup):
    waiting_question = State()


INSTRUCTIONS = {
    "iphone": """📱 <b>Установка на iPhone (Streisand)</b>

1. Скачайте приложение <b>Streisand</b> из App Store
2. Откройте приложение
3. Нажмите "+" в правом верхнем углу
4. Выберите "Импорт из буфера обмена"
5. Скопируйте вашу ссылку (кнопка "🔗 Моя ссылка" выше)
6. Вернитесь в Streisand — профиль импортируется автоматически
7. Нажмите на переключатель вверху для подключения

✅ Готово! Интернет работает через защищённое соединение.""",

    "android": """📱 <b>Установка на Android (Streisand)</b>

1. Скачайте приложение <b>Streisand</b> из Google Play
2. Откройте приложение
3. Нажмите "+" в правом верхнем углу
4. Выберите "Импорт из буфера обмена"
5. Скопируйте вашу ссылку (кнопка "🔗 Моя ссылка" выше)
6. Вернитесь в Streisand — профиль импортируется автоматически
7. Нажмите на переключатель вверху для подключения

✅ Готово!""",

    "windows": """💻 <b>Установка на Windows (Hiddify)</b>

1. Скачайте Hiddify: https://hiddify.com
2. Установите и запустите
3. Нажмите "+" → "Добавить из буфера обмена"
4. Скопируйте вашу ссылку (кнопка "🔗 Моя ссылка" выше)
5. Вставьте — профиль добавится
6. Нажмите "Подключить"

⚠️ В настройках Hiddify отключите "Блокировать рекламу" — иначе не будут работать сервисы Яндекса.

✅ Готово!""",

    "macos": """🖥 <b>Установка на macOS (Streisand)</b>

1. Откройте App Store → найдите "Streisand"
   (приложение для iPad, но работает на Mac с Apple Silicon)
2. Скачайте и откройте
3. Нажмите "+" → "Импорт из буфера обмена"
4. Скопируйте вашу ссылку (кнопка "🔗 Моя ссылка" выше)
5. Профиль импортируется → включите переключатель

✅ Готово!""",
}


@router.callback_query(F.data == "back_menu")
async def back_to_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(
        "🔒 <b>NetLink</b> — защищённый доступ\n\nВыберите действие:",
        reply_markup=main_menu_kb(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "my_link")
async def show_link(callback: CallbackQuery):
    user = await db.get_user(callback.from_user.id)
    if not user or user["status"] != "approved" or not user["vless_link"]:
        await callback.answer("Доступ не активен", show_alert=True)
        return

    await callback.message.edit_text(
        f"🔗 <b>Ваша персональная ссылка:</b>\n\n"
        f"<code>{user['vless_link']}</code>\n\n"
        f"Нажмите на ссылку чтобы скопировать.",
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

    full_text = "\n\n" + "═" * 30 + "\n\n".join(texts)
    if len(full_text) > 4000:
        for text in texts:
            await callback.message.answer(text, parse_mode="HTML")
        await callback.message.answer(
            "⬆️ Инструкции для ваших платформ выше",
            reply_markup=back_to_menu_kb(),
        )
    else:
        await callback.message.edit_text(
            "\n\n".join(texts),
            reply_markup=back_to_menu_kb(),
            parse_mode="HTML",
        )
    await callback.answer()


@router.callback_query(F.data == "my_devices")
async def show_devices(callback: CallbackQuery):
    user = await db.get_user(callback.from_user.id)
    if not user or user["status"] != "approved":
        await callback.answer("Доступ не активен", show_alert=True)
        return

    platforms = json.loads(user["platforms"] or "[]")
    platform_names = {"iphone": "iPhone", "android": "Android", "windows": "Windows", "macos": "macOS"}
    platforms_str = ", ".join(platform_names.get(p, p) for p in platforms)
    approved_date = (user["approved_at"] or "")[:10]

    await callback.message.edit_text(
        f"📊 <b>Ваш профиль</b>\n\n"
        f"👤 {user['fio']}\n"
        f"📱 Устройств: {user['devices_count']}\n"
        f"💻 Платформы: {platforms_str}\n"
        f"📅 Доступ с: {approved_date}",
        reply_markup=back_to_menu_kb(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "ask_question")
async def start_question(callback: CallbackQuery, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    if not user or user["status"] != "approved":
        await callback.answer("Доступ не активен", show_alert=True)
        return

    await callback.message.edit_text(
        "💬 Задайте ваш вопрос о работе сервиса. Я постараюсь помочь!",
        reply_markup=back_to_menu_kb(),
        parse_mode="HTML",
    )
    await state.set_state(AskQuestion.waiting_question)
    await callback.answer()


@router.message(AskQuestion.waiting_question)
async def process_question(message: Message, state: FSMContext):
    question = message.text.strip()
    if not question:
        return

    user = await db.get_user(message.from_user.id)
    if not user or user["status"] != "approved":
        await state.clear()
        return

    ai_response = await deepseek.ask(question)

    if ai_response:
        await db.save_ai_conversation(message.from_user.id, question, ai_response)
        await message.answer(
            ai_response + "\n\nЗадайте ещё вопрос или вернитесь в меню.",
            reply_markup=back_to_menu_kb(),
            parse_mode="HTML",
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
