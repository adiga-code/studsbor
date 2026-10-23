"""Точка входа Telegram-бота: long polling + планировщик напоминаний."""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from app.bot.handlers import build_router
from app.bot.middlewares import DbSessionMiddleware
from app.config import get_settings
from app.db.session import get_sessionmaker
from app.logging_setup import setup_logging
from app.scheduler import build_scheduler
from app.services.fundraisers import ensure_general_fund

log = logging.getLogger(__name__)

COMMANDS = [
    BotCommand(command="menu", description="Главное меню"),
    BotCommand(command="help", description="Что умеет бот"),
    BotCommand(command="cancel", description="Отменить текущее действие"),
]


def create_bot() -> Bot:
    return Bot(
        get_settings().bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )


async def main() -> None:
    setup_logging("bot")
    settings = get_settings()
    if not settings.bot_token:
        raise SystemExit("Не задан BOT_TOKEN")

    async with get_sessionmaker()() as session:
        await ensure_general_fund(session)

    bot = create_bot()
    dp = Dispatcher()
    dp.update.outer_middleware(DbSessionMiddleware())
    dp.include_router(build_router())

    scheduler = build_scheduler(bot)
    scheduler.start()
    await bot.set_my_commands(COMMANDS)
    me = await bot.get_me()
    log.info("Бот @%s запущен, приём платежей: %s", me.username, settings.payments_enabled)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
