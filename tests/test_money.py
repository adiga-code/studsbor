import pytest

from app.money import format_rub, from_api_value, parse_rub, progress_bar, to_api_value


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("500", 50000),
        ("1 000", 100000),
        ("250,50", 25050),
        ("99.9", 9990),
        ("300 ₽", 30000),
        ("300руб", 30000),
        ("150 р.", 15000),
        ("0", None),
        ("-100", None),
        ("10.123", None),
        ("abc", None),
        ("", None),
        ("1e3", None),
    ],
)
def test_parse_rub(text, expected):
    assert parse_rub(text) == expected


def test_format_rub():
    assert format_rub(50000) == "500 ₽"
    assert format_rub(123456789) == "1 234 567,89 ₽"
    assert format_rub(5) == "0,05 ₽"


def test_api_values_roundtrip():
    assert to_api_value(25050) == "250.50"
    assert to_api_value(7) == "0.07"
    assert from_api_value("250.50") == 25050
    assert from_api_value("100.00") == 10000


def test_progress_bar():
    assert progress_bar(50, 100).endswith("50%")
    assert progress_bar(300, 100).startswith("▰" * 10)
    assert progress_bar(1, 0) == ""
