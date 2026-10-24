from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, BigIntPK, UTCDateTime, utcnow


class FundraiserStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class PaymentStatus(StrEnum):
    NEW = "new"  # счёт выставлен в Telegram, ждёт оплаты
    SUCCEEDED = "succeeded"
    CANCELED = "canceled"  # счёт устарел и больше не принимается
    FAILED = "failed"  # Telegram не принял счёт (например, сумма вне лимитов)
    REVIEW = "review"  # оплата пришла, но не совпала с нашими данными — нужна ручная проверка


class RefundStatus(StrEnum):
    SUCCEEDED = "succeeded"


class AdminRole(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(128))
    display_name: Mapped[str | None] = mapped_column(String(64))
    is_anonymous: Mapped[bool] = mapped_column(Boolean, default=False)
    reminders_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Периодичность напоминаний в месяцах: 1 — ежемесячно, 3 — раз в квартал.
    reminder_period_months: Mapped[int] = mapped_column(Integer, default=1)
    # Пользователь заблокировал бота — рассылки ему не отправляются.
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    admin: Mapped["Admin | None"] = relationship(
        back_populates="user", foreign_keys="Admin.user_id", lazy="selectin"
    )

    @property
    def public_name(self) -> str:
        if self.is_anonymous:
            return "Анонимный участник"
        return self.display_name or self.first_name or "Участник"


class Fundraiser(Base):
    __tablename__ = "fundraisers"
    __table_args__ = (
        CheckConstraint("goal_amount IS NULL OR goal_amount > 0", name="goal_positive"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True)
    title: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(Text, default="")
    # Целевая сумма в копейках; у общего фонда цели может не быть.
    goal_amount: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16), default=FundraiserStatus.OPEN)
    is_general: Mapped[bool] = mapped_column(Boolean, default=False)
    starts_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    ends_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    @property
    def is_open(self) -> bool:
        return self.status == FundraiserStatus.OPEN and (
            self.ends_at is None or self.ends_at > utcnow()
        )


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        Index("ix_payments_status_paid_at", "status", "paid_at"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    fundraiser_id: Mapped[int] = mapped_column(ForeignKey("fundraisers.id"), index=True)
    amount: Mapped[int] = mapped_column(BigInteger)  # копейки
    currency: Mapped[str] = mapped_column(String(3), default="RUB")
    # Уникальный payload счёта Telegram — по нему сопоставляется оплата.
    invoice_payload: Mapped[str] = mapped_column(String(64), unique=True)
    # Идентификаторы операции: в Telegram и у платёжного провайдера (ЮKassa).
    telegram_payment_charge_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    provider_payment_charge_id: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), default=PaymentStatus.NEW)
    description: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
    paid_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    user: Mapped[User] = relationship(lazy="joined")
    fundraiser: Mapped[Fundraiser] = relationship(lazy="joined")
    refunds: Mapped[list["Refund"]] = relationship(back_populates="payment", lazy="selectin")

    @property
    def refunded_amount(self) -> int:
        return sum(r.amount for r in self.refunds if r.status == RefundStatus.SUCCEEDED)


class Refund(Base):
    """Возвраты учитываются отдельно, чтобы не завышать рейтинг и статистику.

    Сам возврат делается в личном кабинете ЮKassa, а здесь владелец его отмечает.
    """

    __tablename__ = "refunds"
    __table_args__ = (CheckConstraint("amount > 0", name="amount_positive"),)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True)
    payment_id: Mapped[int] = mapped_column(ForeignKey("payments.id"), index=True)
    amount: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16), default=RefundStatus.SUCCEEDED)
    reason: Mapped[str | None] = mapped_column(String(255))
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    succeeded_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    payment: Mapped[Payment] = relationship(back_populates="refunds")


class Expense(Base):
    __tablename__ = "expenses"
    __table_args__ = (CheckConstraint("amount > 0", name="amount_positive"),)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True)
    fundraiser_id: Mapped[int] = mapped_column(ForeignKey("fundraisers.id"), index=True)
    amount: Mapped[int] = mapped_column(BigInteger)
    description: Mapped[str] = mapped_column(Text)
    # Чек: файл в Telegram (фото или документ) и/или внешняя ссылка.
    receipt_file_id: Mapped[str | None] = mapped_column(String(255))
    receipt_file_type: Mapped[str | None] = mapped_column(String(16))
    receipt_url: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    spent_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    fundraiser: Mapped[Fundraiser] = relationship(lazy="joined")


class Admin(Base):
    __tablename__ = "admins"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(16), default=AdminRole.ADMIN)
    granted_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    user: Mapped[User] = relationship(back_populates="admin", foreign_keys=[user_id])


class NotificationLog(Base):
    __tablename__ = "notification_logs"
    __table_args__ = (Index("ix_notification_logs_user_type", "user_id", "notification_type"),)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    notification_type: Mapped[str] = mapped_column(String(32))
    sent_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    status: Mapped[str] = mapped_column(String(16))
    error: Mapped[str | None] = mapped_column(Text)


class PaymentEvent(Base):
    """Журнал обработанных платёжных событий; уникальный ключ защищает от повторной обработки."""

    __tablename__ = "payment_events"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True)
    event_key: Mapped[str] = mapped_column(String(160), unique=True)
    event_type: Mapped[str] = mapped_column(String(64))
    payment_id: Mapped[int | None] = mapped_column(ForeignKey("payments.id"), index=True)
    payload: Mapped[str | None] = mapped_column(Text)
    processed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class FsmState(Base):
    """Состояние диалогов бота (ввод суммы, создание сбора и т. п.).

    Хранится в базе, чтобы переживать перезапуск бота и работать в бессерверном режиме.
    """

    __tablename__ = "fsm_states"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    state: Mapped[str | None] = mapped_column(String(255))
    data: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
