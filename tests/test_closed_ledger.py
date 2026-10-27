from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from src import state
from src.closed_ledger import (
    build_closed_alert_record,
    check_csp_decay,
    evaluate_expired_alert,
    evaluate_swing_stale,
    flush_pending_closed_alerts,
    format_closed_alert_message,
    post_closed_alert,
)
from src.discord_alerts import WEBHOOK_ENV_NAMES
from src.market import ET


def _row(
    alert_id: int,
    alert_type: str,
    payload: dict,
    *,
    symbol: str = "SPY",
    entry: float = 100.0,
    created: datetime | None = None,
) -> dict:
    return {
        "id": alert_id,
        "alert_type": alert_type,
        "symbol": symbol,
        "title": f"{alert_type} alert",
        "payload": json.dumps(payload),
        "price_at_alert": entry,
        "created_at": (created or datetime(2026, 10, 26, 10, 0, tzinfo=ET)).isoformat(),
    }


class ClosedAlertLedgerTests(unittest.TestCase):
    def test_closed_ledger_has_its_own_environment_routed_channel(self) -> None:
        self.assertEqual(
            WEBHOOK_ENV_NAMES["closed_ledger"],
            "DISCORD_WEBHOOK_CLOSED_LEDGER",
        )

    def test_pnl_uses_option_premium_or_directional_swing_prices(self) -> None:
        cases = (
            (
                "flow",
                {"right": "CALL", "strike": 100, "expiration": "2026-11-20", "dte": 25, "mid": 2},
                "INVALIDATED",
                1.5,
                -50.0,
                -25.0,
            ),
            (
                "flow",
                {"right": "CALL", "strike": 100, "expiration": "2026-11-20", "dte": 25, "mid": 2},
                "2R TARGET",
                3.0,
                100.0,
                50.0,
            ),
            (
                "flow",
                {"right": "PUT", "strike": 100, "expiration": "2026-11-20", "dte": 25, "mid": 2},
                "EXPIRED WORTHLESS",
                0.0,
                -200.0,
                -100.0,
            ),
            (
                "csp",
                {"right": "PUT", "strike": 100, "expiration": "2026-11-20", "dte": 25, "premium": 3},
                "CSP 90% DECAY",
                0.3,
                270.0,
                90.0,
            ),
            (
                "csp",
                {"right": "PUT", "strike": 100, "expiration": "2026-11-20", "dte": 25, "premium": 2},
                "EXPIRED ASSIGNED",
                5.0,
                -300.0,
                -150.0,
            ),
            (
                "swing",
                {"direction": "BEARISH"},
                "STALE",
                95.0,
                5.0,
                5.0,
            ),
        )
        for index, (alert_type, payload, reason, exit_price, pnl, pnl_pct) in enumerate(cases, 1):
            with self.subTest(reason=reason):
                row = _row(index, alert_type, payload)
                record = build_closed_alert_record(
                    row, payload, reason, exit_price, date(2026, 10, 27)
                )
                self.assertEqual(record["pnl"], pnl)
                self.assertEqual(record["pnl_pct"], pnl_pct)

    def test_message_includes_all_required_fields_and_missing_prices(self) -> None:
        row = _row(12, "flow", {"right": "CALL", "strike": 100, "expiration": "2026-11-20", "dte": 25})
        record = build_closed_alert_record(
            row, {"right": "CALL", "strike": 100, "expiration": "2026-11-20", "dte": 25},
            "INVALIDATED", None, date(2026, 10, 27)
        )
        record["entry_price"] = None
        message = format_closed_alert_message(record)
        for required in (
            "Alert #12",
            "Scan type: flow",
            "Ticker: SPY",
            "Direction: CALL",
            "Contract: $100 CALL, exp 2026-11-20, 25 DTE at alert",
            "Entry price: unavailable",
            "Exit price: unavailable",
            "Closing reason: INVALIDATED",
            "P&L per contract: unavailable (unavailable)",
            "Alert date:",
            "Close date:",
            "Days open:",
            "Hypothetical — alert price = entry, 1 contract / 1 share, before commissions and spreads. Not brokerage P&L.",
        ):
            self.assertIn(required, message)

    def test_each_closing_reason_posts_one_required_record(self) -> None:
        reasons = (
            "INVALIDATED",
            "2R TARGET",
            "EXPIRED WORTHLESS",
            "EXPIRED ASSIGNED",
            "CSP 90% DECAY",
            "STALE",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.object(state, "DB_PATH", Path(temp_dir) / "alerts.db"),
                patch("src.closed_ledger.WEBHOOKS", {"closed_ledger": "ledger-url"}),
                patch("src.closed_ledger.send_alert", return_value=True) as send,
            ):
                for alert_id, reason in enumerate(reasons, 1):
                    alert_type = "swing" if reason == "STALE" else "flow"
                    payload = (
                        {"direction": "BULLISH"}
                        if alert_type == "swing"
                        else {
                            "right": "CALL",
                            "strike": 100,
                            "expiration": "2026-11-20",
                            "dte": 25,
                            "mid": 2,
                        }
                    )
                    row = _row(alert_id, alert_type, payload)
                    self.assertTrue(
                        post_closed_alert(
                            row, payload, reason, 1.0, date(2026, 10, 27)
                        )
                    )
                    description = send.call_args.kwargs["description"]
                    self.assertIn(f"Alert #{alert_id}", description)
                    self.assertIn(f"Closing reason: {reason}", description)
                    self.assertIn("P&L per", description)
                    self.assertEqual(send.call_args.kwargs["stream"], "closed_ledger")
                self.assertEqual(send.call_count, len(reasons))

    def test_repeated_closure_posts_only_once_for_alert_number(self) -> None:
        row = _row(
            77, "flow",
            {"right": "CALL", "strike": 100, "expiration": "2026-11-20", "dte": 25, "mid": 2},
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.object(state, "DB_PATH", Path(temp_dir) / "alerts.db"),
                patch("src.closed_ledger.WEBHOOKS", {"closed_ledger": "ledger-url"}),
                patch("src.closed_ledger.send_alert", return_value=True) as send,
            ):
                self.assertTrue(
                    post_closed_alert(row, {"right": "CALL", "strike": 100, "expiration": "2026-11-20", "dte": 25, "mid": 2},
                                      "INVALIDATED", 1.5, date(2026, 10, 27))
                )
                self.assertFalse(
                    post_closed_alert(row, {"right": "CALL", "strike": 100, "expiration": "2026-11-20", "dte": 25, "mid": 2},
                                      "2R TARGET", 3.0, date(2026, 10, 27))
                )
                self.assertEqual(send.call_count, 1)
                self.assertTrue(state.closed_alert_is_posted(77))

    def test_missing_webhook_sends_one_health_note_without_ledger_fallback(self) -> None:
        row = _row(88, "swing", {"direction": "BULLISH"})
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.object(state, "DB_PATH", Path(temp_dir) / "alerts.db"),
                patch("src.closed_ledger.WEBHOOKS", {"health": "health-url"}),
                patch("src.closed_ledger.send_alert", return_value=True) as send,
            ):
                post_closed_alert(row, {"direction": "BULLISH"}, "STALE", 95.0, date(2026, 10, 27))
                post_closed_alert(row, {"direction": "BULLISH"}, "STALE", 95.0, date(2026, 10, 27))
                send.assert_called_once()
                self.assertEqual(send.call_args.kwargs["alert_type"], "health")
                self.assertNotEqual(
                    send.call_args.kwargs.get("stream"), "closed_ledger"
                )
                self.assertFalse(state.closed_alert_is_posted(88))

    def test_stale_sending_claim_is_recovered_after_restart(self) -> None:
        row = _row(
            89,
            "flow",
            {"right": "CALL", "strike": 100, "expiration": "2026-11-20", "mid": 2},
        )
        now = datetime.now(ET)
        record = build_closed_alert_record(
            row, json.loads(row["payload"]), "INVALIDATED", 1.5, now.date()
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.object(state, "DB_PATH", Path(temp_dir) / "alerts.db"),
                patch("src.closed_ledger.WEBHOOKS", {"closed_ledger": "ledger-url"}),
                patch("src.closed_ledger.send_alert", return_value=True) as send,
            ):
                state.save_closed_alert_record(89, record)
                self.assertTrue(state.claim_closed_alert_record(89))
                with state._connect() as conn:
                    old_update = (
                        datetime.now(timezone.utc) - timedelta(minutes=20)
                    ).isoformat()
                    conn.execute(
                        "UPDATE closed_alert_ledger SET updated_at = ? WHERE alert_id = 89",
                        (old_update,),
                    )
                    conn.commit()
                self.assertEqual(flush_pending_closed_alerts(now), 1)
                self.assertTrue(state.closed_alert_is_posted(89))
                send.assert_called_once()

    def test_pending_record_retries_when_dedicated_webhook_is_configured(self) -> None:
        row = _row(89, "swing", {"direction": "BULLISH"})
        webhooks = {"health": "health-url"}
        now = datetime(2026, 10, 27, 16, 30, tzinfo=ET)
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.object(state, "DB_PATH", Path(temp_dir) / "alerts.db"),
                patch("src.closed_ledger.WEBHOOKS", webhooks),
                patch("src.closed_ledger.send_alert", return_value=True) as send,
            ):
                self.assertFalse(
                    post_closed_alert(
                        row, {"direction": "BULLISH"}, "STALE", 95.0,
                        date(2026, 10, 27), now
                    )
                )
                webhooks["closed_ledger"] = "ledger-url"
                self.assertEqual(flush_pending_closed_alerts(now), 1)
                self.assertEqual(flush_pending_closed_alerts(now), 0)
                self.assertEqual(send.call_count, 2)
                self.assertEqual(send.call_args.kwargs["stream"], "closed_ledger")
                self.assertIn("Closing reason: STALE", send.call_args.kwargs["description"])
                self.assertTrue(state.closed_alert_is_posted(89))

    def test_csp_decay_closes_at_or_below_ten_percent_of_original_premium(self) -> None:
        row = _row(
            90, "csp",
            {"right": "PUT", "strike": 100, "expiration": "2026-11-20", "premium": 2},
        )
        now = datetime(2026, 10, 27, 11, 0, tzinfo=ET)
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.object(state, "DB_PATH", Path(temp_dir) / "alerts.db"),
                patch("src.closed_ledger.WEBHOOKS", {"closed_ledger": "ledger-url"}),
                patch("src.closed_ledger._option_mark", return_value=0.2),
                patch("src.closed_ledger.send_alert", return_value=True) as send,
            ):
                self.assertTrue(check_csp_decay(row, {"premium": 2}, now))
                self.assertIn("Closing reason: CSP 90% DECAY", send.call_args.kwargs["description"])

    def test_option_mark_requires_matching_fresh_contract_quote(self) -> None:
        import polars as pl

        from src.closed_ledger import _option_mark

        now = datetime(2026, 10, 27, 11, 0, tzinfo=ET)
        row = _row(
            91,
            "csp",
            {"strike": 100, "expiration": "2026-11-20", "premium": 2},
        )
        quote = {
            "strike": 100.0,
            "right": "PUT",
            "bid": 0.1,
            "ask": 0.3,
            "timestamp": now,
        }
        with patch("src.closed_ledger.get_option_chain", return_value=pl.DataFrame([quote])):
            self.assertEqual(
                _option_mark(row, {"strike": 100, "expiration": "2026-11-20"}, now, True),
                0.2,
            )
        stale_quote = {**quote, "timestamp": now.replace(minute=40)}
        with patch(
            "src.closed_ledger.get_option_chain",
            return_value=pl.DataFrame([stale_quote]),
        ):
            self.assertIsNone(
                _option_mark(row, {"strike": 100, "expiration": "2026-11-20"}, now, True)
            )

    def test_after_close_option_mark_requires_a_quote_from_that_trading_day(self) -> None:
        import polars as pl

        from src.closed_ledger import _option_mark

        now = datetime(2026, 10, 27, 17, 15, tzinfo=ET)
        row = _row(
            92,
            "csp",
            {"strike": 100, "expiration": "2026-11-20", "premium": 2},
        )
        quote = {
            "strike": 100.0,
            "right": "PUT",
            "bid": 0.1,
            "ask": 0.3,
            "timestamp": now.replace(hour=15, minute=55),
        }
        with patch(
            "src.closed_ledger.get_option_chain",
            return_value=pl.DataFrame([{**quote, "timestamp": now.replace(day=26)}]),
        ):
            self.assertIsNone(
                _option_mark(
                    row,
                    {"strike": 100, "expiration": "2026-11-20"},
                    now,
                    require_fresh=False,
                    require_current_day=True,
                )
            )
        with patch(
            "src.closed_ledger.get_option_chain",
            return_value=pl.DataFrame([quote]),
        ):
            self.assertEqual(
                _option_mark(
                    row,
                    {"strike": 100, "expiration": "2026-11-20"},
                    now,
                    require_fresh=False,
                    require_current_day=True,
                ),
                0.2,
            )

    def test_expiry_closure_classifies_intrinsic_value(self) -> None:
        now = datetime(2026, 11, 20, 16, 30, tzinfo=ET)
        assigned = _row(
            101, "flow",
            {"right": "CALL", "strike": 100, "expiration": "2026-11-20", "dte": 25, "mid": 2},
        )
        worthless = _row(
            102, "flow",
            {"right": "PUT", "strike": 100, "expiration": "2026-11-20", "dte": 25, "mid": 2},
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.object(state, "DB_PATH", Path(temp_dir) / "alerts.db"),
                patch("src.closed_ledger.WEBHOOKS", {"closed_ledger": "ledger-url"}),
                patch("src.closed_ledger._daily_close", side_effect=[105.0, 105.0]),
                patch("src.closed_ledger.send_alert", return_value=True) as send,
            ):
                self.assertTrue(evaluate_expired_alert(assigned, {"right": "CALL", "strike": 100, "expiration": "2026-11-20", "dte": 25, "mid": 2}, now))
                self.assertTrue(evaluate_expired_alert(worthless, {"right": "PUT", "strike": 100, "expiration": "2026-11-20", "dte": 25, "mid": 2}, now))
                messages = [call.kwargs["description"] for call in send.call_args_list]
                self.assertIn("Closing reason: EXPIRED ASSIGNED", messages[0])
                self.assertIn("Closing reason: EXPIRED WORTHLESS", messages[1])

    def test_swing_stale_requires_twenty_sessions_and_current_close(self) -> None:
        row = _row(
            111,
            "swing",
            {"direction": "BULLISH"},
            created=datetime(2026, 10, 1, 16, 0, tzinfo=ET),
        )
        now = datetime(2026, 10, 30, 16, 15, tzinfo=ET)
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.object(state, "DB_PATH", Path(temp_dir) / "alerts.db"),
                patch("src.closed_ledger.WEBHOOKS", {"closed_ledger": "ledger-url"}),
                patch("src.closed_ledger._daily_close", return_value=95.0),
                patch("src.closed_ledger.send_alert", return_value=True) as send,
            ):
                self.assertTrue(evaluate_swing_stale(row, {"direction": "BULLISH"}, now))
                self.assertIn("Closing reason: STALE", send.call_args.kwargs["description"])

    def test_open_alerts_do_not_post(self) -> None:
        now = datetime(2026, 10, 27, 16, 30, tzinfo=ET)
        future_expiry = _row(
            120, "flow",
            {"right": "CALL", "strike": 100, "expiration": "2026-11-20", "mid": 2},
        )
        recent_swing = _row(
            121,
            "swing",
            {"direction": "BULLISH"},
            created=datetime(2026, 10, 26, 16, 0, tzinfo=ET),
        )
        with (
            patch("src.closed_ledger._daily_close") as daily_close,
            patch("src.closed_ledger.post_closed_alert") as post,
        ):
            self.assertFalse(
                evaluate_expired_alert(future_expiry, {"expiration": "2026-11-20"}, now)
            )
            self.assertFalse(
                evaluate_swing_stale(recent_swing, {"direction": "BULLISH"}, now)
            )
        daily_close.assert_not_called()
        post.assert_not_called()

    def test_price_followup_closure_rules_reach_ledger(self) -> None:
        from src.followups import _price_events, run_flow_decision_followups

        now = datetime(2026, 10, 26, 10, 30, tzinfo=ET)
        swing_row = _row(
            130,
            "swing",
            {"direction": "BULLISH", "invalidation": 95.0},
            entry=100.0,
        )
        flow_payload = {
            "right": "CALL",
            "stream": "confirmed",
            "strike": 100,
            "expiration": "2026-11-20",
            "dte": 25,
            "mid": 2.0,
            "direction": "BULLISH",
            "invalidation": 95.0,
        }
        flow_row = _row(131, "flow", flow_payload, entry=100.0)
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.object(state, "DB_PATH", Path(temp_dir) / "alerts.db"),
                patch("src.closed_ledger.WEBHOOKS", {"closed_ledger": "ledger-url"}),
                patch("src.closed_ledger._option_mark", return_value=1.5),
                patch("src.closed_ledger.send_alert", return_value=True) as ledger_send,
                patch("src.followups._send_followup", return_value=True),
                patch("src.followups._followup_rows", return_value=[flow_row]),
                patch("src.followups.get_live_price", return_value=94.0),
                patch("src.followups.build_context", return_value={
                    "ema_fast": 101.0,
                    "ema_slow": 100.0,
                    "levels": {"low_20": 0.0},
                }),
                patch("src.followups.get_opposing_flow_summary", return_value={
                    "buy_premium": 0.0,
                    "buy_share": 0.0,
                    "confirmed": False,
                    "top_contract": None,
                }),
            ):
                _price_events(
                    swing_row, json.loads(swing_row["payload"]), 94.0, now
                )
                run_flow_decision_followups(now)

            reasons = [
                call.kwargs["description"].split("Closing reason: ", 1)[1].splitlines()[0]
                for call in ledger_send.call_args_list
            ]
            self.assertEqual(reasons, ["INVALIDATED", "INVALIDATED"])


if __name__ == "__main__":
    unittest.main()
