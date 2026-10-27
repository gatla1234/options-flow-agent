from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from src.config import ALERT_COOLDOWN_HOURS, DAY_COOLDOWN_SEC
from src import discord_alerts, state
from src.discord_alerts import flush_alert_queue, send_alert


class DiscordRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        db_patch = patch.object(state, "DB_PATH", Path(temp_dir.name) / "alerts.db")
        db_patch.start()
        self.addCleanup(db_patch.stop)
        with discord_alerts._DELIVERY_LOCK:
            discord_alerts._SENT_AT.clear()

    def tearDown(self) -> None:
        with discord_alerts._DELIVERY_LOCK:
            discord_alerts._SENT_AT.clear()

    def test_confirmed_flow_uses_its_dedicated_webhook_and_cooldown(self) -> None:
        response = Mock(status_code=204)
        with (
            patch("src.discord_alerts.WEBHOOKS", {"confirmed": "confirmed-url"}),
            patch("src.discord_alerts.requests.post", return_value=response) as post,
            patch("src.discord_alerts.recently_sent", return_value=False) as recently_sent,
            patch("src.discord_alerts.log_alert"),
        ):
            sent = send_alert(
                title="CONFIRMED CALL",
                description="test",
                alert_type="flow",
                alert_key="confirmed:test",
                stream="confirmed",
            )

        self.assertTrue(sent)
        self.assertEqual(post.call_args.args[0], "confirmed-url")
        recently_sent.assert_called_once_with(
            "confirmed:test", ALERT_COOLDOWN_HOURS["confirmed"]
        )

    def test_daytrade_flow_uses_its_webhook_and_sixty_minute_cooldown(self) -> None:
        response = Mock(status_code=204)
        with (
            patch("src.discord_alerts.WEBHOOKS", {"daytrade": "daytrade-url"}),
            patch("src.discord_alerts.requests.post", return_value=response) as post,
            patch(
                "src.discord_alerts.recently_sent_seconds", return_value=False
            ) as recently_sent,
            patch("src.discord_alerts.log_alert"),
        ):
            sent = send_alert(
                title="DAY-TRADE PUT",
                description="test",
                alert_type="flow",
                alert_key="daytrade:test",
                stream="daytrade",
            )

        self.assertTrue(sent)
        self.assertEqual(post.call_args.args[0], "daytrade-url")
        recently_sent.assert_called_once_with(
            "daytrade:test", DAY_COOLDOWN_SEC
        )

    def test_missing_dedicated_webhook_does_not_fall_back_to_another_route(self) -> None:
        with (
            patch("src.discord_alerts.WEBHOOKS", {}),
            patch("src.discord_alerts.requests.post") as post,
        ):
            sent = send_alert(
                title="DAY-TRADE CALL",
                description="test",
                alert_type="flow",
                stream="daytrade",
            )

        self.assertFalse(sent)
        post.assert_not_called()

    def test_reports_use_their_dedicated_webhook(self) -> None:
        response = Mock(status_code=204)
        with (
            patch("src.discord_alerts.WEBHOOKS", {"reports": "reports-url"}),
            patch("src.discord_alerts.requests.post", return_value=response) as post,
            patch("src.discord_alerts.log_alert"),
        ):
            sent = send_alert(
                title="Pre-market report",
                description="test",
                alert_type="health",
                stream="reports",
            )

        self.assertTrue(sent)
        self.assertEqual(post.call_args.args[0], "reports-url")

    def test_scan_summary_uses_its_dedicated_webhook(self) -> None:
        response = Mock(status_code=204)
        with (
            patch("src.discord_alerts.WEBHOOKS", {"scan_summary": "summary-url"}),
            patch("src.discord_alerts.requests.post", return_value=response) as post,
        ):
            sent = send_alert(
                title="Daily scan summary",
                description="test",
                alert_type="health",
                stream="scan_summary",
                skip_dedupe=True,
            )

        self.assertTrue(sent)
        self.assertEqual(post.call_args.args[0], "summary-url")

    def test_queued_scan_summary_failure_notifies_system_health(self) -> None:
        state.enqueue_discord_alert(
            {
                "route": "scan_summary",
                "title": "Daily scan summary — SWING",
                "description": "test",
                "color": 0x5865F2,
                "fields": None,
                "alert_type": "health",
                "alert_key": None,
                "symbol": None,
                "price": None,
                "payload": None,
                "cross_stream_key": None,
            }
        )
        with (
            patch("src.discord_alerts.WEBHOOKS", {"scan_summary": "summary-url"}),
            patch("src.discord_alerts.requests.post", return_value=Mock(status_code=500)),
            patch("src.discord_alerts._notify_scan_summary_delivery_failure") as notify,
        ):
            self.assertEqual(flush_alert_queue(), 0)

        notify.assert_called_once()
        self.assertIn("SWING", notify.call_args.args[0])
        self.assertTrue(state.queued_discord_alerts(limit=1))

    def test_request_failure_does_not_log_webhook_url(self) -> None:
        webhook = "https://discord.example/webhooks/private-token"
        logged = []
        with (
            patch("src.discord_alerts.WEBHOOKS", {"confirmed": webhook}),
            patch(
                "src.discord_alerts.requests.post",
                side_effect=requests.Timeout(f"request timed out for {webhook}"),
            ),
            patch("src.discord_alerts.print", side_effect=logged.append),
        ):
            sent = send_alert(
                title="CONFIRMED CALL",
                description="test",
                alert_type="flow",
                stream="confirmed",
            )

        self.assertFalse(sent)
        self.assertTrue(logged)
        self.assertNotIn(webhook, " ".join(str(message) for message in logged))

    def test_http_failure_does_not_log_response_body(self) -> None:
        webhook = "https://discord.example/webhooks/private-token"
        response = Mock(
            status_code=500,
            text=f"server error for {webhook}",
        )
        logged = []
        with (
            patch("src.discord_alerts.WEBHOOKS", {"confirmed": webhook}),
            patch("src.discord_alerts.requests.post", return_value=response),
            patch("src.discord_alerts.print", side_effect=logged.append),
        ):
            sent = send_alert(
                title="CONFIRMED CALL",
                description="test",
                alert_type="flow",
                stream="confirmed",
            )

        self.assertFalse(sent)
        self.assertTrue(logged)
        self.assertNotIn(webhook, " ".join(str(message) for message in logged))

    def test_recent_contract_signal_in_other_stream_is_suppressed(self) -> None:
        with (
            patch("src.discord_alerts.WEBHOOKS", {"daytrade": "daytrade-url"}),
            patch(
                "src.discord_alerts.recently_sent_cross_stream",
                return_value=True,
            ) as recently_sent,
            patch("src.discord_alerts.requests.post") as post,
        ):
            sent = send_alert(
                title="DAY-TRADE CALL",
                description="test",
                alert_type="flow",
                stream="daytrade",
                cross_stream_key="contract:SPY:2026-11-06:100:CALL",
            )

        self.assertFalse(sent)
        recently_sent.assert_called_once()
        post.assert_not_called()

    def test_rate_limit_queues_and_prioritizes_confirmed_alerts(self) -> None:
        response = Mock(status_code=204)
        with (
            patch(
                "src.discord_alerts.WEBHOOKS",
                {"health": "health-url", "confirmed": "confirmed-url", "daytrade": "daytrade-url"},
            ),
            patch("src.discord_alerts.requests.post", return_value=response) as post,
            patch("src.discord_alerts.time.time", return_value=100.0) as clock,
            patch("src.discord_alerts.log_alert"),
        ):
            for index in range(8):
                self.assertTrue(
                    send_alert(
                        title=f"health-{index}",
                        description="test",
                        alert_type="health",
                    )
                )
            self.assertTrue(
                send_alert(
                    title="DAY-TRADE CALL",
                    description="test",
                    alert_type="flow",
                    stream="daytrade",
                )
            )
            self.assertTrue(
                send_alert(
                    title="CONFIRMED CALL",
                    description="test",
                    alert_type="flow",
                    stream="confirmed",
                )
            )
            self.assertEqual(post.call_count, 8)
            queued_titles = [
                request["title"] for request in state.queued_discord_alerts(limit=5)
            ]
            self.assertEqual(queued_titles, ["CONFIRMED CALL", "DAY-TRADE CALL"])

            discord_alerts._SENT_AT.clear()
            clock.return_value = 120.0
            self.assertEqual(flush_alert_queue(), 0)
            self.assertEqual(post.call_count, 8)

            clock.return_value = 161.0
            self.assertEqual(flush_alert_queue(), 2)

        sent_titles = [call.kwargs["json"]["embeds"][0]["title"] for call in post.call_args_list]
        self.assertEqual(sent_titles[-2:], ["CONFIRMED CALL", "DAY-TRADE CALL"])
        self.assertEqual(state.queued_discord_alerts(limit=5), [])


if __name__ == "__main__":
    unittest.main()
