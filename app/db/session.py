import asyncio
import ssl
import uuid
from typing import Any

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings

_engine: AsyncEngine | None = None
_engine_loop: asyncio.AbstractEventLoop | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None
_override: async_sessionmaker[AsyncSession] | None = None

# Порт пулера Supabase в режиме transaction (PgBouncer/Supavisor): там нельзя
# переиспользовать подготовленные запросы между транзакциями.
TRANSACTION_POOLER_PORT = 6543


def engine_options(database_url: str, serverless: bool = False) -> tuple[str, dict[str, Any]]:
    """Приводит строку подключения к виду для asyncpg и подбирает параметры движка.

    Принимает строки из панели Supabase как есть: postgres://… или postgresql://…,
    с параметром sslmode или без него.
    """
    url = make_url(database_url)
    if url.drivername in ("postgres", "postgresql", "postgresql+psycopg2", "postgresql+psycopg"):
        url = url.set(drivername="postgresql+asyncpg")
    connect_args: dict[str, Any] = {}
    options: dict[str, Any] = {"pool_pre_ping": True}
    if not url.drivername.startswith("postgresql"):
        return url.render_as_string(hide_password=False), options

    query = dict(url.query)
    sslmode = query.pop("sslmode", None)
    host = url.host or ""
    if sslmode in ("require", "verify-ca", "verify-full") or host.endswith((".supabase.com", ".supabase.co")):
        # Сертификат пулера Supabase не проверяем по цепочке, но канал шифруем.
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        connect_args["ssl"] = context
    if url.port == TRANSACTION_POOLER_PORT:
        query["prepared_statement_cache_size"] = "0"
        connect_args["statement_cache_size"] = 0
        connect_args["prepared_statement_name_func"] = lambda: f"__asyncpg_{uuid.uuid4()}__"
    url = url.set(query=query)

    if serverless:
        # Маленький пул: соединения переиспользуются в рамках «тёплого» экземпляра
        # функции, а не открываются заново на каждое обращение к базе (это и есть
        # основная задержка). Старые соединения выбрасываются, не дожидаясь обрыва.
        options.update(pool_size=2, max_overflow=3, pool_recycle=60, pool_timeout=10)
        connect_args["timeout"] = 10
    if connect_args:
        options["connect_args"] = connect_args
    return url.render_as_string(hide_password=False), options


def _current_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def get_engine() -> AsyncEngine:
    """Движок привязан к циклу событий: соединения asyncpg нельзя использовать из другого.

    Если платформа запустила обработчик в новом цикле, создаётся новый движок, а
    старые соединения закрываются сборщиком мусора.
    """
    global _engine, _engine_loop, _sessionmaker
    loop = _current_loop()
    if _engine is None or (loop is not None and _engine_loop is not None and loop is not _engine_loop):
        settings = get_settings()
        url, options = engine_options(settings.database_url, settings.serverless)
        _engine = create_async_engine(url, **options)
        _engine_loop = loop
        _sessionmaker = None
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _override is not None:
        return _override
    engine = get_engine()
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    return _sessionmaker


def set_sessionmaker(maker: async_sessionmaker[AsyncSession] | None) -> None:
    """Подмена фабрики сессий (используется в тестах)."""
    global _override
    _override = maker
