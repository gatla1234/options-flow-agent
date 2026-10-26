from __future__ import annotations

import hashlib
import json
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any

import requests
from rich import print

from src.config import (
    FOLLOWUP_MAX_AGE_DAYS,
    FOLLOWUP_NEWS_LOOKBACK_HOURS,
    FOLLOWUP_PRICE_POLL_SECONDS,
)
from src.data import get_live_price
from src.discord_alerts import send_alert
from src.market import ET, is_rth
from src.state import (
    followup_alerts_since,
    followup_event_sent,
    get_flag,
    mark_followup_event_sent,
    set_flag,
)
from src.util import as_float

_FOLLOWUP_COLORS = {
    "flow": 0x5865F2,
    "swing": 0xF1C40F,
    "csp": 0x3498DB,
}


def _parse_payload(row: dict[str, Any]) -> dict[str, Any] | None:
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
    description = (
        f"**Follow-up to original alert #{alert_id}:** {original_title}\n"
        f"**Original alert time:** {created_display}\n"
        f"**Underlying now:** "
        f"{f'${current_price:.2f}' if current_price is not None else 'Not available'}\n"
        f"**Update:** {details}\n\n"
        "This is a reminder to review the original setup if relevant. "
        "The system does not know whether you entered or still hold a position. "
        "This is not a buy, sell, or hold instruction."
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
    )
    if sent:
        mark_followup_event_sent(alert_id, event_key)
    return sent


def _price_events(
    row: dict[str, Any],
    payload: dict[str, Any],
    price: float,
) -> int:
    symbol = str(row.get("symbol") or "unknown")
    events_sent = 0
    invalidation = _invalidation_reference(payload)
    if invalidation:
        level, condition, level_name = invalidation
        breached = (
            condition == "below" and level > 0 and price <= level
        ) or (
            condition == "above" and level > 0 and price >= level
        )
        if breached:
            event = f"invalidation:{condition}:{level:.2f}"
            if _send_followup(
                row,
                event,
                "Original invalidation level reached",
                (
                    f"{symbol} is ${price:.2f}; the original invalidation reference "
                    f"is {condition} ${level:.2f} ({level_name})."
                ),
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
            event = f"target-2r:{target:.2f}"
            if _send_followup(
                row,
                event,
                "Original 2R reference reached",
                (
                    f"{symbol} is ${price:.2f}, at/past the original 2R underlying "
                    f"reference of ${target:.2f}. Consider reviewing the setup; "
                    "this is not a profit-taking instruction."
                ),
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
                    f"strike of ${strike:.2f}. Review assignment risk if you "
                    "entered the CSP; no position is assumed."
                ),
                price,
                payload,
            ):
                events_sent += 1
    return events_sent


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
        if price is None or price <= 0:
            continue
        sent += _price_events(row, payload, price)
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
                "exercise risk if you entered the trade."
            )
        else:
            event, label = "expiration:5d", "Contract nearing expiration"
            detail = (
                f"The original contract expires {expiry_date.isoformat()} "
                f"({days_left} calendar days away). Review it if you entered "
                "the trade."
            )
        if _send_followup(row, event, label, detail, None, payload):
            sent += 1
    return sent


def run_scheduled_followups(now: datetime | None = None) -> int:
    """Run expiration and company-news checks from scheduled report windows."""
    now = now or datetime.now(ET)
    return run_expiration_followups(now) + run_news_followups(now)
