"""Business days: Monday to Friday without the TARGET closing days (world file §5)."""

from datetime import UTC, date, datetime

import pytest

from support_tools.business_days import (
    add_business_days,
    cet_date,
    easter_sunday,
    is_business_day,
    target_closing_days,
)


@pytest.mark.parametrize(
    ("year", "easter"),
    [(2024, date(2024, 3, 31)), (2025, date(2025, 4, 20)), (2026, date(2026, 4, 5)),
     (2027, date(2027, 3, 28))],
)  # fmt: skip
def test_easter_sunday(year: int, easter: date) -> None:
    assert easter_sunday(year) == easter


def test_the_target_closing_days_of_2026() -> None:
    assert target_closing_days(2026) == {
        date(2026, 1, 1),
        date(2026, 4, 3),
        date(2026, 4, 6),
        date(2026, 5, 1),
        date(2026, 12, 25),
        date(2026, 12, 26),
    }


def test_weekends_and_closing_days_are_not_business_days() -> None:
    assert is_business_day(date(2026, 11, 13))  # Friday
    assert not is_business_day(date(2026, 11, 14))  # Saturday
    assert not is_business_day(date(2026, 11, 15))  # Sunday
    assert not is_business_day(date(2026, 5, 1))  # Friday, closing day


def test_adding_business_days_skips_weekends_and_closing_days() -> None:
    assert add_business_days(date(2026, 11, 12), 1) == date(2026, 11, 13)
    assert add_business_days(date(2026, 11, 13), 1) == date(2026, 11, 16)
    # Thursday 24 Dec 2026 + 1: Fri 25 and Sat 26 closed, Sun 27 → Mon 28.
    assert add_business_days(date(2026, 12, 24), 1) == date(2026, 12, 28)
    assert add_business_days(date(2026, 11, 14), 0) == date(2026, 11, 14)


def test_a_negative_count_is_refused() -> None:
    with pytest.raises(ValueError, match="negative"):
        add_business_days(date(2026, 11, 12), -1)


def test_cet_date_follows_summer_time() -> None:
    # 22:30 UTC is the next day in CEST (+2) and in CET (+1).
    assert cet_date(datetime(2026, 7, 1, 22, 30, tzinfo=UTC)) == date(2026, 7, 2)
    assert cet_date(datetime(2026, 1, 1, 22, 30, tzinfo=UTC)) == date(2026, 1, 1)
    assert cet_date(datetime(2026, 1, 1, 23, 0, tzinfo=UTC)) == date(2026, 1, 2)
