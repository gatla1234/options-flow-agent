from __future__ import annotations

import unittest
from unittest.mock import patch

import polars as pl

from src.flow import (
    _classify_aggressor,
    _flow_confirmation,
    run_flow_scan,
    scan_symbol,
)
from src.options_data import get_underlying_price
from src.csp import find_csp_opportunities
from datetime import date, timedelta


NO_RECOMMENDATION_PHRASES = (
    "not a recommendation",
    "not financial advice",
    "not a trade instruction",
    "not a buy, sell, or hold instruction",
)


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
            "prior_day_high": 99.0,
            "prior_day_low": 95.0,
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

    def test_equal_buy_premium_candidates_rank_by_buy_share(self) -> None:
        quotes = pl.DataFrame(
            [
                {"strike": 100.0, "right": "CALL", "bid": 0.99, "ask": 1.01},
                {"strike": 101.0, "right": "CALL", "bid": 0.99, "ask": 1.01},
            ]
        )
        oi = pl.DataFrame(
            [
                {"strike": 100.0, "right": "CALL", "open_interest": 200},
                {"strike": 101.0, "right": "CALL", "open_interest": 400},
            ]
        )
        ohlc = pl.DataFrame(
            [
                {"strike": 100.0, "right": "CALL", "volume": 400, "close": 1.0},
                {"strike": 101.0, "right": "CALL", "volume": 600, "close": 1.0},
            ]
        )
        greeks = pl.DataFrame(
            [
                {"strike": 100.0, "right": "CALL", "delta": 0.45},
                {"strike": 101.0, "right": "CALL", "delta": 0.45},
            ]
        )

        def trades_for_strike(symbol, expiration, strike, right):
            self.assertEqual(symbol, "SPY")
            self.assertEqual(expiration, self._expiration())
            self.assertEqual(right, "CALL")
            records = [
                {"price": 1.01, "size": 400, "bid": 0.99, "ask": 1.01}
            ]
            if strike == 101.0:
                records.append(
                    {"price": 1.00, "size": 200, "bid": 0.99, "ask": 1.01}
                )
            return pl.DataFrame(records)

        with (
            patch("src.flow.get_underlying_price", return_value=100.0),
            patch("src.flow.get_expirations", return_value=[self._expiration()]),
            patch("src.flow.get_option_chain", return_value=quotes),
            patch("src.flow.get_oi_chain", return_value=oi),
            patch("src.flow.get_ohlc_chain", return_value=ohlc),
            patch("src.flow.get_greeks_chain", return_value=greeks),
            patch("src.flow.get_trade_quotes", side_effect=trades_for_strike),
        ):
            results = scan_symbol("SPY", self._bullish_technicals())

        self.assertEqual([result["strike"] for result in results], [100.0, 101.0])
        self.assertEqual(results[0]["buy_premium"], results[1]["buy_premium"])
        self.assertGreater(results[0]["buy_pct"], results[1]["buy_pct"])

    def test_tape_ranking_checks_every_filter_qualified_contract(self) -> None:
        strikes = [100.0 + index for index in range(7)]
        quotes = pl.DataFrame(
            [
                {"strike": strike, "right": "CALL", "bid": 0.99, "ask": 1.01}
                for strike in strikes
            ]
        )
        oi = pl.DataFrame(
            [
                {"strike": strike, "right": "CALL", "open_interest": 200}
                for strike in strikes
            ]
        )
        ohlc = pl.DataFrame(
            [
                {
                    "strike": strike,
                    "right": "CALL",
                    "volume": 1000 if strike != strikes[-1] else 400,
                    "close": 1.0,
                }
                for strike in strikes
            ]
        )
        greeks = pl.DataFrame(
            [
                {"strike": strike, "right": "CALL", "delta": 0.45}
                for strike in strikes
            ]
        )

        def tape_for_strike(symbol, expiration, strike, right):
            if strike == strikes[-1]:
                return pl.DataFrame(
                    [{"price": 1.01, "size": 400, "bid": 0.99, "ask": 1.01}]
                )
            return pl.DataFrame()

        with (
            patch("src.flow.get_underlying_price", return_value=100.0),
            patch("src.flow.get_expirations", return_value=[self._expiration()]),
            patch("src.flow.get_option_chain", return_value=quotes),
            patch("src.flow.get_oi_chain", return_value=oi),
            patch("src.flow.get_ohlc_chain", return_value=ohlc),
            patch("src.flow.get_greeks_chain", return_value=greeks),
            patch("src.flow.get_trade_quotes", side_effect=tape_for_strike) as get_tape,
        ):
            results = scan_symbol("SPY", self._bullish_technicals())

        self.assertEqual(get_tape.call_count, len(strikes))
        self.assertEqual([result["strike"] for result in results], [strikes[-1]])
        self.assertEqual(results[0]["buy_premium"], 40_400)

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

    def test_day_trade_confirmation_requires_prior_day_breakout(self) -> None:
        technicals = self._bullish_technicals()

        call_confirmation = _flow_confirmation(
            technicals, "CALL", 100.0, "daytrade"
        )
        put_confirmation = _flow_confirmation(
            technicals, "PUT", 94.0, "daytrade"
        )

        self.assertEqual(call_confirmation["level"], "prior_day_high")
        self.assertEqual(call_confirmation["price"], 99.0)
        self.assertEqual(put_confirmation["level"], "prior_day_low")
        self.assertEqual(put_confirmation["price"], 95.0)
        self.assertIsNone(_flow_confirmation(technicals, "CALL", 99.0, "daytrade"))
        self.assertIsNone(_flow_confirmation(technicals, "PUT", 95.0, "daytrade"))

    def test_day_trade_checks_all_expirations_and_uses_dte_tiered_premium(self) -> None:
        quotes = pl.DataFrame(
            [{"strike": 100.0, "right": "CALL", "bid": 0.99, "ask": 1.01}]
        )
        oi = pl.DataFrame(
            [{"strike": 100.0, "right": "CALL", "open_interest": 300}]
        )
        ohlc = pl.DataFrame(
            [{"strike": 100.0, "right": "CALL", "volume": 600, "close": 1.0}]
        )
        buy_tape = pl.DataFrame(
            [{"price": 1.01, "size": 600, "bid": 0.99, "ask": 1.01}]
        )
        expirations = [
            (date.today() + timedelta(days=dte)).isoformat() for dte in (0, 2, 7)
        ]
        funnel: dict[str, int] = {}

        with (
            patch("src.flow.get_underlying_price", return_value=100.0),
            patch("src.flow.get_expirations", return_value=expirations) as get_exps,
            patch("src.flow.get_option_chain", return_value=quotes) as get_quotes,
            patch("src.flow.get_oi_chain", return_value=oi),
            patch("src.flow.get_ohlc_chain", return_value=ohlc),
            patch("src.flow.get_greeks_chain", return_value=self._greeks()),
            patch("src.flow.get_trade_quotes", return_value=buy_tape),
        ):
            results = scan_symbol(
                "SPY",
                self._bullish_technicals(),
                stream="daytrade",
                funnel=funnel,
            )

        get_exps.assert_called_once_with("SPY", max_dte=7, min_dte=0)
        self.assertEqual(get_quotes.call_count, len(expirations))
        self.assertEqual([result["dte"] for result in results], [2, 7])
        self.assertTrue(all(result["stream"] == "daytrade" for result in results))
        self.assertEqual(funnel["dte"], 3)
        self.assertEqual(funnel["delta"], 3)
        self.assertEqual(funnel["strike"], 3)
        self.assertEqual(funnel["volume_oi"], 3)
        self.assertEqual(funnel["premium"], 2)
        self.assertEqual(funnel["buy_pct"], 2)
        self.assertEqual(funnel["trend"], 2)
        self.assertEqual(funnel["confirmation"], 2)

    def test_run_scan_keeps_stream_labels_and_routes_distinctly(self) -> None:
        def result(stream: str) -> dict:
            right = "CALL" if stream == "confirmed" else "PUT"
            return {
                "stream": stream,
                "right": right,
                "symbol": "SPY",
                "strike": 100.0,
                "expiration": "2026-11-06",
                "dte": 11,
                "delta": 0.45,
                "mid": 1.0,
                "bid": 0.99,
                "ask": 1.01,
                "buy_premium": 40_000,
                "buy_pct": 0.8,
                "volume": 400,
                "oi": 200,
                "vol_oi": 2.0,
                "otm_pct": 0.0,
                "underlying": 100.0,
                "conditions": ["tape"],
                "tech_aligned": True,
                "tech_note": "uptrend",
                "underlying_target_2r": 104.0,
                "risk_reward_ratio": 2.0,
                "invalidation": {"condition": "below", "price": 98.0, "level": "EMA"},
                "confirmation": {"level": "prior_high", "price": 99.0},
            }

        def fake_scan_symbol(
            symbol: str, tech: dict | None, stream: str, funnel: dict[str, int]
        ) -> list[dict]:
            self.assertEqual(symbol, "SPY")
            self.assertIsNotNone(tech)
            self.assertEqual(funnel, {})
            return [result(stream)]

        with (
            patch("src.flow.FLOW_TICKERS", ["SPY"]),
            patch("src.flow.build_context", return_value=self._bullish_technicals()),
            patch("src.flow.scan_symbol", side_effect=fake_scan_symbol),
            patch("src.flow.log_funnel_scan"),
            patch("src.flow.send_alert") as send_alert,
        ):
            run_flow_scan()

        self.assertEqual(send_alert.call_count, 2)
        titles = [call.kwargs["title"] for call in send_alert.call_args_list]
        self.assertIn("CONFIRMED CALL", titles[0])
        self.assertIn("DAY-TRADE PUT", titles[1])
        descriptions = [
            call.kwargs["description"] for call in send_alert.call_args_list
        ]
        self.assertIn("Suggestion: consider long calls on SPY.", descriptions[0])
        self.assertIn("Suggestion: consider long puts on SPY.", descriptions[1])
        for description in descriptions:
            for phrase in NO_RECOMMENDATION_PHRASES:
                self.assertNotIn(phrase, description.lower())
        self.assertEqual(
            [call.kwargs["stream"] for call in send_alert.call_args_list],
            ["confirmed", "daytrade"],
        )


if __name__ == "__main__":
    unittest.main()
