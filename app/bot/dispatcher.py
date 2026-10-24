from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from app.bot.handlers import build_router
from app.bot.middlewares import DbSessionMiddleware
from app.bot.storage import SqlStorage
from app.config import get_settings

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


def create_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=SqlStorage())
    dp.update.outer_middleware(DbSessionMiddleware())
    dp.include_router(build_router())
    return dp
