from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from bot.services.netlink_v2 import settings as v2_settings


def agreement_start_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Ознакомиться с условиями", callback_data="show_agreement")]
    ])


def agreement_accept_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Принимаю условия", callback_data="accept_agreement")],
        [InlineKeyboardButton(text="❌ Отклоняю", callback_data="reject_agreement")],
    ])


def platforms_kb(selected: set[str] | None = None) -> InlineKeyboardMarkup:
    selected = selected or set()
    platforms = [
        ("📱 iPhone", "iphone"),
        ("📱 Android", "android"),
        ("💻 Windows", "windows"),
        ("🖥 macOS", "macos"),
    ]
    rows = []
    for label, key in platforms:
        check = "✅ " if key in selected else ""
        rows.append([InlineKeyboardButton(
            text=f"{check}{label}", callback_data=f"platform_{key}"
        )])
    rows.append([InlineKeyboardButton(text="✅ Готово", callback_data="platforms_done")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def main_menu_kb(telegram_id: int | None = None) -> InlineKeyboardMarkup:
    """Main user menu.

    When the NetLink 2.0 pilot is enabled AND the user is in the pilot
    allowlist, an extra v2 enrollment action row is appended. With the flag
    off (default) the keyboard is byte-for-byte the legacy one.
    """
    rows = [
        [InlineKeyboardButton(text="🔗 Моя ссылка", callback_data="my_link")],
        [InlineKeyboardButton(text="📖 Инструкция", callback_data="instruction")],
        [InlineKeyboardButton(text="💬 Задать вопрос", callback_data="ask_question")],
        [InlineKeyboardButton(text="⚙️ Мои устройства", callback_data="my_devices")],
        [InlineKeyboardButton(text="📱 Добавить устройство", callback_data="add_device")],
        [InlineKeyboardButton(text="🔄 Сменить платформу", callback_data="change_platform")],
        [InlineKeyboardButton(text="🔄 Начать заново", callback_data="user_reset")],
    ]
    if telegram_id is not None and v2_settings.is_pilot_user(telegram_id):
        rows.append(
            [InlineKeyboardButton(text="🚀 Подключить NetLink 2.0", callback_data="v2_enroll")]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def add_device_platforms_kb(available: list[str]) -> InlineKeyboardMarkup:
    """Keyboard with only the platforms the user doesn't already own."""
    labels = {
        "iphone": "📱 iPhone",
        "android": "📱 Android",
        "windows": "💻 Windows",
        "macos": "🖥 macOS",
    }
    rows = [
        [InlineKeyboardButton(text=labels[p], callback_data=f"adddev_{p}")]
        for p in available
    ]
    rows.append([InlineKeyboardButton(text="🔙 Назад в меню", callback_data="back_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_to_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад в меню", callback_data="back_menu")]
    ])


def link_and_back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📖 Инструкция", callback_data="instruction")],
        [InlineKeyboardButton(text="💬 Задать вопрос", callback_data="ask_question")],
        [InlineKeyboardButton(text="🔙 Назад в меню", callback_data="back_menu")],
    ])
