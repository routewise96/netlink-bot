from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def approve_reject_kb(request_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve_{request_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{request_id}"),
        ]
    ])


def admin_panel_kb(pending_count: int = 0) -> InlineKeyboardMarkup:
    pending_label = f"📋 Заявки ({pending_count})" if pending_count else "📋 Заявки"
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=pending_label, callback_data="admin_requests"),
            InlineKeyboardButton(text="👥 Пользователи", callback_data="admin_users"),
        ],
        [
            InlineKeyboardButton(text="🚫 Заблокированные", callback_data="admin_blocked"),
            InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats"),
        ],
        [
            InlineKeyboardButton(text="🧪 Тест-режим", callback_data="admin_test_mode"),
        ],
    ])


def user_detail_kb(telegram_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔴 Заблокировать", callback_data=f"block_{telegram_id}")],
        [InlineKeyboardButton(text="🔗 Его ссылка", callback_data=f"userlink_{telegram_id}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_users")],
    ])


def unblock_kb(telegram_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 Разблокировать", callback_data=f"unblock_{telegram_id}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_blocked")],
    ])


def back_to_admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад в админ-панель", callback_data="admin_panel")]
    ])
