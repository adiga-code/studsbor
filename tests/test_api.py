import httpx
import pytest
from sqlalchemy import select

from app.api.main import create_app
from app.db.models import Payment, PaymentStatus
from app.services.fundraisers import ensure_general_fund
from app.services.payments import create_payment
from app.services.users import get_or_create_user
from app.services.yookassa import is_yookassa_ip
from tests.helpers import make_bot

YOOKASSA_IP = "185.71.76.1"


@pytest.fixture
async def api(maker):
    app = create_app()
    bot, recorder = make_bot()
    app.state.bot = bot
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, recorder


def test_ip_allowlist():
    assert is_yookassa_ip("185.71.76.5")
    assert is_yookassa_ip("77.75.156.11")
    assert is_yookassa_ip("2a02:5180::1")
    assert not is_yookassa_ip("8.8.8.8")
    assert not is_yookassa_ip("garbage")
    assert not is_yookassa_ip(None)


async def test_webhook_rejects_wrong_secret_and_ip(api, gateway):
    client, _ = api
    body = {"event": "payment.succeeded", "object": {"id": "yk-1"}}
    resp = await client.post("/yookassa/webhook/wrong", json=body, headers={"X-Real-IP": YOOKASSA_IP})
    assert resp.status_code == 404
    resp = await client.post("/yookassa/webhook/hook-secret", json=body, headers={"X-Real-IP": "1.2.3.4"})
    assert resp.status_code == 403


async def test_webhook_confirms_payment_once(api, gateway, maker):
    client, recorder = api
    async with maker() as s:
        user = await get_or_create_user(s, 42, "anna", "Анна")
        fund = await ensure_general_fund(s)
        await create_payment(s, user, fund.id, 50000)
    gateway.succeed("yk-1")

    body = {"type": "notification", "event": "payment.succeeded", "object": {"id": "yk-1"}}
    for _ in range(3):
        resp = await client.post("/yookassa/webhook/hook-secret", json=body, headers={"X-Real-IP": YOOKASSA_IP})
        assert resp.status_code == 200

    async with maker() as s:
        payment = await s.scalar(select(Payment))
        assert payment.status == PaymentStatus.SUCCEEDED
    thanks = [m for m in recorder.requests if "Спасибо" in (getattr(m, "text", "") or "")]
    assert len(thanks) == 1 and thanks[0].chat_id == 42


async def test_webhook_returns_503_when_yookassa_unavailable(api, gateway, maker):
    client, _ = api

    async def broken(_):
        from app.services.yookassa import YooKassaError

        raise YooKassaError("timeout", retryable=True)

    gateway.get_payment = broken
    body = {"event": "payment.succeeded", "object": {"id": "yk-1"}}
    resp = await client.post("/yookassa/webhook/hook-secret", json=body, headers={"X-Real-IP": YOOKASSA_IP})
    assert resp.status_code == 503


async def test_health(api):
    client, _ = api
    resp = await client.get("/health")
    assert resp.json() == {"status": "ok"}
