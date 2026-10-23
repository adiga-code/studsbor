import asyncio
import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.db.models import FundraiserStatus, Payment, PaymentEvent, PaymentStatus, RefundStatus
from app.services import stats
from app.services.fundraisers import create_fundraiser, ensure_general_fund
from app.services.payments import (
    PaymentError,
    PaymentResult,
    RefundResult,
    apply_remote_payment,
    create_payment,
    create_refund,
    handle_notification,
    refresh_payment,
)
from app.services.users import get_or_create_user


async def _user(session, tg_id=1, name="Анна"):
    return await get_or_create_user(session, tg_id, f"user{tg_id}", name)


def _notification(pid, event="payment.succeeded"):
    return {"type": "notification", "event": event, "object": {"id": pid, "status": "succeeded"}}


async def test_create_payment(session, gateway):
    user = await _user(session)
    fund = await ensure_general_fund(session)
    payment = await create_payment(session, user, fund.id, 50000)

    assert payment.status == PaymentStatus.PENDING
    assert payment.yookassa_payment_id == "yk-1"
    assert payment.confirmation_url == "https://yoomoney.test/yk-1"
    remote = gateway.payments["yk-1"]
    assert remote["amount"] == {"value": "500.00", "currency": "RUB"}
    assert remote["metadata"] == {"payment_id": str(payment.id), "fundraiser_id": str(fund.id), "user_id": str(user.id)}
    assert remote["description"] == "Добровольный взнос: Общий фонд студсовета"


async def test_create_payment_validation(session, gateway, monkeypatch):
    user = await _user(session)
    fund = await ensure_general_fund(session)
    with pytest.raises(PaymentError, match="Минимальная"):
        await create_payment(session, user, fund.id, 500)
    with pytest.raises(PaymentError, match="Максимальная"):
        await create_payment(session, user, fund.id, 100_000_01)

    closed = await create_fundraiser(session, "Выпускной", "", None, None, user)
    closed.status = FundraiserStatus.CLOSED
    await session.commit()
    with pytest.raises(PaymentError, match="закрыт"):
        await create_payment(session, user, closed.id, 50000)

    expired = await create_fundraiser(
        session, "Старый", "", None, datetime.now(UTC) - timedelta(days=1), user
    )
    with pytest.raises(PaymentError, match="закрыт"):
        await create_payment(session, user, expired.id, 50000)

    monkeypatch.setenv("PAYMENTS_ENABLED", "false")
    from app.config import get_settings

    get_settings.cache_clear()
    with pytest.raises(PaymentError, match="не подключён"):
        await create_payment(session, user, fund.id, 50000)
    assert gateway.create_calls == 0


async def test_duplicate_notifications_counted_once(session, gateway):
    user = await _user(session)
    fund = await ensure_general_fund(session)
    payment = await create_payment(session, user, fund.id, 30000)
    gateway.succeed("yk-1")

    first = await handle_notification(session, _notification("yk-1"))
    second = await handle_notification(session, _notification("yk-1"))
    third = await refresh_payment(session, payment)

    assert isinstance(first, PaymentResult) and first.newly_succeeded
    assert second.duplicate and not second.newly_succeeded
    assert third.duplicate and not third.newly_succeeded
    assert (await stats.collected_by_fundraiser(session)) == {fund.id: 30000}
    assert await session.scalar(select(func.count(PaymentEvent.id))) == 1


async def test_notification_body_is_not_trusted(session, gateway):
    """Уведомление «succeeded» не учитывается, пока API ЮKassa не подтвердит оплату."""
    user = await _user(session)
    fund = await ensure_general_fund(session)
    payment = await create_payment(session, user, fund.id, 30000)

    result = await handle_notification(session, _notification("yk-1"))

    assert not result.newly_succeeded
    await session.refresh(payment)
    assert payment.status == PaymentStatus.PENDING
    assert (await stats.collected_by_fundraiser(session)) == {}


async def test_amount_mismatch_goes_to_review(session, gateway):
    user = await _user(session)
    fund = await ensure_general_fund(session)
    payment = await create_payment(session, user, fund.id, 30000)
    gateway.succeed("yk-1")
    gateway.payments["yk-1"]["amount"]["value"] = "3.00"

    result = await handle_notification(session, _notification("yk-1"))

    assert not result.newly_succeeded
    await session.refresh(payment)
    assert payment.status == PaymentStatus.REVIEW
    assert (await stats.totals(session)).income == 0


async def test_canceled_payment(session, gateway):
    user = await _user(session)
    fund = await ensure_general_fund(session)
    payment = await create_payment(session, user, fund.id, 30000)
    gateway.cancel("yk-1")

    result = await handle_notification(session, _notification("yk-1", "payment.canceled"))

    assert result.newly_canceled
    await session.refresh(payment)
    assert payment.status == PaymentStatus.CANCELED
    assert payment.cancellation_reason == "expired_on_confirmation"
    # Поздний «succeeded» для отменённого платежа не меняет статус.
    gateway.succeed("yk-1")
    late = await handle_notification(session, _notification("yk-1"))
    assert not late.newly_succeeded
    assert (await stats.totals(session)).income == 0


async def test_lost_create_response_is_recovered_by_metadata(session, gateway):
    user = await _user(session)
    fund = await ensure_general_fund(session)
    payment = Payment(
        user_id=user.id, fundraiser_id=fund.id, amount=20000, idempotence_key="k1",
        status=PaymentStatus.NEW, description="x",
    )
    session.add(payment)
    await session.commit()
    remote = await gateway.create_payment(
        amount=20000, description="x", idempotence_key="k1", return_url="https://t.me",
        metadata={"payment_id": str(payment.id), "fundraiser_id": str(fund.id)},
    )
    gateway.succeed(remote["id"])

    result = await handle_notification(session, _notification(remote["id"]))

    assert result.newly_succeeded
    assert result.payment.yookassa_payment_id == remote["id"]


async def test_unknown_payment_is_ignored(session, gateway):
    gateway.payments["alien"] = {"id": "alien", "status": "succeeded", "metadata": {}}
    assert await handle_notification(session, _notification("alien")) is None


async def test_refund_reduces_stats_and_rating(session, gateway):
    owner = await _user(session, 1000, "Владелец")
    user = await _user(session)
    fund = await ensure_general_fund(session)
    payment = await create_payment(session, user, fund.id, 100000)
    gateway.succeed("yk-1")
    await handle_notification(session, _notification("yk-1"))

    with pytest.raises(PaymentError, match="Доступно"):
        await create_refund(session, payment.id, 200000, owner, "слишком много")

    refund = await create_refund(session, payment.id, 40000, owner, "частичный возврат")
    assert refund.status == RefundStatus.PENDING
    # До подтверждения возврат не учитывается.
    assert (await stats.rating(session))[0].total == 100000

    gateway.refunds[refund.yookassa_refund_id]["status"] = "succeeded"
    note = {"event": "refund.succeeded", "object": {"id": refund.yookassa_refund_id}}
    first = await handle_notification(session, note)
    second = await handle_notification(session, note)

    assert isinstance(first, RefundResult) and first.newly_succeeded
    assert second.duplicate
    assert (await stats.rating(session))[0].total == 60000
    totals = await stats.totals(session)
    assert (totals.income, totals.refunds, totals.net_income) == (100000, 40000, 60000)
    assert (await stats.collected_by_fundraiser(session))[fund.id] == 60000
    # Остаток доступен к возврату, но не больше.
    with pytest.raises(PaymentError):
        await create_refund(session, payment.id, 60001, owner, "")


async def test_payment_status_progression_via_sync(session, gateway):
    user = await _user(session)
    fund = await ensure_general_fund(session)
    payment = await create_payment(session, user, fund.id, 30000)

    pending = await refresh_payment(session, payment)
    assert not pending.newly_succeeded and not pending.duplicate
    gateway.succeed("yk-1")
    done = await apply_remote_payment(session, await gateway.get_payment("yk-1"))
    assert done.newly_succeeded
    assert payment.paid_at is not None


@pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL", "").startswith("postgresql"),
    reason="конкурентная блокировка проверяется только на PostgreSQL",
)
async def test_concurrent_notifications_counted_once(maker, gateway):
    async with maker() as s:
        user = await _user(s)
        fund = await ensure_general_fund(s)
        await create_payment(s, user, fund.id, 30000)
    gateway.succeed("yk-1")

    async def deliver():
        async with maker() as s:
            return await handle_notification(s, _notification("yk-1"))

    results = await asyncio.gather(*(deliver() for _ in range(8)))

    assert sum(r.newly_succeeded for r in results) == 1
    async with maker() as s:
        assert (await stats.collected_by_fundraiser(s)) == {fund.id: 30000}
