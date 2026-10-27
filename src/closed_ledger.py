from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from rich import print

from src.data import ET, _is_fresh_intraday_snapshot, _snapshot_timestamp, get_daily_bars
from src.discord_alerts import WEBHOOKS, send_alert
from src.market import is_trading_day, market_close_time
from src.options_data import get_option_chain
from src.state import (
    claim_closed_alert_record,
    closed_alert_is_posted,
    closed_alert_is_recorded,
    get_flag,
    mark_closed_alert_record_posted,
    pending_closed_alert_records,
    recover_stale_closed_alert_claims,
    release_closed_alert_record,
    save_closed_alert_record,
    set_flag,
    followup_alerts_since,
)
from src.util import as_float, first_present

_REASONS = {
    "INVALIDATED",
    "2R TARGET",
    "EXPIRED WORTHLESS",
    "EXPIRED ASSIGNED",
    "CSP 90% DECAY",
    "STALE",
}


def _local_date(value: Any) -> date | None:
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


def _contract_text(alert_type: str, payload: dict[str, Any]) -> str:
    if alert_type == "swing":
        return (
            "Underlying; strike unavailable; expiration unavailable; "
            "DTE at alert unavailable"
        )
    strike = as_float(payload.get("strike"))
    expiration = str(payload.get("expiration") or "unavailable")
    dte = payload.get("dte")
    if dte is None:
        created = _local_date(payload.get("_created_at"))
        expiry = _local_date(expiration)
        dte = (expiry - created).days if created and expiry else None
    dte_text = str(dte) if dte is not None else "unavailable"
    strike_text = f"${strike:g}" if strike > 0 else "unavailable"
    right = str(payload.get("right") or ("PUT" if alert_type == "csp" else "unavailable")).upper()
    return (
        f"{strike_text} {right}, exp {expiration}, "
        f"{dte_text} DTE at alert"
    )


def _pnl_values(
    alert_type: str,
    entry: float,
    exit_price: float | None,
    direction: str,
) -> tuple[float | None, float | None]:
    if entry <= 0 or exit_price is None or exit_price < 0:
        return None, None
    if alert_type == "swing":
        sign = -1.0 if direction in {"BEARISH", "SHORT"} else 1.0
        pnl = sign * (exit_price - entry)
        return pnl, pnl / entry * 100
    if alert_type == "csp":
        pnl_per_share = entry - exit_price
        return pnl_per_share * 100, pnl_per_share / entry * 100
    pnl_per_share = exit_price - entry
    return pnl_per_share * 100, pnl_per_share / entry * 100


def build_closed_alert_record(
    row: dict[str, Any],
    payload: dict[str, Any],
    reason: str,
    exit_price: float | None,
    closed_on: date,
) -> dict[str, Any]:
    if reason not in _REASONS:
        raise ValueError(f"Unsupported closure reason: {reason}")
    alert_type = str(row.get("alert_type") or "unavailable").lower()
    stream = str(payload.get("stream") or "").lower()
    scan_type = (
        {"confirmed": "CONFIRMED flow", "daytrade": "DAY-TRADE flow"}.get(
            stream, alert_type
        )
        if alert_type == "flow"
        else alert_type
    )
    symbol = str(row.get("symbol") or "unavailable")
    direction = str(
        payload.get("direction")
        or payload.get("right")
        or ("SHORT PUT" if alert_type == "csp" else "unavailable")
    ).upper()
    entry = (
        as_float(row.get("price_at_alert"))
        if alert_type == "swing"
        else as_float(payload.get("premium" if alert_type == "csp" else "mid"))
    )
    created_date = _local_date(row.get("created_at"))
    payload_for_contract = {**payload, "_created_at": row.get("created_at")}
    pnl, pnl_pct = _pnl_values(alert_type, entry, exit_price, direction)
    elapsed = (closed_on - created_date).days if created_date else None
    return {
        "alert_id": int(row["id"]),
        "scan_type": scan_type,
        "ticker": symbol,
        "direction": direction,
        "contract": _contract_text(alert_type, payload_for_contract),
        "entry_price": entry if entry > 0 else None,
        "exit_price": exit_price,
        "closing_reason": reason,
        "pnl": pnl,
        "pnl_pct": pnl_pct,
        "alert_date": created_date.isoformat() if created_date else None,
        "close_date": closed_on.isoformat(),
        "days_open": elapsed if elapsed is not None else None,
    }


def format_closed_alert_message(record: dict[str, Any]) -> str:
    def price(value: Any) -> str:
        number = as_float(value)
        return f"${number:.2f}" if value is not None and number >= 0 else "unavailable"

    def amount(value: Any, suffix: str) -> str:
        if value is None:
            return "unavailable"
        number = as_float(value)
        sign = "-" if number < 0 else "+"
        return f"{sign}${abs(number):,.2f}{suffix}"

    entry = price(record.get("entry_price"))
    exit_value = price(record.get("exit_price"))
    pnl = amount(record.get("pnl"), "")
    pnl_pct = (
        f"{as_float(record['pnl_pct']):+.2f}%"
        if record.get("pnl_pct") is not None
        else "unavailable"
    )
    return (
        f"Alert #{record['alert_id']}\n"
        f"Scan type: {record['scan_type']}\n"
        f"Ticker: {record['ticker']}\n"
        f"Direction: {record['direction']}\n"
        f"Contract: {record['contract']}\n"
        f"Entry price: {entry}\n"
        f"Exit price: {exit_value}\n"
        f"Closing reason: {record['closing_reason']}\n"
        f"P&L per {'share' if record['scan_type'] == 'swing' else 'contract'}: "
        f"{pnl} ({pnl_pct})\n"
        f"Alert date: {record['alert_date'] or 'unavailable'}\n"
        f"Close date: {record['close_date'] or 'unavailable'}\n"
        f"Days open: {record['days_open'] if record['days_open'] is not None else 'unavailable'}\n"
        "Hypothetical — alert price = entry, 1 contract / 1 share, "
        "before commissions and spreads. Not brokerage P&L."
    )


def _notify_missing_webhook_once(now: datetime) -> None:
    today = now.astimezone(ET).date().isoformat()
    flag = f"closed-ledger-missing-webhook:{today}"
    if get_flag(flag):
        return
    send_alert(
        title="Closed ledger webhook missing",
        description=(
            "DISCORD_WEBHOOK_CLOSED_LEDGER is missing or empty; "
            "closed-alert ledger posts are being skipped until configured."
        ),
        color=0xFFAA00,
        alert_type="health",
        alert_key=f"closed-ledger-missing-webhook:{today}",
    )
    set_flag(flag, "sent")


def post_closed_alert(
    row: dict[str, Any],
    payload: dict[str, Any],
    reason: str,
    exit_price: float | None,
    closed_on: date,
    now: datetime | None = None,
) -> bool:
    alert_id = int(row["id"])
    if closed_alert_is_posted(alert_id):
        return False
    record = build_closed_alert_record(row, payload, reason, exit_price, closed_on)
    persisted = save_closed_alert_record(alert_id, record)
    if persisted.get("closing_reason") not in _REASONS:
        raise ValueError(f"Unsupported persisted closing reason for alert #{alert_id}")
    if not WEBHOOKS.get("closed_ledger"):
        _notify_missing_webhook_once(now or datetime.now(ET))
        return False
    return _deliver_closed_record(alert_id, persisted)


def _deliver_closed_record(alert_id: int, record: dict[str, Any]) -> bool:
    if not claim_closed_alert_record(alert_id):
        return False
    try:
        sent = send_alert(
            title=f"Closed alert #{alert_id} — {record['ticker']}",
            description=format_closed_alert_message(record),
            color=0x808080,
            alert_type="health",
            stream="closed_ledger",
        )
    except Exception:
        release_closed_alert_record(alert_id)
        raise
    if not sent:
        release_closed_alert_record(alert_id)
        return False
    mark_closed_alert_record_posted(alert_id)
    return True


def flush_pending_closed_alerts(now: datetime | None = None) -> int:
    now = now or datetime.now(ET)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    recover_stale_closed_alert_claims(
        now.astimezone(timezone.utc) - timedelta(minutes=15)
    )
    pending = pending_closed_alert_records()
    if not pending:
        return 0
    if not WEBHOOKS.get("closed_ledger"):
        _notify_missing_webhook_once(now)
        return 0
    delivered = 0
    for item in pending:
        if _deliver_closed_record(item["alert_id"], item["record"]):
            delivered += 1
    return delivered


def _option_mark(
    row: dict[str, Any],
    payload: dict[str, Any],
    now: datetime,
    require_fresh: bool,
    require_current_day: bool = False,
) -> float | None:
    expiration = str(payload.get("expiration") or "")
    symbol = str(row.get("symbol") or "")
    strike = as_float(payload.get("strike"))
    right = str(payload.get("right") or ("PUT" if row.get("alert_type") == "csp" else "")).upper()
    if not symbol or not expiration or strike <= 0 or not right:
        return None
    chain = get_option_chain(symbol, expiration)
    if chain is None or chain.is_empty():
        return None
    for contract in chain.to_dicts():
        contract_right = str(first_present(contract, "right", default="")).upper()
        contract_right = {"C": "CALL", "P": "PUT"}.get(contract_right, contract_right)
        if (
            contract_right != right
            or abs(as_float(contract.get("strike")) - strike) > 0.001
        ):
            continue
        if require_fresh and not _is_fresh_intraday_snapshot(contract, now):
            return None
        if require_current_day:
            timestamp = _snapshot_timestamp(contract)
            if (
                timestamp is None
                or timestamp.astimezone(ET).date() != now.astimezone(ET).date()
            ):
                return None
        bid = as_float(first_present(contract, "bid", "bid_price"))
        ask = as_float(first_present(contract, "ask", "ask_price"))
        if bid > 0 and ask >= bid and ask > 0:
            return (bid + ask) / 2
        return None
    return None


def check_csp_decay(
    row: dict[str, Any], payload: dict[str, Any], now: datetime
) -> bool:
    if closed_alert_is_recorded(int(row["id"])):
        return post_closed_alert(
            row, payload, "CSP 90% DECAY", None, now.astimezone(ET).date(), now
        )
    premium = as_float(payload.get("premium"))
    if row.get("alert_type") != "csp" or premium <= 0:
        return False
    mark = _option_mark(row, payload, now, require_fresh=True)
    if mark is None or mark > premium * 0.10:
        return False
    return post_closed_alert(
        row, payload, "CSP 90% DECAY", mark, now.astimezone(ET).date(), now
    )


def close_underlying_rule(
    row: dict[str, Any],
    payload: dict[str, Any],
    reason: str,
    underlying_price: float,
    now: datetime,
) -> bool:
    if closed_alert_is_recorded(int(row["id"])):
        return post_closed_alert(
            row, payload, reason, None, now.astimezone(ET).date(), now
        )
    alert_type = str(row.get("alert_type") or "")
    exit_price = (
        underlying_price
        if alert_type == "swing"
        else _option_mark(row, payload, now, require_fresh=True)
    )
    return post_closed_alert(
        row, payload, reason, exit_price, now.astimezone(ET).date(), now
    )


def _daily_close(symbol: str, target_date: date) -> float | None:
    bars = get_daily_bars(symbol)
    if bars is None or bars.is_empty() or "close" not in bars.columns:
        return None
    date_column = "created" if "created" in bars.columns else None
    if date_column is None:
        return None
    for bar in bars.sort(date_column, descending=True).to_dicts():
        timestamp = _snapshot_timestamp({"created": bar[date_column]})
        bar_date = timestamp.astimezone(ET).date() if timestamp else _local_date(bar[date_column])
        if bar_date == target_date:
            close = as_float(bar.get("close"))
            return close if close > 0 else None
    return None


def _intrinsic(right: str, strike: float, underlying: float) -> float:
    if right == "CALL":
        return max(underlying - strike, 0.0)
    if right == "PUT":
        return max(strike - underlying, 0.0)
    return 0.0


def evaluate_expired_alert(
    row: dict[str, Any], payload: dict[str, Any], now: datetime
) -> bool:
    if row.get("alert_type") == "swing":
        return False
    expiry = _local_date(payload.get("expiration"))
    if expiry is None or expiry > now.astimezone(ET).date():
        return False
    if expiry == now.astimezone(ET).date() and now.astimezone(ET).time() < time(16, 0):
        return False
    symbol = str(row.get("symbol") or "")
    spot = _daily_close(symbol, expiry)
    strike = as_float(payload.get("strike"))
    right = str(payload.get("right") or ("PUT" if row.get("alert_type") == "csp" else "")).upper()
    if spot is None or strike <= 0 or right not in {"CALL", "PUT"}:
        return False
    intrinsic = _intrinsic(right, strike, spot)
    reason = "EXPIRED ASSIGNED" if intrinsic > 0 else "EXPIRED WORTHLESS"
    return post_closed_alert(row, payload, reason, intrinsic, expiry, now)


def evaluate_swing_stale(
    row: dict[str, Any], payload: dict[str, Any], now: datetime
) -> bool:
    if row.get("alert_type") != "swing":
        return False
    created = _local_date(row.get("created_at"))
    today = now.astimezone(ET).date()
    if created is None or today <= created:
        return False
    sessions = sum(
        1
        for offset in range(1, (today - created).days + 1)
        if is_trading_day(created + timedelta(days=offset))
    )
    if sessions < 20:
        return False
    close = _daily_close(str(row.get("symbol") or ""), today)
    if close is None:
        return False
    return post_closed_alert(row, payload, "STALE", close, today, now)


def evaluate_eod_closures(rows: list[dict[str, Any]], now: datetime) -> int:
    local_now = now.astimezone(ET)
    if (
        not is_trading_day(local_now.date())
        or local_now.time() < market_close_time(local_now.date())
    ):
        return 0
    closed = 0
    for row in rows:
        if closed_alert_is_recorded(int(row["id"])):
            continue
        try:
            payload = json.loads(row.get("payload") or "{}")
        except (TypeError, json.JSONDecodeError):
            print(f"[yellow]Skipping ledger evaluation for alert #{row.get('id')}: invalid payload[/yellow]")
            continue
        if not isinstance(payload, dict) or payload.get("is_followup"):
            continue
        if evaluate_expired_alert(row, payload, now) or evaluate_swing_stale(row, payload, now):
            closed += 1
    return closed


def evaluate_scheduled_eod_closures(now: datetime) -> int:
    cutoff = now - timedelta(days=45)
    rows = followup_alerts_since(cutoff)
    return evaluate_eod_closures(rows, now)
