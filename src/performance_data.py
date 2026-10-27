from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

from src.closed_ledger import _option_mark
from src.data import ET, get_daily_bars, get_live_price
from src.market import is_rth, is_trading_day, market_close_time, options_close_time
from src.util import as_float

SCAN_GROUPS = (
    ("confirmed", "CONFIRMED FLOW"),
    ("daytrade", "DAY-TRADE FLOW"),
    ("swing", "SWING"),
    ("csp", "CSP"),
)


def alert_payload(alert: dict[str, Any]) -> dict[str, Any]:
    payload = alert.get("payload")
    return payload if isinstance(payload, dict) else {}


def alert_group(alert: dict[str, Any]) -> str:
    alert_type = str(alert.get("alert_type") or "")
    payload = alert_payload(alert)
    if alert_type == "flow":
        stream = str(payload.get("stream") or "").lower()
        if stream in {"confirmed", "daytrade"}:
            return stream
        title = str(alert.get("title") or "").upper()
        return "daytrade" if "DAY-TRADE" in title else "confirmed"
    return alert_type


def local_date(value: Any) -> date | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ET)
    return parsed.astimezone(ET).date()


def contract_mark(alert: dict[str, Any], now: datetime) -> float | None:
    payload = alert_payload(alert)
    local_now = now.astimezone(ET)
    mark = _option_mark(
        alert,
        payload,
        now,
        require_fresh=is_option_session(now),
        require_current_day=(
            is_trading_day(local_now.date())
            and local_now.time() >= market_close_time(local_now.date())
        ),
    )
    return mark


def is_market_session(now: datetime) -> bool:
    return is_rth(now)


def is_option_session(now: datetime) -> bool:
    local_now = now.astimezone(ET)
    return (
        is_trading_day(local_now.date())
        and time(9, 30) <= local_now.time() < options_close_time(local_now.date())
    )


def underlying_mark(alert: dict[str, Any], now: datetime) -> float | None:
    symbol = str(alert.get("symbol") or "")
    if not symbol:
        return None
    if is_rth(now):
        return get_live_price(symbol, require_current_day=True)
    local_now = now.astimezone(ET)
    expected_date = (
        local_now.date()
        if (
            is_trading_day(local_now.date())
            and local_now.time() >= market_close_time(local_now.date())
        )
        else None
    )
    return latest_daily_close(symbol, expected_date)


def _invalidation(payload: dict[str, Any], direction: str) -> tuple[float, str] | None:
    value = payload.get("invalidation")
    if isinstance(value, dict):
        level = as_float(value.get("price"))
        condition = str(value.get("condition") or "").lower()
    else:
        level = as_float(value)
        condition = "below" if direction in {"BULLISH", "CALL", "LONG"} else "above"
    if level <= 0 or condition not in {"above", "below"}:
        return None
    return level, condition


def _direction(alert: dict[str, Any], payload: dict[str, Any]) -> str:
    direction = str(payload.get("direction") or payload.get("right") or "").upper()
    if not direction and alert.get("alert_type") == "csp":
        return "SHORT PUT"
    return direction or "unavailable"


def _progress(start: float, current: float, target: float, is_long: bool) -> float | None:
    distance = target - start if is_long else start - target
    movement = current - start if is_long else start - current
    if distance <= 0:
        return None
    return movement / distance


def _days_open(created_at: Any, today: date) -> int | None:
    created = local_date(created_at)
    return (today - created).days if created else None


def mark_open_alert(alert: dict[str, Any], now: datetime) -> dict[str, Any]:
    payload = alert_payload(alert)
    alert_type = str(alert.get("alert_type") or "")
    direction = _direction(alert, payload)
    today = now.astimezone(ET).date()
    entry_underlying = as_float(alert.get("price_at_alert"))
    current_underlying = underlying_mark(alert, now)
    entry_contract = as_float(payload.get("mid"))
    entry_premium = as_float(payload.get("premium"))
    current_contract = contract_mark(alert, now) if alert_type in {"flow", "csp"} else None
    target = as_float(
        payload.get("underlying_target_2r", payload.get("target_2r"))
    )
    invalidation = _invalidation(payload, direction)
    if alert_type == "swing":
        entry = entry_underlying
        current = current_underlying
        long_side = direction not in {"BEARISH", "SHORT"}
        pnl = (
            (current - entry) * (1 if long_side else -1)
            if entry > 0 and current is not None
            else None
        )
        pnl_pct = pnl / entry * 100 if pnl is not None and entry > 0 else None
    elif alert_type == "csp":
        entry = entry_premium
        current = current_contract
        pnl = (
            (entry - current) * 100
            if entry > 0 and current is not None
            else None
        )
        pnl_pct = (
            (entry - current) / entry * 100
            if entry > 0 and current is not None
            else None
        )
    else:
        entry = entry_contract
        current = current_contract
        pnl = (
            (current - entry) * 100
            if entry > 0 and current is not None
            else None
        )
        pnl_pct = pnl / (entry * 100) * 100 if pnl is not None and entry > 0 else None

    is_long_underlying = direction not in {"BEARISH", "PUT", "SHORT"}
    target_progress = (
        _progress(entry_underlying, current_underlying, target, is_long_underlying)
        if current_underlying is not None and entry_underlying > 0 and target > 0
        else None
    )
    invalidation_progress = None
    invalidation_status = "unavailable"
    invalidation_level = None
    if invalidation is not None:
        invalidation_level, condition = invalidation
        invalidation_status = "price unavailable" if current_underlying is None else (
            "broken" if (
                condition == "below" and current_underlying <= invalidation_level
            ) or (
                condition == "above" and current_underlying >= invalidation_level
            ) else "intact"
        )
        invalidation_progress = _progress(
            entry_underlying,
            current_underlying if current_underlying is not None else 0,
            invalidation_level,
            condition == "above",
        ) if current_underlying is not None else None
    strike = as_float(payload.get("strike"))
    max_profit = entry_premium * 100 if alert_type == "csp" and entry_premium > 0 else None
    return {
        "id": int(alert["id"]),
        "alert": alert,
        "payload": payload,
        "group": alert_group(alert),
        "scan_type": alert_type,
        "symbol": str(alert.get("symbol") or "unavailable"),
        "direction": direction,
        "created_at": alert.get("created_at"),
        "days_open": _days_open(alert.get("created_at"), today),
        "entry_underlying": entry_underlying if entry_underlying > 0 else None,
        "current_underlying": current_underlying,
        "entry_contract": entry if entry > 0 else None,
        "current_contract": current,
        "pnl": pnl,
        "pnl_pct": pnl_pct,
        "target": target if target > 0 else None,
        "target_progress": target_progress,
        "invalidation": invalidation_level,
        "invalidation_status": invalidation_status,
        "invalidation_progress": invalidation_progress,
        "strike": strike if strike > 0 else None,
        "expiration": payload.get("expiration"),
        "max_profit": max_profit,
        "cash_required": strike * 100 if alert_type == "csp" and strike > 0 else None,
    }


def trading_sessions_open(created_at: Any, today: date) -> int | None:
    created = local_date(created_at)
    if created is None or today <= created:
        return 0
    return sum(
        1
        for offset in range(1, (today - created).days + 1)
        if is_trading_day(created + timedelta(days=offset))
    )


def latest_daily_close(
    symbol: str, expected_date: date | None = None
) -> float | None:
    bars = get_daily_bars(symbol)
    if bars is None or bars.is_empty() or "close" not in bars.columns:
        return None
    if expected_date is not None:
        date_column = "created" if "created" in bars.columns else None
        if date_column is None:
            return None
        latest_date = local_date(bars.sort(date_column).tail(1)[date_column][0])
        if latest_date != expected_date:
            return None
    close = as_float(bars.tail(1)["close"][0])
    return close if close > 0 else None
