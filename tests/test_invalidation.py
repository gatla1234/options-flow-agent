from __future__ import annotations

import unittest

from src.swing_signals import confirmed_flow_level, underlying_invalidation


class UnderlyingInvalidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = {
            "live": 100.0,
            "levels": {
                "ema_slow": 98.0,
                "prior_low": 96.0,
                "low_20": 90.0,
                "prior_high": 104.0,
                "high_20": 110.0,
            },
        }

    def test_bullish_setup_uses_nearest_daily_support_below_price(self) -> None:
        result = underlying_invalidation(self.context, "BULLISH")

        self.assertEqual(
            result,
            {"price": 98.0, "level": "ema_slow", "condition": "below"},
        )

    def test_bearish_setup_uses_nearest_daily_resistance_above_price(self) -> None:
        result = underlying_invalidation(self.context, "BEARISH")

        self.assertEqual(
            result,
            {"price": 104.0, "level": "prior_high", "condition": "above"},
        )

    def test_reference_price_selects_the_relevant_side_of_levels(self) -> None:
        result = underlying_invalidation(
            self.context, "BULLISH", reference_price=95.0
        )

        self.assertEqual(
            result,
            {"price": 90.0, "level": "low_20", "condition": "below"},
        )

    def test_returns_none_when_no_valid_reference_level_exists(self) -> None:
        context = {"live": 100.0, "levels": {"prior_low": 0, "low_20": 0}}

        self.assertIsNone(underlying_invalidation(context, "BULLISH"))
        self.assertIsNone(underlying_invalidation(None, "BULLISH"))

    def test_call_confirmation_requires_break_above_daily_resistance(self) -> None:
        context = {
            "levels": {"prior_high": 101.0, "high_20": 103.0},
        }

        self.assertIsNone(confirmed_flow_level(context, "CALL", 100.0))
        self.assertEqual(
            confirmed_flow_level(context, "CALL", 102.0),
            {"price": 101.0, "level": "prior_high", "direction": "BULLISH"},
        )

    def test_put_confirmation_requires_break_below_daily_support(self) -> None:
        context = {
            "levels": {"prior_low": 99.0, "low_20": 97.0},
        }

        self.assertIsNone(confirmed_flow_level(context, "PUT", 100.0))
        self.assertEqual(
            confirmed_flow_level(context, "PUT", 98.0),
            {"price": 99.0, "level": "prior_low", "direction": "BEARISH"},
        )


if __name__ == "__main__":
    unittest.main()
