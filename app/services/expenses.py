import logging
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Expense, User

log = logging.getLogger(__name__)


async def add_expense(
    session: AsyncSession,
    *,
    fundraiser_id: int,
    amount: int,
    description: str,
    spent_at: datetime,
    created_by: User,
    receipt_file_id: str | None = None,
    receipt_file_type: str | None = None,
    receipt_url: str | None = None,
) -> Expense:
    expense = Expense(
        fundraiser_id=fundraiser_id,
        amount=amount,
        description=description,
        spent_at=spent_at,
        created_by=created_by.id,
        receipt_file_id=receipt_file_id,
        receipt_file_type=receipt_file_type,
        receipt_url=receipt_url,
    )
    session.add(expense)
    await session.commit()
    log.info("Пользователь %s добавил расход %s на %s", created_by.id, expense.id, amount)
    return expense
