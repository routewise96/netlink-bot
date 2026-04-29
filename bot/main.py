"""NetLink Telegram Bot — entry point."""
import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, MenuButtonCommands

from bot.config import ADMIN_CHAT_ID, BOT_TOKEN, RESERVED_EMAILS
from bot.db.models import init_db
from bot.handlers import start, user, admin
from bot.services.proxy import check_reserved_emails

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def _verify_reserved_emails(bot: Bot) -> None:
    """Verify family-reserved emails are intact in x-ui; alert admin if not."""
    try:
        ok, problems = check_reserved_emails()
    except Exception as e:
        logger.error(f"Reserved emails check failed: {e}")
        return
    if problems:
        msg = (
            "⚠️ <b>Reserved family emails check FAILED</b>\n\n"
            + "\n".join(f"• {p}" for p in problems)
            + "\n\nЭти UUID могут быть выданы новым юзерам. Проверь "
            "<code>RESERVED_EMAILS</code> в bot/config.py и пул в x-ui."
        )
        logger.warning(msg.replace("\n", " | "))
        try:
            await bot.send_message(ADMIN_CHAT_ID, msg, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Failed to alert admin about reserved emails: {e}")
    else:
        logger.info(
            f"Reserved family emails: {len(ok)}/{len(RESERVED_EMAILS)} OK in x-ui pool"
        )


async def main():
    logger.info("Starting NetLink bot...")

    await init_db()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())

    dp.include_router(start.router)
    dp.include_router(user.router)
    dp.include_router(admin.router)

    await bot.set_my_commands([
        BotCommand(command="start", description="Главное меню"),
    ])
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())

    await _verify_reserved_emails(bot)

    logger.info("Bot is running")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
