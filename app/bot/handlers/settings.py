from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot import texts
from app.bot.handlers.common import show
from app.bot.states import Profile
from app.db.models import User
from app.money import format_rub
from app.services import stats

router = Router(name="settings")

MAX_NAME_LENGTH = 32


def _settings_text(user: User) -> str:
    if user.reminders_enabled:
        period = "каждый месяц" if user.reminder_period_months == 1 else "раз в 3 месяца"
        reminders = f"включены ({period})"
    else:
        reminders = "выключены"
    return (
        "⚙️ <b>Настройки</b>\n\n"
        f"Имя в рейтинге: <b>{texts.esc(user.display_name or user.first_name or '—')}</b>\n"
        f"Показ в рейтинге: {'анонимно' if user.is_anonymous else 'с именем'}\n"
        f"Напоминания: {reminders}"
    )


async def menu_settings(message: Message, db_user: User) -> None:
    await message.answer(_settings_text(db_user), reply_markup=kb.settings_menu(db_user))


@router.callback_query(kb.SettingsCb.filter(F.action == "name"))
async def ask_name(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Profile.display_name)
    await callback.message.answer(
        f"Как подписать вас в рейтинге? Напишите имя до {MAX_NAME_LENGTH} символов. /cancel — отмена."
    )
    await callback.answer()


@router.message(Profile.display_name, F.text)
async def set_name(message: Message, state: FSMContext, session: AsyncSession, db_user: User) -> None:
    name = " ".join(message.text.split())
    if not name or name.startswith("/") or len(name) > MAX_NAME_LENGTH:
        await message.answer(f"Имя должно быть от 1 до {MAX_NAME_LENGTH} символов.")
        return
    db_user.display_name = name
    await session.commit()
    await state.clear()
    await message.answer(_settings_text(db_user), reply_markup=kb.settings_menu(db_user))


@router.callback_query(kb.SettingsCb.filter(F.action.in_({"anon", "remind", "period", "remind_off"})))
async def toggle(
    callback: CallbackQuery, callback_data: kb.SettingsCb, session: AsyncSession, db_user: User
) -> None:
    action = callback_data.action
    if action == "anon":
        db_user.is_anonymous = not db_user.is_anonymous
    elif action == "remind":
        db_user.reminders_enabled = not db_user.reminders_enabled
    elif action == "period":
        db_user.reminder_period_months = 3 if db_user.reminder_period_months == 1 else 1
    elif action == "remind_off":
        db_user.reminders_enabled = False
    await session.commit()
    if action == "remind_off":
        await callback.answer("Напоминания отключены. Включить их снова можно в настройках.", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
        return
    await show(callback, _settings_text(db_user), kb.settings_menu(db_user))


@router.callback_query(kb.SettingsCb.filter(F.action == "history"))
async def history(callback: CallbackQuery, session: AsyncSession, db_user: User) -> None:
    payments = await stats.user_payments(session, db_user.id, limit=15)
    total = await stats.user_place(session, db_user.id)
    if not payments:
        text = "У вас пока нет взносов."
    else:
        lines = [
            f"{texts.fmt_date(p.paid_at)} · {format_rub(p.amount)} · {texts.esc(p.fundraiser.title)}"
            + (f" (возврат {format_rub(p.refunded_amount)})" if p.refunded_amount else "")
            for p in payments
        ]
        text = "📜 <b>Мои взносы</b>\n\n" + "\n".join(lines) + f"\n\nВсего: <b>{format_rub(total.total)}</b>"
    await show(callback, text, kb.settings_menu(db_user))
