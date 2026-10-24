"""Планировщик для режима polling (Docker/VPS)."""

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import get_settings
from app.jobs import nightly_cleanup, send_reminders


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
