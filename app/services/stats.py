"""Статистика, рейтинги и отчётность.

Все суммы считаются по успешным платежам за вычетом успешных возвратов —
никаких вручную редактируемых счётчиков.
"""

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.db.models import Expense, Fundraiser, Payment, PaymentStatus, Refund, RefundStatus, User


def month_bounds(moment: datetime, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """Начало текущего и следующего календарного месяца в часовом поясе tz."""
    local = moment.astimezone(tz)
    start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


def _refunded_subquery():
    return (
        select(Refund.payment_id, func.sum(Refund.amount).label("refunded"))
        .where(Refund.status == RefundStatus.SUCCEEDED)
        .group_by(Refund.payment_id)
        .subquery()
    )


def _net_payments(*columns) -> tuple[Select, object]:
    """SELECT по успешным платежам с колонкой «сумма за вычетом возвратов»."""
    refunded = _refunded_subquery()
    net = Payment.amount - func.coalesce(refunded.c.refunded, 0)
    stmt = (
        select(*columns, func.coalesce(func.sum(net), 0).label("net"))
        .select_from(Payment)
        .outerjoin(refunded, refunded.c.payment_id == Payment.id)
        .where(Payment.status == PaymentStatus.SUCCEEDED)
    )
    return stmt, net


@dataclass
class RatingRow:
    place: int
    user_id: int
    name: str
    total: int


@dataclass
class UserPlace:
    place: int | None
    total: int


async def rating(
    session: AsyncSession, since: datetime | None = None, until: datetime | None = None, limit: int = 10
) -> list[RatingRow]:
    stmt, net = _net_payments(Payment.user_id)
    if since is not None:
        stmt = stmt.where(Payment.paid_at >= since)
    if until is not None:
        stmt = stmt.where(Payment.paid_at < until)
    totals = stmt.group_by(Payment.user_id).subquery()
    rows = await session.execute(
        select(User, totals.c.net)
        .join(totals, totals.c.user_id == User.id)
        .where(totals.c.net > 0)
        .order_by(totals.c.net.desc(), User.id)
        .limit(limit)
    )
    return [
        RatingRow(place=i, user_id=user.id, name=user.public_name, total=int(total))
        for i, (user, total) in enumerate(rows.all(), start=1)
    ]


async def user_place(
    session: AsyncSession, user_id: int, since: datetime | None = None, until: datetime | None = None
) -> UserPlace:
    stmt, _ = _net_payments(Payment.user_id)
    if since is not None:
        stmt = stmt.where(Payment.paid_at >= since)
    if until is not None:
        stmt = stmt.where(Payment.paid_at < until)
    totals = stmt.group_by(Payment.user_id).subquery()
    own = await session.scalar(select(totals.c.net).where(totals.c.user_id == user_id))
    own = int(own or 0)
    if own <= 0:
        return UserPlace(place=None, total=0)
    # При равных суммах выше тот, у кого меньше id — так же, как в rating().
    better = await session.scalar(
        select(func.count()).select_from(totals).where(
            (totals.c.net > own) | ((totals.c.net == own) & (totals.c.user_id < user_id))
        )
    )
    return UserPlace(place=int(better or 0) + 1, total=own)


async def collected_by_fundraiser(session: AsyncSession) -> dict[int, int]:
    stmt, _ = _net_payments(Payment.fundraiser_id)
    rows = await session.execute(stmt.group_by(Payment.fundraiser_id))
    return {fid: int(total) for fid, total in rows.all()}


async def spent_by_fundraiser(session: AsyncSession) -> dict[int, int]:
    rows = await session.execute(
        select(Expense.fundraiser_id, func.sum(Expense.amount)).group_by(Expense.fundraiser_id)
    )
    return {fid: int(total) for fid, total in rows.all()}


@dataclass
class Totals:
    income: int  # успешные платежи до возвратов
    refunds: int
    expenses: int
    payers: int
    payments_count: int

    @property
    def net_income(self) -> int:
        return self.income - self.refunds

    @property
    def balance(self) -> int:
        return self.net_income - self.expenses


async def totals(
    session: AsyncSession, since: datetime | None = None, until: datetime | None = None
) -> Totals:
    pay = select(
        func.coalesce(func.sum(Payment.amount), 0),
        func.count(func.distinct(Payment.user_id)),
        func.count(Payment.id),
    ).where(Payment.status == PaymentStatus.SUCCEEDED)
    ref = select(func.coalesce(func.sum(Refund.amount), 0)).where(
        Refund.status == RefundStatus.SUCCEEDED
    )
    exp = select(func.coalesce(func.sum(Expense.amount), 0))
    if since is not None:
        pay = pay.where(Payment.paid_at >= since)
        ref = ref.where(Refund.succeeded_at >= since)
        exp = exp.where(Expense.spent_at >= since)
    if until is not None:
        pay = pay.where(Payment.paid_at < until)
        ref = ref.where(Refund.succeeded_at < until)
        exp = exp.where(Expense.spent_at < until)
    income, payers, count = (await session.execute(pay)).one()
    return Totals(
        income=int(income),
        refunds=int(await session.scalar(ref) or 0),
        expenses=int(await session.scalar(exp) or 0),
        payers=int(payers),
        payments_count=int(count),
    )


async def recent_payments(session: AsyncSession, limit: int = 15) -> list[Payment]:
    rows = await session.scalars(
        select(Payment)
        .where(Payment.status == PaymentStatus.SUCCEEDED)
        .order_by(Payment.paid_at.desc())
        .limit(limit)
    )
    return list(rows)


async def recent_expenses(
    session: AsyncSession, limit: int = 10, fundraiser_id: int | None = None
) -> list[Expense]:
    stmt = select(Expense).order_by(Expense.spent_at.desc(), Expense.id.desc()).limit(limit)
    if fundraiser_id is not None:
        stmt = stmt.where(Expense.fundraiser_id == fundraiser_id)
    return list(await session.scalars(stmt))


async def user_payments(session: AsyncSession, user_id: int, limit: int = 10) -> list[Payment]:
    rows = await session.scalars(
        select(Payment)
        .where(Payment.user_id == user_id, Payment.status == PaymentStatus.SUCCEEDED)
        .order_by(Payment.paid_at.desc())
        .limit(limit)
    )
    return list(rows)


async def fundraiser_summary(session: AsyncSession, fundraiser: Fundraiser) -> tuple[int, int]:
    """(собрано, потрачено) по одному сбору."""
    collected = (await collected_by_fundraiser(session)).get(fundraiser.id, 0)
    spent = (await spent_by_fundraiser(session)).get(fundraiser.id, 0)
    return collected, spent


def now_month_bounds(tz: ZoneInfo) -> tuple[datetime, datetime]:
    return month_bounds(utcnow(), tz)
