import asyncio
import json
import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.db.models import FundraiserStatus, Payment, PaymentEvent, PaymentStatus
from app.services import stats
from app.services.fundraisers import create_fundraiser, ensure_general_fund
from app.services.payments import (
    PaymentError,
    SuccessfulPayment,
    apply_successful_payment,
    check_pre_checkout,
    create_invoice,
    expire_stale_invoices,
    invoice_params,
    record_refund,
)
from app.services.users import get_or_create_user

TG = 42


async def _user(session, tg_id=TG, name="Анна"):
    return await get_or_create_user(session, tg_id, f"user{tg_id}", name)


def _paid(payment, charge="ch-1", amount=None, currency="RUB"):
    return SuccessfulPayment(
        currency=currency,
        total_amount=payment.amount if amount is None else amount,
        invoice_payload=payment.invoice_payload,
        telegram_payment_charge_id=charge,
        provider_payment_charge_id=f"yk-{charge}",
    )


async def test_create_invoice_and_params(session):
    user = await _user(session)
    fund = await ensure_general_fund(session)
    payment = await create_invoice(session, user, fund.id, 50000)

    assert payment.status == PaymentStatus.NEW
    params = invoice_params(payment)
    assert params["payload"] == payment.invoice_payload
    assert params["provider_token"] == "381764678:TEST:provider"
    assert params["currency"] == "RUB"
    assert params["prices"] == [{"label": "Взнос", "amount": 50000}]
    assert len(params["title"]) <= 32
    assert "Тестовый получатель" in params["description"] and "не оплата услуг" in params["description"]
    assert "provider_data" not in params


async def test_invoice_with_receipt(session, monkeypatch):
    monkeypatch.setenv("RECEIPT_ENABLED", "true")
    get_settings.cache_clear()
    user = await _user(session)
    fund = await create_fundraiser(session, "Очень длинное название сбора на выпускной", "", None, None, user)
    payment = await create_invoice(session, user, fund.id, 25050)

    params = invoice_params(payment)
    assert params["title"].endswith("…") and len(params["title"]) == 32
    assert params["need_email"] and params["send_email_to_provider"]
    item = json.loads(params["provider_data"])["receipt"]["items"][0]
    assert item["amount"] == {"value": "250.50", "currency": "RUB"}
    assert item["vat_code"] == 1 and item["quantity"] == "1.00"


async def test_create_invoice_validation(session, monkeypatch):
    user = await _user(session)
    fund = await ensure_general_fund(session)
    with pytest.raises(PaymentError, match="Минимальная"):
        await create_invoice(session, user, fund.id, 5000)
    with pytest.raises(PaymentError, match="Максимальная"):
        await create_invoice(session, user, fund.id, 100_000_01)

    closed = await create_fundraiser(session, "Выпускной", "", None, None, user)
    closed.status = FundraiserStatus.CLOSED
    await session.commit()
    with pytest.raises(PaymentError, match="закрыт"):
        await create_invoice(session, user, closed.id, 50000)

    monkeypatch.setenv("PAYMENT_PROVIDER_TOKEN", "")
    get_settings.cache_clear()
    with pytest.raises(PaymentError, match="не подключён"):
        await create_invoice(session, user, fund.id, 50000)


async def test_pre_checkout_checks(session):
    user = await _user(session)
    fund = await ensure_general_fund(session)
    payment = await create_invoice(session, user, fund.id, 30000)
    payload = payment.invoice_payload

    assert await check_pre_checkout(session, TG, payload, "RUB", 30000) is None
    assert "не найден" in await check_pre_checkout(session, TG, "unknown", "RUB", 30000)
    assert "не найден" in await check_pre_checkout(session, 7, payload, "RUB", 30000)
    assert "не совпадает" in await check_pre_checkout(session, TG, payload, "RUB", 100)
    assert "не совпадает" in await check_pre_checkout(session, TG, payload, "USD", 30000)

    fund.status = FundraiserStatus.CLOSED
    await session.commit()
    assert "закрыт" in await check_pre_checkout(session, TG, payload, "RUB", 30000)
    fund.status = FundraiserStatus.OPEN

    payment.created_at = datetime.now(UTC) - timedelta(hours=25)
    await session.commit()
    assert "устарел" in await check_pre_checkout(session, TG, payload, "RUB", 30000)
    assert await expire_stale_invoices(session) == 1
    await session.refresh(payment)
    assert payment.status == PaymentStatus.CANCELED
    assert "устарел" in await check_pre_checkout(session, TG, payload, "RUB", 30000)


async def test_successful_payment_counted_once(session):
    user = await _user(session)
    fund = await ensure_general_fund(session)
    payment = await create_invoice(session, user, fund.id, 30000)

    first = await apply_successful_payment(session, TG, _paid(payment))
    second = await apply_successful_payment(session, TG, _paid(payment))

    assert first.newly_succeeded and not first.duplicate
    assert second.duplicate and not second.newly_succeeded
    assert first.payment.status == PaymentStatus.SUCCEEDED
    assert first.payment.provider_payment_charge_id == "yk-ch-1"
    assert first.payment.paid_at is not None
    assert first.payment.user.telegram_id == TG
    assert (await stats.collected_by_fundraiser(session)) == {fund.id: 30000}
    assert await session.scalar(select(func.count(PaymentEvent.id))) == 1
    # По оплаченному счёту повторный pre_checkout не пропускается.
    assert "уже оплачен" in await check_pre_checkout(session, TG, payment.invoice_payload, "RUB", 30000)


async def test_second_charge_of_same_invoice_is_kept(session):
    """Если Telegram всё же провёл вторую оплату того же счёта, деньги не теряются в учёте."""
    user = await _user(session)
    fund = await ensure_general_fund(session)
    payment = await create_invoice(session, user, fund.id, 30000)
    await apply_successful_payment(session, TG, _paid(payment, "ch-1"))
    again = await apply_successful_payment(session, TG, _paid(payment, "ch-2"))

    assert again.newly_succeeded and again.payment.id != payment.id
    assert (await stats.totals(session)).income == 60000


async def test_late_payment_for_expired_invoice_is_counted(session):
    user = await _user(session)
    fund = await ensure_general_fund(session)
    payment = await create_invoice(session, user, fund.id, 30000)
    payment.status = PaymentStatus.CANCELED
    await session.commit()

    result = await apply_successful_payment(session, TG, _paid(payment))
    assert result.newly_succeeded
    assert (await stats.totals(session)).income == 30000


async def test_amount_mismatch_goes_to_review(session):
    user = await _user(session)
    fund = await ensure_general_fund(session)
    payment = await create_invoice(session, user, fund.id, 30000)

    result = await apply_successful_payment(session, TG, _paid(payment, amount=100))

    assert not result.newly_succeeded
    assert result.payment.status == PaymentStatus.REVIEW
    assert (await stats.totals(session)).income == 0


async def test_unknown_invoice(session):
    sp = SuccessfulPayment("RUB", 100, "nope", "ch-x", "yk-x")
    assert await apply_successful_payment(session, TG, sp) is None


async def test_refund_reduces_stats_and_rating(session):
    owner = await _user(session, 1000, "Владелец")
    user = await _user(session)
    fund = await ensure_general_fund(session)
    payment = await create_invoice(session, user, fund.id, 100000)
    await apply_successful_payment(session, TG, _paid(payment))

    with pytest.raises(PaymentError, match="Доступно"):
        await record_refund(session, payment.id, 200000, owner, "слишком много")

    await record_refund(session, payment.id, 40000, owner, "частичный возврат")

    assert (await stats.rating(session))[0].total == 60000
    totals = await stats.totals(session)
    assert (totals.income, totals.refunds, totals.net_income) == (100000, 40000, 60000)
    assert (await stats.collected_by_fundraiser(session))[fund.id] == 60000
    with pytest.raises(PaymentError):
        await record_refund(session, payment.id, 60001, owner, "")
    rest = await record_refund(session, payment.id, None, owner, "остаток")
    assert rest.amount == 60000
    assert await stats.rating(session) == []

    unpaid = await create_invoice(session, user, fund.id, 30000)
    with pytest.raises(PaymentError, match="успешному"):
        await record_refund(session, unpaid.id, None, owner, "")


@pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL", "").startswith("postgresql"),
    reason="конкурентная блокировка проверяется только на PostgreSQL",
)
async def test_concurrent_deliveries_counted_once(maker):
    async with maker() as s:
        user = await _user(s)
        fund = await ensure_general_fund(s)
        payment = await create_invoice(s, user, fund.id, 30000)

    async def deliver():
        async with maker() as s:
            return await apply_successful_payment(s, TG, _paid(payment))

    results = await asyncio.gather(*(deliver() for _ in range(8)))

    assert sum(bool(r and r.newly_succeeded) for r in results) == 1
    async with maker() as s:
        assert (await stats.collected_by_fundraiser(s)) == {fund.id: 30000}
        assert await s.scalar(select(func.count(Payment.id))) == 1
