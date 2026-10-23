import logging
from contextlib import suppress

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot import texts
from app.bot.handlers.common import show
from app.bot.sender import notify_payment_succeeded
from app.bot.states import Donate
from app.config import get_settings
from app.db.models import Fundraiser, Payment, PaymentStatus, User
from app.money import format_rub, parse_rub
from app.services.fundraisers import list_fundraisers
from app.services.payments import (
    PaymentError,
    create_payment,
    payment_description,
    refresh_payment,
    validate_amount,
)
from app.services.yookassa import YooKassaError

log = logging.getLogger(__name__)
router = Router(name="donate")


async def start_donation(message: Message, session: AsyncSession, state: FSMContext) -> None:
    await state.clear()
    if not get_settings().payments_enabled:
        await message.answer(texts.PAYMENTS_DISABLED)
        return
    funds = await list_fundraisers(session, only_open=True)
    if not funds:
        await message.answer("Сейчас нет открытых сборов.")
        return
    await message.answer("Что поддержим?", reply_markup=kb.choose_fundraiser(funds))


@router.callback_query(kb.MenuCb.filter(F.action == "donate"))
async def reminder_donate(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    await callback.answer()
    await start_donation(callback.message, session, state)


@router.callback_query(kb.DonateCb.filter())
async def pick_fundraiser(
    callback: CallbackQuery, callback_data: kb.DonateCb, session: AsyncSession, state: FSMContext
) -> None:
    settings = get_settings()
    fundraiser = await session.get(Fundraiser, callback_data.fundraiser_id)
    if not settings.payments_enabled:
        await callback.answer("Приём взносов пока не подключён.", show_alert=True)
        return
    if fundraiser is None or not fundraiser.is_open:
        await callback.answer("Этот сбор уже закрыт.", show_alert=True)
        return
    await state.set_state(Donate.amount)
    await state.update_data(fundraiser_id=fundraiser.id)
    await callback.message.answer(
        f"<b>{texts.esc(fundraiser.title)}</b>\n\n"
        f"Введите сумму в рублях — от {format_rub(settings.min_amount_rub * 100)} "
        f"до {format_rub(settings.max_amount_rub * 100)} — или выберите вариант ниже.",
        reply_markup=kb.quick_amounts(),
    )
    await callback.answer()


async def _ask_confirmation(
    message: Message, state: FSMContext, session: AsyncSession, amount: int, edit: bool = False
) -> None:
    data = await state.get_data()
    fundraiser = await session.get(Fundraiser, data.get("fundraiser_id", 0))
    if fundraiser is None or not fundraiser.is_open:
        await state.clear()
        await message.answer("Этот сбор уже закрыт.")
        return
    try:
        validate_amount(amount)
    except PaymentError as exc:
        await message.answer(f"{exc} Введите другую сумму.")
        return
    await state.update_data(amount=amount)
    await state.set_state(Donate.confirm)
    text = (
        "<b>Проверьте данные платежа</b>\n\n"
        f"Назначение: {texts.esc(payment_description(fundraiser))}\n"
        f"Сумма: <b>{format_rub(amount)}</b>\n"
        f"Получатель средств: {texts.esc(get_settings().recipient_name)}\n\n"
        "Взнос добровольный и не является оплатой услуг. Оплата проходит на защищённой "
        "странице ЮKassa, а взнос учитывается только после подтверждения платежа."
    )
    if edit:
        await message.edit_text(text, reply_markup=kb.confirm_payment())
    else:
        await message.answer(text, reply_markup=kb.confirm_payment())


@router.message(Donate.amount, F.text)
async def amount_entered(message: Message, state: FSMContext, session: AsyncSession) -> None:
    amount = parse_rub(message.text)
    if amount is None:
        await message.answer("Не получилось распознать сумму. Напишите число, например: 300 или 250,50.")
        return
    await _ask_confirmation(message, state, session, amount)


@router.callback_query(Donate.amount, kb.AmountCb.filter())
async def amount_chosen(
    callback: CallbackQuery, callback_data: kb.AmountCb, state: FSMContext, session: AsyncSession
) -> None:
    await callback.answer()
    await _ask_confirmation(callback.message, state, session, callback_data.rubles * 100, edit=True)


@router.callback_query(kb.PayCb.filter(F.action == "cancel"))
async def payment_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await show(callback, "Платёж отменён.")


@router.callback_query(Donate.confirm, kb.PayCb.filter(F.action == "confirm"))
async def payment_confirm(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: User
) -> None:
    data = await state.get_data()
    # Сбрасываем состояние до создания платежа: повторное нажатие не создаст второй платёж.
    await state.clear()
    await callback.answer("Создаём платёж…")
    try:
        payment = await create_payment(session, db_user, data["fundraiser_id"], data["amount"])
    except PaymentError as exc:
        await callback.message.edit_text(str(exc))
        return
    if not payment.confirmation_url:
        log.error("ЮKassa не вернула ссылку для платежа %s", payment.id)
        await callback.message.edit_text("Не удалось получить ссылку на оплату. Попробуйте позже.")
        return
    await callback.message.edit_text(
        f"Платёж на {format_rub(payment.amount)} создан.\n\n"
        "Нажмите «Оплатить» — откроется защищённая страница ЮKassa. После оплаты бот пришлёт "
        "подтверждение. Возврат на страницу бота сам по себе не означает, что платёж прошёл: "
        "если подтверждения нет, нажмите «Проверить оплату».",
        reply_markup=kb.pay_link(payment.confirmation_url, payment.id),
    )


@router.callback_query(kb.PayCb.filter(F.action == "confirm"))
async def payment_confirm_expired(callback: CallbackQuery) -> None:
    await callback.answer("Эта форма устарела — начните заново.", show_alert=True)


@router.callback_query(kb.PayCheckCb.filter())
async def payment_check(
    callback: CallbackQuery, callback_data: kb.PayCheckCb, session: AsyncSession, db_user: User, bot: Bot
) -> None:
    payment = await session.get(Payment, callback_data.payment_id)
    if payment is None or payment.user_id != db_user.id:
        await callback.answer("Платёж не найден.", show_alert=True)
        return
    try:
        result = await refresh_payment(session, payment)
    except YooKassaError:
        log.exception("Ошибка проверки платежа %s", payment.id)
        await callback.answer("Не удалось связаться с ЮKassa, попробуйте позже.", show_alert=True)
        return
    if result is not None:
        payment = result.payment
        if result.newly_succeeded:
            await notify_payment_succeeded(bot, session, payment)
    status = texts.STATUS_TITLES.get(payment.status, payment.status)
    if payment.status in (PaymentStatus.PENDING, PaymentStatus.WAITING_FOR_CAPTURE):
        await callback.answer(f"Статус: {status}. Если вы уже оплатили, подождите минуту.", show_alert=True)
        return
    await callback.answer(f"Статус: {status}")
    with suppress(TelegramBadRequest):
        await callback.message.edit_reply_markup(reply_markup=None)
