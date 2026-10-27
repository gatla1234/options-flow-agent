from __future__ import annotations

import unittest
from datetime import date, datetime, time

from src.market import (
    ET,
    is_eod_window,
    is_rth,
    market_close_time,
    options_close_time,
)


class MarketCalendarTests(unittest.TestCase):
    def test_early_close_changes_stock_and_option_session_end(self) -> None:
        day = date(2026, 11, 27)
        self.assertEqual(market_close_time(day), time(13, 0))
        self.assertEqual(options_close_time(day), time(13, 15))
        self.assertTrue(is_rth(datetime(2026, 11, 27, 12, 59, tzinfo=ET)))
        self.assertFalse(is_rth(datetime(2026, 11, 27, 13, 0, tzinfo=ET)))
        self.assertTrue(is_eod_window(datetime(2026, 11, 27, 13, 0, tzinfo=ET)))

    def test_regular_session_close_times_are_unchanged(self) -> None:
        day = date(2026, 10, 27)
        self.assertEqual(market_close_time(day), time(16, 0))
        self.assertEqual(options_close_time(day), time(16, 15))
        self.assertTrue(is_rth(datetime(2026, 10, 27, 15, 59, tzinfo=ET)))
        self.assertFalse(is_rth(datetime(2026, 10, 27, 16, 0, tzinfo=ET)))


if __name__ == "__main__":
    unittest.main()
