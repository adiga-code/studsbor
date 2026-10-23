from aiogram.fsm.state import State, StatesGroup


class Donate(StatesGroup):
    amount = State()


class Profile(StatesGroup):
    display_name = State()


class FundCreate(StatesGroup):
    title = State()
    description = State()
    goal = State()
    deadline = State()
    confirm = State()


class ExpenseAdd(StatesGroup):
    fundraiser = State()
    amount = State()
    description = State()
    receipt = State()
    date = State()
    confirm = State()


class MemberSearch(StatesGroup):
    query = State()


class Broadcast(StatesGroup):
    text = State()
    confirm = State()


class RefundFlow(StatesGroup):
    confirm = State()
