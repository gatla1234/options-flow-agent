from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

import polars as pl
from requests import Timeout

from src import state
from src.market import ET
from src.open_board import render_open_board, update_open_board
from src.performance_data import latest_daily_close, mark_open_alert


def _flow_alert(
    alert_id: int = 1,
    *,
    entry_mid: float = 2.0,
    current_spot: float = 105.0,
    target: float = 110.0,
    invalidation: float = 95.0,
) -> dict:
    return {
        "id": alert_id,
        "alert_type": "flow",
        "symbol": "SPY",
        "title": "CONFIRMED CALL",
        "payload": {
            "stream": "confirmed",
            "right": "CALL",
            "strike": 600,
            "expiration": "2026-11-20",
            "mid": entry_mid,
            "underlying_target_2r": target,
            "invalidation": {"price": invalidation, "condition": "below"},
        },
        "price_at_alert": 100.0,
        "created_at": "2026-10-26T10:00:00-04:00",
    }


def _mark(
    alert: dict | None = None,
    *,
    current_mid: float | None = 3.5,
    current_spot: float | None = 108.0,
) -> dict:
    alert = alert or _flow_alert()
    with (
        patch("src.performance_data.underlying_mark", return_value=current_spot),
        patch("src.performance_data.contract_mark", return_value=current_mid),
    ):
        return mark_open_alert(alert, datetime(2026, 10, 27, 11, 0, tzinfo=ET))


class OpenBoardRenderingTests(unittest.TestCase):
    def test_render_shows_pnl_progress_invalidation_and_near_flags(self) -> None:
        mark = _mark(
            _flow_alert(current_spot=108.0, target=110.0),
            current_mid=3.0,
            current_spot=108.0,
        )
        rendered = render_open_board(
            [mark], datetime(2026, 10, 27, 11, 0, tzinfo=ET)
        )
        text = rendered["all"]
        self.assertIn("Alert #1 SPY CALL $600C", text)
        self.assertIn("option $2.00 → $3.00", text)
        self.assertIn("+$100.00 (+50.0%)", text)
        self.assertIn("2R $110.00 (80% progress)", text)
        self.assertIn("invalidation $95.00 (intact)", text)
        self.assertIn("🎯 NEAR 2R", text)
        self.assertIn("per contract", text)
        self.assertIn("per share", text)
        self.assertNotIn("Suggestion:", text)

    def test_flow_put_and_call_marks_use_mid_to_mid_pnl(self) -> None:
        call = _flow_alert(31, entry_mid=2.0)
        put = _flow_alert(32, entry_mid=2.0)
        put["payload"]["right"] = "PUT"
        with (
            patch("src.performance_data.underlying_mark", return_value=100.0),
            patch("src.performance_data.contract_mark", side_effect=[3.0, 1.5]),
        ):
            marks = [
                mark_open_alert(call, datetime(2026, 10, 27, 11, 0, tzinfo=ET)),
                mark_open_alert(put, datetime(2026, 10, 27, 11, 0, tzinfo=ET)),
            ]
        self.assertEqual(marks[0]["pnl"], 100.0)
        self.assertEqual(marks[0]["pnl_pct"], 50.0)
        self.assertEqual(marks[1]["pnl"], -50.0)
        self.assertEqual(marks[1]["pnl_pct"], -25.0)

    def test_alerts_are_oldest_first_within_a_scan_group(self) -> None:
        older = _mark(_flow_alert(41))
        newer = _mark(_flow_alert(42))
        older["created_at"] = "2026-10-25T10:00:00-04:00"
        newer["created_at"] = "2026-10-26T10:00:00-04:00"
        text = render_open_board(
            [newer, older], datetime(2026, 10, 27, 11, 0, tzinfo=ET)
        )["all"]
        self.assertLess(text.index("Alert #41"), text.index("Alert #42"))

    def test_after_close_underlying_mark_requires_current_day_bar(self) -> None:
        bars = pl.DataFrame(
            {
                "created": ["2026-10-26", "2026-10-27"],
                "close": [100.0, 105.0],
            }
        )
        with patch("src.performance_data.get_daily_bars", return_value=bars):
            self.assertEqual(
                latest_daily_close("SPY", datetime(2026, 10, 27).date()),
                105.0,
            )
            self.assertIsNone(
                latest_daily_close("SPY", datetime(2026, 10, 28).date())
            )

    def test_invalidation_flag_uses_seventy_five_percent_threshold(self) -> None:
        below_threshold = _mark(
            _flow_alert(invalidation=90.0),
            current_mid=2.1,
            current_spot=93.0,
        )
        at_threshold = _mark(
            _flow_alert(invalidation=90.0),
            current_mid=2.1,
            current_spot=92.5,
        )
        self.assertNotIn("⚠ NEAR INVALIDATION", render_open_board(
            [below_threshold], datetime(2026, 10, 27, 11, 0, tzinfo=ET)
        )["all"])
        self.assertIn("⚠ NEAR INVALIDATION", render_open_board(
            [at_threshold], datetime(2026, 10, 27, 11, 0, tzinfo=ET)
        )["all"])

    def test_swing_and_csp_lines_use_their_own_pnl_basis(self) -> None:
        swing = {
            "id": 20,
            "alert_type": "swing",
            "symbol": "TQQQ",
            "title": "Swing long",
            "payload": {
                "direction": "BULLISH",
                "target_2r": 110.0,
                "invalidation": {"price": 90.0, "condition": "below"},
            },
            "price_at_alert": 100.0,
            "created_at": "2026-10-26T16:00:00-04:00",
        }
        csp = {
            "id": 21,
            "alert_type": "csp",
            "symbol": "SOXL",
            "title": "CSP",
            "payload": {
                "strike": 90.0,
                "expiration": "2026-11-20",
                "premium": 2.0,
            },
            "price_at_alert": 100.0,
            "created_at": "2026-10-26T10:00:00-04:00",
        }
        with (
            patch("src.performance_data.underlying_mark", side_effect=[105.0, 95.0]),
            patch("src.performance_data.contract_mark", return_value=0.5),
        ):
            marks = [
                mark_open_alert(swing, datetime(2026, 10, 27, 11, 0, tzinfo=ET)),
                mark_open_alert(csp, datetime(2026, 10, 27, 11, 0, tzinfo=ET)),
            ]
        text = render_open_board(
            marks, datetime(2026, 10, 27, 11, 0, tzinfo=ET)
        )["all"]
        self.assertIn("P&L/share +$5.00 (+5.0%)", text)
        self.assertIn("2R $110.00 (50% progress)", text)
        self.assertIn("$90 put exp 2026-11-20", text)
        self.assertIn("premium $2.00 → $0.50", text)
        self.assertIn("P&L +$150.00 · max profit captured 75.0% / $200.00", text)
        self.assertIn("$95.00 above $90.00 strike", text)
        self.assertIn("cash required +$9,000.00", text)

    def test_empty_board_and_unavailable_price_keep_expected_text(self) -> None:
        now = datetime(2026, 10, 27, 11, 0, tzinfo=ET)
        self.assertIn(
            "No open alerts — all alerts are closed. See closed-ledger for history.",
            render_open_board([], now)["all"],
        )
        failed_mark = _mark(current_mid=None, current_spot=None)
        text = render_open_board([failed_mark], now)["all"]
        self.assertIn("price unavailable", text)
        self.assertIn("options total: +$0.00 per contract (1 unavailable excluded)", text)
        self.assertIn("Alert #1", text)

    def test_over_limit_board_splits_by_scan_type_without_dropping_alerts(self) -> None:
        marks = []
        for alert_id, stream in enumerate(("confirmed", "daytrade"), 1):
            alert = _flow_alert(alert_id)
            alert["payload"]["stream"] = stream
            marks.append(_mark(alert))
        rendered = render_open_board(
            marks,
            datetime(2026, 10, 27, 11, 0, tzinfo=ET),
            message_limit=500,
        )
        self.assertIn("confirmed", rendered)
        self.assertIn("daytrade", rendered)
        content = "\n".join(rendered.values())
        self.assertIn("Alert #1", content)
        self.assertIn("Alert #2", content)

    def test_closed_market_footer(self) -> None:
        text = render_open_board(
            [_mark()],
            datetime(2026, 10, 27, 16, 20, tzinfo=ET),
            market_closed=True,
        )["all"]
        self.assertIn("Market closed — last update 16:20 ET", text)


class OpenBoardDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "alerts.db"
        self.now = datetime(2026, 10, 27, 10, 0, tzinfo=ET)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _db(self):
        return patch.object(state, "DB_PATH", self.db_path)

    def test_restart_edits_persisted_message_id_without_posting_new(self) -> None:
        with self._db():
            state.set_tracker_message_id("open_board:all", "persisted-id")
            with (
                patch("src.open_board.WEBHOOKS", {"open_ledger": "webhook"}),
                patch("src.open_board.tracked_alert_rows", return_value=[]),
                patch(
                    "src.discord_alerts.WEBHOOKS",
                    {"open_ledger": "https://discord.test/api/webhooks/id/token"},
                ),
                patch("src.discord_alerts.requests.patch", return_value=Mock(status_code=200)) as edit,
                patch("src.discord_alerts.requests.post") as post,
            ):
                self.assertTrue(update_open_board(self.now))
                edit.assert_called_once()
                self.assertIn("/messages/persisted-id", edit.call_args.args[0])
                post.assert_not_called()

    def test_edit_failure_creates_and_stores_replacement_id(self) -> None:
        response = Mock(status_code=200)
        response.json.return_value = {"id": "replacement"}
        with self._db():
            state.set_tracker_message_id("open_board:all", "deleted")
            with (
                patch("src.open_board.WEBHOOKS", {"open_ledger": "webhook"}),
                patch("src.open_board.tracked_alert_rows", return_value=[]),
                patch("src.discord_alerts.WEBHOOKS", {
                    "open_ledger": "https://discord.test/api/webhooks/id/token",
                }),
                patch("src.discord_alerts.requests.patch", return_value=Mock(status_code=404)),
                patch("src.discord_alerts.requests.post", return_value=response) as post,
            ):
                self.assertTrue(update_open_board(self.now))
                post.assert_called_once()
                self.assertEqual(state.get_tracker_message_id("open_board:all"), "replacement")

    def test_transient_edit_failure_keeps_stored_id_without_posting_duplicate(self) -> None:
        with self._db():
            state.set_tracker_message_id("open_board:all", "existing")
            with (
                patch("src.open_board.WEBHOOKS", {"open_ledger": "webhook"}),
                patch("src.open_board.tracked_alert_rows", return_value=[]),
                patch(
                    "src.discord_alerts.WEBHOOKS",
                    {"open_ledger": "https://discord.test/api/webhooks/id/token"},
                ),
                patch(
                    "src.discord_alerts.requests.patch",
                    side_effect=Timeout,
                ),
                patch("src.discord_alerts.requests.post") as post,
            ):
                self.assertFalse(update_open_board(self.now))
                post.assert_not_called()
                self.assertEqual(
                    state.get_tracker_message_id("open_board:all"), "existing"
                )

    def test_closed_alert_is_removed_from_next_board_render(self) -> None:
        with self._db():
            with (
                patch("src.open_board.WEBHOOKS", {"open_ledger": "webhook"}),
                patch("src.open_board.tracked_alert_rows", return_value=[_flow_alert()]),
                patch("src.open_board.mark_open_alert", return_value=_mark()),
                patch("src.open_board.edit_or_create_webhook_message", return_value=("one", True)) as update,
                patch("src.open_board.tracker_message_ids", return_value={"open_board:all": "one"}),
            ):
                self.assertTrue(update_open_board(self.now))
                update.reset_mock()
                with patch("src.open_board.tracked_alert_rows", return_value=[]):
                    self.assertTrue(update_open_board(
                        datetime(2026, 10, 27, 10, 16, tzinfo=ET)
                    ))
                self.assertIn(
                    "No open alerts",
                    update.call_args.args[2],
                )

    def test_missing_webhook_emits_one_health_attempt_and_never_board(self) -> None:
        with self._db():
            with (
                patch("src.open_board.WEBHOOKS", {"health": "health"}),
                patch("src.open_board.send_alert") as send,
            ):
                self.assertFalse(update_open_board(self.now))
                self.assertFalse(update_open_board(self.now))
                send.assert_called_once()
                self.assertEqual(send.call_args.kwargs["alert_type"], "health")

    def test_board_waits_fifteen_minutes_between_intraday_edits(self) -> None:
        with self._db():
            with (
                patch("src.open_board.WEBHOOKS", {"open_ledger": "webhook"}),
                patch("src.open_board.tracked_alert_rows", return_value=[]),
                patch(
                    "src.open_board.edit_or_create_webhook_message",
                    return_value=("id", True),
                ) as edit,
            ):
                self.assertTrue(update_open_board(self.now))
                self.assertFalse(update_open_board(
                    datetime(2026, 10, 27, 10, 14, tzinfo=ET)
                ))
                self.assertTrue(update_open_board(
                    datetime(2026, 10, 27, 10, 15, tzinfo=ET)
                ))
                self.assertEqual(edit.call_count, 2)

    def test_sqlite_open_alert_query_excludes_followups_and_closed_alerts(self) -> None:
        with self._db():
            state.log_alert(
                "flow-one",
                "flow",
                "SPY",
                "Original flow",
                {"stream": "confirmed", "mid": 2.0},
                100.0,
            )
            state.log_alert(
                "flow-followup",
                "flow",
                "SPY",
                "Follow-up",
                {"is_followup": True, "stream": "confirmed"},
                100.0,
            )
            state.log_alert(
                "swing-two",
                "swing",
                "TQQQ",
                "Original swing",
                {"direction": "BULLISH"},
                50.0,
            )
            rows = state.tracked_alert_rows()
            self.assertEqual([row["symbol"] for row in rows], ["SPY", "TQQQ"])
            state.save_closed_alert_record(1, {"closing_reason": "INVALIDATED"})
            rows = state.tracked_alert_rows()
            self.assertEqual([row["symbol"] for row in rows], ["TQQQ"])


if __name__ == "__main__":
    unittest.main()
