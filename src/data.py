from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import polars as pl
import requests
from rich import print

from src.config import FINNHUB_MAX_QUOTE_AGE_SECONDS, LOOKBACK_DAYS
from src.theta import get_client
from src.util import as_float, first_present

ET = ZoneInfo("America/New_York")


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


def _get_finnhub_price(symbol: str, require_current_day: bool) -> float | None:
    api_key = os.getenv("FINNHUB_API_KEY")
    if not api_key:
        return None

    try:
        response = requests.get(
            "https://finnhub.io/api/v1/quote",
            params={"symbol": symbol, "token": api_key},
            timeout=10,
        )
    except requests.RequestException as exc:
        print(
            f"[yellow]Finnhub quote request failed for {symbol}: "
            f"{type(exc).__name__}[/yellow]"
        )
        return None

    if response.status_code != 200:
        print(
            f"[yellow]Finnhub quote failed for {symbol}: "
            f"HTTP {response.status_code}[/yellow]"
        )
        return None

    try:
        quote = response.json()
        price = as_float(quote.get("c"))
        timestamp = as_float(quote.get("t"))
    except (AttributeError, TypeError, ValueError):
        print(f"[yellow]Finnhub returned an invalid quote for {symbol}[/yellow]")
        return None

    if price <= 0 or timestamp <= 0:
        print(f"[yellow]Finnhub returned no usable price for {symbol}[/yellow]")
        return None

    if require_current_day:
        quote_time = datetime.fromtimestamp(timestamp, timezone.utc)
        now = datetime.now(timezone.utc)
        age_seconds = (now - quote_time).total_seconds()
        if (
            quote_time.astimezone(ET).date() != now.astimezone(ET).date()
            or age_seconds < 0
            or age_seconds > FINNHUB_MAX_QUOTE_AGE_SECONDS
        ):
            print(
                f"[yellow]Finnhub quote for {symbol} is not fresh enough "
                "for an intraday alert[/yellow]"
            )
            return None

    return price


def get_live_price(
    symbol: str, require_current_day: bool = False
) -> float | None:
    """Get the latest price, optionally refusing prior-session and EOD fallbacks."""
    finnhub_price = _get_finnhub_price(symbol, require_current_day)
    if finnhub_price is not None:
        return finnhub_price

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
