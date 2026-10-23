from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import UTCDateTime
from app.db.models import NotificationLog, User

REMINDER = "reminder"
BROADCAST = "broadcast"
PAYMENT_SUCCESS = "payment_success"

STATUS_SENT = "sent"
STATUS_FAILED = "failed"
STATUS_BLOCKED = "blocked"


def _month_index(moment: datetime, tz: ZoneInfo) -> int:
    local = moment.astimezone(tz)
    return local.year * 12 + local.month


async def users_due_for_reminder(
    session: AsyncSession, now: datetime, tz: ZoneInfo, reminder_day: int = 1
) -> list[User]:
    """Пользователи, которым пора отправить напоминание.

    Напоминание уходит не раньше reminder_day-го числа и не чаще, чем раз в
    reminder_period_months календарных месяцев (считая по часовому поясу tz).
    """
    if now.astimezone(tz).day < reminder_day:
        return []
    last_sent = (
        select(NotificationLog.user_id, func.max(NotificationLog.sent_at, type_=UTCDateTime()).label("last"))
        .where(NotificationLog.notification_type == REMINDER, NotificationLog.status == STATUS_SENT)
        .group_by(NotificationLog.user_id)
        .subquery()
    )
    rows = await session.execute(
        select(User, last_sent.c.last)
        .outerjoin(last_sent, last_sent.c.user_id == User.id)
        .where(User.reminders_enabled.is_(True), User.is_blocked.is_(False))
        .order_by(User.id)
    )
    current = _month_index(now, tz)
    due = []
    for user, last in rows.all():
        if last is None:
            # Новичкам не напоминаем в месяц регистрации — они только что видели бота.
            if _month_index(user.created_at, tz) < current:
                due.append(user)
            continue
        if current - _month_index(last, tz) >= max(user.reminder_period_months, 1):
            due.append(user)
    return due


async def log_notification(
    session: AsyncSession, user: User, notification_type: str, status: str, error: str | None = None
) -> None:
    session.add(
        NotificationLog(user_id=user.id, notification_type=notification_type, status=status, error=error)
    )
    if status == STATUS_BLOCKED:
        user.is_blocked = True
    await session.commit()


async def broadcast_recipients(session: AsyncSession) -> list[User]:
    return list(await session.scalars(select(User).where(User.is_blocked.is_(False)).order_by(User.id)))
