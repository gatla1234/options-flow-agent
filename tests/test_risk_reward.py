from __future__ import annotations

import unittest
from datetime import date, timedelta
from unittest.mock import patch

import polars as pl

from src.csp import find_csp_opportunities, scan_csps
from src.swing_signals import calculate_signals, format_swing_description


class RiskRewardTests(unittest.TestCase):
    def test_swing_signal_reports_two_to_one_underlying_reward(self) -> None:
        context = {
            "live": 110.0,
            "close": 109.0,
            "ema_fast": 108.0,
            "ema_slow": 105.0,
            "volume_ratio": 1.6,
            "rs": 1.2,
            "nearby_levels": ["prior_high"],
            "levels": {"prior_low": 104.0, "low_20": 100.0},
            "trend": "UPTREND",
            "volume": 1_000_000,
            "benchmark": "QQQ",
        }

        signal = calculate_signals(context)

        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal["invalidation"], 104.0)
        self.assertEqual(signal["target_2r"], 122.0)
        self.assertEqual(signal["risk_reward_ratio"], 2.0)
        description = format_swing_description(signal)
        self.assertIn(
            "Suggested take-profit / reassess price (2R reference):** $122.00",
            description,
        )
        self.assertIn("Risk:Reward (underlying):** 1:2.0", description)

    def test_bearish_swing_target_is_below_entry(self) -> None:
        context = {
            "live": 90.0,
            "close": 91.0,
            "ema_fast": 92.0,
            "ema_slow": 95.0,
            "volume_ratio": 1.6,
            "rs": -1.2,
            "nearby_levels": ["prior_low"],
            "levels": {"prior_low": 94.0, "prior_high": 98.0, "low_20": 88.0},
            "trend": "DOWNTREND",
            "volume": 1_000_000,
            "benchmark": "QQQ",
        }

        signal = calculate_signals(context)

        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal["invalidation"], 98.0)
        self.assertEqual(signal["target_2r"], 74.0)

    def test_csp_reports_max_reward_to_max_loss_at_expiration(self) -> None:
        expiration = (date.today() + timedelta(days=14)).isoformat()
        chain = pl.DataFrame(
            [{"strike": 95.0, "right": "PUT", "bid": 1.91, "ask": 2.09, "delta": -0.2}]
        )
        technicals = {
            "ok_to_own": True,
            "trend": "UPTREND",
            "levels": {"ema_slow": 96.0, "prior_low": 95.0, "low_20": 90.0},
        }

        with (
            patch("src.csp.get_underlying_price", return_value=100.0),
            patch("src.csp.get_expirations", return_value=[expiration]),
            patch("src.csp.get_option_chain", return_value=chain),
            patch("src.csp.get_greeks_chain", return_value=chain),
            patch("src.csp.join_on_contract", return_value=chain),
        ):
            opportunities = find_csp_opportunities("SOXL", technicals)

        self.assertEqual(len(opportunities), 1)
        opportunity = opportunities[0]
        self.assertEqual(opportunity["max_profit"], 200.0)
        self.assertEqual(opportunity["max_loss"], 9_300.0)
        self.assertEqual(opportunity["reward_risk_ratio"], 0.0215)

    def test_csp_discord_alert_labels_max_reward_risk(self) -> None:
        opportunity = {
            "symbol": "SOXL",
            "strike": 95.0,
            "expiration": "2026-11-09",
            "dte": 14,
            "premium": 2.0,
            "premium_pct": 2.11,
            "annualized": 55.0,
            "cash_required": 9_500.0,
            "max_profit": 200.0,
            "max_loss": 9_300.0,
            "otm_pct": 5.0,
            "underlying": 100.0,
            "delta": -0.2,
            "iv": None,
            "support_note": "ema_slow $96.00 (1.0% away)",
            "trend": "UPTREND",
            "ok_to_own": True,
            "bid": 1.9,
            "ask": 2.1,
            "spread_pct": 10.0,
            "invalidation": None,
            "score": 55.0,
        }

        with (
            patch("src.csp.CSP_TICKERS", ["SOXL"]),
            patch("src.csp.build_context", return_value={}),
            patch("src.csp.find_csp_opportunities", return_value=[opportunity]),
            patch("src.csp.send_alert") as send_alert,
        ):
            scan_csps()

        description = send_alert.call_args.kwargs["description"]
        self.assertIn("Max reward:risk at expiration:** 1:46.5", description)
        self.assertIn("$200 max profit : $9,300 max loss", description)


if __name__ == "__main__":
    unittest.main()
