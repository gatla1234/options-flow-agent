from __future__ import annotations

import unittest
from datetime import datetime, timedelta

import polars as pl

from src.data import ET
from src.swing_signals import _latest_completed_bar


class PriorDayLevelTests(unittest.TestCase):
    def test_latest_completed_bar_skips_current_day_bar(self) -> None:
        today = datetime.now(ET).date()
        bars = pl.DataFrame(
            [
                {
                    "created": today - timedelta(days=2),
                    "high": 105.0,
                    "low": 95.0,
                },
                {"created": today, "high": 110.0, "low": 99.0},
            ]
        )

        bar = _latest_completed_bar(bars)

        self.assertEqual(bar["high"], 105.0)
        self.assertEqual(bar["low"], 95.0)

    def test_missing_date_column_fails_closed(self) -> None:
        bars = pl.DataFrame([{"high": 105.0, "low": 95.0}])

        self.assertIsNone(_latest_completed_bar(bars))


if __name__ == "__main__":
    unittest.main()
