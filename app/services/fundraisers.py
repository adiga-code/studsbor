import logging
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.db.models import Fundraiser, FundraiserStatus, User

log = logging.getLogger(__name__)

GENERAL_FUND_TITLE = "Общий фонд студсовета"
GENERAL_FUND_DESCRIPTION = "Мероприятия, реквизит и развитие студенческой жизни."


async def ensure_general_fund(session: AsyncSession) -> Fundraiser:
    fund = await session.scalar(select(Fundraiser).where(Fundraiser.is_general.is_(True)))
    if fund is None:
        fund = Fundraiser(
            title=GENERAL_FUND_TITLE,
            description=GENERAL_FUND_DESCRIPTION,
            status=FundraiserStatus.OPEN,
            is_general=True,
        )
        session.add(fund)
        await session.commit()
        log.info("Создан общий фонд (id=%s)", fund.id)
    return fund


async def list_fundraisers(session: AsyncSession, only_open: bool = False) -> list[Fundraiser]:
    stmt = select(Fundraiser).order_by(Fundraiser.is_general.desc(), Fundraiser.id.desc())
    if only_open:
        now = utcnow()
        stmt = stmt.where(
            Fundraiser.status == FundraiserStatus.OPEN,
            (Fundraiser.ends_at.is_(None)) | (Fundraiser.ends_at > now),
        )
    return list(await session.scalars(stmt))


async def create_fundraiser(
    session: AsyncSession,
    title: str,
    description: str,
    goal_amount: int | None,
    ends_at: datetime | None,
    created_by: User,
) -> Fundraiser:
    fundraiser = Fundraiser(
        title=title,
        description=description,
        goal_amount=goal_amount,
        ends_at=ends_at,
        status=FundraiserStatus.OPEN,
        is_general=False,
        created_by=created_by.id,
    )
    session.add(fundraiser)
    await session.commit()
    log.info("Пользователь %s создал сбор %s «%s»", created_by.id, fundraiser.id, title)
    return fundraiser


async def set_status(
    session: AsyncSession, fundraiser: Fundraiser, status: FundraiserStatus, by_user: User
) -> None:
    fundraiser.status = status
    # Повторное открытие просроченного сбора снимает срок.
    if status == FundraiserStatus.OPEN and fundraiser.ends_at and fundraiser.ends_at <= utcnow():
        fundraiser.ends_at = None
    await session.commit()
    log.info("Пользователь %s перевёл сбор %s в статус %s", by_user.id, fundraiser.id, status)


async def close_expired(session: AsyncSession) -> int:
    result = await session.execute(
        update(Fundraiser)
        .where(
            Fundraiser.status == FundraiserStatus.OPEN,
            Fundraiser.ends_at.is_not(None),
            Fundraiser.ends_at <= utcnow(),
        )
        .values(status=FundraiserStatus.CLOSED)
    )
    await session.commit()
    return result.rowcount or 0
