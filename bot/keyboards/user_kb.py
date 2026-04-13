from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def agreement_start_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Ознакомиться с условиями", callback_data="show_agreement")]
    ])


def agreement_accept_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Принимаю условия", callback_data="accept_agreement")],
        [InlineKeyboardButton(text="❌ Отклоняю", callback_data="reject_agreement")],
    ])


def devices_count_kb() -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(text=f"📱 {n}", callback_data=f"devices_{n}")
        for n in range(1, 6)
    ]
    return InlineKeyboardMarkup(inline_keyboard=[buttons])


def platforms_kb(selected: set[str] | None = None) -> InlineKeyboardMarkup:
    selected = selected or set()
    platforms = [
        ("iPhone", "iphone"),
        ("Android", "android"),
        ("Windows", "windows"),
        ("macOS", "macos"),
    ]
    rows = []
    for label, key in platforms:
        check = "✅ " if key in selected else ""
        rows.append([InlineKeyboardButton(
            text=f"{check}{label}", callback_data=f"platform_{key}"
        )])
    rows.append([InlineKeyboardButton(text="✅ Готово", callback_data="platforms_done")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Моя ссылка", callback_data="my_link")],
        [InlineKeyboardButton(text="📖 Инструкция", callback_data="instruction")],
        [InlineKeyboardButton(text="💬 Задать вопрос", callback_data="ask_question")],
        [InlineKeyboardButton(text="⚙️ Мои устройства", callback_data="my_devices")],
    ])


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
