"""Платежи через Telegram Payments (провайдер ЮKassa, подключённый в @BotFather).

Схема:
1. Бот создаёт запись payments (статус new) с уникальным payload и выставляет счёт.
2. Перед списанием Telegram присылает pre_checkout_query — проверяем, что счёт
   действующий, сбор открыт, сумма и плательщик совпадают. Иначе отказываем.
3. После оплаты приходит successful_payment — только он означает, что деньги
   получены. Событие регистрируется в payment_events по telegram_payment_charge_id,
   повтор не меняет данные.
"""

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.base import utcnow
from app.db.models import Fundraiser, Payment, PaymentEvent, PaymentStatus, Refund, RefundStatus, User
from app.money import format_rub, to_api_value

log = logging.getLogger(__name__)

CURRENCY = "RUB"


def _for_update(stmt):
    """SELECT … FOR UPDATE с перечитыванием объекта, даже если он уже есть в сессии."""
    return stmt.with_for_update(of=Payment).execution_options(populate_existing=True)


class PaymentError(Exception):
    """Ошибка, текст которой можно показать пользователю."""


@dataclass
class PaymentResult:
    payment: Payment
    newly_succeeded: bool = False
    duplicate: bool = False


@dataclass
class SuccessfulPayment:
    """Поля Telegram SuccessfulPayment, нужные для учёта (без зависимости от aiogram)."""

    currency: str
    total_amount: int
    invoice_payload: str
    telegram_payment_charge_id: str
    provider_payment_charge_id: str


def validate_amount(amount: int) -> None:
    settings = get_settings()
    if amount < settings.min_amount_rub * 100:
        raise PaymentError(f"Минимальная сумма — {format_rub(settings.min_amount_rub * 100)}.")
    if amount > settings.max_amount_rub * 100:
        raise PaymentError(f"Максимальная сумма — {format_rub(settings.max_amount_rub * 100)}.")


def payments_available() -> bool:
    settings = get_settings()
    return settings.payments_enabled and bool(settings.payment_provider_token)


def payment_description(fundraiser: Fundraiser) -> str:
    return f"Добровольный взнос: {fundraiser.title}"


async def create_invoice(session: AsyncSession, user: User, fundraiser_id: int, amount: int) -> Payment:
    """Создаёт запись платежа, под которую бот выставит счёт."""
    if not payments_available():
        raise PaymentError("Приём платежей пока не подключён.")
    validate_amount(amount)
    fundraiser = await session.get(Fundraiser, fundraiser_id)
    if fundraiser is None or not fundraiser.is_open:
        raise PaymentError("Этот сбор закрыт.")
    payment = Payment(
        user=user,
        fundraiser=fundraiser,
        amount=amount,
        currency=CURRENCY,
        invoice_payload=uuid.uuid4().hex,
        status=PaymentStatus.NEW,
        description=payment_description(fundraiser)[:255],
    )
    session.add(payment)
    await session.commit()
    return payment


def invoice_params(payment: Payment) -> dict[str, Any]:
    """Параметры sendInvoice (без chat_id)."""
    settings = get_settings()
    fundraiser = payment.fundraiser
    title = fundraiser.title if len(fundraiser.title) <= 32 else fundraiser.title[:31] + "…"
    description = (
        f"Добровольный взнос, не оплата услуг. Получатель: {settings.recipient_name}. "
        f"Учитывается в статистике сразу после оплаты."
    )
    params: dict[str, Any] = {
        "title": title,
        "description": description[:255],
        "payload": payment.invoice_payload,
        "provider_token": settings.payment_provider_token,
        "currency": CURRENCY,
        "prices": [{"label": "Взнос", "amount": payment.amount}],
    }
    if settings.receipt_enabled:
        params["need_email"] = True
        params["send_email_to_provider"] = True
        params["provider_data"] = json.dumps(
            {
                "receipt": {
                    "items": [
                        {
                            "description": payment.description[:128],
                            "quantity": "1.00",
                            "amount": {"value": to_api_value(payment.amount), "currency": CURRENCY},
                            "vat_code": settings.receipt_vat_code,
                            "payment_mode": settings.receipt_payment_mode,
                            "payment_subject": settings.receipt_payment_subject,
                        }
                    ]
                }
            },
            ensure_ascii=False,
        )
    return params


async def mark_failed(session: AsyncSession, payment: Payment) -> None:
    payment.status = PaymentStatus.FAILED
    await session.commit()


async def check_pre_checkout(
    session: AsyncSession, telegram_id: int, payload: str, currency: str, total_amount: int
) -> str | None:
    """Проверка перед списанием. Возвращает текст отказа или None, если оплату можно принять."""
    payment = await session.scalar(select(Payment).where(Payment.invoice_payload == payload))
    if payment is None or payment.user.telegram_id != telegram_id:
        log.warning("pre_checkout: неизвестный счёт %s от %s", payload, telegram_id)
        return "Счёт не найден. Сформируйте новый в боте."
    if payment.status != PaymentStatus.NEW:
        return "Этот счёт уже оплачен или устарел. Сформируйте новый в боте."
    if utcnow() - payment.created_at > timedelta(hours=get_settings().invoice_ttl_hours):
        return "Счёт устарел. Сформируйте новый в боте."
    if currency != payment.currency or total_amount != payment.amount:
        log.error("pre_checkout: сумма %s %s не совпадает со счётом %s", total_amount, currency, payment.id)
        return "Сумма не совпадает со счётом. Сформируйте новый в боте."
    if not payment.fundraiser.is_open:
        return "Этот сбор уже закрыт."
    if not payments_available():
        return "Приём платежей временно остановлен."
    return None


async def _register_event(session: AsyncSession, key: str, event_type: str, payment_id: int | None, payload) -> bool:
    """Возвращает False, если событие уже было обработано."""
    exists = await session.scalar(select(PaymentEvent.id).where(PaymentEvent.event_key == key))
    if exists:
        return False
    session.add(
        PaymentEvent(
            event_key=key,
            event_type=event_type,
            payment_id=payment_id,
            payload=json.dumps(payload, ensure_ascii=False, default=str)[:10000],
        )
    )
    return True


async def apply_successful_payment(
    session: AsyncSession, telegram_id: int, sp: SuccessfulPayment
) -> PaymentResult | None:
    """Учитывает подтверждённую Telegram оплату ровно один раз."""
    payment = await session.scalar(_for_update(select(Payment).where(Payment.invoice_payload == sp.invoice_payload)))
    if payment is None:
        # Деньги списаны, а счёта нет (например, база восстановлена из старой копии).
        log.critical("Оплата %s по неизвестному счёту %s от %s", sp.telegram_payment_charge_id,
                     sp.invoice_payload, telegram_id)
        await session.commit()
        return None

    key = f"tg:{sp.telegram_payment_charge_id}"
    if not await _register_event(session, key, "successful_payment", payment.id, sp.__dict__):
        # commit, а не rollback: rollback сбросил бы загруженные объекты сессии.
        await session.commit()
        return PaymentResult(payment, duplicate=True)

    result = PaymentResult(payment)
    if payment.status == PaymentStatus.SUCCEEDED:
        # Тот же счёт оплачен второй раз — деньги получены, учитываем отдельным платежом.
        log.warning("Счёт %s оплачен повторно (%s)", payment.id, sp.telegram_payment_charge_id)
        payment = Payment(
            user_id=payment.user_id,
            fundraiser_id=payment.fundraiser_id,
            amount=sp.total_amount,
            currency=sp.currency,
            invoice_payload=f"{payment.invoice_payload[:40]}:{uuid.uuid4().hex[:16]}",
            description=payment.description,
            status=PaymentStatus.NEW,
        )
        session.add(payment)
        result = PaymentResult(payment)

    payment.telegram_payment_charge_id = sp.telegram_payment_charge_id
    payment.provider_payment_charge_id = sp.provider_payment_charge_id
    payment.paid_at = utcnow()
    if sp.currency != payment.currency or sp.total_amount != payment.amount:
        payment.status = PaymentStatus.REVIEW
        log.error("Платёж %s: оплачено %s %s вместо %s — на ручную проверку",
                  payment.id, sp.total_amount, sp.currency, payment.amount)
    else:
        # Оплата могла прийти и по счёту, помеченному устаревшим, — деньги всё равно получены.
        payment.status = PaymentStatus.SUCCEEDED
        result.newly_succeeded = True

    try:
        await session.commit()
    except IntegrityError:
        # Параллельный обработчик успел учесть ту же оплату.
        await session.rollback()
        return None
    log.info("Платёж %s подтверждён: %s (ЮKassa %s)", payment.id, payment.amount, sp.provider_payment_charge_id)
    await session.refresh(payment, ["user", "fundraiser"])
    return result


async def expire_stale_invoices(session: AsyncSession) -> int:
    deadline = utcnow() - timedelta(hours=get_settings().invoice_ttl_hours)
    result = await session.execute(
        update(Payment)
        .where(Payment.status == PaymentStatus.NEW, Payment.created_at < deadline)
        .values(status=PaymentStatus.CANCELED)
    )
    await session.commit()
    return result.rowcount or 0


async def refundable_amount(session: AsyncSession, payment: Payment) -> int:
    refunded = await session.scalar(
        select(func.coalesce(func.sum(Refund.amount), 0)).where(
            Refund.payment_id == payment.id, Refund.status == RefundStatus.SUCCEEDED
        )
    )
    return payment.amount - int(refunded or 0)


async def record_refund(
    session: AsyncSession, payment_id: int, amount: int | None, by_user: User, reason: str
) -> Refund:
    """Отмечает возврат, уже проведённый в личном кабинете ЮKassa."""
    payment = await session.scalar(_for_update(select(Payment).where(Payment.id == payment_id)))
    if payment is None or payment.status != PaymentStatus.SUCCEEDED:
        await session.commit()
        raise PaymentError("Отметить возврат можно только по успешному платежу.")
    available = await refundable_amount(session, payment)
    amount = available if amount is None else amount
    if amount <= 0 or amount > available:
        await session.commit()
        raise PaymentError(f"Доступно к возврату: {format_rub(max(available, 0))}.")
    refund = Refund(
        payment_id=payment.id,
        amount=amount,
        status=RefundStatus.SUCCEEDED,
        reason=reason,
        created_by=by_user.id,
        succeeded_at=utcnow(),
    )
    session.add(refund)
    await session.commit()
    log.info("Пользователь %s отметил возврат %s по платежу %s", by_user.id, amount, payment.id)
    return refund
