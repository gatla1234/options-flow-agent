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
}

MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)
EOD_SCAN_END = time(16, 45)


def now_et() -> datetime:
    return datetime.now(ET)


def is_trading_day(day: date | None = None) -> bool:
    day = day or now_et().date()
    return day.weekday() < 5 and day not in NYSE_HOLIDAYS


def is_rth(when: datetime | None = None) -> bool:
    when = when or now_et()
    if not is_trading_day(when.date()):
        return False
    t = when.time()
    return MARKET_OPEN <= t < MARKET_CLOSE


def is_eod_window(when: datetime | None = None) -> bool:
    """After the cash close, before we stop the daily swing/CSP pass."""
    when = when or now_et()
    if not is_trading_day(when.date()):
        return False
    t = when.time()
    return MARKET_CLOSE <= t <= EOD_SCAN_END
