"""Фоновые задачи: напоминания, закрытие просроченных сборов, сверка платежей."""

import asyncio
import logging
from datetime import timedelta

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.bot import keyboards as kb
from app.bot import texts
from app.bot.sender import notify_payment_succeeded, send_and_log
from app.config import get_settings
from app.db.base import utcnow
from app.db.session import get_sessionmaker
from app.money import format_rub
from app.services import notifications as n
from app.services import stats
from app.services.fundraisers import close_expired, list_fundraisers
from app.services.payments import pending_payments, refresh_payment
from app.services.yookassa import YooKassaError

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
    if not settings.payments_enabled:
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


async def close_expired_fundraisers() -> None:
    async with get_sessionmaker()() as session:
        closed = await close_expired(session)
        if closed:
            log.info("Закрыто просроченных сборов: %s", closed)


async def sync_pending_payments(bot: Bot) -> None:
    """Сверка на случай потерянных уведомлений ЮKassa."""
    if not get_settings().payments_enabled:
        return
    async with get_sessionmaker()() as session:
        for payment in await pending_payments(session, older_than=timedelta(minutes=5)):
            try:
                result = await refresh_payment(session, payment)
            except YooKassaError as exc:
                log.warning("Сверка платежа %s не удалась: %s", payment.id, exc)
                continue
            if result and result.newly_succeeded:
                log.info("Платёж %s подтверждён при сверке", payment.id)
                await notify_payment_succeeded(bot, session, result.payment)


def build_scheduler(bot: Bot) -> AsyncIOScheduler:
    settings = get_settings()
    scheduler = AsyncIOScheduler(timezone=settings.tz)
    scheduler.add_job(
        send_reminders, "cron", hour=settings.reminder_hour, minute=0, args=[bot],
        id="reminders", max_instances=1, coalesce=True, misfire_grace_time=3600,
    )
    scheduler.add_job(
        close_expired_fundraisers, "cron", hour=0, minute=5,
        id="close_expired", max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        sync_pending_payments, "interval", minutes=settings.pending_sync_minutes, args=[bot],
        id="sync_pending", max_instances=1, coalesce=True,
    )
    return scheduler
