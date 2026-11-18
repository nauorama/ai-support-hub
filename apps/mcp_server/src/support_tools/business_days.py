"""Business days and CET dates for the expected dates of the core mock (world file §5).

Business days are Monday to Friday without the closing days of the euro payment system (TARGET):
1 January, Good Friday, Easter Monday, 1 May, 25 and 26 December. "CET" in the world file means
Central European Time including summer time, so local dates and the SEPA cut-off use
Europe/Berlin.
"""

from datetime import date, datetime, timedelta
from functools import cache
from zoneinfo import ZoneInfo

CET = ZoneInfo("Europe/Berlin")


def easter_sunday(year: int) -> date:
    """Gregorian Easter Sunday (the anonymous Gregorian algorithm, "Meeus/Jones/Butcher")."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    el = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * el) // 451
    month, day = divmod(h + el - 7 * m + 114, 31)
    return date(year, month, day + 1)


@cache
def target_closing_days(year: int) -> frozenset[date]:
    easter = easter_sunday(year)
    return frozenset(
        {
            date(year, 1, 1),
            easter - timedelta(days=2),
            easter + timedelta(days=1),
            date(year, 5, 1),
            date(year, 12, 25),
            date(year, 12, 26),
        }
    )


def is_business_day(day: date) -> bool:
    return day.weekday() < 5 and day not in target_closing_days(day.year)


def add_business_days(start: date, count: int) -> date:
    """The `count`-th business day after `start` (`start` itself is not counted)."""
    if count < 0:
        raise ValueError("count must not be negative")
    day = start
    left = count
    while left > 0:
        day += timedelta(days=1)
        if is_business_day(day):
            left -= 1
    return day


def cet_date(moment: datetime) -> date:
    """The calendar date of an aware datetime in CET/CEST."""
    return moment.astimezone(CET).date()
