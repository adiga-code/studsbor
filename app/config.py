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
    yookassa_shop_id: str = ""
    yookassa_secret_key: str = ""
    yookassa_api_url: str = "https://api.yookassa.ru/v3"
    # Секрет в пути webhook-а: https://<домен>/yookassa/webhook/<secret>
    yookassa_webhook_secret: str = ""
    # Проверять, что уведомление пришло с IP-адресов ЮKassa.
    yookassa_check_ip: bool = True
    # Заголовок, в котором Nginx передаёт реальный IP клиента.
    real_ip_header: str = "X-Real-IP"
    payment_return_url: str = ""

    # Кому перечисляются средства — показывается перед оплатой.
    recipient_name: str = "Получатель не указан"
    min_amount_rub: int = 10
    max_amount_rub: int = 100_000

    timezone: str = "Europe/Moscow"
    # Напоминания: проверка раз в день в указанный час, рассылка не раньше указанного числа месяца.
    reminder_hour: int = 12
    reminder_day: int = 1
    # Как часто сверять «зависшие» платежи с ЮKassa (минуты).
    pending_sync_minutes: int = 10

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

    @property
    def return_url(self) -> str:
        if self.payment_return_url:
            return self.payment_return_url
        if self.bot_username:
            return f"https://t.me/{self.bot_username}"
        return "https://t.me"


@lru_cache
def get_settings() -> Settings:
    return Settings()
