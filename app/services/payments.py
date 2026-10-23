"""Создание платежей и обработка статусов ЮKassa.

Ключевые правила:
* платёж создаётся только на сервере, с уникальным ключом идемпотентности;
* сумма учитывается только после подтверждённого статуса succeeded, полученного
  из API ЮKassa (тело уведомления используется лишь как сигнал «перепроверь»);
* каждое событие регистрируется в payment_events, повтор не меняет данные.
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.base import utcnow
from app.db.models import (
    Fundraiser,
    Payment,
    PaymentEvent,
    PaymentStatus,
    Refund,
    RefundStatus,
    User,
)
from app.money import format_rub, from_api_value
from app.services.yookassa import (
    PaymentGateway,
    YooKassaError,
    get_gateway,
    new_idempotence_key,
)

log = logging.getLogger(__name__)

# Статусы, после которых платёж больше не меняется.
FINAL_STATUSES = {PaymentStatus.SUCCEEDED, PaymentStatus.CANCELED, PaymentStatus.REVIEW}


def _for_update(stmt):
    """SELECT … FOR UPDATE с перечитыванием объекта, даже если он уже есть в сессии."""
    return stmt.with_for_update(of=Payment).execution_options(populate_existing=True)


class PaymentError(Exception):
    """Ошибка, текст которой можно показать пользователю."""


@dataclass
class PaymentResult:
    payment: Payment
    newly_succeeded: bool = False
    newly_canceled: bool = False
    duplicate: bool = False


@dataclass
class RefundResult:
    refund: Refund
    newly_succeeded: bool = False
    duplicate: bool = False


def validate_amount(amount: int) -> None:
    settings = get_settings()
    if amount < settings.min_amount_rub * 100:
        raise PaymentError(f"Минимальная сумма — {format_rub(settings.min_amount_rub * 100)}.")
    if amount > settings.max_amount_rub * 100:
        raise PaymentError(f"Максимальная сумма — {format_rub(settings.max_amount_rub * 100)}.")


def payment_description(fundraiser: Fundraiser) -> str:
    return f"Добровольный взнос: {fundraiser.title}"[:128]


async def create_payment(
    session: AsyncSession,
    user: User,
    fundraiser_id: int,
    amount: int,
    gateway: PaymentGateway | None = None,
) -> Payment:
    settings = get_settings()
    if not settings.payments_enabled:
        raise PaymentError("Приём платежей пока не подключён.")
    validate_amount(amount)
    fundraiser = await session.get(Fundraiser, fundraiser_id)
    if fundraiser is None or not fundraiser.is_open:
        raise PaymentError("Этот сбор закрыт.")

    payment = Payment(
        user_id=user.id,
        fundraiser_id=fundraiser.id,
        amount=amount,
        currency="RUB",
        idempotence_key=new_idempotence_key(),
        status=PaymentStatus.NEW,
        description=payment_description(fundraiser),
    )
    session.add(payment)
    # Фиксируем намерение до обращения к ЮKassa: id попадёт в metadata платежа.
    await session.commit()

    gateway = gateway or get_gateway()
    remote: dict[str, Any] | None = None
    for attempt in range(3):
        try:
            remote = await gateway.create_payment(
                amount=amount,
                description=payment.description,
                idempotence_key=payment.idempotence_key,
                return_url=settings.return_url,
                metadata={
                    "payment_id": str(payment.id),
                    "fundraiser_id": str(fundraiser.id),
                    "user_id": str(user.id),
                },
            )
            break
        except YooKassaError as exc:
            log.warning("Не удалось создать платёж %s (попытка %s): %s", payment.id, attempt + 1, exc)
            if not exc.retryable or attempt == 2:
                payment.status = PaymentStatus.FAILED
                await session.commit()
                raise PaymentError("Платёжный сервис недоступен, попробуйте позже.") from exc
            await asyncio.sleep(1 + attempt)

    assert remote is not None
    payment.yookassa_payment_id = remote["id"]
    payment.status = remote.get("status", PaymentStatus.PENDING)
    payment.confirmation_url = (remote.get("confirmation") or {}).get("confirmation_url")
    await session.commit()
    log.info("Создан платёж %s (ЮKassa %s) на %s", payment.id, payment.yookassa_payment_id, amount)
    return payment


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _mismatch_reason(payment: Payment, remote: dict[str, Any]) -> str | None:
    amount = remote.get("amount") or {}
    if amount.get("currency") != payment.currency:
        return f"валюта {amount.get('currency')}"
    if from_api_value(amount.get("value", "0")) != payment.amount:
        return f"сумма {amount.get('value')}"
    metadata = remote.get("metadata") or {}
    if metadata.get("payment_id") not in (None, str(payment.id)):
        return f"payment_id в metadata {metadata.get('payment_id')}"
    if metadata.get("fundraiser_id") not in (None, str(payment.fundraiser_id)):
        return f"сбор {metadata.get('fundraiser_id')}"
    if not remote.get("paid", False):
        return "paid=false"
    return None


async def _register_event(
    session: AsyncSession, key: str, event_type: str, payment_id: int | None, payload: dict
) -> bool:
    """Возвращает False, если событие уже было обработано."""
    exists = await session.scalar(select(PaymentEvent.id).where(PaymentEvent.event_key == key))
    if exists:
        return False
    session.add(
        PaymentEvent(
            event_key=key,
            event_type=event_type,
            payment_id=payment_id,
            payload=json.dumps(payload, ensure_ascii=False)[:10000],
        )
    )
    return True


async def _lock_payment(session: AsyncSession, remote: dict[str, Any]) -> Payment | None:
    remote_id = remote["id"]
    payment = await session.scalar(
        _for_update(select(Payment).where(Payment.yookassa_payment_id == remote_id))
    )
    if payment is not None:
        return payment
    # Ответ на создание платежа мог потеряться — ищем по нашему id из metadata.
    local_id = (remote.get("metadata") or {}).get("payment_id")
    if not (local_id and str(local_id).isdigit()):
        return None
    payment = await session.scalar(
        _for_update(select(Payment).where(Payment.id == int(local_id)))
    )
    if payment is None:
        return None
    if payment.yookassa_payment_id is None:
        payment.yookassa_payment_id = remote_id
        return payment
    log.error("Платёж %s уже связан с %s, а не с %s", payment.id, payment.yookassa_payment_id, remote_id)
    return None


async def apply_remote_payment(
    session: AsyncSession, remote: dict[str, Any], event_type: str = "sync"
) -> PaymentResult | None:
    """Применяет проверенный объект платежа из API ЮKassa к нашей записи."""
    payment = await _lock_payment(session, remote)
    if payment is None:
        log.warning("Неизвестный платёж ЮKassa %s", remote.get("id"))
        await session.commit()
        return None

    remote_status = remote.get("status", "")
    key = f"payment:{remote['id']}:{remote_status}"
    if not await _register_event(session, key, event_type, payment.id, remote):
        # commit, а не rollback: rollback сбросил бы загруженные объекты сессии.
        await session.commit()
        return PaymentResult(payment, duplicate=True)

    result = PaymentResult(payment)
    if payment.status in FINAL_STATUSES:
        log.info("Платёж %s уже в статусе %s, событие %s пропущено", payment.id, payment.status, key)
    elif remote_status == PaymentStatus.SUCCEEDED:
        reason = _mismatch_reason(payment, remote)
        if reason:
            payment.status = PaymentStatus.REVIEW
            log.error("Платёж %s не прошёл проверку (%s) — отправлен на ручную проверку", payment.id, reason)
        else:
            payment.status = PaymentStatus.SUCCEEDED
            payment.paid_at = _parse_time(remote.get("captured_at")) or utcnow()
            result.newly_succeeded = True
    elif remote_status == PaymentStatus.CANCELED:
        payment.status = PaymentStatus.CANCELED
        payment.cancellation_reason = (remote.get("cancellation_details") or {}).get("reason")
        result.newly_canceled = True
    elif remote_status in (PaymentStatus.PENDING, PaymentStatus.WAITING_FOR_CAPTURE):
        payment.status = remote_status

    try:
        await session.commit()
    except IntegrityError:
        # Параллельный обработчик успел зарегистрировать то же событие.
        await session.rollback()
        await session.refresh(payment)
        return PaymentResult(payment, duplicate=True)
    if result.newly_succeeded:
        log.info("Платёж %s подтверждён: %s", payment.id, payment.amount)
    return result


async def apply_remote_refund(
    session: AsyncSession, remote: dict[str, Any], event_type: str = "sync"
) -> RefundResult | None:
    payment = await session.scalar(
        _for_update(select(Payment).where(Payment.yookassa_payment_id == remote.get("payment_id")))
    )
    if payment is None:
        log.warning("Возврат %s относится к неизвестному платежу %s", remote.get("id"), remote.get("payment_id"))
        await session.commit()
        return None

    remote_status = remote.get("status", "")
    key = f"refund:{remote['id']}:{remote_status}"
    refund = await session.scalar(select(Refund).where(Refund.yookassa_refund_id == remote["id"]))
    if not await _register_event(session, key, event_type, payment.id, remote):
        await session.commit()
        return RefundResult(refund, duplicate=True) if refund else None

    if refund is None:
        refund = Refund(
            payment_id=payment.id,
            yookassa_refund_id=remote["id"],
            amount=from_api_value(remote["amount"]["value"]),
            status=RefundStatus.PENDING,
            reason=remote.get("description"),
        )
        session.add(refund)

    result = RefundResult(refund)
    if remote_status == RefundStatus.SUCCEEDED and refund.status != RefundStatus.SUCCEEDED:
        refund.status = RefundStatus.SUCCEEDED
        refund.succeeded_at = utcnow()
        result.newly_succeeded = True
    elif remote_status == RefundStatus.CANCELED and refund.status == RefundStatus.PENDING:
        refund.status = RefundStatus.CANCELED

    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return None
    return result


async def handle_notification(
    session: AsyncSession, body: dict[str, Any], gateway: PaymentGateway | None = None
) -> PaymentResult | RefundResult | None:
    """Обрабатывает HTTP-уведомление ЮKassa.

    Тело уведомления не считается доказательством оплаты: актуальный объект
    всегда запрашивается из API по его идентификатору.
    """
    gateway = gateway or get_gateway()
    event = str(body.get("event", ""))
    object_id = (body.get("object") or {}).get("id")
    if not object_id:
        log.warning("Уведомление без object.id: %s", event)
        return None
    if event.startswith("payment."):
        remote = await gateway.get_payment(object_id)
        return await apply_remote_payment(session, remote, event)
    if event.startswith("refund."):
        remote = await gateway.get_refund(object_id)
        return await apply_remote_refund(session, remote, event)
    log.info("Событие %s не обрабатывается", event)
    return None


async def refresh_payment(
    session: AsyncSession, payment: Payment, gateway: PaymentGateway | None = None
) -> PaymentResult | None:
    """Запрашивает актуальный статус платежа в ЮKassa (кнопка «Проверить оплату», сверка)."""
    if payment.yookassa_payment_id is None or payment.status in FINAL_STATUSES:
        return PaymentResult(payment, duplicate=True)
    gateway = gateway or get_gateway()
    remote = await gateway.get_payment(payment.yookassa_payment_id)
    return await apply_remote_payment(session, remote, "sync")


async def pending_payments(session: AsyncSession, older_than: timedelta) -> list[Payment]:
    now = utcnow()
    rows = await session.scalars(
        select(Payment)
        .where(
            Payment.status.in_([PaymentStatus.PENDING, PaymentStatus.WAITING_FOR_CAPTURE]),
            Payment.yookassa_payment_id.is_not(None),
            Payment.created_at < now - older_than,
            Payment.created_at > now - timedelta(days=7),
        )
        .order_by(Payment.id)
    )
    return list(rows)


async def create_refund(
    session: AsyncSession,
    payment_id: int,
    amount: int | None,
    by_user: User,
    reason: str,
    gateway: PaymentGateway | None = None,
) -> Refund:
    payment = await session.scalar(_for_update(select(Payment).where(Payment.id == payment_id)))
    if payment is None or payment.status != PaymentStatus.SUCCEEDED:
        await session.commit()
        raise PaymentError("Вернуть можно только успешный платёж.")
    reserved = await session.scalar(
        select(func.coalesce(func.sum(Refund.amount), 0)).where(
            Refund.payment_id == payment.id, Refund.status != RefundStatus.CANCELED
        )
    )
    available = payment.amount - reserved
    amount = available if amount is None else amount
    if amount <= 0 or amount > available:
        await session.commit()
        raise PaymentError(f"Доступно к возврату: {format_rub(max(available, 0))}.")

    gateway = gateway or get_gateway()
    try:
        remote = await gateway.create_refund(
            payment_id=payment.yookassa_payment_id,
            amount=amount,
            idempotence_key=new_idempotence_key(),
            description=reason or f"Возврат взноса #{payment.id}",
        )
    except YooKassaError as exc:
        await session.commit()
        raise PaymentError(f"ЮKassa отклонила возврат: {exc}") from exc

    refund = Refund(
        payment_id=payment.id,
        yookassa_refund_id=remote["id"],
        amount=amount,
        status=remote.get("status", RefundStatus.PENDING),
        reason=reason,
        created_by=by_user.id,
    )
    if refund.status == RefundStatus.SUCCEEDED:
        refund.succeeded_at = utcnow()
    session.add(refund)
    await session.commit()
    log.info("Создан возврат %s по платежу %s на %s", refund.yookassa_refund_id, payment.id, amount)
    return refund
