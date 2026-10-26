from __future__ import annotations

from datetime import date, timedelta

import polars as pl
from rich import print

from src.config import LOOKBACK_DAYS
from src.theta import get_client
from src.util import as_float, first_present


def get_daily_bars(symbol: str, days: int = LOOKBACK_DAYS) -> pl.DataFrame | None:
    """Recent daily OHLC bars."""
    end = date.today()
    start = end - timedelta(days=days + 15)
    try:
        df = get_client().stock_history_eod(
            symbol=symbol,
            start_date=start,
            end_date=end,
        )
        if df is None or df.is_empty():
            return None
        sort_col = "created" if "created" in df.columns else df.columns[0]
        return df.sort(sort_col)
    except Exception as e:
        print(f"[red]EOD error {symbol}: {e}[/red]")
        return None


def get_live_price(
    symbol: str, require_current_day: bool = False
) -> float | None:
    """Get the latest price, optionally refusing prior-session and EOD fallbacks."""
    client = get_client()
    try:
        trades = client.stock_snapshot_trade(
            symbol=symbol, current_day_only=require_current_day
        )
        if trades is not None and not trades.is_empty():
            row = trades.tail(1).to_dicts()[0]
            price = as_float(first_present(row, "price", "close", "last"))
            if price > 0:
                return price
    except Exception as e:
        print(f"[yellow]Live trade miss {symbol}: {e}[/yellow]")

    try:
        quotes = client.stock_snapshot_quote(
            symbol=symbol, current_day_only=require_current_day
        )
        if quotes is not None and not quotes.is_empty():
            row = quotes.tail(1).to_dicts()[0]
            bid = as_float(first_present(row, "bid"))
            ask = as_float(first_present(row, "ask"))
            if bid > 0 and ask > 0:
                return (bid + ask) / 2
            last = as_float(first_present(row, "price", "close", "last"))
            if last > 0:
                return last
    except Exception as e:
        print(f"[yellow]Live quote miss {symbol}: {e}[/yellow]")

    if require_current_day:
        return None

    bars = get_daily_bars(symbol, days=15)
    if bars is None or bars.is_empty() or "close" not in bars.columns:
        return None
    return as_float(bars.tail(1)["close"][0])
