"""Фоновые задачи: напоминания, закрытие просроченных сборов и устаревших счетов."""

import asyncio
import logging

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.bot import keyboards as kb
from app.bot import texts
from app.bot.sender import send_and_log
from app.config import get_settings
from app.db.base import utcnow
from app.db.session import get_sessionmaker
from app.money import format_rub
from app.services import notifications as n
from app.services import stats
from app.services.fundraisers import close_expired, list_fundraisers
from app.services.payments import expire_stale_invoices, payments_available

log = logging.getLogger(__name__)


async def reminder_text(session) -> str:
    funds = await list_fundraisers(session, only_open=True)
    collected = await stats.collected_by_fundraiser(session)
    lines = [
        "👋 <b>Фонд студсовета</b>",
        "",
        "Напоминаем: можно поддержать общий фонд или активные проекты — любой суммой.",
    ]
    if funds:
        lines.append("")
        for f in funds[:5]:
            line = f"• {texts.esc(f.title)} — {format_rub(collected.get(f.id, 0))}"
            if f.goal_amount:
                line += f" из {format_rub(f.goal_amount)}"
            lines.append(line)
    lines += ["", "Частоту напоминаний можно изменить в «⚙️ Настройки»."]
    return "\n".join(lines)


async def send_reminders(bot: Bot) -> None:
    settings = get_settings()
    if not payments_available():
        log.info("Приём платежей выключен — напоминания не отправляются")
        return
    async with get_sessionmaker()() as session:
        users = await n.users_due_for_reminder(session, utcnow(), settings.tz, settings.reminder_day)
        if not users:
            return
        text = await reminder_text(session)
        sent = 0
        for user in users:
            status = await send_and_log(
                bot, session, user, n.REMINDER, text, reply_markup=kb.reminder_actions()
            )
            sent += status == n.STATUS_SENT
            await asyncio.sleep(0.05)
        log.info("Напоминания: отправлено %s из %s", sent, len(users))


async def nightly_cleanup() -> None:
    """Закрывает просроченные сборы и помечает устаревшие неоплаченные счета."""
    async with get_sessionmaker()() as session:
        closed = await close_expired(session)
        expired = await expire_stale_invoices(session)
    if closed or expired:
        log.info("Закрыто сборов: %s, устаревших счетов: %s", closed, expired)


def build_scheduler(bot: Bot) -> AsyncIOScheduler:
    settings = get_settings()
    scheduler = AsyncIOScheduler(timezone=settings.tz)
    scheduler.add_job(
        send_reminders, "cron", hour=settings.reminder_hour, minute=0, args=[bot],
        id="reminders", max_instances=1, coalesce=True, misfire_grace_time=3600,
    )
    scheduler.add_job(
        nightly_cleanup, "cron", hour=0, minute=5,
        id="nightly_cleanup", max_instances=1, coalesce=True,
    )
    return scheduler
