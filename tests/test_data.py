from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import requests
import polars as pl

from src.data import get_live_price
from src.swing_signals import build_context


class LivePriceTests(unittest.TestCase):
    @staticmethod
    def _response(price: float, timestamp: datetime) -> Mock:
        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            "c": price,
            "t": timestamp.timestamp(),
        }
        return response

    def test_uses_fresh_finnhub_quote_during_market_hours(self) -> None:
        response = self._response(600.25, datetime.now(timezone.utc))
        with (
            patch.dict(os.environ, {"FINNHUB_API_KEY": "test-key"}),
            patch("src.data.requests.get", return_value=response) as request,
            patch("src.data.get_client") as get_client,
        ):
            price = get_live_price("SPY", require_current_day=True)

        self.assertEqual(price, 600.25)
        self.assertEqual(request.call_args.kwargs["params"]["symbol"], "SPY")
        self.assertEqual(request.call_args.kwargs["params"]["token"], "test-key")
        get_client.assert_not_called()

    def test_rejects_stale_finnhub_quote_for_intraday_scan(self) -> None:
        stale_time = datetime.now(timezone.utc) - timedelta(minutes=10)
        response = self._response(600.25, stale_time)
        trades = Mock()
        trades.is_empty.return_value = True
        client = Mock()
        client.stock_snapshot_trade.return_value = trades
        quotes = Mock()
        quotes.is_empty.return_value = True
        client.stock_snapshot_quote.return_value = quotes

        with (
            patch.dict(os.environ, {"FINNHUB_API_KEY": "test-key"}),
            patch("src.data.requests.get", return_value=response),
            patch("src.data.get_client", return_value=client),
        ):
            price = get_live_price("SPY", require_current_day=True)

        self.assertIsNone(price)
        client.stock_snapshot_trade.assert_called_once_with(
            symbol="SPY", current_day_only=True
        )

    def test_falls_back_to_theta_when_finnhub_is_not_configured(self) -> None:
        trades = Mock()
        trades.is_empty.return_value = False
        trades.tail.return_value.to_dicts.return_value = [{"price": 500.5}]
        client = Mock()
        client.stock_snapshot_trade.return_value = trades

        with (
            patch.dict(os.environ, {}, clear=True),
            patch("src.data.get_client", return_value=client),
        ):
            price = get_live_price("SPY", require_current_day=True)

        self.assertEqual(price, 500.5)
        client.stock_snapshot_trade.assert_called_once_with(
            symbol="SPY", current_day_only=True
        )

    def test_falls_back_to_theta_when_finnhub_request_fails(self) -> None:
        trades = Mock()
        trades.is_empty.return_value = False
        trades.tail.return_value.to_dicts.return_value = [{"price": 499.75}]
        client = Mock()
        client.stock_snapshot_trade.return_value = trades

        with (
            patch.dict(os.environ, {"FINNHUB_API_KEY": "test-key"}),
            patch(
                "src.data.requests.get",
                side_effect=requests.ConnectionError("network unavailable"),
            ),
            patch("src.data.get_client", return_value=client),
        ):
            price = get_live_price("SPY", require_current_day=True)

        self.assertEqual(price, 499.75)
        client.stock_snapshot_trade.assert_called_once()

    def test_swing_context_does_not_use_eod_price_as_intraday_price(self) -> None:
        daily_bars = pl.DataFrame(
            {
                "close": [100.0],
                "high": [101.0],
                "low": [99.0],
                "volume": [1000],
                "ema_fast": [99.0],
                "ema_slow": [98.0],
                "avg_volume": [900.0],
                "high_20": [102.0],
                "low_20": [95.0],
            }
        )
        with (
            patch("src.swing_signals._prep", return_value=daily_bars),
            patch("src.swing_signals.get_daily_bars", return_value=daily_bars),
            patch("src.swing_signals.is_rth", return_value=True),
            patch("src.swing_signals.get_live_price", return_value=None) as live_price,
        ):
            context = build_context("SPY")

        self.assertIsNone(context)
        live_price.assert_called_once_with("SPY", require_current_day=True)


if __name__ == "__main__":
    unittest.main()
