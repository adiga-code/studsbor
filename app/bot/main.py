"""Точка входа Telegram-бота: long polling + планировщик напоминаний."""

import asyncio
import logging

from app.bot.dispatcher import COMMANDS, create_bot, create_dispatcher
from app.config import get_settings
from app.db.session import get_sessionmaker
from app.logging_setup import setup_logging
from app.scheduler import build_scheduler
from app.services.fundraisers import ensure_general_fund
from app.services.payments import payments_available

log = logging.getLogger(__name__)


async def main() -> None:
    setup_logging("bot")
    settings = get_settings()
    if not settings.bot_token:
        raise SystemExit("Не задан BOT_TOKEN")

    async with get_sessionmaker()() as session:
        await ensure_general_fund(session)

    bot = create_bot()
    dp = create_dispatcher()

    scheduler = build_scheduler(bot)
    scheduler.start()
    await bot.set_my_commands(COMMANDS)
    me = await bot.get_me()
    log.info("Бот @%s запущен, приём платежей: %s", me.username, payments_available())
    # Если раньше бот работал через вебхук (Vercel), polling без этого не стартует.
    await bot.delete_webhook()
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
