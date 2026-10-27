from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import polars as pl
import requests
from rich import print

from src.config import FINNHUB_MAX_QUOTE_AGE_SECONDS, LOOKBACK_DAYS
from src.theta import get_client
from src.util import as_float, first_present

ET = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class FinnhubQuote:
    price: float
    previous_close: float | None
    timestamp: datetime


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


def get_finnhub_quote(symbol: str) -> FinnhubQuote | None:
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
        previous_close = as_float(quote.get("pc"))
        timestamp = as_float(quote.get("t"))
        quoted_at = datetime.fromtimestamp(timestamp, timezone.utc)
    except (AttributeError, OSError, OverflowError, TypeError, ValueError):
        print(f"[yellow]Finnhub returned an invalid quote for {symbol}[/yellow]")
        return None

    if (
        not math.isfinite(price)
        or price <= 0
        or not math.isfinite(timestamp)
        or timestamp <= 0
    ):
        print(f"[yellow]Finnhub returned no usable price for {symbol}[/yellow]")
        return None

    return FinnhubQuote(
        price,
        previous_close
        if math.isfinite(previous_close) and previous_close > 0
        else None,
        quoted_at,
    )


def _get_finnhub_price(symbol: str, require_current_day: bool) -> float | None:
    quote = get_finnhub_quote(symbol)
    if quote is None:
        return None

    if require_current_day:
        now = datetime.now(timezone.utc)
        age_seconds = (now - quote.timestamp).total_seconds()
        if (
            quote.timestamp.astimezone(ET).date() != now.astimezone(ET).date()
            or age_seconds < 0
            or age_seconds > FINNHUB_MAX_QUOTE_AGE_SECONDS
        ):
            print(
                f"[yellow]Finnhub quote for {symbol} is not fresh enough "
                "for an intraday alert[/yellow]"
            )
            return None

    return quote.price


def _snapshot_timestamp(row: dict) -> datetime | None:
    for key in ("timestamp", "datetime", "created", "date_time", "time"):
        value = row.get(key)
        if isinstance(value, datetime):
            return value.replace(tzinfo=ET) if value.tzinfo is None else value
        if isinstance(value, (int, float)):
            try:
                epoch = value / 1000 if value > 10_000_000_000 else value
                return datetime.fromtimestamp(epoch, timezone.utc)
            except (OSError, OverflowError, ValueError):
                continue
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                continue
            if "T" not in value and " " not in value:
                continue
            return parsed.replace(tzinfo=ET) if parsed.tzinfo is None else parsed

    snapshot_date = row.get("date")
    milliseconds = row.get("ms_of_day")
    if snapshot_date is not None and milliseconds is not None:
        try:
            day = (
                snapshot_date
                if isinstance(snapshot_date, date)
                else date.fromisoformat(str(snapshot_date)[:10])
            )
            ms = int(milliseconds)
            if ms < 0 or ms >= 24 * 60 * 60 * 1000:
                return None
            seconds, millis = divmod(ms, 1000)
            return datetime.combine(day, datetime.min.time(), ET).replace(
                hour=seconds // 3600,
                minute=(seconds % 3600) // 60,
                second=seconds % 60,
                microsecond=millis * 1000,
            )
        except (TypeError, ValueError, OverflowError):
            return None
    return None


def _is_fresh_intraday_snapshot(row: dict, now: datetime) -> bool:
    timestamp = _snapshot_timestamp(row)
    if timestamp is None:
        return False
    now_utc = now.astimezone(timezone.utc)
    timestamp_utc = timestamp.astimezone(timezone.utc)
    age_seconds = (now_utc - timestamp_utc).total_seconds()
    return (
        timestamp.astimezone(ET).date() == now.astimezone(ET).date()
        and 0 <= age_seconds <= FINNHUB_MAX_QUOTE_AGE_SECONDS
    )


def get_live_price(
    symbol: str, require_current_day: bool = False
) -> float | None:
    """Get the latest price, optionally refusing prior-session and EOD fallbacks."""
    finnhub_price = _get_finnhub_price(symbol, require_current_day)
    if finnhub_price is not None:
        return finnhub_price

    client = get_client()
    now = datetime.now(timezone.utc)
    try:
        trades = client.stock_snapshot_trade(
            symbol=symbol, current_day_only=require_current_day
        )
        if trades is not None and not trades.is_empty():
            row = trades.tail(1).to_dicts()[0]
            price = as_float(first_present(row, "price", "close", "last"))
            if price > 0 and (
                not require_current_day or _is_fresh_intraday_snapshot(row, now)
            ):
                return price
            if require_current_day:
                print(f"[yellow]ThetaData trade snapshot for {symbol} is stale or untimestamped[/yellow]")
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
            is_fresh = (
                not require_current_day or _is_fresh_intraday_snapshot(row, now)
            )
            if bid > 0 and ask > 0 and is_fresh:
                return (bid + ask) / 2
            last = as_float(first_present(row, "price", "close", "last"))
            if last > 0 and is_fresh:
                return last
            if require_current_day and not is_fresh:
                print(f"[yellow]ThetaData quote snapshot for {symbol} is stale or untimestamped[/yellow]")
    except Exception as e:
        print(f"[yellow]Live quote miss {symbol}: {e}[/yellow]")

    if require_current_day:
        return None

    bars = get_daily_bars(symbol, days=15)
    if bars is None or bars.is_empty() or "close" not in bars.columns:
        return None
    return as_float(bars.tail(1)["close"][0])
