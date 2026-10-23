import os
from datetime import UTC, datetime
from typing import Any

os.environ.update(
    {
        "BOT_TOKEN": "123456:TEST",
        "BOT_USERNAME": "studsbor_test_bot",
        "PAYMENTS_ENABLED": "true",
        "YOOKASSA_WEBHOOK_SECRET": "hook-secret",
        "RECIPIENT_NAME": "Тестовый получатель",
        "OWNER_TELEGRAM_IDS": "1000",
        "LOG_DIR": os.environ.get("LOG_DIR", "/tmp/studsbor-test-logs"),
    }
)

import pytest  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import models  # noqa: E402, F401
from app.db.base import Base  # noqa: E402
from app.db.session import set_sessionmaker  # noqa: E402
from app.money import to_api_value  # noqa: E402
from app.services.yookassa import set_gateway  # noqa: E402


class FakeGateway:
    """Имитация API ЮKassa: хранит платежи в памяти."""

    def __init__(self) -> None:
        self.payments: dict[str, dict[str, Any]] = {}
        self.refunds: dict[str, dict[str, Any]] = {}
        self.idempotence: dict[str, str] = {}
        self.create_calls = 0

    async def create_payment(self, *, amount, description, idempotence_key, return_url, metadata):
        self.create_calls += 1
        if idempotence_key in self.idempotence:
            return dict(self.payments[self.idempotence[idempotence_key]])
        pid = f"yk-{len(self.payments) + 1}"
        self.payments[pid] = {
            "id": pid,
            "status": "pending",
            "paid": False,
            "amount": {"value": to_api_value(amount), "currency": "RUB"},
            "description": description,
            "metadata": dict(metadata),
            "confirmation": {"type": "redirect", "confirmation_url": f"https://yoomoney.test/{pid}"},
        }
        self.idempotence[idempotence_key] = pid
        return dict(self.payments[pid])

    async def get_payment(self, payment_id):
        return dict(self.payments[payment_id])

    async def create_refund(self, *, payment_id, amount, idempotence_key, description):
        rid = f"rf-{len(self.refunds) + 1}"
        self.refunds[rid] = {
            "id": rid,
            "payment_id": payment_id,
            "status": "pending",
            "amount": {"value": to_api_value(amount), "currency": "RUB"},
            "description": description,
        }
        return dict(self.refunds[rid])

    async def get_refund(self, refund_id):
        return dict(self.refunds[refund_id])

    def succeed(self, pid: str, captured_at: datetime | None = None) -> None:
        captured_at = captured_at or datetime.now(UTC)
        self.payments[pid].update(
            status="succeeded", paid=True, captured_at=captured_at.isoformat().replace("+00:00", "Z")
        )

    def cancel(self, pid: str) -> None:
        self.payments[pid].update(status="canceled", cancellation_details={"reason": "expired_on_confirmation"})


@pytest.fixture(autouse=True)
def _settings():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def maker():
    url = os.environ.get("TEST_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    kwargs = {"poolclass": StaticPool} if url.startswith("sqlite") else {}
    engine = create_async_engine(url, **kwargs)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    set_sessionmaker(session_maker)
    yield session_maker
    await engine.dispose()


@pytest.fixture
async def session(maker):
    async with maker() as s:
        yield s


@pytest.fixture
def gateway():
    fake = FakeGateway()
    set_gateway(fake)
    yield fake
    set_gateway(None)
