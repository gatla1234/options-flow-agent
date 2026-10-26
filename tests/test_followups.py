from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from src import state
from src.followups import (
    run_expiration_followups,
    run_news_followups,
    run_price_followups,
)
from src.grade import grade_open_alerts
from src.market import ET


def _alert(
    alert_id: int,
    alert_type: str,
    symbol: str,
    payload: dict,
    created_at: datetime | None = None,
) -> dict:
    return {
        "id": alert_id,
        "alert_type": alert_type,
        "symbol": symbol,
        "title": f"Original {alert_type} alert — {symbol}",
        "payload": json.dumps(payload),
        "price_at_alert": 100.0,
        "created_at": (created_at or datetime(2026, 10, 26, 9, 0, tzinfo=ET)).isoformat(),
    }


class PriceFollowupTests(unittest.TestCase):
    def test_swing_invalidation_sends_followup_tied_to_original_alert(self) -> None:
        original = _alert(
            42,
            "swing",
            "TQQQ",
            {
                "direction": "BULLISH",
                "invalidation": 95.0,
                "target_2r": 110.0,
            },
        )
        with (
            patch("src.followups._followup_rows", return_value=[original]),
            patch("src.followups.get_flag", return_value=None),
            patch("src.followups.set_flag"),
            patch("src.followups.get_live_price", return_value=94.5),
            patch("src.followups.followup_event_sent", return_value=False),
            patch("src.followups.mark_followup_event_sent") as mark_sent,
            patch("src.followups.send_alert", return_value=True) as send_alert,
        ):
            count = run_price_followups(datetime(2026, 10, 26, 10, 0, tzinfo=ET))

        self.assertEqual(count, 1)
        kwargs = send_alert.call_args.kwargs
        self.assertEqual(kwargs["alert_type"], "swing")
        self.assertIn("FOLLOW-UP", kwargs["title"])
        self.assertIn("original alert #42", kwargs["description"])
        self.assertIn("does not know whether you entered", kwargs["description"])
        self.assertEqual(kwargs["payload"]["original_alert_id"], 42)
        mark_sent.assert_called_once_with(42, "invalidation:below:95.00")

    def test_flow_target_reached_sends_one_event_only(self) -> None:
        original = _alert(
            7,
            "flow",
            "SPY",
            {"right": "CALL", "underlying_target_2r": 105.0},
        )
        with (
            patch("src.followups._followup_rows", return_value=[original]),
            patch("src.followups.get_flag", return_value=None),
            patch("src.followups.set_flag"),
            patch("src.followups.get_live_price", return_value=106.0),
            patch("src.followups.followup_event_sent", return_value=True),
            patch("src.followups.send_alert") as send_alert,
        ):
            count = run_price_followups(datetime(2026, 10, 26, 10, 0, tzinfo=ET))

        self.assertEqual(count, 0)
        send_alert.assert_not_called()

    def test_put_target_uses_bearish_direction(self) -> None:
        original = _alert(
            9,
            "flow",
            "QQQ",
            {"right": "PUT", "underlying_target_2r": 95.0},
        )
        with (
            patch("src.followups._followup_rows", return_value=[original]),
            patch("src.followups.get_flag", return_value=None),
            patch("src.followups.set_flag"),
            patch("src.followups.get_live_price", return_value=94.0),
            patch("src.followups.followup_event_sent", return_value=False),
            patch("src.followups.mark_followup_event_sent") as mark_sent,
            patch("src.followups.send_alert", return_value=True) as send_alert,
        ):
            count = run_price_followups(datetime(2026, 10, 26, 10, 0, tzinfo=ET))

        self.assertEqual(count, 1)
        self.assertIn("2R reference reached", send_alert.call_args.kwargs["title"])
        mark_sent.assert_called_once_with(9, "target-2r:95.00")

    def test_csp_strike_breach_is_assignment_review_not_a_sell_instruction(self) -> None:
        original = _alert(
            8,
            "csp",
            "SOXL",
            {"strike": 50.0, "expiration": "2026-11-20"},
        )
        with (
            patch("src.followups._followup_rows", return_value=[original]),
            patch("src.followups.get_flag", return_value=None),
            patch("src.followups.set_flag"),
            patch("src.followups.get_live_price", return_value=49.5),
            patch("src.followups.followup_event_sent", return_value=False),
            patch("src.followups.mark_followup_event_sent"),
            patch("src.followups.send_alert", return_value=True) as send_alert,
        ):
            count = run_price_followups(datetime(2026, 10, 26, 10, 0, tzinfo=ET))

        self.assertEqual(count, 1)
        description = send_alert.call_args.kwargs["description"]
        self.assertIn("assignment risk", description)
        self.assertIn("no position is assumed", description)


class NewsFollowupTests(unittest.TestCase):
    def test_company_news_published_after_original_alert_sends_once(self) -> None:
        created = datetime(2026, 10, 26, 9, 0, tzinfo=ET)
        original = _alert(15, "flow", "SPY", {"right": "CALL"}, created)
        published = int((created + timedelta(minutes=30)).timestamp())
        response = Mock(status_code=200)
        response.json.return_value = [
            {
                "id": 1234,
                "datetime": published,
                "headline": "Company announces earnings update",
                "source": "Newswire",
                "url": "https://example.invalid/news/1234",
            }
        ]
        with (
            patch("src.followups._followup_rows", return_value=[original]),
            patch.dict("os.environ", {"FINNHUB_API_KEY": "test-key"}),
            patch("src.followups.requests.get", return_value=response) as request,
            patch("src.followups.followup_event_sent", return_value=False),
            patch("src.followups.mark_followup_event_sent") as mark_sent,
            patch("src.followups.send_alert", return_value=True) as send_alert,
        ):
            count = run_news_followups(
                datetime(2026, 10, 26, 10, 0, tzinfo=ET)
            )

        self.assertEqual(count, 1)
        self.assertEqual(request.call_args.kwargs["params"]["symbol"], "SPY")
        self.assertIn("New company headline", send_alert.call_args.kwargs["title"])
        self.assertIn("headline alone does not prove", send_alert.call_args.kwargs["description"])
        mark_sent.assert_called_once_with(15, "news:1234")

    def test_news_before_original_alert_is_ignored(self) -> None:
        created = datetime(2026, 10, 26, 9, 0, tzinfo=ET)
        original = _alert(16, "swing", "TQQQ", {"direction": "BULLISH"}, created)
        response = Mock(status_code=200)
        response.json.return_value = [
            {
                "id": 1235,
                "datetime": int((created - timedelta(minutes=10)).timestamp()),
                "headline": "Earlier headline",
            }
        ]
        with (
            patch("src.followups._followup_rows", return_value=[original]),
            patch.dict("os.environ", {"FINNHUB_API_KEY": "test-key"}),
            patch("src.followups.requests.get", return_value=response),
            patch("src.followups.send_alert") as send_alert,
        ):
            count = run_news_followups(
                datetime(2026, 10, 26, 10, 0, tzinfo=ET)
            )

        self.assertEqual(count, 0)
        send_alert.assert_not_called()


class ExpirationFollowupTests(unittest.TestCase):
    def test_sends_nearest_expiration_milestone_once(self) -> None:
        original = _alert(
            22,
            "flow",
            "QQQ",
            {"right": "PUT", "expiration": "2026-10-27"},
        )
        with (
            patch("src.followups._followup_rows", return_value=[original]),
            patch("src.followups.followup_event_sent", return_value=False),
            patch("src.followups.mark_followup_event_sent") as mark_sent,
            patch("src.followups.send_alert", return_value=True) as send_alert,
        ):
            count = run_expiration_followups(
                datetime(2026, 10, 26, 9, 0, tzinfo=ET)
            )

        self.assertEqual(count, 1)
        self.assertIn("expires tomorrow", send_alert.call_args.kwargs["title"])
        self.assertIn("exercise risk", send_alert.call_args.kwargs["description"])
        mark_sent.assert_called_once_with(22, "expiration:1d")

    def test_does_not_send_expiration_reminder_far_from_expiry(self) -> None:
        original = _alert(
            23,
            "csp",
            "BITX",
            {"expiration": "2026-11-20"},
        )
        with (
            patch("src.followups._followup_rows", return_value=[original]),
            patch("src.followups.send_alert") as send_alert,
        ):
            count = run_expiration_followups(
                datetime(2026, 10, 26, 9, 0, tzinfo=ET)
            )

        self.assertEqual(count, 0)
        send_alert.assert_not_called()


class FollowupPersistenceTests(unittest.TestCase):
    def test_followup_event_is_persisted_once_per_original_alert(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "alerts.db"
            with patch.object(state, "DB_PATH", database):
                state.log_alert(
                    "original",
                    "flow",
                    "SPY",
                    "Original flow alert",
                    {"right": "CALL"},
                    100.0,
                )
                rows = state.followup_alerts_since(
                    datetime(2026, 10, 25, tzinfo=ET)
                )
                self.assertEqual(len(rows), 1)
                alert_id = rows[0]["id"]

                self.assertFalse(state.followup_event_sent(alert_id, "target:105"))
                state.mark_followup_event_sent(alert_id, "target:105")
                state.mark_followup_event_sent(alert_id, "target:105")

                self.assertTrue(state.followup_event_sent(alert_id, "target:105"))


class FollowupGradingTests(unittest.TestCase):
    def test_followup_messages_are_not_graded_as_original_trade_alerts(self) -> None:
        row = {
            "id": 99,
            "alert_type": "swing",
            "symbol": "TQQQ",
            "price_at_alert": 100.0,
            "created_at": (datetime.now(ET) - timedelta(days=1)).astimezone().isoformat(),
            "payload": json.dumps(
                {"is_followup": True, "direction": "BULLISH"}
            ),
        }
        with (
            patch("src.grade.ungraded_alerts", return_value=[row]),
            patch("src.grade.get_live_price") as get_live_price,
            patch("src.grade.grade_alert") as grade_alert,
        ):
            grade_open_alerts()

        get_live_price.assert_not_called()
        grade_alert.assert_not_called()


if __name__ == "__main__":
    unittest.main()
