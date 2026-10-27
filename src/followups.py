from __future__ import annotations

import hashlib
import json
import os
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

import requests
from rich import print

from src.config import (
    CSP_AFTERNOON_END_TIME,
    FLOW_FOLLOWUP_TAPE_WINDOW_SEC,
    FOLLOWUP_MAX_AGE_DAYS,
    FOLLOWUP_NEWS_LOOKBACK_HOURS,
    FOLLOWUP_PRICE_POLL_SECONDS,
    FOLLOWUP_RETRACE_FRACTION,
    SWING_FOLLOWUP_MAX_TRADING_DAYS,
)
from src.csp import find_csp_opportunities
from src.closed_ledger import (
    check_csp_decay,
    close_underlying_rule,
    evaluate_scheduled_eod_closures,
)
from src.data import ET, _snapshot_timestamp, get_daily_bars, get_live_price
from src.discord_alerts import send_alert
from src.flow import _premium_threshold, get_opposing_flow_summary
from src.market import is_eod_window, is_rth, is_trading_day
from src.state import (
    closed_alert_is_recorded,
    followup_alerts_since,
    followup_event_sent,
    get_flag,
    mark_followup_event_sent,
    set_flag,
)
from src.swing_signals import build_context
from src.util import as_float

_FOLLOWUP_COLORS = {
    "flow": 0x5865F2,
    "swing": 0xF1C40F,
    "csp": 0x3498DB,
}


def _parse_payload(row: dict[str, Any]) -> dict[str, Any] | None:
    if closed_alert_is_recorded(int(row["id"])):
        return None
    try:
        payload = json.loads(row.get("payload") or "{}")
    except (TypeError, json.JSONDecodeError):
        print(f"[yellow]Skipping follow-up for alert {row['id']}: invalid payload[/yellow]")
        return None
    if not isinstance(payload, dict):
        print(f"[yellow]Skipping follow-up for alert {row['id']}: payload is not an object[/yellow]")
        return None
    if payload.get("is_followup"):
        return None
    return payload


def _invalidation_reference(
    payload: dict[str, Any],
) -> tuple[float, str, str] | None:
    value = payload.get("invalidation")
    if isinstance(value, dict):
        level = as_float(value.get("price"))
        condition = str(value.get("condition") or "").lower()
        name = str(value.get("level") or "level")
        if level > 0 and condition in {"above", "below"}:
            return level, condition, name
        return None

    level = as_float(value)
    direction = str(payload.get("direction") or "").upper()
    if level <= 0:
        return None
    if direction == "BULLISH":
        return level, "below", "invalidation"
    if direction == "BEARISH":
        return level, "above", "invalidation"
    return None


def _followup_rows(now: datetime) -> list[dict[str, Any]]:
    cutoff = now.astimezone(timezone.utc) - timedelta(days=FOLLOWUP_MAX_AGE_DAYS)
    return followup_alerts_since(cutoff)


def _send_followup(
    row: dict[str, Any],
    event_key: str,
    title_suffix: str,
    details: str,
    suggestion: str,
    current_price: float | None,
    payload: dict[str, Any],
) -> bool:
    alert_id = int(row["id"])
    if followup_event_sent(alert_id, event_key):
        return False

    original_title = str(row.get("title") or f"{row['alert_type']} alert")
    created = str(row.get("created_at") or "")
    try:
        created_at = datetime.fromisoformat(created)
        created_display = (
            created_at.astimezone(ET).strftime("%Y-%m-%d %H:%M ET")
            if created_at.tzinfo is not None
            else created
        )
    except ValueError:
        created_display = created or "unknown"
    symbol = str(row.get("symbol") or "unknown")
    alert_type = str(row["alert_type"])
    entry_price = as_float(row.get("price_at_alert"))
    if entry_price <= 0:
        print(f"[yellow]Skipping follow-up for alert #{alert_id}: entry price unavailable[/yellow]")
        return False
    if alert_type == "csp":
        strike = as_float(payload.get("strike"))
        expiration = str(payload.get("expiration") or "unknown")
        premium = as_float(payload.get("premium"))
        entry_text = (
            f"Entry: underlying ${entry_price:.2f}; sold ${strike:g} put, "
            f"exp {expiration}, for ${premium:.2f} per share.\n"
        )
    else:
        entry_text = f"Entry: ${entry_price:.2f}\n"
    description = (
        f"FOLLOW-UP (alert #{alert_id})\n"
        f"{entry_text}"
        f"Original alert: {original_title}\n"
        f"**Original alert time:** {created_display}\n"
        f"**Underlying now:** "
        f"{f'${current_price:.2f}' if current_price is not None else 'Not available'}\n"
        f"**Update:** {details}\n\n"
        f"{suggestion}"
    )
    followup_payload = {
        **payload,
        "is_followup": True,
        "original_alert_id": alert_id,
        "original_alert_title": original_title,
        "followup_event": event_key,
    }
    sent = send_alert(
        title=f"🔔 FOLLOW-UP — {title_suffix} — {symbol} (alert #{alert_id})",
        description=description,
        color=_FOLLOWUP_COLORS.get(alert_type, 0x808080),
        alert_type=alert_type,
        alert_key=f"followup:{alert_id}:{event_key}",
        symbol=symbol,
        price=current_price,
        payload=followup_payload,
        stream=payload.get("stream"),
    )
    if sent:
        mark_followup_event_sent(alert_id, event_key)
    return sent


def _price_events(
    row: dict[str, Any],
    payload: dict[str, Any],
    price: float,
    now: datetime,
) -> int:
    symbol = str(row.get("symbol") or "unknown")
    events_sent = 0
    invalidation = _invalidation_reference(payload)
    if invalidation and str(row.get("alert_type")) != "flow":
        level, condition, level_name = invalidation
        breached = (
            condition == "below" and level > 0 and price <= level
        ) or (
            condition == "above" and level > 0 and price >= level
        )
        if breached:
            close_underlying_rule(row, payload, "INVALIDATED", price, now)
            event = f"invalidation:{condition}:{level:.2f}"
            if _send_followup(
                row,
                event,
                "Original invalidation level reached",
                (
                    f"{symbol} is ${price:.2f}; the original invalidation reference "
                    f"is {condition} ${level:.2f} ({level_name})."
                ),
                "Suggestion: consider exiting.",
                price,
                payload,
            ):
                events_sent += 1

    target = as_float(
        payload.get("underlying_target_2r", payload.get("target_2r"))
    )
    if target > 0:
        direction = str(payload.get("direction") or "").upper()
        if not direction:
            right = str(payload.get("right") or "").upper()
            direction = {"CALL": "BULLISH", "PUT": "BEARISH"}.get(right, "")
        reached = (
            direction == "BULLISH" and price >= target
        ) or (
            direction == "BEARISH" and price <= target
        )
        if reached:
            close_underlying_rule(row, payload, "2R TARGET", price, now)
            event = f"target-2r:{target:.2f}"
            if _send_followup(
                row,
                event,
                "Original 2R reference reached",
                (
                    f"{symbol} is ${price:.2f}, at/past the original 2R underlying "
                    f"reference of ${target:.2f}. Consider reviewing the setup."
                ),
                "Suggestion: consider taking profits.",
                price,
                payload,
            ):
                events_sent += 1

    if str(row.get("alert_type")) == "csp":
        strike = as_float(payload.get("strike"))
        if strike > 0 and price <= strike:
            event = f"csp-strike:{strike:.2f}"
            if _send_followup(
                row,
                event,
                "CSP strike approached or crossed",
                (
                    f"{symbol} is ${price:.2f}, at/below the original short-put "
                    f"strike of ${strike:.2f}. Review assignment risk at the "
                    "original strike."
                ),
                "Suggestion: review the position for assignment.",
                price,
                payload,
            ):
                events_sent += 1
    return events_sent


def _invalidation_broken(price: float, invalidation: tuple[float, str, str] | None) -> bool:
    if not invalidation:
        return False
    level, condition, _ = invalidation
    return (condition == "below" and price <= level) or (
        condition == "above" and price >= level
    )


def _retracement_fraction(
    entry: float, current: float, invalidation: tuple[float, str, str] | None
) -> float | None:
    if entry <= 0 or current <= 0 or not invalidation:
        return None
    level, condition, _ = invalidation
    distance = entry - level if condition == "below" else level - entry
    progress = entry - current if condition == "below" else current - entry
    if distance <= 0 or progress <= 0 or _invalidation_broken(current, invalidation):
        return None
    if (condition == "below" and current >= entry) or (
        condition == "above" and current <= entry
    ):
        return None
    return progress / distance


def _flow_contract(payload: dict[str, Any]) -> str:
    right = str(payload.get("right") or "").upper()
    strike = as_float(payload.get("strike"))
    expiration = str(payload.get("expiration") or "unknown")
    dte = payload.get("dte")
    dte_text = f", {dte} DTE" if dte is not None else ""
    return f"${strike:g}{right[:1]} exp {expiration}{dte_text}"


def _evaluate_flow_decision(
    row: dict[str, Any],
    payload: dict[str, Any],
    current_price: float,
    opposing: dict[str, Any],
    session_date: date,
) -> tuple[str, str, str] | None:
    entry = as_float(row.get("price_at_alert"))
    invalidation = _invalidation_reference(payload)
    original_right = str(payload.get("right") or "").upper()
    stream = str(payload.get("stream") or "confirmed")
    original_contract = _flow_contract(payload)
    dte = payload.get("dte")
    if dte is None:
        try:
            dte = (
                date.fromisoformat(str(payload["expiration"]))
                - session_date
            ).days
        except (KeyError, TypeError, ValueError):
            return None

    threshold = _premium_threshold(stream, int(dte))
    opposing_trigger = (
        as_float(opposing.get("buy_premium")) >= threshold
        and as_float(opposing.get("buy_share")) >= 0.60
    )
    opposite_right = "PUT" if original_right == "CALL" else "CALL"
    if opposing_trigger and opposing.get("confirmed") and opposing.get("top_contract"):
        top = opposing["top_contract"]
        details = (
            f"${as_float(opposing['buy_premium']):,.0f} aggressive "
            f"{opposite_right} premium in the last "
            f"{FLOW_FOLLOWUP_TAPE_WINDOW_SEC // 60} minutes "
            f"({as_float(opposing['buy_share']):.1%} of classified premium) "
            f"on {row.get('symbol')}, with opposing confirmation."
        )
        new_contract = (
            f"${as_float(top.get('strike')):g}{opposite_right[:1]} exp "
            f"{top.get('expiration')}, {top.get('dte')} DTE "
            f"(${as_float(top.get('buy_premium')):,.0f} aggressive premium"
        )
        if top.get("mid") is not None:
            new_contract += f", ${as_float(top['mid']):.2f} mid"
        if top.get("delta") is not None:
            new_contract += f", delta {as_float(top['delta']):+.2f}"
        if top.get("spread_pct") is not None:
            new_contract += f", spread {as_float(top['spread_pct']):.1f}%"
        new_contract += ")"
        suggestion = (
            f"Suggestion: reverse — consider exiting {original_contract} and "
            f"buying {new_contract}."
        )
        return (
            f"flow-reverse:{session_date.isoformat()}",
            details,
            suggestion,
        )

    if _invalidation_broken(current_price, invalidation):
        level = invalidation[0] if invalidation else 0
        reason = f"invalidation broken at ${level:.2f}"
    elif opposing_trigger:
        reason = "opposing flow without opposing confirmation"
    else:
        reason = ""
    if reason:
        return (
            f"flow-exit:{session_date.isoformat()}",
            reason,
            f"Suggestion: exit {original_contract}.",
        )

    if opposing_trigger:
        return None
    try:
        remaining_dte = (
            date.fromisoformat(str(payload.get("expiration"))) - session_date
        ).days
    except (TypeError, ValueError):
        remaining_dte = -1
    retracement = _retracement_fraction(entry, current_price, invalidation)
    if (
        retracement is not None
        and retracement >= FOLLOWUP_RETRACE_FRACTION
        and remaining_dte >= 7
    ):
        level = invalidation[0]
        average = (entry + current_price) / 2
        return (
            f"flow-average:{session_date.isoformat()}",
            f"{row.get('symbol')} retraced {retracement:.0%} of the distance "
            f"from entry toward invalidation (${level:.2f}), invalidation intact. "
            f"Equal-size average: (${entry:.2f} + ${current_price:.2f}) / 2 "
            f"= ${average:.2f}.",
            f"Suggestion: consider averaging — adding an equal size at the "
            f"current price ${current_price:.2f} gives an average of ${average:.2f}.",
        )
    return None


def run_flow_decision_followups(now: datetime | None = None) -> int:
    """Evaluate same-session flow alerts during the existing regular-hours scan loop."""
    now = now or datetime.now(ET)
    if not is_rth(now):
        return 0
    session_date = now.astimezone(ET).date()
    rows = []
    for row in _followup_rows(now):
        if row.get("alert_type") != "flow":
            continue
        payload = _parse_payload(row)
        if not payload or payload.get("stream") not in {"confirmed", "daytrade"}:
            continue
        try:
            created = datetime.fromisoformat(str(row["created_at"])).astimezone(ET)
        except (KeyError, TypeError, ValueError):
            continue
        if created.date() == session_date:
            rows.append((row, payload))
    if not rows:
        return 0

    symbols = {str(row["symbol"]) for row, _ in rows if row.get("symbol")}
    prices = {
        symbol: get_live_price(symbol, require_current_day=True)
        for symbol in symbols
    }
    tech_cache: dict[str, dict[str, Any] | None] = {}
    opposing_cache: dict[tuple[str, str, str], dict[str, Any]] = {}
    sent = 0
    for row, payload in rows:
        symbol = str(row.get("symbol") or "")
        current_price = prices.get(symbol)
        if current_price is None or current_price <= 0:
            continue
        if symbol not in tech_cache:
            try:
                tech_cache[symbol] = build_context(symbol)
            except Exception as exc:
                print(f"[yellow]Flow follow-up context failed for {symbol}: {type(exc).__name__}[/yellow]")
                tech_cache[symbol] = None
        tech = tech_cache[symbol]
        if tech is not None:
            tech = dict(tech)
            tech["live"] = current_price
            ema_fast = as_float(tech.get("ema_fast"))
            ema_slow = as_float(tech.get("ema_slow"))
            tech["trend"] = (
                "UPTREND"
                if current_price > ema_fast > ema_slow
                else "DOWNTREND"
                if current_price < ema_fast < ema_slow
                else "NEUTRAL"
            )
            low_20 = as_float((tech.get("levels") or {}).get("low_20"))
            tech["ok_to_own"] = tech["trend"] != "DOWNTREND" and (
                low_20 <= 0 or current_price >= low_20 * 0.97
            )

        if _invalidation_broken(
            current_price, _invalidation_reference(payload)
        ):
            close_underlying_rule(
                row, payload, "INVALIDATED", current_price, now
            )

        key = (symbol, str(payload["right"]).upper(), str(payload["stream"]))
        if key not in opposing_cache:
            try:
                opposing_cache[key] = get_opposing_flow_summary(
                    symbol,
                    key[1],
                    key[2],
                    now,
                    tech,
                    current_price,
                )
            except Exception as exc:
                print(f"[yellow]Opposing flow lookup failed for {symbol}: {type(exc).__name__}[/yellow]")
                opposing_cache[key] = {
                    "buy_premium": 0.0,
                    "buy_share": 0.0,
                    "confirmed": False,
                    "top_contract": None,
                }

        decision = _evaluate_flow_decision(
            row, payload, current_price, opposing_cache[key], session_date
        )
        if decision is None:
            continue
        event_key, details, suggestion = decision
        if event_key.startswith("flow-exit:") and details.startswith(
            "invalidation broken at "
        ):
            close_underlying_rule(
                row, payload, "INVALIDATED", current_price, now
            )
        if _send_followup(
            row,
            event_key,
            "Flow decision update",
            details,
            suggestion,
            current_price,
            payload,
        ):
            sent += 1
    return sent


def run_price_followups(now: datetime | None = None) -> int:
    """Check recent original alerts against fresh underlying prices during RTH."""
    now = now or datetime.now(ET)
    if not is_rth(now):
        return 0

    rows = _followup_rows(now)
    candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
    symbols: set[str] = set()
    for row in rows:
        payload = _parse_payload(row)
        symbol = str(row.get("symbol") or "")
        if payload is None or not symbol:
            continue
        if payload.get("expiration"):
            try:
                if (
                    date.fromisoformat(str(payload["expiration"])[:10])
                    < now.astimezone(ET).date()
                ):
                    continue
            except ValueError:
                print(
                    f"[yellow]Skipping invalid expiration in alert "
                    f"{row['id']}[/yellow]"
                )
                continue
        candidates.append((row, payload))
        symbols.add(symbol)

    prices: dict[str, float | None] = {}
    for symbol in symbols:
        flag = f"followup_price_check:{symbol}"
        last_check = get_flag(flag)
        if last_check:
            try:
                last_checked_at = datetime.fromisoformat(last_check)
            except ValueError:
                print(f"[yellow]Ignoring invalid follow-up check time for {symbol}[/yellow]")
            else:
                elapsed = (
                    now.astimezone(timezone.utc)
                    - last_checked_at.astimezone(timezone.utc)
                ).total_seconds()
                if 0 <= elapsed < FOLLOWUP_PRICE_POLL_SECONDS:
                    prices[symbol] = None
                    continue
        set_flag(flag, now.astimezone(timezone.utc).isoformat())
        prices[symbol] = get_live_price(symbol, require_current_day=True)
    sent = 0
    for row, payload in candidates:
        price = prices.get(str(row.get("symbol")))
        if price is not None and price > 0:
            sent += _price_events(row, payload, price, now)
            if row.get("alert_type") == "csp":
                check_csp_decay(row, payload, now)
    return sent


def run_swing_average_followups(now: datetime | None = None) -> int:
    """Evaluate swing-alert pullbacks using later completed daily closes only."""
    now = now or datetime.now(ET)
    if not is_eod_window(now):
        return 0
    today = now.astimezone(ET).date()
    sent = 0
    for row in _followup_rows(now):
        if row.get("alert_type") != "swing":
            continue
        payload = _parse_payload(row)
        symbol = str(row.get("symbol") or "")
        if payload is None or not symbol:
            continue
        try:
            created = datetime.fromisoformat(str(row["created_at"])).astimezone(ET)
        except (KeyError, TypeError, ValueError):
            continue
        entry = as_float(row.get("price_at_alert"))
        invalidation = _invalidation_reference(payload)
        if entry <= 0 or invalidation is None:
            continue

        bars = get_daily_bars(symbol)
        if bars is None or bars.is_empty() or "close" not in bars.columns:
            continue
        date_column = "created" if "created" in bars.columns else None
        if date_column is None:
            continue
        rows = []
        for bar in bars.sort(date_column).to_dicts():
            bar_date = _snapshot_timestamp({"created": bar[date_column]})
            if bar_date is None:
                try:
                    bar_day = date.fromisoformat(str(bar[date_column])[:10])
                except ValueError:
                    continue
            else:
                bar_day = bar_date.astimezone(ET).date()
            if created.date() < bar_day <= today:
                close = as_float(bar.get("close"))
                if close > 0:
                    rows.append((bar_day, close))
        trading_dates = sorted({bar_day for bar_day, _ in rows})
        expected_dates = []
        for ordinal in range(created.date().toordinal() + 1, today.toordinal() + 1):
            session = date.fromordinal(ordinal)
            if is_trading_day(session):
                expected_dates.append(session)
        if (
            not trading_dates
            or trading_dates[-1] != today
            or len(expected_dates) > SWING_FOLLOWUP_MAX_TRADING_DAYS
            or not set(expected_dates).issubset(trading_dates)
        ):
            continue
        close = next(value for bar_day, value in reversed(rows) if bar_day == today)
        if _invalidation_broken(close, invalidation):
            continue
        retracement = _retracement_fraction(entry, close, invalidation)
        if retracement is None or retracement < FOLLOWUP_RETRACE_FRACTION:
            continue
        level = invalidation[0]
        average = (entry + close) / 2
        if _send_followup(
            row,
            "swing-average",
            "Swing pullback update",
            f"{symbol} retraced {retracement:.0%} of the distance from entry "
            f"toward invalidation (${level:.2f}), invalidation intact. "
            f"Equal-size average: (${entry:.2f} + ${close:.2f}) / 2 "
            f"= ${average:.2f}.",
            f"Suggestion: consider averaging — adding an equal size at the "
            f"current close ${close:.2f} gives an average of ${average:.2f}.",
            close,
            payload,
        ):
            sent += 1
    return sent


def _csp_average_description(
    symbol: str,
    entry: float,
    original_strike: float,
    original_expiration: str,
    original_premium: float,
    candidate: dict[str, Any],
    retracement: float,
) -> tuple[str, str]:
    new_strike = as_float(candidate["strike"])
    new_expiration = str(candidate["expiration"])
    new_premium = as_float(candidate["premium"])
    total_premium = (original_premium + new_premium) * 100
    additional_cash = as_float(candidate["cash_required"])
    facts = (
        f"Entry: underlying ${entry:.2f}; sold ${original_strike:g} put, "
        f"exp {original_expiration}, for ${original_premium:.2f} per share. "
        f"{symbol} retraced {retracement:.0%} of the distance toward the put "
        f"strike ${original_strike:g}, still above the strike."
    )
    if new_strike == original_strike:
        breakeven = original_strike - (original_premium + new_premium) / 2
        facts += (
            f" Equal-strike legs: ${original_premium:.2f} and "
            f"${new_premium:.2f} per share; average breakeven "
            f"${breakeven:.2f}."
        )
    else:
        facts += (
            f" Original leg breakeven: ${original_strike - original_premium:.2f} "
            f"(${original_premium:.2f} premium); additional leg breakeven: "
            f"${new_strike - new_premium:.2f} (${new_premium:.2f} premium)."
        )
    facts += (
        f" Total premium across both contracts: ${total_premium:,.2f}. "
        f"Additional put cash required: ${additional_cash:,.0f}."
    )
    suggestion = (
        f"Suggestion: consider averaging — selling an additional put "
        f"(${new_strike:g}, exp {new_expiration}) for ${new_premium:.2f} per "
        f"share gives total premium ${total_premium:,.2f} and cash required "
        f"${additional_cash:,.0f} for the additional put."
    )
    return facts, suggestion


def run_csp_average_followups(now: datetime | None = None) -> int:
    """Check CSP pullbacks during the existing scheduled CSP scan windows."""
    now = now or datetime.now(ET)
    if not is_rth(now):
        return 0
    local_now = now.astimezone(ET)
    current_time = local_now.time()
    if not (
        (time(10, 15) <= current_time < time(11, 0))
        or (
            time(15, 30)
            <= current_time
            < time.fromisoformat(CSP_AFTERNOON_END_TIME)
        )
    ):
        return 0

    today = local_now.date()
    sent = 0
    candidate_cache: dict[str, list[dict[str, Any]]] = {}
    price_cache: dict[str, float | None] = {}
    context_cache: dict[str, dict[str, Any] | None] = {}
    for row in _followup_rows(now):
        if row.get("alert_type") != "csp":
            continue
        payload = _parse_payload(row)
        symbol = str(row.get("symbol") or "")
        if payload is None or not symbol:
            continue
        original_strike = as_float(payload.get("strike"))
        original_premium = as_float(payload.get("premium"))
        original_expiration = str(payload.get("expiration") or "")
        entry = as_float(row.get("price_at_alert"))
        try:
            remaining_dte = (date.fromisoformat(original_expiration) - today).days
        except ValueError:
            continue
        if (
            original_strike <= 0
            or original_premium <= 0
            or entry <= original_strike
            or remaining_dte < 7
        ):
            continue

        if symbol not in price_cache:
            price_cache[symbol] = get_live_price(symbol, require_current_day=True)
        current = price_cache[symbol]
        if current is None or current <= original_strike:
            continue
        retracement = (entry - current) / (entry - original_strike)
        if retracement < FOLLOWUP_RETRACE_FRACTION:
            continue

        if symbol not in context_cache:
            try:
                context_cache[symbol] = build_context(symbol)
            except Exception as exc:
                print(f"[yellow]CSP follow-up context failed for {symbol}: {type(exc).__name__}[/yellow]")
                context_cache[symbol] = None
        if context_cache[symbol] is None:
            continue
        if symbol not in candidate_cache:
            candidate_cache[symbol] = find_csp_opportunities(
                symbol, context_cache[symbol]
            )
        candidates = candidate_cache[symbol]
        if not candidates:
            continue
        selected = next(
            (
                item
                for item in candidates
                if as_float(item.get("strike")) == original_strike
                and str(item.get("expiration")) == original_expiration
            ),
            candidates[0],
        )
        details, suggestion = _csp_average_description(
            symbol,
            entry,
            original_strike,
            original_expiration,
            original_premium,
            selected,
            retracement,
        )
        if _send_followup(
            row,
            "csp-average",
            "CSP pullback update",
            details,
            suggestion,
            current,
            payload,
        ):
            sent += 1
    return sent


def _fetch_company_news(
    symbol: str, start_date: date, end_date: date
) -> list[dict[str, Any]]:
    api_key = os.getenv("FINNHUB_API_KEY")
    if not api_key:
        return []
    try:
        response = requests.get(
            "https://finnhub.io/api/v1/company-news",
            params={
                "symbol": symbol,
                "from": start_date.isoformat(),
                "to": end_date.isoformat(),
                "token": api_key,
            },
            timeout=10,
        )
    except requests.RequestException as exc:
        print(f"[yellow]Company news failed for {symbol}: {type(exc).__name__}[/yellow]")
        return []
    if response.status_code != 200:
        print(f"[yellow]Company news failed for {symbol}: HTTP {response.status_code}[/yellow]")
        return []
    try:
        news = response.json()
    except ValueError:
        print(f"[yellow]Company news returned invalid JSON for {symbol}[/yellow]")
        return []
    if not isinstance(news, list):
        print(f"[yellow]Company news returned an unexpected response for {symbol}[/yellow]")
        return []
    return [item for item in news if isinstance(item, dict)]


def _news_event_key(item: dict[str, Any]) -> str:
    news_id = item.get("id")
    if news_id is not None:
        return f"news:{news_id}"
    identity = "|".join(
        (
            str(item.get("datetime") or ""),
            str(item.get("headline") or ""),
            str(item.get("url") or ""),
        )
    )
    return "news:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]


def _news_timestamp(item: dict[str, Any]) -> float:
    try:
        return float(item.get("datetime"))
    except (TypeError, ValueError):
        return 0.0


def run_news_followups(now: datetime | None = None) -> int:
    """Notify once for each recent company headline published after an alert."""
    now = now or datetime.now(ET)
    rows = _followup_rows(now)
    parsed: list[tuple[dict[str, Any], dict[str, Any], datetime]] = []
    for row in rows:
        payload = _parse_payload(row)
        if payload is None or not row.get("symbol"):
            continue
        try:
            created = datetime.fromisoformat(str(row["created_at"]))
        except (KeyError, TypeError, ValueError):
            print(f"[yellow]Skipping alert {row['id']}: invalid creation timestamp[/yellow]")
            continue
        parsed.append((row, payload, created))
    if not parsed:
        return 0

    local_now = now.astimezone(ET)
    recent_cutoff = local_now - timedelta(hours=FOLLOWUP_NEWS_LOOKBACK_HOURS)
    by_symbol: dict[str, list[tuple[dict[str, Any], dict[str, Any], datetime]]] = {}
    for item in parsed:
        by_symbol.setdefault(str(item[0]["symbol"]), []).append(item)

    sent = 0
    for symbol, symbol_alerts in by_symbol.items():
        earliest = min(item[2].astimezone(ET).date() for item in symbol_alerts)
        news_items = _fetch_company_news(symbol, earliest, local_now.date())
        news_items.sort(key=_news_timestamp, reverse=True)
        for row, payload, created in symbol_alerts:
            for item in news_items:
                try:
                    published = datetime.fromtimestamp(
                        float(item.get("datetime")), timezone.utc
                    ).astimezone(ET)
                except (OSError, OverflowError, TypeError, ValueError):
                    continue
                if (
                    published <= created.astimezone(ET)
                    or published < recent_cutoff
                    or published > local_now
                ):
                    continue
                headline = str(item.get("headline") or "").strip()
                if not headline:
                    continue
                source = str(item.get("source") or "Finnhub company news")
                if _send_followup(
                    row,
                    _news_event_key(item),
                    "New company headline",
                    (
                        f"New headline after the original alert: **{headline}** "
                        f"({source}, {published:%Y-%m-%d %H:%M ET}). "
                        "A headline alone does not prove the original thesis is "
                        "invalid; review the source and context."
                    ),
                    "Suggestion: review the original setup.",
                    None,
                    payload,
                ):
                    sent += 1
                break
    return sent


def run_expiration_followups(now: datetime | None = None) -> int:
    """Send one reminder at the most relevant five-day, one-day, or expiry point."""
    now = now or datetime.now(ET)
    today = now.astimezone(ET).date()
    evaluate_scheduled_eod_closures(now)
    sent = 0
    for row in _followup_rows(now):
        payload = _parse_payload(row)
        expiration = payload.get("expiration") if payload else None
        if not expiration:
            continue
        try:
            expiry_date = date.fromisoformat(str(expiration)[:10])
        except ValueError:
            print(f"[yellow]Skipping invalid expiration in alert {row['id']}[/yellow]")
            continue
        days_left = (expiry_date - today).days
        if days_left > 5:
            continue
        if days_left <= 0:
            event, label = "expiration:expired", "Contract expiration date reached"
            detail = (
                f"The original contract expiration is {expiry_date.isoformat()}. "
                "Check the broker and clearing status for expiration or assignment; "
                "the system cannot see your holdings."
            )
        elif days_left == 1:
            event, label = "expiration:1d", "Contract expires tomorrow"
            detail = (
                f"The original contract expires {expiry_date.isoformat()} "
                "(1 day away). Review time decay, liquidity, and assignment or "
                "exercise risk."
            )
        else:
            event, label = "expiration:5d", "Contract nearing expiration"
            detail = (
                f"The original contract expires {expiry_date.isoformat()} "
                f"({days_left} calendar days away). Review the contract's "
                "time decay and liquidity."
            )
        if _send_followup(
            row,
            event,
            label,
            detail,
            "Suggestion: review the original setup.",
            None,
            payload,
        ):
            sent += 1
    return sent


def run_scheduled_followups(now: datetime | None = None) -> int:
    """Run expiration and company-news checks from scheduled report windows."""
    now = now or datetime.now(ET)
    return run_expiration_followups(now) + run_news_followups(now)
