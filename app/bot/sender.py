"""Отправка сообщений с учётом блокировок и лимитов Telegram."""

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.db.models import Payment, Refund, User
from app.money import format_rub
from app.services import notifications as n

log = logging.getLogger(__name__)


async def safe_send(bot: Bot, chat_id: int, text: str, **kwargs) -> tuple[str, str | None]:
    """Возвращает (статус, ошибка); статусы — из app.services.notifications."""
    for _ in range(2):
        try:
            await bot.send_message(chat_id, text, **kwargs)
            return n.STATUS_SENT, None
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after + 1)
        except TelegramForbiddenError as exc:
            return n.STATUS_BLOCKED, str(exc)
        except TelegramAPIError as exc:
            log.warning("Не удалось отправить сообщение %s: %s", chat_id, exc)
            return n.STATUS_FAILED, str(exc)
    return n.STATUS_FAILED, "retry_after"


async def send_and_log(
    bot: Bot, session: AsyncSession, user: User, notification_type: str, text: str, **kwargs
) -> str:
    status, error = await safe_send(bot, user.telegram_id, text, **kwargs)
    await n.log_notification(session, user, notification_type, status, error)
    return status


def payment_success_text(payment: Payment) -> str:
    return (
        f"✅ <b>Спасибо!</b> Взнос {format_rub(payment.amount)} в «{texts.esc(payment.fundraiser.title)}» "
        "получен и учтён в статистике."
    )


async def notify_payment_succeeded(bot: Bot, session: AsyncSession, payment: Payment) -> None:
    await send_and_log(bot, session, payment.user, n.PAYMENT_SUCCESS, payment_success_text(payment))


async def notify_refund_succeeded(bot: Bot, session: AsyncSession, refund: Refund) -> None:
    payment = await session.get(Payment, refund.payment_id)
    if payment is None:
        return
    text = (
        f"↩️ Оформлен возврат {format_rub(refund.amount)} по взносу от {texts.fmt_date(payment.paid_at)} "
        f"в «{texts.esc(payment.fundraiser.title)}»."
    )
    await send_and_log(bot, session, payment.user, "refund", text)
