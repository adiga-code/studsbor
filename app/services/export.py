"""CSV-выгрузки для бухгалтерии: разделитель «;» и UTF-8 с BOM, чтобы файл открывался в Excel."""

import csv
import io
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Expense, Payment
from app.money import to_api_value


def _fmt_time(value, tz: ZoneInfo) -> str:
    return value.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S") if value else ""


def _to_bytes(header: list[str], rows: list[list]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8-sig")


async def payments_csv(session: AsyncSession, tz: ZoneInfo) -> bytes:
    payments = await session.scalars(select(Payment).order_by(Payment.id))
    rows = [
        [
            p.id,
            _fmt_time(p.created_at, tz),
            _fmt_time(p.paid_at, tz),
            p.status,
            to_api_value(p.amount),
            to_api_value(p.refunded_amount),
            p.currency,
            p.fundraiser_id,
            p.fundraiser.title,
            p.user.telegram_id,
            p.user.username or "",
            p.user.display_name or "",
            p.provider_payment_charge_id or "",
            p.telegram_payment_charge_id or "",
        ]
        for p in payments
    ]
    header = [
        "id", "создан", "оплачен", "статус", "сумма", "возвращено", "валюта", "сбор_id", "сбор",
        "telegram_id", "username", "имя", "yookassa_id", "telegram_charge_id",
    ]
    return _to_bytes(header, rows)


async def expenses_csv(session: AsyncSession, tz: ZoneInfo) -> bytes:
    expenses = await session.scalars(select(Expense).order_by(Expense.spent_at, Expense.id))
    rows = [
        [
            e.id,
            _fmt_time(e.spent_at, tz),
            to_api_value(e.amount),
            e.fundraiser_id,
            e.fundraiser.title,
            e.description,
            e.receipt_url or "",
            "да" if e.receipt_file_id else "нет",
            e.created_by,
        ]
        for e in expenses
    ]
    header = ["id", "дата", "сумма", "сбор_id", "сбор", "назначение", "ссылка_на_чек", "файл_чека", "внёс_user_id"]
    return _to_bytes(header, rows)
