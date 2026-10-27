from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from src import state
from src import closed_ledger
from src.followups import (
    _csp_average_description,
    _evaluate_flow_decision,
    run_expiration_followups,
    run_news_followups,
    run_price_followups,
    run_csp_average_followups,
    run_flow_decision_followups,
    run_swing_average_followups,
)
from src.grade import grade_open_alerts
from src.market import ET

NO_RECOMMENDATION_PHRASES = (
    "not a recommendation",
    "not financial advice",
    "not a trade instruction",
    "not a buy, sell, or hold instruction",
)

_FOLLOWUP_TEST_DB = tempfile.TemporaryDirectory()
_LEDGER_WEBHOOKS_PATCH = patch.object(closed_ledger, "WEBHOOKS", {})
_LEDGER_SEND_PATCH = patch.object(closed_ledger, "send_alert")
_ORIGINAL_STATE_DB_PATH = state.DB_PATH


def setUpModule() -> None:
    state.DB_PATH = Path(_FOLLOWUP_TEST_DB.name) / "alerts.db"
    _LEDGER_WEBHOOKS_PATCH.start()
    _LEDGER_SEND_PATCH.start()


def tearDownModule() -> None:
    _LEDGER_SEND_PATCH.stop()
    _LEDGER_WEBHOOKS_PATCH.stop()
    state.DB_PATH = _ORIGINAL_STATE_DB_PATH
    _FOLLOWUP_TEST_DB.cleanup()


def _assert_suggestion_without_disclaimer(
    test_case: unittest.TestCase, output: str, expected: str
) -> None:
    test_case.assertIn(expected, output)
    for phrase in NO_RECOMMENDATION_PHRASES:
        test_case.assertNotIn(phrase, output.lower())


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
        self.assertIn("FOLLOW-UP (alert #42)", kwargs["description"])
        self.assertIn("Entry: $100.00", kwargs["description"])
        self.assertNotIn("does not know whether you entered", kwargs["description"])
        self.assertNotIn("assuming you entered", kwargs["description"])
        _assert_suggestion_without_disclaimer(
            self, kwargs["description"], "Suggestion: consider exiting."
        )
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
        _assert_suggestion_without_disclaimer(
            self,
            send_alert.call_args.kwargs["description"],
            "Suggestion: consider taking profits.",
        )
        mark_sent.assert_called_once_with(9, "target-2r:95.00")

    def test_csp_strike_breach_suggests_assignment_review(self) -> None:
        original = _alert(
            8,
            "csp",
            "SOXL",
            {"strike": 50.0, "expiration": "2026-11-20", "premium": 2.0},
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
        self.assertIn("Entry: underlying $100.00; sold $50 put", description)
        self.assertNotIn("assuming you entered", description)
        self.assertNotIn("does not know whether you entered", description)
        _assert_suggestion_without_disclaimer(
            self,
            description,
            "Suggestion: review the position for assignment.",
        )


class FlowDecisionFollowupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 10, 26, 10, 30, tzinfo=ET)
        self.row = _alert(
            55,
            "flow",
            "SPY",
            {
                "right": "CALL",
                "strike": 600.0,
                "expiration": "2026-11-06",
                "dte": 11,
                "stream": "confirmed",
                "invalidation": {
                    "price": 90.0,
                    "condition": "below",
                    "level": "prior_low",
                },
            },
            datetime(2026, 10, 26, 10, 0, tzinfo=ET),
        )
        self.payload = json.loads(self.row["payload"])

    def _decision(
        self,
        price: float,
        opposing: dict | None = None,
    ) -> tuple[str, str, str] | None:
        return _evaluate_flow_decision(
            self.row,
            self.payload,
            price,
            opposing or {"buy_premium": 0, "buy_share": 0},
            self.now.date(),
        )

    def test_pullback_produces_equal_size_average_from_alert_entry(self) -> None:
        decision = self._decision(95.0)

        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(decision[0], "flow-average:2026-10-26")
        self.assertIn("retraced 50%", decision[1])
        self.assertIn("= $97.50", decision[1])
        self.assertIn("current price $95.00 gives an average of $97.50", decision[2])

    def test_opposing_flow_with_confirmation_reverses_and_lists_contract(self) -> None:
        opposing = {
            "buy_premium": 35_000,
            "buy_share": 0.7,
            "confirmed": True,
            "top_contract": {
                "strike": 590,
                "expiration": "2026-11-06",
                "dte": 11,
                "buy_premium": 25_000,
                "delta": -0.42,
                "spread_pct": 5.2,
            },
        }

        decision = self._decision(95.0, opposing)

        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(decision[0], "flow-reverse:2026-10-26")
        self.assertIn("$35,000 aggressive PUT premium", decision[1])
        self.assertIn("Suggestion: reverse", decision[2])
        self.assertIn("$590P exp 2026-11-06, 11 DTE", decision[2])
        self.assertIn("delta -0.42", decision[2])
        self.assertIn("spread 5.2%", decision[2])

    def test_opposing_flow_without_confirmation_exits_before_average(self) -> None:
        decision = self._decision(
            95.0,
            {"buy_premium": 30_000, "buy_share": 0.6, "confirmed": False},
        )

        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(decision[0], "flow-exit:2026-10-26")
        self.assertIn("opposing flow without opposing confirmation", decision[1])
        self.assertIn("Suggestion: exit $600C", decision[2])

    def test_invalidation_break_exits(self) -> None:
        decision = self._decision(89.0)

        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(decision[0], "flow-exit:2026-10-26")
        self.assertIn("invalidation broken at $90.00", decision[1])

    def test_pullback_under_seven_remaining_dte_does_not_average(self) -> None:
        self.payload["expiration"] = "2026-11-01"
        self.payload["dte"] = 6

        self.assertIsNone(self._decision(95.0))

    def test_stale_underlying_quote_fails_closed(self) -> None:
        with (
            patch("src.followups._followup_rows", return_value=[self.row]),
            patch("src.followups.get_live_price", return_value=None),
            patch("src.followups.get_opposing_flow_summary") as opposing,
            patch("src.followups.send_alert") as send_alert,
        ):
            sent = run_flow_decision_followups(self.now)

        self.assertEqual(sent, 0)
        opposing.assert_not_called()
        send_alert.assert_not_called()

    def test_repeat_flow_decision_is_suppressed(self) -> None:
        recorded: set[str] = set()

        def already_sent(alert_id: int, event_key: str) -> bool:
            return event_key in recorded

        def mark_sent(alert_id: int, event_key: str) -> None:
            recorded.add(event_key)

        with (
            patch("src.followups._followup_rows", return_value=[self.row]),
            patch("src.followups.get_live_price", return_value=95.0),
            patch("src.followups.build_context", return_value=None),
            patch(
                "src.followups.get_opposing_flow_summary",
                return_value={"buy_premium": 0, "buy_share": 0},
            ),
            patch("src.followups.followup_event_sent", side_effect=already_sent),
            patch("src.followups.mark_followup_event_sent", side_effect=mark_sent),
            patch("src.followups.send_alert", return_value=True) as send_alert,
        ):
            first = run_flow_decision_followups(self.now)
            second = run_flow_decision_followups(self.now)

        self.assertEqual(first, 1)
        self.assertEqual(second, 0)
        send_alert.assert_called_once()
        description = send_alert.call_args.kwargs["description"]
        _assert_suggestion_without_disclaimer(
            self, description, "Suggestion: consider averaging"
        )
        self.assertIn("Entry: $100.00", description)
        self.assertNotIn("assuming you entered", description)
        self.assertNotIn("does not know whether you entered", description)


class SwingAverageFollowupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 10, 27, 16, 15, tzinfo=ET)
        self.row = _alert(
            60,
            "swing",
            "TQQQ",
            {
                "direction": "BULLISH",
                "invalidation": {
                    "price": 90.0,
                    "condition": "below",
                    "level": "prior_low",
                },
            },
            datetime(2026, 10, 26, 16, 5, tzinfo=ET),
        )

    def _bars(self, *dates: str, closes: tuple[float, ...] | None = None):
        closes = closes or tuple([100.0] * (len(dates) - 1) + [95.0])
        import polars as pl

        return pl.DataFrame({"created": list(dates), "close": list(closes)})

    def test_later_daily_close_pullback_averages_from_signal_close(self) -> None:
        bars = self._bars("2026-10-26", "2026-10-27")
        with (
            patch("src.followups.is_eod_window", return_value=True),
            patch("src.followups._followup_rows", return_value=[self.row]),
            patch("src.followups.get_daily_bars", return_value=bars),
            patch("src.followups.followup_event_sent", return_value=False),
            patch("src.followups.mark_followup_event_sent") as mark_sent,
            patch("src.followups.send_alert", return_value=True) as send_alert,
        ):
            sent = run_swing_average_followups(self.now)

        self.assertEqual(sent, 1)
        description = send_alert.call_args.kwargs["description"]
        self.assertIn("Entry: $100.00", description)
        self.assertIn("retraced 50%", description)
        self.assertIn("average of $97.50", description)
        self.assertIn("Suggestion: consider averaging", description)
        self.assertNotIn("assuming you entered", description)
        self.assertNotIn("does not know whether you entered", description)
        mark_sent.assert_called_once_with(60, "swing-average")

    def test_broken_invalidation_and_small_pullback_do_not_average(self) -> None:
        for close in (89.0, 96.0):
            with self.subTest(close=close):
                bars = self._bars(
                    "2026-10-26",
                    "2026-10-27",
                    closes=(100.0, float(close)),
                )
                with (
                    patch("src.followups.is_eod_window", return_value=True),
                    patch("src.followups._followup_rows", return_value=[self.row]),
                    patch("src.followups.get_daily_bars", return_value=bars),
                    patch("src.followups.send_alert") as send_alert,
                ):
                    self.assertEqual(run_swing_average_followups(self.now), 0)
                send_alert.assert_not_called()

    def test_alert_older_than_five_trading_bars_does_not_average(self) -> None:
        self.row["created_at"] = datetime(
            2026, 10, 19, 16, 5, tzinfo=ET
        ).isoformat()
        bars = self._bars(
            "2026-10-19",
            "2026-10-20",
            "2026-10-21",
            "2026-10-22",
            "2026-10-23",
            "2026-10-26",
            "2026-10-27",
        )
        with (
            patch("src.followups.is_eod_window", return_value=True),
            patch("src.followups._followup_rows", return_value=[self.row]),
            patch("src.followups.get_daily_bars", return_value=bars),
            patch("src.followups.send_alert") as send_alert,
        ):
            self.assertEqual(run_swing_average_followups(self.now), 0)
        send_alert.assert_not_called()


class CspAverageFollowupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 10, 27, 10, 30, tzinfo=ET)
        self.row = _alert(
            70,
            "csp",
            "SOXL",
            {
                "strike": 90.0,
                "expiration": "2026-11-20",
                "premium": 2.0,
            },
            datetime(2026, 10, 26, 10, 30, tzinfo=ET),
        )
        self.candidate = {
            "strike": 90.0,
            "expiration": "2026-11-20",
            "premium": 3.0,
            "cash_required": 9_000.0,
        }

    def test_same_strike_average_reports_total_premium_cash_and_breakeven(self) -> None:
        with (
            patch("src.followups.is_rth", return_value=True),
            patch("src.followups._followup_rows", return_value=[self.row]),
            patch("src.followups.get_live_price", return_value=95.0),
            patch("src.followups.build_context", return_value={"ok_to_own": True}),
            patch(
                "src.followups.find_csp_opportunities",
                return_value=[self.candidate],
            ),
            patch("src.followups.followup_event_sent", return_value=False),
            patch("src.followups.mark_followup_event_sent") as mark_sent,
            patch("src.followups.send_alert", return_value=True) as send_alert,
        ):
            sent = run_csp_average_followups(self.now)

        self.assertEqual(sent, 1)
        description = send_alert.call_args.kwargs["description"]
        self.assertIn("Entry: underlying $100.00; sold $90 put", description)
        self.assertIn("retraced 50%", description)
        self.assertIn("average breakeven $87.50", description)
        self.assertIn("Total premium across both contracts: $500.00", description)
        self.assertIn("Additional put cash required: $9,000", description)
        self.assertIn("Suggestion: consider averaging", description)
        self.assertNotIn("assuming you entered", description)
        self.assertNotIn("does not know whether you entered", description)
        mark_sent.assert_called_once_with(70, "csp-average")

    def test_different_strike_lists_each_leg_without_blended_breakeven(self) -> None:
        candidate = {**self.candidate, "strike": 88.0, "premium": 2.5}
        facts, suggestion = _csp_average_description(
            "SOXL",
            100.0,
            90.0,
            "2026-11-20",
            2.0,
            candidate,
            0.5,
        )

        self.assertIn("Original leg breakeven: $88.00", facts)
        self.assertIn("additional leg breakeven: $85.50", facts)
        self.assertNotIn("average breakeven", facts)
        self.assertIn("total premium $450.00", suggestion)

    def test_csp_guards_for_strike_remaining_dte_and_no_candidate(self) -> None:
        test_cases = (
            (89.9, "2026-11-20", [self.candidate]),
            (95.0, "2026-11-02", [self.candidate]),
            (95.0, "2026-11-20", []),
        )
        for current, expiration, candidates in test_cases:
            with self.subTest(current=current, expiration=expiration):
                payload = json.loads(self.row["payload"])
                payload["expiration"] = expiration
                row = {**self.row, "payload": json.dumps(payload)}
                with (
                    patch("src.followups.is_rth", return_value=True),
                    patch("src.followups._followup_rows", return_value=[row]),
                    patch("src.followups.get_live_price", return_value=current),
                    patch("src.followups.build_context", return_value={"ok_to_own": True}),
                    patch(
                        "src.followups.find_csp_opportunities",
                        return_value=candidates,
                    ),
                    patch("src.followups.send_alert") as send_alert,
                ):
                    self.assertEqual(run_csp_average_followups(self.now), 0)
                send_alert.assert_not_called()


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
        _assert_suggestion_without_disclaimer(
            self,
            send_alert.call_args.kwargs["description"],
            "Suggestion: review the original setup.",
        )
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
        self.assertIn("Entry: $100.00", send_alert.call_args.kwargs["description"])
        _assert_suggestion_without_disclaimer(
            self,
            send_alert.call_args.kwargs["description"],
            "Suggestion: review the original setup.",
        )
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
                    "contract:SPY:2026-11-06:100:CALL",
                )
                self.assertTrue(
                    state.recently_sent_cross_stream(
                        "contract:SPY:2026-11-06:100:CALL", 3600
                    )
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
