from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

from src.data import FinnhubQuote
from src.market import ET
from src.market_reports import (
    _fetch_macro_headlines,
    _quote_is_current,
    build_market_report,
)
from src.main import run_scheduled_report


class MarketReportQuoteTests(unittest.TestCase):
    def test_morning_report_requires_fresh_pre_market_quote(self) -> None:
        now = datetime(2026, 10, 26, 9, 0, tzinfo=ET)
        fresh = FinnhubQuote(601.0, 600.0, now - timedelta(seconds=30))
        stale = FinnhubQuote(601.0, 600.0, now - timedelta(minutes=6))
        prior_close = FinnhubQuote(
            600.0,
            599.0,
            datetime(2026, 10, 23, 16, 0, tzinfo=ET),
        )

        self.assertTrue(_quote_is_current(fresh, "morning", now))
        self.assertFalse(_quote_is_current(stale, "morning", now))
        self.assertFalse(_quote_is_current(prior_close, "morning", now))

    def test_eod_report_accepts_only_current_day_near_close_quote(self) -> None:
        now = datetime(2026, 10, 26, 16, 30, tzinfo=ET)
        close_quote = FinnhubQuote(
            601.0,
            600.0,
            datetime(2026, 10, 26, 16, 0, tzinfo=ET),
        )
        old_quote = FinnhubQuote(
            601.0,
            600.0,
            datetime(2026, 10, 26, 15, 0, tzinfo=ET),
        )

        self.assertTrue(_quote_is_current(close_quote, "eod", now))
        self.assertFalse(_quote_is_current(old_quote, "eod", now))


class MarketReportContentTests(unittest.TestCase):
    def test_morning_report_has_three_modules_and_never_uses_stale_prices(self) -> None:
        now = datetime(2026, 10, 26, 9, 0, tzinfo=ET)
        with (
            patch("src.market_reports._get_report_quotes", return_value={}),
            patch("src.market_reports._fetch_macro_headlines", return_value=[]),
        ):
            report = build_market_report("morning", now)

        self.assertEqual(report.count("\n---\n"), 2)
        self.assertEqual(report.count("## "), 3)
        self.assertIn("| Ticker | Pre-Market Price | Overnight Change (%) |", report)
        self.assertIn("| SPY | Unavailable | Unavailable |", report)
        self.assertIn("No specific catalyst is attributed", report)
        self.assertIn("pre-market options-tape feed is available", report)
        self.assertIn("Pre-market IV repricing cannot be verified", report)
        self.assertNotIn("Suggestion:", report)

    def test_macro_news_filters_to_recent_source_backed_catalysts(self) -> None:
        now = datetime(2026, 10, 26, 9, 0, tzinfo=ET)
        recent = int((now - timedelta(minutes=15)).timestamp())
        old = int((now - timedelta(days=2)).timestamp())
        response = Mock(status_code=200)
        response.json.return_value = [
            {
                "headline": "Fed signals rate pause",
                "datetime": recent,
                "source": "Newswire",
            },
            {
                "headline": "Old CPI story",
                "datetime": old,
                "source": "Newswire",
            },
            {
                "headline": "Unrelated company profile",
                "datetime": recent,
                "source": "Newswire",
            },
        ]
        with (
            patch.dict("os.environ", {"FINNHUB_API_KEY": "test-key"}),
            patch("src.market_reports.requests.get", return_value=response),
        ):
            headlines = _fetch_macro_headlines(now, "morning")

        self.assertEqual([item["headline"] for item in headlines], ["Fed signals rate pause"])

    def test_eod_report_summarizes_flow_and_prior_delivered_csp_alerts(self) -> None:
        now = datetime(2026, 10, 26, 16, 30, tzinfo=ET)
        quote = FinnhubQuote(101.0, 100.0, now - timedelta(minutes=5))
        quotes = {
            symbol: quote
            for symbol in (
                "SPY", "QQQ", "IWM", "SMH",
                "TQQQ", "TNA", "SOXL", "BITX",
                "IBIT", "GLD", "SLV", "USO",
            )
        }
        flow_setup = {
            "symbol": "IBIT",
            "strike": 40,
            "right": "CALL",
            "expiration": "2026-11-06",
            "buy_premium": 35_000,
        }
        with (
            patch("src.market_reports._get_report_quotes", return_value=quotes),
            patch("src.market_reports._fetch_macro_headlines", return_value=[]),
            patch("src.market_reports.get_daily_bars", return_value=None),
            patch("src.market_reports.build_context", return_value={}),
            patch("src.market_reports.scan_symbol", return_value=[flow_setup]),
            patch(
                "src.market_reports.alerts_between",
                return_value=[{"title": "CSP — TQQQ $45P"}],
            ),
        ):
            report = build_market_report("eod", now)

        self.assertIn("| Ticker | Close / Last Price | Day Change (%) |", report)
        self.assertIn("| SPY | $101.00 | +1.00% |", report)
        self.assertEqual(report.count("\n---\n"), 2)
        self.assertEqual(report.count("## "), 3)
        self.assertIn("IBIT $40C 2026-11-06 ($35,000 ask-side premium)", report)
        self.assertIn("CSP — TQQQ $45P", report)
        self.assertIn("not a fresh post-close IV quote", report)

    def test_invalid_report_type_and_naive_time_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_market_report("weekly")
        with self.assertRaises(ValueError):
            build_market_report("morning", datetime(2026, 10, 26, 9, 0))


class ScheduledMarketReportTests(unittest.TestCase):
    def test_morning_report_is_sent_once_and_flagged_only_on_success(self) -> None:
        when = datetime(2026, 10, 26, 9, 0, tzinfo=ET)
        with (
            patch("src.main.get_flag", return_value=None),
            patch("src.main.build_market_report", return_value="report"),
            patch("src.main.send_alert", return_value=True) as send_alert,
            patch("src.main.set_flag") as set_flag,
        ):
            result = run_scheduled_report("morning", when)

        self.assertTrue(result)
        send_alert.assert_called_once()
        set_flag.assert_called_once_with("market_report_morning", "2026-10-26")

    def test_failed_delivery_does_not_mark_report_as_sent(self) -> None:
        when = datetime(2026, 10, 26, 16, 30, tzinfo=ET)
        with (
            patch("src.main.get_flag", return_value=None),
            patch("src.main.build_market_report", return_value="report"),
            patch("src.main.send_alert", return_value=False),
            patch("src.main.set_flag") as set_flag,
        ):
            result = run_scheduled_report("eod", when)

        self.assertFalse(result)
        set_flag.assert_not_called()

    def test_report_does_not_repeat_after_daily_flag_is_set(self) -> None:
        when = datetime(2026, 10, 26, 9, 15, tzinfo=ET)
        with (
            patch("src.main.get_flag", return_value="2026-10-26"),
            patch("src.main.build_market_report") as build_report,
            patch("src.main.send_alert") as send_alert,
        ):
            result = run_scheduled_report("morning", when)

        self.assertFalse(result)
        build_report.assert_not_called()
        send_alert.assert_not_called()

    def test_report_skips_holiday_and_outside_schedule(self) -> None:
        with (
            patch("src.main.get_flag") as get_flag,
            patch("src.main.build_market_report") as build_report,
        ):
            holiday = run_scheduled_report(
                "morning", datetime(2026, 12, 25, 9, 0, tzinfo=ET)
            )
            outside = run_scheduled_report(
                "eod", datetime(2026, 10, 26, 16, 20, tzinfo=ET)
            )

        self.assertFalse(holiday)
        self.assertFalse(outside)
        get_flag.assert_not_called()
        build_report.assert_not_called()


if __name__ == "__main__":
    unittest.main()
