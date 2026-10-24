import httpx
import pytest

import app.web as web
from app.db.models import Fundraiser
from tests.helpers import make_bot, make_dispatcher, message_update

HOOK = {"X-Telegram-Bot-Api-Secret-Token": "hook-secret"}


@pytest.fixture
async def client(maker, monkeypatch):
    bot, recorder = make_bot()
    monkeypatch.setattr(web, "create_bot", lambda: bot)
    monkeypatch.setattr(web, "dispatcher", make_dispatcher)
    transport = httpx.ASGITransport(app=web.app)
    async with httpx.AsyncClient(transport=transport, base_url="https://fund.vercel.app") as c:
        yield c, recorder


async def test_webhook_requires_secret(client):
    c, recorder = client
    update = message_update(42, "/start").model_dump(mode="json", exclude_none=True)
    assert (await c.post("/api/telegram", json=update)).status_code == 403
    wrong = {"X-Telegram-Bot-Api-Secret-Token": "x"}
    assert (await c.post("/api/telegram", json=update, headers=wrong)).status_code == 403
    assert recorder.requests == []


async def test_webhook_feeds_update(client):
    c, recorder = client
    update = message_update(42, "/start").model_dump(mode="json", exclude_none=True)
    resp = await c.post("/api/telegram", json=update, headers=HOOK)
    assert resp.status_code == 200
    assert "Фонд студсовета" in recorder.texts()[-1]


async def test_cron_requires_bearer(client):
    c, _ = client
    assert (await c.get("/api/cron/daily")).status_code == 401
    assert (await c.get("/api/cron/daily", headers={"Authorization": "Bearer nope"})).status_code == 401
    resp = await c.get("/api/cron/daily", headers={"Authorization": "Bearer cron-secret"})
    assert resp.json() == {"ok": True}


async def test_setup_registers_webhook(client, maker, monkeypatch):
    c, recorder = client
    monkeypatch.setattr(web, "_run_migrations", lambda: None)
    assert (await c.get("/api/setup")).status_code == 403
    resp = await c.get("/api/setup", params={"key": "hook-secret"}, headers={"host": "fund.vercel.app"})
    body = resp.json()
    assert body["migrations"] == "ok" and body["webhook"] == "https://fund.vercel.app/api/telegram"
    hook = recorder.last("SetWebhook")
    assert hook.secret_token == "hook-secret" and "pre_checkout_query" in hook.allowed_updates
    async with maker() as s:
        assert await s.get(Fundraiser, 1) is not None


async def test_health(client):
    c, _ = client
    assert (await c.get("/api/health")).json() == {"status": "ok"}
