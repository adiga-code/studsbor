from functools import lru_cache
from typing import Annotated
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str = ""
    bot_username: str = ""
    database_url: str = "postgresql+asyncpg://studsbor:studsbor@localhost:5432/studsbor"

    # Telegram ID владельцев: получают роль owner при старте.
    owner_telegram_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)

    # Приём платежей включается только после согласования схемы с вузом и ЮKassa.
    payments_enabled: bool = False
    # Токен провайдера из @BotFather → Bot Settings → Payments (ЮKassa Test или Live).
    payment_provider_token: str = ""
    # Чек по 54-ФЗ: ЮKassa получит данные чека, а Telegram попросит у плательщика e-mail.
    receipt_enabled: bool = False
    receipt_vat_code: int = 1
    receipt_payment_subject: str = "another"
    receipt_payment_mode: str = "full_payment"

    # Кому перечисляются средства — показывается перед оплатой.
    recipient_name: str = "Получатель не указан"
    # У Telegram есть свой минимум суммы (около 1 $ в рублях) — ниже счёт не выставится.
    min_amount_rub: int = 100
    max_amount_rub: int = 100_000

    timezone: str = "Europe/Moscow"
    # Напоминания: проверка раз в день в указанный час, рассылка не раньше указанного числа месяца.
    reminder_hour: int = 12
    reminder_day: int = 1
    # Через сколько часов неоплаченный счёт перестаёт приниматься.
    invoice_ttl_hours: int = 24

    log_level: str = "INFO"
    log_dir: str = "logs"

    @field_validator("owner_telegram_ids", mode="before")
    @classmethod
    def _split_ids(cls, value):
        if isinstance(value, str):
            return [int(part) for part in value.replace(";", ",").split(",") if part.strip()]
        return value

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@lru_cache
def get_settings() -> Settings:
    return Settings()
