from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# NYSE full closures (observed dates). Update yearly.
NYSE_HOLIDAYS = {
    date(2025, 1, 1),
    date(2025, 1, 20),
    date(2025, 2, 17),
    date(2025, 4, 18),
    date(2025, 5, 26),
    date(2025, 6, 19),
    date(2025, 7, 4),
    date(2025, 9, 1),
    date(2025, 11, 27),
    date(2025, 12, 25),
    date(2026, 1, 1),
    date(2026, 1, 19),
    date(2026, 2, 16),
    date(2026, 4, 3),
    date(2026, 5, 25),
    date(2026, 6, 19),
    date(2026, 7, 3),
    date(2026, 9, 7),
    date(2026, 11, 26),
    date(2026, 12, 25),
    date(2027, 1, 1),
    date(2027, 1, 18),
    date(2027, 2, 15),
    date(2027, 3, 26),
    date(2027, 5, 31),
    date(2027, 6, 18),
    date(2027, 7, 5),
    date(2027, 9, 6),
    date(2027, 11, 25),
    date(2027, 12, 24),
}

NYSE_EARLY_CLOSES = {
    date(2025, 7, 3): time(13, 0),
    date(2025, 11, 28): time(13, 0),
    date(2025, 12, 24): time(13, 0),
    date(2026, 11, 27): time(13, 0),
    date(2026, 12, 24): time(13, 0),
    date(2027, 7, 2): time(13, 0),
    date(2027, 11, 26): time(13, 0),
}

MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)
OPTIONS_CLOSE = time(16, 15)
EARLY_OPTIONS_CLOSE = time(13, 15)
EOD_SCAN_END = time(16, 45)


def now_et() -> datetime:
    return datetime.now(ET)


def is_trading_day(day: date | None = None) -> bool:
    day = day or now_et().date()
    return day.weekday() < 5 and day not in NYSE_HOLIDAYS


def market_close_time(day: date) -> time:
    return NYSE_EARLY_CLOSES.get(day, MARKET_CLOSE)


def options_close_time(day: date) -> time:
    return EARLY_OPTIONS_CLOSE if day in NYSE_EARLY_CLOSES else OPTIONS_CLOSE


def is_rth(when: datetime | None = None) -> bool:
    when = when or now_et()
    if not is_trading_day(when.date()):
        return False
    t = when.time()
    return MARKET_OPEN <= t < market_close_time(when.date())


def is_eod_window(when: datetime | None = None) -> bool:
    """After the cash close, before we stop the daily swing/CSP pass."""
    when = when or now_et()
    if not is_trading_day(when.date()):
        return False
    t = when.time()
    return market_close_time(when.date()) <= t <= EOD_SCAN_END
