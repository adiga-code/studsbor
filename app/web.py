"""HTTP-вход для бессерверного режима (Vercel).

* POST /api/telegram    — вебхук Telegram (проверяется секрет в заголовке);
* GET  /api/cron/daily  — ежедневные задачи, вызывается Vercel Cron;
* GET  /api/setup?key=… — применить миграции и зарегистрировать вебхук;
* GET  /api/health      — проверка базы.
"""

import asyncio
import hmac
import logging
import os
import time
from functools import cache
from pathlib import Path

from aiogram import Dispatcher
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Update
from fastapi import FastAPI, HTTPException, Request
from sqlalchemy import text

from app.bot.dispatcher import COMMANDS, create_bot, create_dispatcher
from app.config import get_settings
from app.db.session import get_sessionmaker
from app.jobs import nightly_cleanup, send_reminders
from app.logging_setup import setup_logging
from app.services.fundraisers import ensure_general_fund
from app.services.payments import payments_available

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent.parent
# Лимит длительности функции на бесплатном тарифе Vercel — 300 с; оставляем запас.
REMINDER_TIME_BUDGET = 200
# Сколько вебхуков Telegram шлёт одновременно: бережём лимит соединений бесплатной базы.
WEBHOOK_MAX_CONNECTIONS = 5

STARTED_AT = time.monotonic()
setup_logging("web")
app = FastAPI(title="Фонд студсовета", docs_url=None, redoc_url=None, openapi_url=None)


@cache
def dispatcher() -> Dispatcher:
    return create_dispatcher()


def _secret_matches(given: str | None, expected: str) -> bool:
    return bool(expected) and hmac.compare_digest((given or "").encode(), expected.encode())


def _public_host(request: Request) -> str:
    return (
        os.environ.get("VERCEL_PROJECT_PRODUCTION_URL")
        or request.headers.get("x-forwarded-host")
        or request.headers.get("host", "")
    )


@app.get("/")
async def index() -> dict:
    return {"service": "Фонд студсовета", "status": "ok"}


@app.get("/api/health")
async def health(telegram: bool = False) -> dict:
    """Проверка и диагностика задержек: /api/health?telegram=1 — ещё и замер до Telegram."""
    timings: dict[str, float] = {}
    async with get_sessionmaker()() as session:
        for name in ("db_first_query_ms", "db_query_ms"):
            started = time.perf_counter()
            await session.execute(text("SELECT 1"))
            timings[name] = round((time.perf_counter() - started) * 1000, 1)
    if telegram:
        bot = create_bot()
        try:
            started = time.perf_counter()
            await bot.get_me()
            timings["telegram_ms"] = round((time.perf_counter() - started) * 1000, 1)
        finally:
            await bot.session.close()
    return {
        "status": "ok",
        "region": os.environ.get("VERCEL_REGION", "local"),
        "instance_age_s": round(time.monotonic() - STARTED_AT),
        **timings,
    }


@app.post("/api/telegram")
async def telegram_webhook(request: Request) -> dict:
    settings = get_settings()
    if not _secret_matches(request.headers.get("X-Telegram-Bot-Api-Secret-Token"), settings.webhook_secret):
        raise HTTPException(status_code=403)
    bot = create_bot()
    try:
        update = Update.model_validate(await request.json(), context={"bot": bot})
        started = time.perf_counter()
        try:
            await dispatcher().feed_update(bot, update)
            log.info("Обновление %s обработано за %.0f мс", update.update_id, (time.perf_counter() - started) * 1000)
        except Exception as exc:
            log.exception("Ошибка обработки обновления %s", update.update_id)
            # Подтверждение оплаты нельзя потерять: ошибка → Telegram пришлёт его повторно
            # (учёт оплаты идемпотентен). Остальные обновления не повторяем, чтобы не
            # дублировать сообщения.
            if update.message and update.message.successful_payment:
                raise HTTPException(status_code=500) from exc
    finally:
        await bot.session.close()
    return {"ok": True}


@app.get("/api/cron/daily")
async def cron_daily(request: Request) -> dict:
    if not _secret_matches(request.headers.get("authorization"), f"Bearer {get_settings().cron_secret}"):
        raise HTTPException(status_code=401)
    await nightly_cleanup()
    bot = create_bot()
    try:
        await send_reminders(bot, time_budget=REMINDER_TIME_BUDGET)
    finally:
        await bot.session.close()
    return {"ok": True}


def _run_migrations() -> None:
    from alembic import command
    from alembic.config import Config

    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.attributes["skip_logging_config"] = True
    command.upgrade(config, "head")


@app.get("/api/setup")
async def setup(request: Request, key: str = "") -> dict:
    """Одноразовая настройка: открыть в браузере https://<проект>.vercel.app/api/setup?key=<WEBHOOK_SECRET>."""
    settings = get_settings()
    if not _secret_matches(key, settings.webhook_secret):
        raise HTTPException(status_code=403, detail="Неверный key (должен совпадать с WEBHOOK_SECRET)")
    if not settings.bot_token:
        raise HTTPException(status_code=500, detail="Не задан BOT_TOKEN")

    result: dict = {}
    try:
        # env.py миграций сам запускает asyncio.run — поэтому в отдельном потоке.
        await asyncio.to_thread(_run_migrations)
        result["migrations"] = "ok"
    except Exception as exc:
        log.exception("Миграции не применились")
        result["migrations"] = f"ошибка: {exc}. Выполните supabase/schema.sql в SQL Editor Supabase."

    async with get_sessionmaker()() as session:
        await ensure_general_fund(session)

    url = f"https://{_public_host(request)}/api/telegram"
    bot = create_bot()
    try:
        await bot.set_webhook(
            url,
            secret_token=settings.webhook_secret,
            allowed_updates=dispatcher().resolve_used_update_types(),
            max_connections=WEBHOOK_MAX_CONNECTIONS,
        )
        await bot.set_my_commands(COMMANDS)
        me = await bot.get_me()
    except TelegramAPIError as exc:
        log.exception("Не удалось зарегистрировать вебхук")
        raise HTTPException(status_code=502, detail=f"Telegram отклонил запрос: {exc}. Проверьте BOT_TOKEN.") from exc
    finally:
        await bot.session.close()
    result.update(
        bot=f"@{me.username}",
        webhook=url,
        payments_enabled=payments_available(),
        status="Готово: напишите боту /start",
    )
    return result
