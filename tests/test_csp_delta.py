from __future__ import annotations

import unittest
from contextlib import ExitStack
from datetime import date, timedelta
from unittest.mock import patch

import polars as pl

from src.csp import find_csp_opportunities, scan_csps


class CspDeltaAvailabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tech = {
            "ok_to_own": True,
            "trend": "UPTREND",
            "levels": {
                "ema_slow": 98.0,
                "prior_low": 97.0,
                "low_20": 95.0,
                "ema_fast": 99.0,
                "prior_high": 102.0,
                "high_20": 105.0,
            },
        }
        self.chain = pl.DataFrame(
            [{"strike": 95.0, "right": "PUT", "bid": 1.99, "ask": 2.01}]
        )

    def _patch_scan_data(self, stack: ExitStack) -> None:
        stack.enter_context(patch("src.csp.get_underlying_price", return_value=100.0))
        stack.enter_context(patch(
                "src.csp.get_expirations",
                return_value=[
                    (date.today() + timedelta(days=14)).isoformat()
                ],
            ))
        stack.enter_context(patch("src.csp.get_option_chain", return_value=self.chain))
        stack.enter_context(patch("src.csp.get_greeks_chain", return_value=None))

    def test_candidate_is_kept_with_delta_unavailable(self) -> None:
        with ExitStack() as stack:
            self._patch_scan_data(stack)
            opportunities = find_csp_opportunities("SPY", self.tech)

        self.assertEqual(len(opportunities), 1)
        self.assertIsNone(opportunities[0]["delta"])

    def test_alert_explicitly_marks_unavailable_delta(self) -> None:
        with ExitStack() as stack:
            stack.enter_context(patch("src.csp.CSP_TICKERS", ["SPY"]))
            stack.enter_context(patch("src.csp.build_context", return_value=self.tech))
            self._patch_scan_data(stack)
            send_alert = stack.enter_context(patch("src.csp.send_alert"))
            stack.enter_context(patch("src.csp.log_funnel_scan"))
            scan_csps()

        self.assertEqual(send_alert.call_count, 1)
        self.assertIn(
            "**Delta:** unavailable (Greeks not retrieved)",
            send_alert.call_args.kwargs["description"],
        )
        self.assertIsNone(send_alert.call_args.kwargs["payload"]["delta"])


if __name__ == "__main__":
    unittest.main()
