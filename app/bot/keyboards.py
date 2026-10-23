from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot import texts
from app.db.models import Fundraiser, User


class MenuCb(CallbackData, prefix="menu"):
    action: str  # donate


class DonateCb(CallbackData, prefix="don"):
    fundraiser_id: int


class AmountCb(CallbackData, prefix="amt"):
    rubles: int


class PayCb(CallbackData, prefix="pay"):
    action: str  # cancel


class FundCb(CallbackData, prefix="fund"):
    action: str  # list | view | expenses
    fundraiser_id: int = 0


class RatingCb(CallbackData, prefix="rat"):
    period: str  # month | all


class ReportCb(CallbackData, prefix="rep"):
    section: str  # summary | income | expenses | funds


class SettingsCb(CallbackData, prefix="set"):
    action: str  # name | anon | remind | period | history


class AdminCb(CallbackData, prefix="adm"):
    action: str


class AdminFundCb(CallbackData, prefix="admf"):
    action: str  # toggle
    fundraiser_id: int


class ExpenseFundCb(CallbackData, prefix="expf"):
    fundraiser_id: int


class MemberCb(CallbackData, prefix="mem"):
    action: str  # view | grant | revoke
    user_id: int


class ConfirmCb(CallbackData, prefix="cnf"):
    flow: str  # fund | expense | broadcast | refund
    ok: bool


QUICK_AMOUNTS = (100, 300, 500, 1000)


def main_menu(is_admin: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=texts.BTN_DONATE)],
        [KeyboardButton(text=texts.BTN_FUNDRAISERS), KeyboardButton(text=texts.BTN_RATING)],
        [KeyboardButton(text=texts.BTN_REPORTS), KeyboardButton(text=texts.BTN_SETTINGS)],
    ]
    if is_admin:
        rows.append([KeyboardButton(text=texts.BTN_ADMIN)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def choose_fundraiser(fundraisers: list[Fundraiser]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for f in fundraisers:
        kb.button(text=f.title, callback_data=DonateCb(fundraiser_id=f.id))
    kb.adjust(1)
    return kb.as_markup()


def quick_amounts() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for rub in QUICK_AMOUNTS:
        kb.button(text=f"{rub} ₽", callback_data=AmountCb(rubles=rub))
    kb.button(text="Отмена", callback_data=PayCb(action="cancel"))
    kb.adjust(len(QUICK_AMOUNTS), 1)
    return kb.as_markup()


def fundraisers_list(fundraisers: list[Fundraiser]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for f in fundraisers:
        kb.button(text=f.title, callback_data=FundCb(action="view", fundraiser_id=f.id))
    kb.adjust(1)
    return kb.as_markup()


def fundraiser_actions(f: Fundraiser, payments_enabled: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if f.is_open and payments_enabled:
        kb.button(text="💳 Поддержать", callback_data=DonateCb(fundraiser_id=f.id))
    kb.button(text="🧾 Расходы", callback_data=FundCb(action="expenses", fundraiser_id=f.id))
    kb.button(text="← Все сборы", callback_data=FundCb(action="list"))
    kb.adjust(1)
    return kb.as_markup()


def rating_tabs(current: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for period, title in (("month", "За месяц"), ("all", "За всё время")):
        mark = "• " if period == current else ""
        kb.button(text=f"{mark}{title}", callback_data=RatingCb(period=period))
    kb.adjust(2)
    return kb.as_markup()


def report_tabs() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Сводка", callback_data=ReportCb(section="summary"))
    kb.button(text="Поступления", callback_data=ReportCb(section="income"))
    kb.button(text="Расходы", callback_data=ReportCb(section="expenses"))
    kb.button(text="По сборам", callback_data=ReportCb(section="funds"))
    kb.adjust(2)
    return kb.as_markup()


def settings_menu(user: User) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Изменить имя", callback_data=SettingsCb(action="name"))
    kb.button(
        text="👀 Показывать меня в рейтинге" if user.is_anonymous else "🙈 Скрыть имя в рейтинге",
        callback_data=SettingsCb(action="anon"),
    )
    kb.button(
        text="🔕 Отключить напоминания" if user.reminders_enabled else "🔔 Включить напоминания",
        callback_data=SettingsCb(action="remind"),
    )
    if user.reminders_enabled:
        kb.button(
            text="📅 Напоминать раз в 3 месяца"
            if user.reminder_period_months == 1
            else "📅 Напоминать каждый месяц",
            callback_data=SettingsCb(action="period"),
        )
    kb.button(text="📜 Мои взносы", callback_data=SettingsCb(action="history"))
    kb.adjust(1)
    return kb.as_markup()


def reminder_actions() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=texts.BTN_DONATE, callback_data=MenuCb(action="donate"))
    kb.button(text="🔕 Больше не напоминать", callback_data=SettingsCb(action="remind_off"))
    kb.adjust(1)
    return kb.as_markup()


def admin_panel(is_owner: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📈 Статистика", callback_data=AdminCb(action="stats"))
    kb.button(text="➕ Создать сбор", callback_data=AdminCb(action="fund_create"))
    kb.button(text="🎯 Управление сборами", callback_data=AdminCb(action="funds"))
    kb.button(text="🧾 Добавить расход", callback_data=AdminCb(action="expense"))
    kb.button(text="👥 Участники", callback_data=AdminCb(action="members"))
    kb.button(text="📤 Экспорт CSV", callback_data=AdminCb(action="export"))
    kb.button(text="📣 Рассылка", callback_data=AdminCb(action="broadcast"))
    if is_owner:
        kb.button(text="🔑 Администраторы", callback_data=AdminCb(action="admins"))
    kb.adjust(2)
    return kb.as_markup()


def admin_funds(fundraisers: list[Fundraiser]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for f in fundraisers:
        icon = "🟢" if f.is_open else "⚪️"
        action = "закрыть" if f.is_open else "открыть"
        kb.button(
            text=f"{icon} {f.title[:40]} — {action}",
            callback_data=AdminFundCb(action="toggle", fundraiser_id=f.id),
        )
    kb.adjust(1)
    return kb.as_markup()


def expense_fundraisers(fundraisers: list[Fundraiser]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for f in fundraisers:
        kb.button(text=f.title, callback_data=ExpenseFundCb(fundraiser_id=f.id))
    kb.adjust(1)
    return kb.as_markup()


def confirm(flow: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Подтвердить", callback_data=ConfirmCb(flow=flow, ok=True))
    kb.button(text="Отмена", callback_data=ConfirmCb(flow=flow, ok=False))
    kb.adjust(2)
    return kb.as_markup()


def members_list(users: list[User]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for u in users:
        label = u.display_name or u.first_name or str(u.telegram_id)
        if u.username:
            label += f" (@{u.username})"
        kb.button(text=label[:60], callback_data=MemberCb(action="view", user_id=u.id))
    kb.adjust(1)
    return kb.as_markup()


def member_actions(user_id: int, role: str | None, can_manage: bool) -> InlineKeyboardMarkup | None:
    if not can_manage or role == "owner":
        return None
    kb = InlineKeyboardBuilder()
    if role == "admin":
        kb.button(text="Снять права администратора", callback_data=MemberCb(action="revoke", user_id=user_id))
    else:
        kb.button(text="Назначить администратором", callback_data=MemberCb(action="grant", user_id=user_id))
    return kb.as_markup()
