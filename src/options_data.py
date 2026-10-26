from __future__ import annotations

from datetime import date

import polars as pl
from rich import print

from src.data import get_live_price
from src.market import is_rth
from src.theta import get_client


def get_underlying_price(symbol: str) -> float | None:
    return get_live_price(symbol, require_current_day=is_rth())


def get_expirations(symbol: str, max_dte: int = 60, min_dte: int = 1) -> list[str]:
    try:
        df = get_client().option_list_expirations(symbol=symbol)
        if df is None or df.is_empty() or "expiration" not in df.columns:
            return []

        today = date.today()
        valid: list[str] = []
        for exp_str in df["expiration"].to_list():
            try:
                exp = date.fromisoformat(str(exp_str)[:10])
                dte = (exp - today).days
                if min_dte <= dte <= max_dte:
                    valid.append(exp.isoformat())
            except ValueError:
                continue
        return sorted(valid)
    except Exception as e:
        print(f"[red]Expirations error {symbol}: {e}[/red]")
        return []


def _safe_call(label: str, fn, **kwargs) -> pl.DataFrame | None:
    try:
        df = fn(**kwargs)
        if df is None or df.is_empty():
            return None
        return df
    except Exception as e:
        print(f"[yellow]{label}: {e}[/yellow]")
        return None


def get_option_chain(symbol: str, expiration: str) -> pl.DataFrame | None:
    return _safe_call(
        f"Chain {symbol} {expiration}",
        get_client().option_snapshot_quote,
        symbol=symbol,
        expiration=expiration,
    )


def get_oi_chain(symbol: str, expiration: str) -> pl.DataFrame | None:
    return _safe_call(
        f"OI {symbol} {expiration}",
        get_client().option_snapshot_open_interest,
        symbol=symbol,
        expiration=expiration,
    )


def get_ohlc_chain(symbol: str, expiration: str) -> pl.DataFrame | None:
    df = _safe_call(
        f"OHLC {symbol} {expiration}",
        get_client().option_snapshot_ohlc,
        symbol=symbol,
        expiration=expiration,
        current_day_only=True,
    )
    if df is None:
        df = _safe_call(
            f"OHLC fallback {symbol} {expiration}",
            get_client().option_snapshot_ohlc,
            symbol=symbol,
            expiration=expiration,
            current_day_only=False,
        )
    return df


def get_greeks_chain(
    symbol: str, expiration: str, stock_price: float | None = None
) -> pl.DataFrame | None:
    kwargs = {"symbol": symbol, "expiration": expiration}
    if stock_price:
        kwargs["stock_price"] = stock_price
    return _safe_call(
        f"Greeks {symbol} {expiration}",
        get_client().option_snapshot_greeks_first_order,
        **kwargs,
    )


def get_trade_quotes(
    symbol: str,
    expiration: str,
    strike: float,
    right: str,
    session_date: date | None = None,
) -> pl.DataFrame | None:
    session_date = session_date or date.today()
    theta_right = "call" if right.upper().startswith("C") else "put"
    return _safe_call(
        f"Trades {symbol} {strike}{right[0]} {expiration}",
        get_client().option_history_trade_quote,
        symbol=symbol,
        expiration=expiration,
        date=session_date,
        strike=str(strike),
        right=theta_right,
        exclusive=True,
    )


def join_on_contract(*frames: pl.DataFrame) -> pl.DataFrame | None:
    usable = [f for f in frames if f is not None and not f.is_empty()]
    if not usable:
        return None
    keys = [c for c in ("strike", "right") if all(c in f.columns for f in usable)]
    if len(keys) < 2:
        return usable[0]
    merged = usable[0]
    for extra in usable[1:]:
        overlap = [c for c in extra.columns if c in merged.columns and c not in keys]
        extra = extra.drop(overlap) if overlap else extra
        merged = merged.join(extra, on=keys, how="left")
    return merged
