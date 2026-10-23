"""Бот с подменённой HTTP-сессией: запросы к Telegram записываются, а не отправляются."""

import itertools
import typing
from datetime import UTC, datetime

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from app.bot.handlers import build_router
from app.bot.middlewares import DbSessionMiddleware

_ids = itertools.count(1)


class RecordingSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod] = []

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        returning = method.__returning__
        if typing.get_origin(returning) in (typing.Union, getattr(__import__("types"), "UnionType", None)):
            return True
        if returning is Message:
            chat_id = getattr(method, "chat_id", 0) or 0
            return Message(
                message_id=next(_ids),
                date=datetime.now(UTC),
                chat=Chat(id=chat_id, type="private"),
                text=getattr(method, "text", None),
            )
        if returning is bool:
            return True
        if returning is User:
            return User(id=1, is_bot=True, first_name="bot", username="studsbor_test_bot")
        return True

    async def stream_content(self, *args, **kwargs):  # pragma: no cover
        raise NotImplementedError
        yield b""

    async def close(self) -> None:
        pass

    def texts(self) -> list[str]:
        return [m.text for m in self.requests if getattr(m, "text", None)]

    def last(self, method_name: str):
        for m in reversed(self.requests):
            if type(m).__name__ == method_name:
                return m
        raise AssertionError(f"{method_name} не вызывался")


def make_bot() -> tuple[Bot, RecordingSession]:
    session = RecordingSession()
    bot = Bot("123456:TEST", session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    return bot, session


_dispatcher: Dispatcher | None = None


def make_dispatcher() -> Dispatcher:
    """Один диспетчер на все тесты (роутеры подключаются однократно), но с чистым FSM."""
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = Dispatcher()
        _dispatcher.update.outer_middleware(DbSessionMiddleware())
        _dispatcher.include_router(build_router())
    _dispatcher.fsm.storage = MemoryStorage()
    return _dispatcher


def tg_user(user_id: int, name: str = "Анна") -> User:
    return User(id=user_id, is_bot=False, first_name=name, username=f"u{user_id}")


def message_update(user_id: int, text: str, name: str = "Анна") -> Update:
    return Update(
        update_id=next(_ids),
        message=Message(
            message_id=next(_ids),
            date=datetime.now(UTC),
            chat=Chat(id=user_id, type="private"),
            from_user=tg_user(user_id, name),
            text=text,
        ),
    )


def callback_update(user_id: int, data: str, name: str = "Анна") -> Update:
    return Update(
        update_id=next(_ids),
        callback_query=CallbackQuery(
            id=str(next(_ids)),
            from_user=tg_user(user_id, name),
            chat_instance="ci",
            data=data,
            message=Message(
                message_id=next(_ids),
                date=datetime.now(UTC),
                chat=Chat(id=user_id, type="private"),
                from_user=User(id=1, is_bot=True, first_name="bot"),
                text="…",
            ),
        ),
    )
