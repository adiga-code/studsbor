import re

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot import texts
from app.db.models import AdminRole, Expense

router = Router(name="common")


async def show(callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    """Редактирует сообщение с кнопками, а если это невозможно — отправляет новое."""
    try:
        await callback.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc):
            await callback.message.answer(text, reply_markup=markup, disable_web_page_preview=True)
    await callback.answer()


@router.message(CommandStart())
@router.message(Command("menu"))
async def cmd_start(message: Message, state: FSMContext, role: AdminRole | None = None) -> None:
    await state.clear()
    await message.answer(texts.WELCOME, reply_markup=kb.main_menu(is_admin=role is not None))


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(texts.HELP)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext, role: AdminRole | None = None) -> None:
    current = await state.get_state()
    await state.clear()
    await message.answer(
        "Действие отменено." if current else "Нечего отменять.",
        reply_markup=kb.main_menu(is_admin=role is not None),
    )


@router.message(Command(re.compile(r"receipt_(\d+)")))
async def cmd_receipt(message: Message, command: CommandObject, session: AsyncSession) -> None:
    expense = await session.get(Expense, int(command.regexp_match.group(1)))
    if expense is None or not expense.receipt_file_id:
        await message.answer("Чек не найден.")
        return
    caption = f"Чек к расходу #{expense.id}: {texts.esc(expense.description)[:900]}"
    if expense.receipt_file_type == "photo":
        await message.answer_photo(expense.receipt_file_id, caption=caption)
    else:
        await message.answer_document(expense.receipt_file_id, caption=caption)


@router.callback_query(F.data == "noop")
async def noop(callback: CallbackQuery) -> None:
    await callback.answer()
