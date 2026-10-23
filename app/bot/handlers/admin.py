"""Администрирование через Telegram. Доступ — по роли в таблице admins."""

import asyncio
import logging
from datetime import date, datetime, time

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot import texts
from app.bot.filters import IsAdmin
from app.bot.handlers.common import show
from app.bot.sender import send_and_log
from app.bot.states import Broadcast, ExpenseAdd, FundCreate, MemberSearch, RefundFlow
from app.config import get_settings
from app.db.base import utcnow
from app.db.models import AdminRole, Fundraiser, FundraiserStatus, Payment, PaymentStatus, User
from app.money import format_rub, parse_rub
from app.services import export, stats
from app.services import notifications as n
from app.services.expenses import add_expense
from app.services.fundraisers import create_fundraiser, list_fundraisers, set_status
from app.services.payments import PaymentError, create_refund
from app.services.users import find_users, get_role, list_admins, set_role

log = logging.getLogger(__name__)
router = Router(name="admin")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

SKIP = {"-", "—", "нет", "пропустить"}


def _parse_date(text: str) -> date | None:
    try:
        return datetime.strptime(text.strip(), "%d.%m.%Y").date()
    except ValueError:
        return None


def _local(day: date, at: time) -> datetime:
    return datetime.combine(day, at, tzinfo=get_settings().tz)


# --- Панель и статистика -------------------------------------------------


@router.message(Command("admin"))
async def admin_panel(message: Message, state: FSMContext, role: AdminRole) -> None:
    await state.clear()
    await message.answer(
        f"🛠 <b>Панель администратора</b>\nВаша роль: {role.value}",
        reply_markup=kb.admin_panel(is_owner=role == AdminRole.OWNER),
    )


async def _stats_text(session: AsyncSession) -> str:
    tz = get_settings().tz
    all_time = await stats.totals(session)
    since, until = stats.now_month_bounds(tz)
    month = await stats.totals(session, since, until)
    users_count = await session.scalar(select(func.count(User.id)))
    reminders = await session.scalar(
        select(func.count(User.id)).where(User.reminders_enabled.is_(True), User.is_blocked.is_(False))
    )
    by_status = dict(
        (await session.execute(select(Payment.status, func.count(Payment.id)).group_by(Payment.status))).all()
    )
    collected = await stats.collected_by_fundraiser(session)
    funds = await list_fundraisers(session, only_open=True)
    lines = [
        "📈 <b>Статистика</b>",
        "",
        "<b>За всё время</b>",
        f"Поступления: {format_rub(all_time.income)}",
        f"Возвраты: {format_rub(all_time.refunds)}",
        f"Расходы: {format_rub(all_time.expenses)}",
        f"Остаток: <b>{format_rub(all_time.balance)}</b>",
        f"Плательщиков: {all_time.payers}, платежей: {all_time.payments_count}",
        "",
        "<b>Текущий месяц</b>",
        f"Поступления: {format_rub(month.net_income)}, расходы: {format_rub(month.expenses)}",
        f"Плательщиков: {month.payers}, платежей: {month.payments_count}",
        "",
        f"Пользователей: {users_count}, с напоминаниями: {reminders}",
        f"Ожидают оплаты: {by_status.get(PaymentStatus.PENDING, 0)}",
    ]
    if by_status.get(PaymentStatus.REVIEW):
        lines.append(f"⚠️ На ручной проверке: {by_status[PaymentStatus.REVIEW]} (см. логи и /payment &lt;id&gt;)")
    if funds:
        lines += ["", "<b>Открытые сборы</b>"]
        lines += [f"• {texts.esc(f.title)}: {format_rub(collected.get(f.id, 0))}" for f in funds]
    return "\n".join(lines)


@router.message(Command("stats"))
async def cmd_stats(message: Message, session: AsyncSession) -> None:
    await message.answer(await _stats_text(session))


@router.callback_query(kb.AdminCb.filter(F.action == "stats"))
async def cb_stats(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.message.answer(await _stats_text(session))
    await callback.answer()


@router.message(Command("payment"))
async def cmd_payment(message: Message, command: CommandObject, session: AsyncSession) -> None:
    if not command.args or not command.args.strip().isdigit():
        await message.answer("Использование: /payment &lt;id платежа&gt;")
        return
    payment = await session.get(Payment, int(command.args.strip()))
    if payment is None:
        await message.answer("Платёж не найден.")
        return
    await message.answer(
        f"<b>Платёж #{payment.id}</b>\n"
        f"Статус: {texts.STATUS_TITLES.get(payment.status, payment.status)}\n"
        f"Сумма: {format_rub(payment.amount)}, возвращено: {format_rub(payment.refunded_amount)}\n"
        f"Сбор: {texts.esc(payment.fundraiser.title)}\n"
        f"Участник: {texts.esc(payment.user.display_name or '—')} (tg {payment.user.telegram_id})\n"
        f"Создан: {texts.fmt_datetime(payment.created_at)}, оплачен: {texts.fmt_datetime(payment.paid_at)}\n"
        f"ЮKassa: <code>{texts.esc(payment.yookassa_payment_id or '—')}</code>"
    )


# --- Создание и управление сборами ---------------------------------------


@router.message(Command("fund_create"))
async def cmd_fund_create(message: Message, state: FSMContext) -> None:
    await state.set_state(FundCreate.title)
    await message.answer("Новый сбор. Введите название (до 128 символов). /cancel — отмена.")


@router.callback_query(kb.AdminCb.filter(F.action == "fund_create"))
async def cb_fund_create(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await cmd_fund_create(callback.message, state)


@router.message(FundCreate.title, F.text)
async def fund_title(message: Message, state: FSMContext) -> None:
    title = message.text.strip()
    if not title or len(title) > 128:
        await message.answer("Название должно быть от 1 до 128 символов.")
        return
    await state.update_data(title=title)
    await state.set_state(FundCreate.description)
    await message.answer("Описание сбора: на что пойдут деньги.")


@router.message(FundCreate.description, F.text)
async def fund_description(message: Message, state: FSMContext) -> None:
    await state.update_data(description=message.text.strip()[:2000])
    await state.set_state(FundCreate.goal)
    await message.answer("Целевая сумма в рублях (или «-», если без цели).")


@router.message(FundCreate.goal, F.text)
async def fund_goal(message: Message, state: FSMContext) -> None:
    text = message.text.strip().lower()
    goal = None if text in SKIP else parse_rub(text)
    if text not in SKIP and goal is None:
        await message.answer("Не понял сумму. Пример: 15000")
        return
    await state.update_data(goal=goal)
    await state.set_state(FundCreate.deadline)
    await message.answer("Срок сбора в формате ДД.ММ.ГГГГ (или «-», если без срока).")


@router.message(FundCreate.deadline, F.text)
async def fund_deadline(message: Message, state: FSMContext) -> None:
    text = message.text.strip().lower()
    ends_at = None
    if text not in SKIP:
        day = _parse_date(text)
        if day is None:
            await message.answer("Формат даты: ДД.ММ.ГГГГ, например 31.12.2026.")
            return
        ends_at = _local(day, time(23, 59, 59))
        if ends_at <= utcnow():
            await message.answer("Срок должен быть в будущем.")
            return
    await state.update_data(ends_at=ends_at.isoformat() if ends_at else None)
    await state.set_state(FundCreate.confirm)
    data = await state.get_data()
    await message.answer(
        "<b>Проверьте сбор</b>\n\n"
        f"Название: {texts.esc(data['title'])}\n"
        f"Описание: {texts.esc(data['description'])}\n"
        f"Цель: {format_rub(data['goal']) if data['goal'] else 'без цели'}\n"
        f"Срок: {texts.fmt_date(ends_at) if ends_at else 'без срока'}",
        reply_markup=kb.confirm("fund"),
    )


@router.callback_query(FundCreate.confirm, kb.ConfirmCb.filter(F.flow == "fund"))
async def fund_confirm(
    callback: CallbackQuery, callback_data: kb.ConfirmCb, state: FSMContext, session: AsyncSession, db_user: User
) -> None:
    data = await state.get_data()
    await state.clear()
    if not callback_data.ok:
        await show(callback, "Создание сбора отменено.")
        return
    fundraiser = await create_fundraiser(
        session,
        title=data["title"],
        description=data["description"],
        goal_amount=data["goal"],
        ends_at=datetime.fromisoformat(data["ends_at"]) if data["ends_at"] else None,
        created_by=db_user,
    )
    await show(callback, f"✅ Сбор «{texts.esc(fundraiser.title)}» открыт.")


@router.callback_query(kb.AdminCb.filter(F.action == "funds"))
async def cb_funds(callback: CallbackQuery, session: AsyncSession) -> None:
    funds = (await list_fundraisers(session))[:30]
    await show(callback, "🎯 <b>Сборы</b>\nНажмите, чтобы открыть или закрыть сбор.", kb.admin_funds(funds))


@router.callback_query(kb.AdminFundCb.filter(F.action == "toggle"))
async def cb_fund_toggle(
    callback: CallbackQuery, callback_data: kb.AdminFundCb, session: AsyncSession, db_user: User
) -> None:
    fundraiser = await session.get(Fundraiser, callback_data.fundraiser_id)
    if fundraiser is None:
        await callback.answer("Сбор не найден.", show_alert=True)
        return
    new_status = FundraiserStatus.CLOSED if fundraiser.is_open else FundraiserStatus.OPEN
    await set_status(session, fundraiser, new_status, db_user)
    funds = (await list_fundraisers(session))[:30]
    await show(callback, "🎯 <b>Сборы</b>\nНажмите, чтобы открыть или закрыть сбор.", kb.admin_funds(funds))


# --- Расходы -------------------------------------------------------------


@router.message(Command("expense_add"))
async def cmd_expense_add(message: Message, state: FSMContext, session: AsyncSession) -> None:
    funds = await list_fundraisers(session)
    await state.set_state(ExpenseAdd.fundraiser)
    await message.answer("Из какого сбора расход?", reply_markup=kb.expense_fundraisers(funds[:30]))


@router.callback_query(kb.AdminCb.filter(F.action == "expense"))
async def cb_expense_add(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await callback.answer()
    await cmd_expense_add(callback.message, state, session)


@router.callback_query(ExpenseAdd.fundraiser, kb.ExpenseFundCb.filter())
async def expense_fundraiser(
    callback: CallbackQuery, callback_data: kb.ExpenseFundCb, state: FSMContext, session: AsyncSession
) -> None:
    fundraiser = await session.get(Fundraiser, callback_data.fundraiser_id)
    if fundraiser is None:
        await callback.answer("Сбор не найден.", show_alert=True)
        return
    await state.update_data(fundraiser_id=fundraiser.id, fundraiser_title=fundraiser.title)
    await state.set_state(ExpenseAdd.amount)
    await show(callback, f"Сбор: {texts.esc(fundraiser.title)}\nСумма расхода в рублях:")


@router.message(ExpenseAdd.amount, F.text)
async def expense_amount(message: Message, state: FSMContext) -> None:
    amount = parse_rub(message.text)
    if amount is None:
        await message.answer("Не понял сумму. Пример: 1250,50")
        return
    await state.update_data(amount=amount)
    await state.set_state(ExpenseAdd.description)
    await message.answer("Назначение расхода: что куплено или оплачено.")


@router.message(ExpenseAdd.description, F.text)
async def expense_description(message: Message, state: FSMContext) -> None:
    await state.update_data(description=message.text.strip()[:2000])
    await state.set_state(ExpenseAdd.receipt)
    await message.answer("Пришлите чек: фото, файл или ссылку. Если чека нет — «-».")


@router.message(ExpenseAdd.receipt)
async def expense_receipt(message: Message, state: FSMContext) -> None:
    receipt: dict[str, str | None] = {"file_id": None, "file_type": None, "url": None}
    if message.photo:
        receipt.update(file_id=message.photo[-1].file_id, file_type="photo")
    elif message.document:
        receipt.update(file_id=message.document.file_id, file_type="document")
    elif message.text and message.text.strip().lower() in SKIP:
        pass
    elif message.text and message.text.strip().startswith(("http://", "https://")):
        receipt.update(url=message.text.strip())
    else:
        await message.answer("Нужно фото, файл, ссылка (http/https) или «-».")
        return
    await state.update_data(receipt=receipt)
    await state.set_state(ExpenseAdd.date)
    await message.answer("Дата расхода ДД.ММ.ГГГГ (или «-» — сегодня).")


@router.message(ExpenseAdd.date, F.text)
async def expense_date(message: Message, state: FSMContext) -> None:
    text = message.text.strip().lower()
    if text in SKIP:
        spent_at = utcnow()
    else:
        day = _parse_date(text)
        if day is None:
            await message.answer("Формат даты: ДД.ММ.ГГГГ.")
            return
        spent_at = _local(day, time(12, 0))
        if spent_at.date() > utcnow().astimezone(get_settings().tz).date():
            await message.answer("Дата расхода не может быть в будущем.")
            return
    await state.update_data(spent_at=spent_at.isoformat())
    await state.set_state(ExpenseAdd.confirm)
    data = await state.get_data()
    receipt = data["receipt"]
    receipt_text = "файл" if receipt["file_id"] else (receipt["url"] or "нет")
    await message.answer(
        "<b>Проверьте расход</b>\n\n"
        f"Сбор: {texts.esc(data['fundraiser_title'])}\n"
        f"Сумма: {format_rub(data['amount'])}\n"
        f"Назначение: {texts.esc(data['description'])}\n"
        f"Чек: {texts.esc(receipt_text)}\n"
        f"Дата: {texts.fmt_date(spent_at)}",
        reply_markup=kb.confirm("expense"),
    )


@router.callback_query(ExpenseAdd.confirm, kb.ConfirmCb.filter(F.flow == "expense"))
async def expense_confirm(
    callback: CallbackQuery, callback_data: kb.ConfirmCb, state: FSMContext, session: AsyncSession, db_user: User
) -> None:
    data = await state.get_data()
    await state.clear()
    if not callback_data.ok:
        await show(callback, "Добавление расхода отменено.")
        return
    receipt = data["receipt"]
    expense = await add_expense(
        session,
        fundraiser_id=data["fundraiser_id"],
        amount=data["amount"],
        description=data["description"],
        spent_at=datetime.fromisoformat(data["spent_at"]),
        created_by=db_user,
        receipt_file_id=receipt["file_id"],
        receipt_file_type=receipt["file_type"],
        receipt_url=receipt["url"],
    )
    await show(callback, f"✅ Расход #{expense.id} на {format_rub(expense.amount)} добавлен в отчётность.")


# --- Участники и роли ----------------------------------------------------


@router.callback_query(kb.AdminCb.filter(F.action == "members"))
async def cb_members(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(MemberSearch.query)
    await callback.message.answer("Введите @username, Telegram ID или часть имени участника.")
    await callback.answer()


@router.message(MemberSearch.query, F.text)
async def member_search(message: Message, state: FSMContext, session: AsyncSession) -> None:
    users = await find_users(session, message.text)
    if not users:
        await message.answer("Никого не нашлось. Попробуйте другой запрос или /cancel.")
        return
    await state.clear()
    await message.answer("Найденные участники:", reply_markup=kb.members_list(users))


async def _member_card(session: AsyncSession, user: User, viewer_role: AdminRole) -> tuple[str, object]:
    role = await get_role(session, user)
    place = await stats.user_place(session, user.id)
    payments = await stats.user_payments(session, user.id, limit=5)
    lines = [
        f"<b>{texts.esc(user.display_name or user.first_name or 'Без имени')}</b>",
        f"Telegram ID: <code>{user.telegram_id}</code>" + (f", @{texts.esc(user.username)}" if user.username else ""),
        f"Зарегистрирован: {texts.fmt_date(user.created_at)}",
        f"Роль: {role.value if role else 'участник'}",
        f"Анонимный профиль: {'да' if user.is_anonymous else 'нет'}",
        f"Всего взносов: {format_rub(place.total)}" + (f", место {place.place}" if place.place else ""),
    ]
    if payments:
        lines += ["", "Последние взносы:"]
        lines += [
            f"#{p.id} {texts.fmt_date(p.paid_at)} · {format_rub(p.amount)} · {texts.esc(p.fundraiser.title)}"
            for p in payments
        ]
    markup = kb.member_actions(user.id, role.value if role else None, viewer_role == AdminRole.OWNER)
    return "\n".join(lines), markup


@router.callback_query(kb.MemberCb.filter(F.action == "view"))
async def member_view(
    callback: CallbackQuery, callback_data: kb.MemberCb, session: AsyncSession, role: AdminRole
) -> None:
    user = await session.get(User, callback_data.user_id)
    if user is None:
        await callback.answer("Участник не найден.", show_alert=True)
        return
    text, markup = await _member_card(session, user, role)
    await show(callback, text, markup)


@router.callback_query(kb.MemberCb.filter(F.action.in_({"grant", "revoke"})), IsAdmin(owner_only=True))
async def member_role(
    callback: CallbackQuery, callback_data: kb.MemberCb, session: AsyncSession, db_user: User, role: AdminRole
) -> None:
    user = await session.get(User, callback_data.user_id)
    if user is None or user.id == db_user.id:
        await callback.answer("Нельзя изменить эту роль.", show_alert=True)
        return
    if await get_role(session, user) == AdminRole.OWNER:
        await callback.answer("Роль владельца задаётся в настройках сервера.", show_alert=True)
        return
    new_role = AdminRole.ADMIN if callback_data.action == "grant" else None
    await set_role(session, user, new_role, db_user)
    log.info("Владелец %s изменил роль пользователя %s на %s", db_user.id, user.id, new_role)
    text, markup = await _member_card(session, user, role)
    await show(callback, text, markup)


@router.callback_query(kb.MemberCb.filter())
async def member_role_denied(callback: CallbackQuery) -> None:
    await callback.answer("Назначать администраторов может только владелец.", show_alert=True)


@router.callback_query(kb.AdminCb.filter(F.action == "admins"), IsAdmin(owner_only=True))
async def cb_admins(callback: CallbackQuery, session: AsyncSession) -> None:
    admins = await list_admins(session)
    lines = [
        f"• {texts.esc(u.display_name or u.first_name or '—')} "
        f"(tg <code>{u.telegram_id}</code>{', @' + texts.esc(u.username) if u.username else ''}) — {r}"
        for u, r in admins
    ]
    await callback.message.answer(
        "🔑 <b>Администраторы</b>\n\n" + "\n".join(lines)
        + "\n\nНазначить или снять администратора: «👥 Участники» → карточка участника."
    )
    await callback.answer()


# --- Экспорт -------------------------------------------------------------


@router.message(Command("export"))
async def cmd_export(message: Message, session: AsyncSession) -> None:
    tz = get_settings().tz
    stamp = utcnow().astimezone(tz).strftime("%Y-%m-%d")
    await message.answer_document(
        BufferedInputFile(await export.payments_csv(session, tz), filename=f"payments_{stamp}.csv"),
        caption="Платежи (все статусы, суммы в рублях).",
    )
    await message.answer_document(
        BufferedInputFile(await export.expenses_csv(session, tz), filename=f"expenses_{stamp}.csv"),
        caption="Расходы.",
    )


@router.callback_query(kb.AdminCb.filter(F.action == "export"))
async def cb_export(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer("Готовим выгрузку…")
    await cmd_export(callback.message, session)


# --- Рассылка ------------------------------------------------------------


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, state: FSMContext) -> None:
    await state.set_state(Broadcast.text)
    await message.answer("Отправьте текст объявления. Форматирование сохранится. /cancel — отмена.")


@router.callback_query(kb.AdminCb.filter(F.action == "broadcast"))
async def cb_broadcast(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await cmd_broadcast(callback.message, state)


@router.message(Broadcast.text, F.text)
async def broadcast_text(message: Message, state: FSMContext, session: AsyncSession) -> None:
    recipients = await n.broadcast_recipients(session)
    await state.update_data(text=message.html_text)
    await state.set_state(Broadcast.confirm)
    await message.answer(message.html_text)
    await message.answer(
        f"☝️ Так будет выглядеть объявление. Получателей: {len(recipients)}. Отправить?",
        reply_markup=kb.confirm("broadcast"),
    )


@router.callback_query(Broadcast.confirm, kb.ConfirmCb.filter(F.flow == "broadcast"))
async def broadcast_confirm(
    callback: CallbackQuery,
    callback_data: kb.ConfirmCb,
    state: FSMContext,
    session: AsyncSession,
    db_user: User,
    bot: Bot,
) -> None:
    data = await state.get_data()
    await state.clear()
    if not callback_data.ok:
        await show(callback, "Рассылка отменена.")
        return
    await show(callback, "📣 Рассылка запущена…")
    log.info("Пользователь %s запустил рассылку", db_user.id)
    counts = {n.STATUS_SENT: 0, n.STATUS_BLOCKED: 0, n.STATUS_FAILED: 0}
    for user in await n.broadcast_recipients(session):
        status = await send_and_log(bot, session, user, n.BROADCAST, data["text"])
        counts[status] = counts.get(status, 0) + 1
        await asyncio.sleep(0.05)  # не превышаем лимиты Telegram
    await callback.message.answer(
        f"Рассылка завершена: доставлено {counts[n.STATUS_SENT]}, "
        f"заблокировали бота {counts[n.STATUS_BLOCKED]}, ошибок {counts[n.STATUS_FAILED]}."
    )


# --- Возвраты (только владелец) ------------------------------------------


@router.message(Command("refund"), IsAdmin(owner_only=True))
async def cmd_refund(message: Message, command: CommandObject, state: FSMContext, session: AsyncSession) -> None:
    args = (command.args or "").split()
    if not args or not args[0].isdigit():
        await message.answer("Использование: /refund &lt;id платежа&gt; [сумма в рублях]\nБез суммы — полный возврат.")
        return
    payment = await session.get(Payment, int(args[0]))
    if payment is None or payment.status != PaymentStatus.SUCCEEDED:
        await message.answer("Вернуть можно только успешный платёж.")
        return
    amount = parse_rub(args[1]) if len(args) > 1 else None
    if len(args) > 1 and amount is None:
        await message.answer("Не понял сумму возврата.")
        return
    await state.set_state(RefundFlow.confirm)
    await state.update_data(payment_id=payment.id, amount=amount)
    await message.answer(
        f"Вернуть {format_rub(amount) if amount else 'полную сумму'} по платежу #{payment.id} "
        f"({format_rub(payment.amount)}, {texts.esc(payment.fundraiser.title)}, "
        f"tg {payment.user.telegram_id})?",
        reply_markup=kb.confirm("refund"),
    )


@router.message(Command("refund"))
async def cmd_refund_denied(message: Message) -> None:
    await message.answer("Оформлять возвраты может только владелец.")


@router.callback_query(RefundFlow.confirm, kb.ConfirmCb.filter(F.flow == "refund"), IsAdmin(owner_only=True))
async def refund_confirm(
    callback: CallbackQuery, callback_data: kb.ConfirmCb, state: FSMContext, session: AsyncSession, db_user: User
) -> None:
    data = await state.get_data()
    await state.clear()
    if not callback_data.ok:
        await show(callback, "Возврат отменён.")
        return
    try:
        refund = await create_refund(
            session, data["payment_id"], data["amount"], db_user, reason=f"Возврат взноса #{data['payment_id']}"
        )
    except PaymentError as exc:
        await show(callback, f"❌ {texts.esc(exc)}")
        return
    await show(
        callback,
        f"Возврат {format_rub(refund.amount)} создан, статус: {refund.status}. "
        "Статистика и рейтинг уменьшатся после подтверждения возврата ЮKassa.",
    )


@router.callback_query(StateFilter(None), kb.ConfirmCb.filter())
async def confirm_expired(callback: CallbackQuery) -> None:
    await callback.answer("Эта форма устарела.", show_alert=True)
