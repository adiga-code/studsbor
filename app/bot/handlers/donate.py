import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, PreCheckoutQuery
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot import texts
from app.bot.handlers.common import show
from app.bot.sender import notify_payment_succeeded
from app.bot.states import Donate
from app.config import get_settings
from app.db.models import Fundraiser, User
from app.money import format_rub, parse_rub
from app.services.fundraisers import list_fundraisers
from app.services.payments import (
    PaymentError,
    SuccessfulPayment,
    apply_successful_payment,
    check_pre_checkout,
    create_invoice,
    invoice_params,
    mark_failed,
    payments_available,
)

log = logging.getLogger(__name__)
router = Router(name="donate")
# Платёжные события подключаются раньше всех, чтобы их не перехватил незавершённый диалог.
payment_router = Router(name="payments")


async def start_donation(message: Message, session: AsyncSession, state: FSMContext) -> None:
    await state.clear()
    if not payments_available():
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
    if not payments_available():
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
        f"до {format_rub(settings.max_amount_rub * 100)} — или выберите вариант ниже.\n\n"
        f"Получатель средств: {texts.esc(settings.recipient_name)}. "
        "Взнос добровольный и не является оплатой услуг.",
        reply_markup=kb.quick_amounts(),
    )
    await callback.answer()


async def _send_invoice(
    message: Message, bot: Bot, state: FSMContext, session: AsyncSession, user: User, amount: int
) -> None:
    data = await state.get_data()
    try:
        payment = await create_invoice(session, user, data.get("fundraiser_id", 0), amount)
    except PaymentError as exc:
        if "Минимальная" in str(exc) or "Максимальная" in str(exc):
            await message.answer(f"{exc} Введите другую сумму.")
        else:
            await state.clear()
            await message.answer(str(exc))
        return
    await state.clear()
    try:
        await bot.send_invoice(chat_id=message.chat.id, **invoice_params(payment))
    except TelegramBadRequest as exc:
        log.error("Telegram не принял счёт %s: %s", payment.id, exc)
        await mark_failed(session, payment)
        await message.answer("Не удалось выставить счёт на эту сумму. Попробуйте другую сумму или позже.")


@router.message(Donate.amount, F.text)
async def amount_entered(
    message: Message, bot: Bot, state: FSMContext, session: AsyncSession, db_user: User
) -> None:
    amount = parse_rub(message.text)
    if amount is None:
        await message.answer("Не получилось распознать сумму. Напишите число, например: 300 или 250,50.")
        return
    await _send_invoice(message, bot, state, session, db_user, amount)


@router.callback_query(Donate.amount, kb.AmountCb.filter())
async def amount_chosen(
    callback: CallbackQuery,
    callback_data: kb.AmountCb,
    bot: Bot,
    state: FSMContext,
    session: AsyncSession,
    db_user: User,
) -> None:
    await callback.answer()
    await _send_invoice(callback.message, bot, state, session, db_user, callback_data.rubles * 100)


@router.callback_query(kb.PayCb.filter(F.action == "cancel"))
async def payment_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await show(callback, "Взнос отменён.")


@payment_router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery, session: AsyncSession) -> None:
    # На ответ у бота 10 секунд; без ответа Telegram сам отменит оплату.
    error = await check_pre_checkout(
        session, query.from_user.id, query.invoice_payload, query.currency, query.total_amount
    )
    if error:
        await query.answer(ok=False, error_message=error)
    else:
        await query.answer(ok=True)


@payment_router.message(F.successful_payment)
async def successful_payment(message: Message, bot: Bot, session: AsyncSession) -> None:
    sp = message.successful_payment
    result = await apply_successful_payment(
        session,
        message.from_user.id,
        SuccessfulPayment(
            currency=sp.currency,
            total_amount=sp.total_amount,
            invoice_payload=sp.invoice_payload,
            telegram_payment_charge_id=sp.telegram_payment_charge_id,
            provider_payment_charge_id=sp.provider_payment_charge_id,
        ),
    )
    if result is None:
        await message.answer(
            "Оплата получена, но мы не нашли счёт. Администратор проверит платёж вручную — "
            "напишите, пожалуйста, в студсовет."
        )
    elif result.newly_succeeded:
        await notify_payment_succeeded(bot, session, result.payment)
    elif not result.duplicate:
        await message.answer("Оплата получена и передана на проверку администратору.")
