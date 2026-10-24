import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.config import get_settings


def setup_logging(name: str) -> None:
    """Логи в консоль, а на обычном сервере — ещё и в файл с ротацией.

    В бессерверном режиме файловая система только для чтения: логи собирает платформа.
    """
    settings = get_settings()
    formatter = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if not settings.serverless:
        log_dir = Path(settings.log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(log_dir / f"{name}.log", maxBytes=5 * 1024 * 1024, backupCount=10, encoding="utf-8")
        )

    root = logging.getLogger()
    root.handlers.clear()
    for handler in handlers:
        handler.setFormatter(formatter)
        root.addHandler(handler)
    root.setLevel(settings.log_level.upper())
