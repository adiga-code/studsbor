from functools import cache

from aiogram import F, Router

from app.bot.handlers import admin, common, donate, menu, public, settings


@cache
def build_router() -> Router:
    # Роутеры — модульные синглтоны и подключаются только один раз.
    root = Router(name="root")
    # Бот работает только в личных сообщениях.
    root.message.filter(F.chat.type == "private")
    root.include_routers(
        common.router,
        menu.router,
        admin.router,
        donate.router,
        public.router,
        settings.router,
    )
    return root
