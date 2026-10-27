from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

from src import scan_summary, state
from src.market import ET


class DailyScanSummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        patcher = patch.object(state, "DB_PATH", Path(temp_dir.name) / "alerts.db")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _log_flow_data(self) -> date:
        day = date(2026, 10, 26)
        state.log_funnel_scan(
            "confirmed",
            "SPY",
            {
                "contracts": 120,
                "dte": 100,
                "delta": 90,
                "strike": 80,
                "volume_oi": 75,
                "premium": 60,
                "buy_pct": 50,
                "trend": 10,
                "confirmation": 5,
            },
            data_available=True,
            scanned_at=datetime(2026, 10, 26, 10, 0, tzinfo=ET),
        )
        state.log_funnel_scan(
            "confirmed",
            "QQQ",
            {
                "contracts": 12,
                "dte": 10,
                "delta": 8,
                "strike": 7,
                "volume_oi": 6,
                "premium": 5,
                "buy_pct": 4,
                "trend": 3,
                "confirmation": 2,
            },
            data_available=True,
            scanned_at=datetime(2026, 10, 26, 10, 0, tzinfo=ET),
        )
        state.log_alert(
            "flow:SPY:call",
            "flow",
            "SPY",
            "CONFIRMED CALL",
            {
                "stream": "confirmed",
                "right": "CALL",
                "strike": 600,
                "expiration": "2026-11-06",
            },
            created_at=datetime(2026, 10, 26, 10, 5, tzinfo=ET),
        )
        state.log_alert(
            "followup:1:flow-average",
            "flow",
            "SPY",
            "FOLLOW-UP",
            {
                "stream": "confirmed",
                "right": "CALL",
                "strike": 600,
                "expiration": "2026-11-06",
                "is_followup": True,
                "followup_event": "flow-average:2026-10-26",
            },
            created_at=datetime(2026, 10, 26, 10, 6, tzinfo=ET),
        )
        state.log_funnel_scan(
            "swing",
            "TQQQ",
            {
                "candidates": 4,
                "trend": 2,
                "volume": 2,
                "relative_strength": 2,
                "confirmation": 1,
                "risk_reward": 1,
            },
            data_available=True,
            scanned_at=datetime(2026, 10, 26, 16, 5, tzinfo=ET),
        )
        state.log_alert(
            "swing:TQQQ:breakdown",
            "swing",
            "TQQQ",
            "BREAKDOWN",
            {"direction": "BEARISH"},
            created_at=datetime(2026, 10, 26, 16, 10, tzinfo=ET),
        )
        state.log_funnel_scan(
            "csp",
            "BITX",
            {
                "ownership_entered": 1,
                "ownership": 1,
                "candidates": 2,
                "contract": 2,
                "max_loss": 2,
                "otm": 2,
                "premium": 2,
                "spread": 1,
                "delta": 1,
            },
            data_available=True,
            scanned_at=datetime(2026, 10, 26, 15, 40, tzinfo=ET),
        )
        state.log_alert(
            "csp:BITX:put",
            "csp",
            "BITX",
            "CSP",
            {"strike": 30, "expiration": "2026-11-30"},
            created_at=datetime(2026, 10, 26, 15, 45, tzinfo=ET),
        )
        state.log_alert(
            "followup:QQQ:news",
            "flow",
            "QQQ",
            "FOLLOW-UP",
            {
                "stream": "confirmed",
                "right": "CALL",
                "strike": 400,
                "expiration": "2026-11-06",
                "is_followup": True,
                "followup_event": "news:article-1",
            },
            created_at=datetime(2026, 10, 26, 11, 0, tzinfo=ET),
        )
        return day

    def test_aggregates_ticker_alerts_followups_and_blockers(self) -> None:
        day = self._log_flow_data()

        summary = scan_summary.build_daily_scan_summary(day)
        message = "\n".join(summary["messages"]["confirmed"])

        self.assertIn("Candidates considered: 120", message)
        self.assertIn("trend gate blocked 40 of 50", message)
        self.assertIn("Alert #1: CALL · $600C exp 2026-11-06 · 10:05 ET", message)
        self.assertIn("Follow-ups: 1 (1 AVERAGE)", message)
        self.assertIn(
            "Follow-up #2: AVERAGE · CALL · $600C exp 2026-11-06 · 10:06 ET",
            message,
        )
        self.assertIn("QQQ CONFIRMED: no alerts", message)
        self.assertIn("Follow-ups: 1 (1 NEWS)", message)
        self.assertEqual(summary["totals"]["confirmed"]["alerts"], 1)
        self.assertEqual(summary["totals"]["confirmed"]["followups"], 2)
        swing_message = "\n".join(summary["messages"]["swing"])
        self.assertIn("TQQQ SWING:", swing_message)
        self.assertIn("Alert #3: BEARISH · underlying · 16:10 ET", swing_message)
        csp_message = "\n".join(summary["messages"]["csp"])
        self.assertIn("BITX CSP:", csp_message)
        self.assertIn("Alert #4: PUT · $30P exp 2026-11-30 · 15:45 ET", csp_message)

    def test_long_section_splits_without_losing_alert_lines(self) -> None:
        day = self._log_flow_data()
        for index in range(25):
            state.log_alert(
                f"flow:SPY:extra:{index}",
                "flow",
                "SPY",
                "CONFIRMED PUT",
                {
                    "stream": "confirmed",
                    "right": "PUT",
                    "strike": 590 - index,
                    "expiration": "2026-11-06",
                },
                created_at=datetime(2026, 10, 26, 11, index, tzinfo=ET),
            )

        with patch.object(scan_summary, "SCAN_SUMMARY_MESSAGE_LIMIT", 300):
            summary = scan_summary.build_daily_scan_summary(day)

        messages = summary["messages"]["confirmed"]
        self.assertGreater(len(messages), 1)
        self.assertTrue(all(len(message) <= 300 for message in messages))
        combined = "\n".join(messages)
        for alert_id in [1, 2, *range(5, 30)]:
            self.assertIn(f"#{alert_id}:", combined)

    def test_missing_webhook_sends_one_health_note_and_skips(self) -> None:
        when = datetime(2026, 10, 26, 17, 0, tzinfo=ET)
        with (
            patch.object(scan_summary, "WEBHOOKS", {"health": "health-url"}),
            patch("src.scan_summary.send_alert", return_value=True) as send,
        ):
            self.assertFalse(scan_summary.run_daily_scan_summary(when))
            self.assertFalse(scan_summary.run_daily_scan_summary(when))

        send.assert_called_once()
        self.assertEqual(send.call_args.kwargs["alert_type"], "health")
        self.assertIsNone(send.call_args.kwargs.get("stream"))
        self.assertIn("DISCORD_WEBHOOK_SCAN_SUMMARY", send.call_args.kwargs["description"])

    def test_posts_one_message_per_scan_type_to_summary_route(self) -> None:
        day = self._log_flow_data()
        when = datetime.combine(day, datetime.min.time(), tzinfo=ET).replace(hour=17)
        with (
            patch.object(scan_summary, "WEBHOOKS", {"scan_summary": "summary-url"}),
            patch("src.scan_summary.send_alert", return_value=True) as send,
        ):
            self.assertTrue(scan_summary.run_daily_scan_summary(when))

        summary_calls = [
            call for call in send.call_args_list
            if call.kwargs.get("stream") == "scan_summary"
        ]
        self.assertEqual(len(summary_calls), 4)
        self.assertEqual(
            {call.kwargs["stream"] for call in summary_calls},
            {"scan_summary"},
        )
        self.assertEqual(
            [call.kwargs["title"] for call in summary_calls],
            [
                "Daily scan summary — CONFIRMED FLOW",
                "Daily scan summary — DAY-TRADE FLOW",
                "Daily scan summary — SWING",
                "Daily scan summary — CSP",
            ],
        )

    def test_non_trading_day_does_not_post(self) -> None:
        when = datetime(2026, 10, 25, 17, 0, tzinfo=ET)
        with patch("src.scan_summary.send_alert") as send:
            self.assertFalse(scan_summary.run_daily_scan_summary(when))
        send.assert_not_called()

    def test_day_with_no_scanner_data_is_labeled_missing_not_summarized(self) -> None:
            when = datetime(2026, 10, 26, 17, 0, tzinfo=ET)
            with (
                patch.object(scan_summary, "WEBHOOKS", {"scan_summary": "summary-url"}),
                patch("src.scan_summary.send_alert", return_value=True) as send,
            ):
                self.assertTrue(scan_summary.run_daily_scan_summary(when))

            self.assertEqual(send.call_count, 2)
            summary_notice = next(
                call for call in send.call_args_list
                if call.kwargs.get("stream") == "scan_summary"
            )
            self.assertIn("scanner data missing", summary_notice.kwargs["description"])
            self.assertIn("skipped_missing_data", state.get_flag("scan_summary:2026-10-26"))


if __name__ == "__main__":
    unittest.main()
