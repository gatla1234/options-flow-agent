from __future__ import annotations

import unittest
from unittest.mock import patch

import polars as pl

from src.flow import _classify_aggressor, scan_symbol
from src.options_data import get_underlying_price
from src.csp import find_csp_opportunities
from datetime import date, timedelta


class AggressorClassificationTests(unittest.TestCase):
    def _expiration(self) -> str:
        return (date.today() + timedelta(days=14)).isoformat()

    @staticmethod
    def _greeks(delta: float = 0.45) -> pl.DataFrame:
        return pl.DataFrame([{"strike": 100.0, "right": "CALL", "delta": delta}])

    @staticmethod
    def _bullish_technicals() -> dict:
        return {
            "trend": "UPTREND",
            "ok_to_own": True,
            "nearby_levels": ["ema_slow"],
            "rs": 0.5,
            "levels": {
                "ema_slow": 98.0,
                "prior_low": 96.0,
                "low_20": 90.0,
                "prior_high": 99.0,
                "high_20": 98.0,
            },
        }

    def test_csp_scan_fails_closed_without_ownership_context(self) -> None:
        with patch("src.csp.get_underlying_price") as underlying_price:
            results = find_csp_opportunities("SOXL", None)

        self.assertEqual(results, [])
        underlying_price.assert_not_called()

    def test_rth_underlying_lookup_requires_current_day_snapshot(self) -> None:
        with (
            patch("src.options_data.is_rth", return_value=True),
            patch("src.options_data.get_live_price", return_value=None) as live_price,
        ):
            result = get_underlying_price("SPY")

        self.assertIsNone(result)
        live_price.assert_called_once_with("SPY", require_current_day=True)

    def test_premium_uses_only_prints_at_or_above_ask(self) -> None:
        trades = pl.DataFrame(
            [
                {"price": 1.05, "size": 150, "bid": 1.00, "ask": 1.05},
                {"price": 1.00, "size": 50, "bid": 1.00, "ask": 1.05},
                {"price": 1.02, "size": 25, "bid": 1.00, "ask": 1.05},
            ]
        )

        result = _classify_aggressor(trades)

        self.assertEqual(result["side"], "BUY")
        self.assertEqual(result["buy_volume"], 150)
        self.assertEqual(result["sell_volume"], 50)
        self.assertEqual(result["buy_premium"], 15_750)
        self.assertEqual(result["total_premium"], 23_300)

    def test_snapshot_premium_estimate_cannot_replace_tape_buy_premium(self) -> None:
        quotes = pl.DataFrame(
            [{"strike": 100.0, "right": "CALL", "bid": 0.99, "ask": 1.01}]
        )
        oi = pl.DataFrame(
            [{"strike": 100.0, "right": "CALL", "open_interest": 200}]
        )
        ohlc = pl.DataFrame(
            [{"strike": 100.0, "right": "CALL", "volume": 400, "close": 1.0}]
        )
        small_buy_tape = pl.DataFrame(
            [{"price": 1.01, "size": 10, "bid": 0.99, "ask": 1.01}]
        )
        technicals = self._bullish_technicals()

        with (
            patch("src.flow.get_underlying_price", return_value=100.0),
            patch("src.flow.get_expirations", return_value=[self._expiration()]),
            patch("src.flow.get_option_chain", return_value=quotes),
            patch("src.flow.get_oi_chain", return_value=oi),
            patch("src.flow.get_ohlc_chain", return_value=ohlc),
            patch("src.flow.get_greeks_chain", return_value=self._greeks()),
            patch("src.flow.get_trade_quotes", return_value=small_buy_tape),
        ):
            results = scan_symbol("SPY", technicals)

        self.assertEqual(results, [])

    def test_alert_candidate_requires_qualifying_buy_tape_and_technicals(self) -> None:
        quotes = pl.DataFrame(
            [{"strike": 100.0, "right": "CALL", "bid": 0.99, "ask": 1.01}]
        )
        oi = pl.DataFrame(
            [{"strike": 100.0, "right": "CALL", "open_interest": 200}]
        )
        ohlc = pl.DataFrame(
            [{"strike": 100.0, "right": "CALL", "volume": 400, "close": 1.0}]
        )
        buy_tape = pl.DataFrame(
            [{"price": 1.01, "size": 400, "bid": 0.99, "ask": 1.01}]
        )
        technicals = self._bullish_technicals()

        with (
            patch("src.flow.get_underlying_price", return_value=100.0),
            patch("src.flow.get_expirations", return_value=[self._expiration()]),
            patch("src.flow.get_option_chain", return_value=quotes),
            patch("src.flow.get_oi_chain", return_value=oi),
            patch("src.flow.get_ohlc_chain", return_value=ohlc),
            patch("src.flow.get_greeks_chain", return_value=self._greeks()),
            patch("src.flow.get_trade_quotes", return_value=buy_tape),
        ):
            results = scan_symbol("SPY", technicals)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["buy_premium"], 40_400)
        self.assertTrue(results[0]["tech_aligned"])
        self.assertEqual(results[0]["dte"], 14)
        self.assertEqual(results[0]["delta"], 0.45)
        self.assertEqual(results[0]["confirmation"]["level"], "prior_high")
        self.assertEqual(results[0]["underlying_target_2r"], 104.0)
        self.assertEqual(results[0]["risk_reward_ratio"], 2.0)
        self.assertEqual(
            results[0]["invalidation"],
            {"price": 98.0, "level": "ema_slow", "condition": "below"},
        )

    def test_contract_outside_delta_range_is_not_recommended(self) -> None:
        quotes = pl.DataFrame(
            [{"strike": 100.0, "right": "CALL", "bid": 0.99, "ask": 1.01}]
        )
        oi = pl.DataFrame(
            [{"strike": 100.0, "right": "CALL", "open_interest": 200}]
        )
        ohlc = pl.DataFrame(
            [{"strike": 100.0, "right": "CALL", "volume": 400, "close": 1.0}]
        )
        buy_tape = pl.DataFrame(
            [{"price": 1.01, "size": 400, "bid": 0.99, "ask": 1.01}]
        )

        with (
            patch("src.flow.get_underlying_price", return_value=100.0),
            patch("src.flow.get_expirations", return_value=[self._expiration()]),
            patch("src.flow.get_option_chain", return_value=quotes),
            patch("src.flow.get_oi_chain", return_value=oi),
            patch("src.flow.get_ohlc_chain", return_value=ohlc),
            patch("src.flow.get_greeks_chain", return_value=self._greeks(0.60)),
            patch("src.flow.get_trade_quotes", return_value=buy_tape) as trades,
        ):
            results = scan_symbol("SPY", self._bullish_technicals())

        self.assertEqual(results, [])
        trades.assert_not_called()

    def test_actual_tape_premium_can_qualify_below_midpoint_estimate(self) -> None:
        quotes = pl.DataFrame(
            [{"strike": 100.0, "right": "CALL", "bid": 0.99, "ask": 1.01}]
        )
        oi = pl.DataFrame(
            [{"strike": 100.0, "right": "CALL", "open_interest": 175}]
        )
        ohlc = pl.DataFrame(
            [{"strike": 100.0, "right": "CALL", "volume": 263, "close": 1.0}]
        )
        buy_tape = pl.DataFrame(
            [{"price": 1.20, "size": 263, "bid": 0.99, "ask": 1.01}]
        )
        technicals = self._bullish_technicals()

        with (
            patch("src.flow.get_underlying_price", return_value=100.0),
            patch("src.flow.get_expirations", return_value=[self._expiration()]),
            patch("src.flow.get_option_chain", return_value=quotes),
            patch("src.flow.get_oi_chain", return_value=oi),
            patch("src.flow.get_ohlc_chain", return_value=ohlc),
            patch("src.flow.get_greeks_chain", return_value=self._greeks()),
            patch("src.flow.get_trade_quotes", return_value=buy_tape),
        ):
            results = scan_symbol("SPY", technicals)

        self.assertEqual(len(results), 1)
        self.assertLess(results[0]["premium_est"], 30_000)
        self.assertAlmostEqual(results[0]["buy_premium"], 31_560)

    def test_alert_candidate_is_rejected_without_technical_confluence(self) -> None:
        quotes = pl.DataFrame(
            [{"strike": 100.0, "right": "CALL", "bid": 0.99, "ask": 1.01}]
        )
        oi = pl.DataFrame(
            [{"strike": 100.0, "right": "CALL", "open_interest": 200}]
        )
        ohlc = pl.DataFrame(
            [{"strike": 100.0, "right": "CALL", "volume": 400, "close": 1.0}]
        )
        buy_tape = pl.DataFrame(
            [{"price": 1.01, "size": 400, "bid": 0.99, "ask": 1.01}]
        )

        with (
            patch("src.flow.get_underlying_price", return_value=100.0),
            patch("src.flow.get_expirations", return_value=[self._expiration()]),
            patch("src.flow.get_option_chain", return_value=quotes),
            patch("src.flow.get_oi_chain", return_value=oi),
            patch("src.flow.get_ohlc_chain", return_value=ohlc),
            patch("src.flow.get_greeks_chain", return_value=self._greeks()),
            patch("src.flow.get_trade_quotes", return_value=buy_tape),
        ):
            results = scan_symbol("SPY", None)

        self.assertEqual(results, [])


if __name__ == "__main__":
    unittest.main()
