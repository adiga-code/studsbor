"""Деньги хранятся в целых копейках. Здесь — разбор ввода и форматирование."""

import re
from decimal import Decimal, InvalidOperation

_CURRENCY_SUFFIX = re.compile(r"(₽|руб(лей|ля|ль)?\.?|р\.?)$", re.IGNORECASE)


def parse_rub(text: str) -> int | None:
    """Разбирает сумму в рублях из пользовательского ввода и возвращает копейки.

    Принимает «500», «1 000», «250,50», «99.9 ₽». Возвращает None для
    некорректного ввода, отрицательных чисел и дробей мельче копейки.
    """
    cleaned = text.strip().replace(" ", "").replace(" ", "")
    cleaned = _CURRENCY_SUFFIX.sub("", cleaned).replace(",", ".")
    if not re.fullmatch(r"\d+(\.\d{1,2})?", cleaned):
        return None
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    kopecks = int(value * 100)
    return kopecks if kopecks > 0 else None


def format_rub(kopecks: int) -> str:
    sign = "-" if kopecks < 0 else ""
    rubles, rest = divmod(abs(kopecks), 100)
    rubles_text = f"{rubles:,}".replace(",", " ")
    if rest:
        return f"{sign}{rubles_text},{rest:02d} ₽"
    return f"{sign}{rubles_text} ₽"


def to_api_value(kopecks: int) -> str:
    """Формат суммы для API ЮKassa: строка с двумя знаками после точки."""
    rubles, rest = divmod(kopecks, 100)
    return f"{rubles}.{rest:02d}"


def from_api_value(value: str) -> int:
    return int((Decimal(value) * 100).to_integral_exact())


def progress_bar(current: int, goal: int, width: int = 10) -> str:
    if goal <= 0:
        return ""
    ratio = max(0.0, min(current / goal, 1.0))
    filled = round(ratio * width)
    return "▰" * filled + "▱" * (width - filled) + f" {int(current * 100 / goal)}%"
