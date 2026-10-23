"""Кнопки главного меню. Роутер подключается первым, поэтому кнопка меню
прерывает любой незавершённый диалог (ввод суммы, создание сбора и т. п.)."""

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.filters import IsAdmin
from app.bot.handlers import admin, donate, public, settings
from app.db.models import AdminRole, User

router = Router(name="menu")


@router.message(F.text == texts.BTN_DONATE)
async def menu_donate(message: Message, session: AsyncSession, state: FSMContext) -> None:
    await donate.start_donation(message, session, state)


@router.message(F.text == texts.BTN_FUNDRAISERS)
async def menu_fundraisers(message: Message, session: AsyncSession, state: FSMContext) -> None:
    await state.clear()
    await public.menu_fundraisers(message, session)


@router.message(F.text == texts.BTN_RATING)
async def menu_rating(message: Message, session: AsyncSession, state: FSMContext, db_user: User) -> None:
    await state.clear()
    await public.menu_rating(message, session, db_user)


@router.message(F.text == texts.BTN_REPORTS)
async def menu_reports(message: Message, session: AsyncSession, state: FSMContext) -> None:
    await state.clear()
    await public.menu_reports(message, session)


@router.message(F.text == texts.BTN_SETTINGS)
async def menu_settings(message: Message, state: FSMContext, db_user: User) -> None:
    await state.clear()
    await settings.menu_settings(message, db_user)


@router.message(F.text == texts.BTN_ADMIN, IsAdmin())
async def menu_admin(message: Message, state: FSMContext, role: AdminRole) -> None:
    await admin.admin_panel(message, state, role)
