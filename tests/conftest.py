import os

os.environ.update(
    {
        "BOT_TOKEN": "123456:TEST",
        "BOT_USERNAME": "studsbor_test_bot",
        "PAYMENTS_ENABLED": "true",
        "PAYMENT_PROVIDER_TOKEN": "381764678:TEST:provider",
        "RECIPIENT_NAME": "Тестовый получатель",
        "OWNER_TELEGRAM_IDS": "1000",
        "WEBHOOK_SECRET": "hook-secret",
        "CRON_SECRET": "cron-secret",
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
