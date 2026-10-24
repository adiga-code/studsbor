from aiogram.fsm.storage.base import StorageKey
from sqlalchemy.pool import NullPool

from app.bot.states import Donate
from app.bot.storage import SqlStorage
from app.db.session import engine_options


def test_engine_options_supabase_session_pooler():
    url, options = engine_options(
        "postgresql://postgres.abc:p%40ss@aws-0-eu-central-1.pooler.supabase.com:5432/postgres", serverless=True
    )
    assert url == "postgresql+asyncpg://postgres.abc:p%40ss@aws-0-eu-central-1.pooler.supabase.com:5432/postgres"
    assert options["poolclass"] is NullPool
    assert "ssl" in options["connect_args"]
    assert "statement_cache_size" not in options["connect_args"]


def test_engine_options_transaction_pooler_and_sslmode():
    url, options = engine_options("postgres://u:p@db.example.com:6543/postgres?sslmode=require")
    assert url.startswith("postgresql+asyncpg://u:p@db.example.com:6543/postgres")
    assert "prepared_statement_cache_size=0" in url and "sslmode" not in url
    args = options["connect_args"]
    assert args["statement_cache_size"] == 0 and "ssl" in args
    assert args["prepared_statement_name_func"]() != args["prepared_statement_name_func"]()
    assert options["pool_pre_ping"] is True


def test_engine_options_local():
    url, options = engine_options("postgresql+asyncpg://studsbor:pw@db:5432/studsbor")
    assert url == "postgresql+asyncpg://studsbor:pw@db:5432/studsbor"
    assert options == {"pool_pre_ping": True}
    assert engine_options("sqlite+aiosqlite:///:memory:")[1] == {"pool_pre_ping": True}


async def test_sql_storage(maker):
    storage = SqlStorage()
    key = StorageKey(bot_id=1, chat_id=42, user_id=42)
    other = StorageKey(bot_id=1, chat_id=7, user_id=7)

    assert await storage.get_state(key) is None
    assert await storage.get_data(key) == {}

    await storage.set_state(key, Donate.amount)
    await storage.update_data(key, {"fundraiser_id": 3})
    await storage.update_data(key, {"title": "Кубок"})
    assert await storage.get_state(key) == Donate.amount.state
    assert await storage.get_data(key) == {"fundraiser_id": 3, "title": "Кубок"}
    assert await storage.get_state(other) is None

    await storage.set_state(key, None)
    await storage.set_data(key, {})
    assert await storage.get_state(key) is None and await storage.get_data(key) == {}
