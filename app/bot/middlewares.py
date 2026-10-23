from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from aiogram.types import User as TgUser

from app.config import get_settings
from app.db.session import get_sessionmaker
from app.services.users import get_or_create_user, get_role


class DbSessionMiddleware(BaseMiddleware):
    """Открывает сессию БД на каждое обновление и подставляет пользователя и его роль.

    Роль берётся из таблицы admins по Telegram ID — не по username и не по чату.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        async with get_sessionmaker()() as session:
            data["session"] = session
            tg_user: TgUser | None = data.get("event_from_user")
            if tg_user is not None and not tg_user.is_bot:
                user = await get_or_create_user(
                    session,
                    tg_user.id,
                    tg_user.username,
                    tg_user.first_name,
                    get_settings().owner_telegram_ids,
                )
                data["db_user"] = user
                data["role"] = await get_role(session, user)
            return await handler(event, data)
