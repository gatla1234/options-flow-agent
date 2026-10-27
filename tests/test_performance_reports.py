from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from src import state
from src.discord_alerts import WEBHOOK_ENV_NAMES
from src.market import ET
from src.performance_reports import (
    build_daily_profit_summary,
    build_weekly_performance_report,
    last_trading_day_of_week,
    run_daily_profit_summary,
    run_weekly_performance_report,
)


def _closed(
    alert_id: int,
    scan_type: str,
    pnl: float,
    close_date: str,
    reason: str = "2R TARGET",
) -> dict:
    return {
        "alert_id": alert_id,
        "scan_type": scan_type,
        "ticker": "SPY",
        "closing_reason": reason,
        "pnl": pnl,
        "pnl_pct": 5.0,
        "close_date": close_date,
        "days_open": 3,
    }


def _mark(alert_id: int, scan_type: str, pnl: float | None) -> dict:
    return {
        "id": alert_id,
        "scan_type": scan_type,
        "group": scan_type,
        "symbol": "SPY",
        "entry_contract": 2.0,
        "pnl": pnl,
        "pnl_pct": 10.0 if pnl is not None else None,
        "days_open": 2,
        "target_progress": 0.5,
        "invalidation_status": "intact",
        "invalidation": 1.0,
        "current_underlying": 100.0,
        "max_profit": 200.0,
    }


class PerformanceReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "alerts.db"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _db(self):
        return patch.object(state, "DB_PATH", self.db_path)

    def test_dedicated_report_webhook_environment_names(self) -> None:
        self.assertEqual(WEBHOOK_ENV_NAMES["profit_summary"], "DISCORD_WEBHOOK_PROFIT_SUMMARY")
        self.assertEqual(WEBHOOK_ENV_NAMES["performance"], "DISCORD_WEBHOOK_PERFORMANCE")

    def test_weekly_periods_units_win_rate_and_monthly_data(self) -> None:
        now = datetime(2026, 10, 30, 17, 30, tzinfo=ET)
        closed = [
            _closed(1, "CONFIRMED FLOW", 10.0, "2025-12-10"),
            _closed(2, "CSP", 20.0, "2026-03-10"),
            _closed(3, "SWING", -2.0, "2026-04-10"),
            _closed(4, "DAY-TRADE FLOW", -5.0, "2026-10-10"),
        ]
        marks = [
            _mark(5, "flow", 50.0),
            _mark(6, "csp", 3.0),
            _mark(7, "swing", 4.0),
        ]
        report = "\n".join(
            build_weekly_performance_report(
                now,
                marks=marks,
                closed=closed,
                creation_dates=["2025-12-01T10:00:00-05:00"],
            )
        )
        self.assertIn("Options (per-contract $): realized +$15.00", report)
        self.assertIn("Options (per-contract $): realized +$25.00", report)
        self.assertIn("unrealized +$53.00", report)
        self.assertIn("Swing (per-share $): realized -$2.00", report)
        self.assertIn("Swing (per-share $): realized -$2.00 · unrealized +$4.00", report)
        self.assertIn("CSP: YTD realized +$20.00 per contract; win rate 1/1", report)
        self.assertIn("Overall win rate: YTD 1/3; all-time 2/4", report)
        self.assertIn("2025-01: no data", report)
        self.assertIn("2026-03: options +$20.00 per contract", report)
        self.assertIn(BASIS, report)
        self.assertNotIn("Options and swing combined", report)

    def test_daily_summary_separates_asset_totals_and_marks_missing_data(self) -> None:
        now = datetime(2026, 10, 27, 17, 15, tzinfo=ET)
        marks = [
            _mark(1, "flow", 100.0),
            _mark(2, "swing", -5.0),
            {**_mark(3, "csp", None), "entry_contract": 2.0},
            {**_mark(4, "flow", None), "entry_contract": None},
        ]
        closed = [
            _closed(10, "CONFIRMED FLOW", 50.0, "2026-10-27"),
            _closed(11, "SWING", 3.0, "2026-10-27"),
            _closed(12, "SWING", 100.0, "2026-10-27", reason="STALE"),
            _closed(13, "CSP", 20.0, "2026-10-26"),
        ]
        report = "\n".join(build_daily_profit_summary(now, marks, closed))
        self.assertIn("entry unavailable", report)
        self.assertIn("price unavailable; excluded from totals", report)
        self.assertIn("invalidation", report)
        self.assertIn("50.0% toward 2R", report)
        self.assertIn("closed today +$50.00", report)
        self.assertIn("Options (per-contract $): open +$100.00 · closed today +$50.00 · combined +$150.00", report)
        self.assertIn("Swing (per-share $): open -$5.00 · closed today +$3.00 · combined -$2.00", report)
        self.assertIn("excluded from win rate and combined totals", report)
        self.assertIn("Win rate today: 2/2", report)
        self.assertNotIn("+$148.00", report)

    def test_weekly_report_labels_missing_marks_and_closed_pnl(self) -> None:
        now = datetime(2026, 10, 30, 17, 30, tzinfo=ET)
        closed = [
            {
                **_closed(1, "CSP", 0.0, "2026-10-20"),
                "pnl": None,
            }
        ]
        report = "\n".join(
            build_weekly_performance_report(
                now,
                marks=[_mark(2, "flow", None)],
                closed=closed,
                creation_dates=["2026-10-01"],
            )
        )
        self.assertIn("realized +$0.00 (1 unavailable excluded)", report)
        self.assertIn("unrealized +$0.00 (1 unavailable excluded)", report)
        self.assertIn("1 unavailable P&L excluded", report)
        self.assertIn("unavailable P&L excluded: options 1, swing 0", report)

    def test_daily_report_missing_webhook_sends_one_health_note(self) -> None:
        now = datetime(2026, 10, 27, 17, 20, tzinfo=ET)
        with self._db():
            with (
                patch("src.performance_reports.WEBHOOKS", {"health": "health"}),
                patch("src.performance_reports.send_alert") as send,
            ):
                self.assertFalse(run_daily_profit_summary(now))
                self.assertFalse(run_daily_profit_summary(now))
                send.assert_called_once()
                self.assertIn(
                    "DISCORD_WEBHOOK_PROFIT_SUMMARY",
                    send.call_args.kwargs["description"],
                )
                self.assertEqual(send.call_args.kwargs["alert_type"], "health")

    def test_daily_report_retries_only_the_failed_part(self) -> None:
        now = datetime(2026, 10, 27, 17, 20, tzinfo=ET)
        with self._db():
            with (
                patch(
                    "src.performance_reports.WEBHOOKS",
                    {"profit_summary": "profit-url"},
                ),
                patch(
                    "src.performance_reports.build_daily_profit_summary",
                    return_value=["part one", "part two"],
                ),
                patch(
                    "src.performance_reports.send_alert",
                    side_effect=[True, False, True],
                ) as send,
            ):
                self.assertFalse(run_daily_profit_summary(now))
                self.assertTrue(run_daily_profit_summary(now))

        self.assertEqual(send.call_count, 3)
        self.assertEqual(
            [call.kwargs["description"] for call in send.call_args_list],
            ["part one", "part two", "part two"],
        )

    def test_daily_report_waits_for_queued_part_delivery(self) -> None:
        now = datetime(2026, 10, 27, 17, 20, tzinfo=ET)
        with self._db():
            state.enqueue_discord_alert(
                {
                    "route": "profit_summary",
                    "title": "Daily summary",
                    "description": "part one",
                    "color": 0x3498DB,
                    "fields": None,
                    "alert_type": "health",
                    "alert_key": "profit-summary:2026-10-27:1",
                    "symbol": None,
                    "price": None,
                    "payload": None,
                    "cross_stream_key": None,
                }
            )
            with (
                patch(
                    "src.performance_reports.WEBHOOKS",
                    {"profit_summary": "profit-url"},
                ),
                patch(
                    "src.performance_reports.build_daily_profit_summary",
                    return_value=["part one"],
                ),
                patch("src.performance_reports.send_alert") as send,
            ):
                self.assertFalse(run_daily_profit_summary(now))
                send.assert_not_called()
                self.assertIsNone(state.get_flag("profit-summary:2026-10-27"))

                state.remove_queued_discord_alert(
                    state.queued_discord_alerts()[0]["id"]
                )
                state.log_alert(
                    "profit-summary:2026-10-27:1",
                    "health",
                    None,
                    "Daily summary",
                )
                self.assertTrue(run_daily_profit_summary(now))
                send.assert_not_called()

    def test_weekly_report_missing_webhook_sends_one_health_note(self) -> None:
        now = datetime(2026, 10, 30, 17, 35, tzinfo=ET)
        with self._db():
            with (
                patch("src.performance_reports.WEBHOOKS", {"health": "health"}),
                patch("src.performance_reports.send_alert") as send,
            ):
                self.assertFalse(run_weekly_performance_report(now))
                self.assertFalse(run_weekly_performance_report(now))
                send.assert_called_once()
                self.assertIn(
                    "DISCORD_WEBHOOK_PERFORMANCE",
                    send.call_args.kwargs["description"],
                )
                self.assertEqual(send.call_args.kwargs["alert_type"], "health")

    def test_weekly_report_uses_last_trading_day_for_holiday_friday(self) -> None:
        self.assertEqual(
            last_trading_day_of_week(datetime(2026, 7, 3).date()).isoformat(),
            "2026-07-02",
        )
        now = datetime(2026, 7, 2, 17, 35, tzinfo=ET)
        with self._db():
            with (
                patch("src.performance_reports.WEBHOOKS", {"performance": "perf"}),
                patch("src.performance_reports.build_weekly_performance_report", return_value=["weekly"]),
                patch("src.performance_reports.send_alert", return_value=True) as send,
            ):
                self.assertTrue(run_weekly_performance_report(now))
                self.assertEqual(send.call_args.kwargs["stream"], "performance")

    def test_weekly_report_retries_only_the_failed_part(self) -> None:
        now = datetime(2026, 10, 30, 17, 35, tzinfo=ET)
        with self._db():
            with (
                patch(
                    "src.performance_reports.WEBHOOKS",
                    {"performance": "performance-url"},
                ),
                patch(
                    "src.performance_reports.build_weekly_performance_report",
                    return_value=["part one", "part two"],
                ),
                patch(
                    "src.performance_reports.send_alert",
                    side_effect=[True, False, True],
                ) as send,
            ):
                self.assertFalse(run_weekly_performance_report(now))
                self.assertTrue(run_weekly_performance_report(now))

        self.assertEqual(send.call_count, 3)
        self.assertEqual(
            [call.kwargs["description"] for call in send.call_args_list],
            ["part one", "part two", "part two"],
        )

    def test_no_trading_day_week_is_skipped(self) -> None:
        with patch("src.performance_reports.is_trading_day", return_value=False):
            self.assertIsNone(last_trading_day_of_week(datetime(2026, 10, 30).date()))


BASIS = (
    "Hypothetical — alert price = entry, 1 contract / 1 share, before "
    "commissions and spreads. Not brokerage P&L."
)


if __name__ == "__main__":
    unittest.main()
