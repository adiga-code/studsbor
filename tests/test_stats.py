from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.db.models import NotificationLog, Payment, PaymentStatus
from app.services import notifications as n
from app.services import stats
from app.services.expenses import add_expense
from app.services.fundraisers import close_expired, create_fundraiser, ensure_general_fund, list_fundraisers
from app.services.users import get_or_create_user

MSK = ZoneInfo("Europe/Moscow")


async def _paid(session, user, fund, amount, paid_at):
    payment = Payment(
        user_id=user.id, fundraiser_id=fund.id, amount=amount, invoice_payload=f"k{amount}{paid_at:%s}{user.id}",
        status=PaymentStatus.SUCCEEDED, paid_at=paid_at, description="",
    )
    session.add(payment)
    await session.commit()
    return payment


def test_month_bounds_moscow():
    start, end = stats.month_bounds(datetime(2026, 12, 31, 21, 30, tzinfo=UTC), MSK)
    # 31.12 21:30 UTC — это уже 1 января по Москве.
    assert (start.year, start.month) == (2027, 1)
    assert start.astimezone(UTC) == datetime(2026, 12, 31, 21, 0, tzinfo=UTC)
    assert (end.year, end.month) == (2027, 2)


async def test_monthly_and_all_time_rating(session):
    fund = await ensure_general_fund(session)
    anna = await get_or_create_user(session, 1, "anna", "Анна")
    boris = await get_or_create_user(session, 2, "boris", "Борис")
    vera = await get_or_create_user(session, 3, "vera", "Вера")
    vera.is_anonymous = True
    await session.commit()

    # 30.09 21:30 UTC = 01.10 00:30 МСК — платёж относится к октябрю.
    await _paid(session, anna, fund, 10000, datetime(2026, 9, 30, 21, 30, tzinfo=UTC))
    await _paid(session, boris, fund, 50000, datetime(2026, 9, 30, 20, 30, tzinfo=UTC))  # сентябрь
    await _paid(session, vera, fund, 20000, datetime(2026, 10, 15, 12, 0, tzinfo=UTC))

    since, until = stats.month_bounds(datetime(2026, 10, 20, tzinfo=UTC), MSK)
    october = await stats.rating(session, since, until)
    assert [(r.name, r.total) for r in october] == [("Анонимный участник", 20000), ("Анна", 10000)]

    all_time = await stats.rating(session)
    assert [r.name for r in all_time] == ["Борис", "Анонимный участник", "Анна"]
    assert [r.place for r in all_time] == [1, 2, 3]

    place = await stats.user_place(session, anna.id, since, until)
    assert (place.place, place.total) == (2, 10000)
    assert (await stats.user_place(session, boris.id, since, until)).place is None


async def test_rating_limit_and_ties(session):
    fund = await ensure_general_fund(session)
    users = [await get_or_create_user(session, 100 + i, None, f"U{i}") for i in range(12)]
    for u in users:
        await _paid(session, u, fund, 1000, datetime(2026, 10, 1, 12, tzinfo=UTC))
    top = await stats.rating(session)
    assert len(top) == 10
    assert [r.user_id for r in top] == [u.id for u in users[:10]]
    assert (await stats.user_place(session, users[11].id)).place == 12


async def test_totals_and_expenses(session):
    fund = await ensure_general_fund(session)
    user = await get_or_create_user(session, 1, None, "Анна")
    project = await create_fundraiser(session, "Кубок", "Призы", 500000, None, user)
    now = datetime.now(UTC)
    await _paid(session, user, fund, 30000, now)
    await _paid(session, user, project, 70000, now)
    await add_expense(
        session, fundraiser_id=project.id, amount=25000, description="Медали", spent_at=now,
        created_by=user, receipt_url="https://example.com/check",
    )
    t = await stats.totals(session)
    assert (t.income, t.expenses, t.balance, t.payers, t.payments_count) == (100000, 25000, 75000, 1, 2)
    assert await stats.fundraiser_summary(session, project) == (70000, 25000)
    expenses = await stats.recent_expenses(session, fundraiser_id=project.id)
    assert expenses[0].fundraiser.title == "Кубок"


async def test_close_expired(session):
    user = await get_or_create_user(session, 1, None, "Анна")
    await ensure_general_fund(session)
    await create_fundraiser(session, "Старый", "", None, datetime.now(UTC) - timedelta(hours=1), user)
    await create_fundraiser(session, "Новый", "", None, datetime.now(UTC) + timedelta(days=5), user)
    assert await close_expired(session) == 1
    assert [f.title for f in await list_fundraisers(session, only_open=True)] == [
        "Общий фонд студсовета", "Новый",
    ]


async def test_reminder_schedule(session):
    now = datetime(2026, 10, 5, 9, tzinfo=UTC)
    old = datetime(2026, 8, 1, tzinfo=UTC)
    newcomer = await get_or_create_user(session, 1, None, "Новичок")
    monthly = await get_or_create_user(session, 2, None, "Ежемесячно")
    quarterly = await get_or_create_user(session, 3, None, "Раз в квартал")
    disabled = await get_or_create_user(session, 4, None, "Без напоминаний")
    reminded = await get_or_create_user(session, 5, None, "Уже получил")
    blocked = await get_or_create_user(session, 6, None, "Заблокировал")
    newcomer.created_at = now
    for u in (monthly, quarterly, disabled, reminded, blocked):
        u.created_at = old
    quarterly.reminder_period_months = 3
    disabled.reminders_enabled = False
    blocked.is_blocked = True
    session.add_all(
        [
            NotificationLog(user_id=quarterly.id, notification_type=n.REMINDER, status=n.STATUS_SENT,
                            sent_at=datetime(2026, 9, 1, 9, tzinfo=UTC)),
            NotificationLog(user_id=monthly.id, notification_type=n.REMINDER, status=n.STATUS_SENT,
                            sent_at=datetime(2026, 9, 1, 9, tzinfo=UTC)),
            NotificationLog(user_id=reminded.id, notification_type=n.REMINDER, status=n.STATUS_SENT,
                            sent_at=datetime(2026, 10, 1, 9, tzinfo=UTC)),
            # Неудачная отправка не считается напоминанием.
            NotificationLog(user_id=monthly.id, notification_type=n.REMINDER, status=n.STATUS_FAILED,
                            sent_at=datetime(2026, 10, 1, 9, tzinfo=UTC)),
        ]
    )
    await session.commit()

    due = await n.users_due_for_reminder(session, now, MSK, reminder_day=1)
    assert [u.display_name for u in due] == ["Ежемесячно"]
    assert await n.users_due_for_reminder(session, now, MSK, reminder_day=10) == []
    later = await n.users_due_for_reminder(session, datetime(2026, 12, 2, tzinfo=UTC), MSK)
    assert {u.display_name for u in later} == {"Ежемесячно", "Раз в квартал", "Уже получил", "Новичок"}
