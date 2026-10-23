from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.db.models import Expense, Fundraiser, FundraiserStatus
from app.money import format_rub, progress_bar

BTN_DONATE = "💳 Внести взнос"
BTN_FUNDRAISERS = "🎯 Наши сборы"
BTN_RATING = "🏆 Рейтинг"
BTN_REPORTS = "📊 Отчётность"
BTN_SETTINGS = "⚙️ Настройки"
BTN_ADMIN = "🛠 Админ-панель"

WELCOME = (
    "<b>Фонд студсовета</b>\n\n"
    "Поддержи мероприятия, реквизит и развитие студенческой жизни.\n\n"
    "Все взносы добровольные. Каждый платёж учитывается только после подтверждения "
    "платёжным сервисом, а поступления и расходы открыты в разделе «Отчётность»."
)

HELP = (
    "<b>Что умеет бот</b>\n\n"
    f"{BTN_DONATE} — поддержать общий фонд или конкретный проект любой суммой.\n"
    f"{BTN_FUNDRAISERS} — активные сборы, цели и прогресс.\n"
    f"{BTN_RATING} — топ-10 участников за месяц и за всё время.\n"
    f"{BTN_REPORTS} — поступления, расходы и чеки.\n"
    f"{BTN_SETTINGS} — имя в рейтинге, анонимность, напоминания.\n\n"
    "/menu — главное меню, /cancel — отменить текущее действие."
)

PAYMENTS_DISABLED = (
    "Приём взносов пока не подключён: схема получения средств согласовывается "
    "с университетом и платёжным сервисом. Мы сообщим, когда можно будет поддержать фонд.\n\n"
    "А пока можно посмотреть сборы и отчётность."
)

STATUS_TITLES = {
    "new": "создаётся",
    "pending": "ожидает оплаты",
    "waiting_for_capture": "ожидает подтверждения",
    "succeeded": "оплачен",
    "canceled": "отменён",
    "failed": "не создан",
    "review": "на проверке",
}

MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


def esc(value: object) -> str:
    return escape(str(value), quote=False)


def tz() -> ZoneInfo:
    return get_settings().tz


def fmt_date(value: datetime | None) -> str:
    return value.astimezone(tz()).strftime("%d.%m.%Y") if value else "—"


def fmt_datetime(value: datetime | None) -> str:
    return value.astimezone(tz()).strftime("%d.%m.%Y %H:%M") if value else "—"


def fundraiser_line(f: Fundraiser, collected: int) -> str:
    line = f"<b>{esc(f.title)}</b> — {format_rub(collected)}"
    if f.goal_amount:
        line += f" из {format_rub(f.goal_amount)}\n{progress_bar(collected, f.goal_amount)}"
    if f.ends_at:
        line += f"\nдо {fmt_date(f.ends_at)}"
    return line


def fundraiser_card(f: Fundraiser, collected: int, spent: int) -> str:
    if f.is_open:
        status = "открыт"
    elif f.status == FundraiserStatus.CLOSED:
        status = "закрыт"
    else:
        status = "срок истёк"
    parts = [f"<b>{esc(f.title)}</b>"]
    if f.description:
        parts.append(esc(f.description))
    parts.append("")
    parts.append(f"Собрано: <b>{format_rub(collected)}</b>")
    if f.goal_amount:
        parts.append(f"Цель: {format_rub(f.goal_amount)}")
        parts.append(progress_bar(collected, f.goal_amount))
    parts.append(f"Потрачено: {format_rub(spent)}")
    if f.ends_at:
        parts.append(f"Срок: до {fmt_date(f.ends_at)}")
    parts.append(f"Статус: {status}")
    return "\n".join(parts)


def expense_line(e: Expense) -> str:
    line = (
        f"{fmt_date(e.spent_at)} · <b>{format_rub(e.amount)}</b> · {esc(e.fundraiser.title)}\n"
        f"{esc(e.description[:300])}"
    )
    if e.receipt_url:
        line += f'\n<a href="{escape(e.receipt_url, quote=True)}">чек по ссылке</a>'
    if e.receipt_file_id:
        line += f"\nчек: /receipt_{e.id}"
    return line
