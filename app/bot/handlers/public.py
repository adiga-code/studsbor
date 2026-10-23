"""Публичные разделы: сборы, рейтинг, отчётность."""

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot import texts
from app.bot.handlers.common import show
from app.config import get_settings
from app.db.models import Fundraiser, User
from app.money import format_rub
from app.services import stats
from app.services.fundraisers import list_fundraisers

router = Router(name="public")

MONTHS = [
    "январь", "февраль", "март", "апрель", "май", "июнь",
    "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
]


# --- Сборы ---------------------------------------------------------------


async def _fundraisers_text(session: AsyncSession) -> tuple[str, list[Fundraiser]]:
    funds = await list_fundraisers(session, only_open=True)
    if not funds:
        return "Сейчас нет открытых сборов.", []
    collected = await stats.collected_by_fundraiser(session)
    body = "\n\n".join(texts.fundraiser_line(f, collected.get(f.id, 0)) for f in funds)
    return f"<b>Наши сборы</b>\n\n{body}\n\nВыберите сбор, чтобы узнать подробности:", funds


async def menu_fundraisers(message: Message, session: AsyncSession) -> None:
    text, funds = await _fundraisers_text(session)
    await message.answer(text, reply_markup=kb.fundraisers_list(funds))


@router.callback_query(kb.FundCb.filter(F.action == "list"))
async def fundraisers_list(callback: CallbackQuery, session: AsyncSession) -> None:
    text, funds = await _fundraisers_text(session)
    await show(callback, text, kb.fundraisers_list(funds))


@router.callback_query(kb.FundCb.filter(F.action == "view"))
async def fundraiser_view(callback: CallbackQuery, callback_data: kb.FundCb, session: AsyncSession) -> None:
    fundraiser = await session.get(Fundraiser, callback_data.fundraiser_id)
    if fundraiser is None:
        await callback.answer("Сбор не найден.", show_alert=True)
        return
    collected, spent = await stats.fundraiser_summary(session, fundraiser)
    await show(
        callback,
        texts.fundraiser_card(fundraiser, collected, spent),
        kb.fundraiser_actions(fundraiser, get_settings().payments_enabled),
    )


@router.callback_query(kb.FundCb.filter(F.action == "expenses"))
async def fundraiser_expenses(
    callback: CallbackQuery, callback_data: kb.FundCb, session: AsyncSession
) -> None:
    fundraiser = await session.get(Fundraiser, callback_data.fundraiser_id)
    if fundraiser is None:
        await callback.answer("Сбор не найден.", show_alert=True)
        return
    expenses = await stats.recent_expenses(session, limit=15, fundraiser_id=fundraiser.id)
    body = "\n\n".join(texts.expense_line(e) for e in expenses) or "Расходов пока нет."
    await show(
        callback,
        f"<b>Расходы: {texts.esc(fundraiser.title)}</b>\n\n{body}",
        kb.fundraiser_actions(fundraiser, get_settings().payments_enabled),
    )


# --- Рейтинг -------------------------------------------------------------


async def _rating_text(session: AsyncSession, period: str, user: User) -> str:
    tz = get_settings().tz
    if period == "month":
        since, until = stats.now_month_bounds(tz)
        title = f"Топ-10 за {MONTHS[since.month - 1]} {since.year}"
    else:
        since = until = None
        title = "Топ-10 за всё время"
    rows = await stats.rating(session, since, until)
    lines = [f"🏆 <b>{title}</b>", ""]
    if not rows:
        lines.append("Пока никого нет — станьте первым!")
    for row in rows:
        mark = texts.MEDALS.get(row.place, f"{row.place}.")
        you = " ← вы" if row.user_id == user.id else ""
        lines.append(f"{mark} {texts.esc(row.name)} — {format_rub(row.total)}{you}")
    own = await stats.user_place(session, user.id, since, until)
    lines.append("")
    if own.place is None:
        lines.append("Вы пока не в рейтинге за этот период.")
    else:
        lines.append(f"Ваше место: {own.place} ({format_rub(own.total)})")
    if user.is_anonymous:
        lines.append("Вы отображаетесь как «Анонимный участник» — это можно изменить в настройках.")
    return "\n".join(lines)


async def menu_rating(message: Message, session: AsyncSession, db_user: User) -> None:
    await message.answer(await _rating_text(session, "month", db_user), reply_markup=kb.rating_tabs("month"))


@router.callback_query(kb.RatingCb.filter())
async def rating_tab(
    callback: CallbackQuery, callback_data: kb.RatingCb, session: AsyncSession, db_user: User
) -> None:
    text = await _rating_text(session, callback_data.period, db_user)
    await show(callback, text, kb.rating_tabs(callback_data.period))


# --- Отчётность ----------------------------------------------------------


def _totals_block(title: str, t: stats.Totals) -> str:
    lines = [
        f"<b>{title}</b>",
        f"Поступления: {format_rub(t.net_income)}",
        f"Расходы: {format_rub(t.expenses)}",
        f"Платежей: {t.payments_count}, участников: {t.payers}",
    ]
    if t.refunds:
        lines.insert(2, f"в т. ч. учтены возвраты: {format_rub(t.refunds)}")
    return "\n".join(lines)


async def _summary_text(session: AsyncSession) -> str:
    tz = get_settings().tz
    all_time = await stats.totals(session)
    since, until = stats.now_month_bounds(tz)
    month = await stats.totals(session, since, until)
    return (
        "📊 <b>Отчётность фонда</b>\n\n"
        f"{_totals_block('За всё время', all_time)}\n"
        f"Остаток: <b>{format_rub(all_time.balance)}</b>\n\n"
        f"{_totals_block(f'За {MONTHS[since.month - 1]} {since.year}', month)}"
    )


async def menu_reports(message: Message, session: AsyncSession) -> None:
    await message.answer(await _summary_text(session), reply_markup=kb.report_tabs())


@router.callback_query(kb.ReportCb.filter())
async def report_tab(callback: CallbackQuery, callback_data: kb.ReportCb, session: AsyncSession) -> None:
    section = callback_data.section
    if section == "income":
        payments = await stats.recent_payments(session, limit=15)
        lines = [
            f"{texts.fmt_date(p.paid_at)} · {format_rub(p.amount)} · {texts.esc(p.fundraiser.title)} · "
            f"{texts.esc(p.user.public_name)}"
            + (f" (возврат {format_rub(p.refunded_amount)})" if p.refunded_amount else "")
            for p in payments
        ]
        text = "<b>Последние поступления</b>\n\n" + ("\n".join(lines) or "Поступлений пока нет.")
    elif section == "expenses":
        expenses = await stats.recent_expenses(session, limit=10)
        body = "\n\n".join(texts.expense_line(e) for e in expenses) or "Расходов пока нет."
        text = f"<b>Последние расходы</b>\n\n{body}"
    elif section == "funds":
        funds = (await list_fundraisers(session))[:20]
        collected = await stats.collected_by_fundraiser(session)
        spent = await stats.spent_by_fundraiser(session)
        lines = [
            f"{'🟢' if f.is_open else '⚪️'} <b>{texts.esc(f.title)}</b>\n"
            f"собрано {format_rub(collected.get(f.id, 0))}, потрачено {format_rub(spent.get(f.id, 0))}"
            + (f", цель {format_rub(f.goal_amount)}" if f.goal_amount else "")
            for f in funds
        ]
        text = "<b>По сборам</b>\n\n" + ("\n\n".join(lines) or "Сборов пока нет.")
    else:
        text = await _summary_text(session)
    await show(callback, text, kb.report_tabs())
