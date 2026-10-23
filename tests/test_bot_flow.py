from sqlalchemy import select

from app.bot import keyboards as kb
from app.bot import texts
from app.db.models import Admin, Expense, Fundraiser, Payment, PaymentStatus, User
from app.services.fundraisers import ensure_general_fund
from tests.helpers import callback_update, make_bot, make_dispatcher, message_update

OWNER = 1000
STUDENT = 42


async def test_donation_flow(maker, gateway):
    async with maker() as s:
        fund = await ensure_general_fund(s)
    bot, api = make_bot()
    dp = make_dispatcher()

    await dp.feed_update(bot, message_update(STUDENT, "/start"))
    assert "Фонд студсовета" in api.texts()[-1]

    await dp.feed_update(bot, message_update(STUDENT, texts.BTN_DONATE))
    choose = api.last("SendMessage")
    assert choose.reply_markup.inline_keyboard[0][0].callback_data == kb.DonateCb(fundraiser_id=fund.id).pack()

    await dp.feed_update(bot, callback_update(STUDENT, kb.DonateCb(fundraiser_id=fund.id).pack()))
    await dp.feed_update(bot, message_update(STUDENT, "abc"))
    assert "Не получилось распознать" in api.texts()[-1]
    await dp.feed_update(bot, message_update(STUDENT, "5"))
    assert "Минимальная сумма" in api.texts()[-1]
    await dp.feed_update(bot, message_update(STUDENT, "250,50"))
    confirm = api.texts()[-1]
    assert "250,50" in confirm and "Тестовый получатель" in confirm and "не является оплатой услуг" in confirm

    await dp.feed_update(bot, callback_update(STUDENT, kb.PayCb(action="confirm").pack()))
    pay_msg = api.last("EditMessageText")
    url_button = pay_msg.reply_markup.inline_keyboard[0][0]
    assert url_button.url == "https://yoomoney.test/yk-1"

    # Повторное нажатие «Перейти к оплате» не создаёт второй платёж.
    await dp.feed_update(bot, callback_update(STUDENT, kb.PayCb(action="confirm").pack()))
    assert gateway.create_calls == 1

    async with maker() as s:
        payment = await s.scalar(select(Payment))
        assert (payment.amount, payment.status) == (25050, PaymentStatus.PENDING)

    # Пользователь вернулся до оплаты — статус ещё «ожидает оплаты».
    check = kb.PayCheckCb(payment_id=payment.id).pack()
    await dp.feed_update(bot, callback_update(STUDENT, check))
    assert "ожидает оплаты" in api.last("AnswerCallbackQuery").text

    gateway.succeed("yk-1")
    await dp.feed_update(bot, callback_update(STUDENT, check))
    assert any("Спасибо" in t for t in api.texts())
    # Повторная проверка не присылает второе «спасибо».
    await dp.feed_update(bot, callback_update(STUDENT, check))
    assert sum("Спасибо" in t for t in api.texts()) == 1

    # Чужой платёж проверить нельзя.
    await dp.feed_update(bot, callback_update(7, check))
    assert api.last("AnswerCallbackQuery").text == "Платёж не найден."

    await dp.feed_update(bot, message_update(STUDENT, texts.BTN_RATING))
    assert "Анна — 250,50" in api.texts()[-1]
    await dp.feed_update(bot, message_update(STUDENT, texts.BTN_REPORTS))
    assert "250,50" in api.texts()[-1]


async def test_menu_button_interrupts_dialog(maker, gateway):
    async with maker() as s:
        fund = await ensure_general_fund(s)
    bot, api = make_bot()
    dp = make_dispatcher()
    await dp.feed_update(bot, callback_update(STUDENT, kb.DonateCb(fundraiser_id=fund.id).pack()))
    await dp.feed_update(bot, message_update(STUDENT, texts.BTN_RATING))
    assert "Топ-10" in api.texts()[-1]
    await dp.feed_update(bot, message_update(STUDENT, "500"))
    assert gateway.create_calls == 0
    assert not any("Проверьте данные" in t for t in api.texts())


async def test_settings(maker):
    bot, api = make_bot()
    dp = make_dispatcher()
    await dp.feed_update(bot, message_update(STUDENT, texts.BTN_SETTINGS))
    await dp.feed_update(bot, callback_update(STUDENT, kb.SettingsCb(action="name").pack()))
    await dp.feed_update(bot, message_update(STUDENT, "  Анна   из ИФ "))
    await dp.feed_update(bot, callback_update(STUDENT, kb.SettingsCb(action="anon").pack()))
    await dp.feed_update(bot, callback_update(STUDENT, kb.SettingsCb(action="period").pack()))
    await dp.feed_update(bot, callback_update(STUDENT, kb.SettingsCb(action="remind_off").pack()))
    async with maker() as s:
        user = await s.scalar(select(User).where(User.telegram_id == STUDENT))
    assert user.display_name == "Анна из ИФ"
    assert user.is_anonymous and user.reminder_period_months == 3 and not user.reminders_enabled


async def test_admin_access_and_fundraiser_creation(maker):
    async with maker() as s:
        await ensure_general_fund(s)
    bot, api = make_bot()
    dp = make_dispatcher()

    # Обычный студент не получает панель.
    await dp.feed_update(bot, message_update(STUDENT, "/admin"))
    assert not any("Панель администратора" in t for t in api.texts())

    await dp.feed_update(bot, message_update(OWNER, "/admin", "Владелец"))
    assert "Панель администратора" in api.texts()[-1]

    for text in ("/fund_create", "Кубок первокурсника", "Призы и аренда зала", "15000", "-"):
        await dp.feed_update(bot, message_update(OWNER, text, "Владелец"))
    assert "Проверьте сбор" in api.texts()[-1]
    await dp.feed_update(bot, callback_update(OWNER, kb.ConfirmCb(flow="fund", ok=True).pack(), "Владелец"))

    async with maker() as s:
        fund = await s.scalar(select(Fundraiser).where(Fundraiser.title == "Кубок первокурсника"))
        assert fund.goal_amount == 1_500_000 and fund.is_open

    # Расход с чеком-ссылкой.
    await dp.feed_update(bot, message_update(OWNER, "/expense_add", "Владелец"))
    await dp.feed_update(bot, callback_update(OWNER, kb.ExpenseFundCb(fundraiser_id=fund.id).pack(), "Владелец"))
    for text in ("1200", "Кубки", "https://check.example/1", "-"):
        await dp.feed_update(bot, message_update(OWNER, text, "Владелец"))
    await dp.feed_update(bot, callback_update(OWNER, kb.ConfirmCb(flow="expense", ok=True).pack(), "Владелец"))
    async with maker() as s:
        expense = await s.scalar(select(Expense))
        assert (expense.amount, expense.receipt_url) == (120000, "https://check.example/1")

    # Владелец назначает студента администратором.
    async with maker() as s:
        student = await s.scalar(select(User).where(User.telegram_id == STUDENT))
    grant = kb.MemberCb(action="grant", user_id=student.id).pack()
    await dp.feed_update(bot, callback_update(OWNER, grant, "Владелец"))
    async with maker() as s:
        assert (await s.get(Admin, student.id)).role == "admin"

    # Администратор не может назначать других и делать возвраты.
    await dp.feed_update(bot, callback_update(STUDENT, grant))
    assert "только владелец" in api.last("AnswerCallbackQuery").text
    await dp.feed_update(bot, message_update(STUDENT, "/refund 1"))
    assert "только владелец" in api.texts()[-1]

    await dp.feed_update(bot, message_update(STUDENT, "/export"))
    assert api.last("SendDocument").document.filename.startswith("expenses_")


async def test_receipt_command(maker):
    async with maker() as s:
        fund = await ensure_general_fund(s)
        user = await s.scalar(select(User)) or User(telegram_id=5, display_name="Казначей")
        s.add(user)
        await s.flush()
        s.add(Expense(fundraiser_id=fund.id, amount=1000, description="Шары", created_by=user.id,
                      receipt_file_id="PHOTO_ID", receipt_file_type="photo"))
        await s.commit()
    bot, api = make_bot()
    dp = make_dispatcher()
    await dp.feed_update(bot, message_update(STUDENT, "/receipt_1"))
    assert api.last("SendPhoto").photo == "PHOTO_ID"
    await dp.feed_update(bot, message_update(STUDENT, "/receipt_999"))
    assert api.texts()[-1] == "Чек не найден."
