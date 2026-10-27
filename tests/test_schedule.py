from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import patch

from src.main import run_scheduled_csp
from src.market import ET


class ScheduledCspTests(unittest.TestCase):
    def test_runs_morning_scan_once_per_trading_day(self) -> None:
        when = datetime(2026, 10, 26, 10, 15, tzinfo=ET)
        with (
            patch("src.main.get_flag", return_value=None),
            patch("src.main.set_flag") as set_flag,
            patch("src.main.run_csp") as run_csp,
        ):
            result = run_scheduled_csp(when)

        self.assertTrue(result)
        run_csp.assert_called_once_with()
        set_flag.assert_called_once_with("csp_morning", "2026-10-26")

    def test_runs_afternoon_scan_once_per_trading_day(self) -> None:
        when = datetime(2026, 10, 26, 16, 14, tzinfo=ET)
        with (
            patch("src.main.get_flag", return_value=None),
            patch("src.main.set_flag") as set_flag,
            patch("src.main.run_csp") as run_csp,
        ):
            result = run_scheduled_csp(when)

        self.assertTrue(result)
        run_csp.assert_called_once_with()
        set_flag.assert_called_once_with("csp_afternoon", "2026-10-26")

    def test_afternoon_scan_window_ends_at_1615(self) -> None:
        when = datetime(2026, 10, 26, 16, 15, tzinfo=ET)
        with (
            patch("src.main.get_flag") as get_flag,
            patch("src.main.run_csp") as run_csp,
        ):
            result = run_scheduled_csp(when)

        self.assertFalse(result)
        get_flag.assert_not_called()
        run_csp.assert_not_called()

    def test_does_not_repeat_scan_for_same_window(self) -> None:
        when = datetime(2026, 10, 26, 10, 30, tzinfo=ET)
        with (
            patch("src.main.get_flag", return_value="2026-10-26"),
            patch("src.main.set_flag") as set_flag,
            patch("src.main.run_csp") as run_csp,
        ):
            result = run_scheduled_csp(when)

        self.assertFalse(result)
        run_csp.assert_not_called()
        set_flag.assert_not_called()

    def test_skips_outside_scan_windows(self) -> None:
        when = datetime(2026, 10, 26, 13, 0, tzinfo=ET)
        with (
            patch("src.main.get_flag") as get_flag,
            patch("src.main.run_csp") as run_csp,
        ):
            result = run_scheduled_csp(when)

        self.assertFalse(result)
        get_flag.assert_not_called()
        run_csp.assert_not_called()

    def test_skips_weekends(self) -> None:
        when = datetime(2026, 10, 25, 10, 15, tzinfo=ET)
        with (
            patch("src.main.get_flag") as get_flag,
            patch("src.main.run_csp") as run_csp,
        ):
            result = run_scheduled_csp(when)

        self.assertFalse(result)
        get_flag.assert_not_called()
        run_csp.assert_not_called()

    def test_skips_afternoon_scan_after_early_market_close(self) -> None:
        when = datetime(2026, 11, 27, 15, 45, tzinfo=ET)
        with (
            patch("src.main.get_flag") as get_flag,
            patch("src.main.run_csp") as run_csp,
        ):
            result = run_scheduled_csp(when)

        self.assertFalse(result)
        get_flag.assert_not_called()
        run_csp.assert_not_called()


if __name__ == "__main__":
    unittest.main()
