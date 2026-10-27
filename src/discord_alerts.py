from __future__ import annotations

import os
import threading
import time
from urllib.parse import urlsplit, urlunsplit
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import requests
from dotenv import load_dotenv
from rich import print

from src.config import (
    ALERT_COOLDOWN_HOURS,
    CROSS_STREAM_DEDUPE_SEC,
    DAY_COOLDOWN_SEC,
    MAX_DISCORD_ALERTS_PER_MINUTE,
)
from src.state import (
    enqueue_discord_alert,
    log_alert,
    pending_discord_alert_duplicate,
    queued_discord_alerts,
    recent_discord_delivery_attempts,
    recently_sent,
    remove_queued_discord_alert,
    recently_sent_cross_stream,
    recently_sent_seconds,
    record_discord_delivery_attempt,
)

load_dotenv()

WEBHOOKS = {
    "confirmed": os.getenv("DISCORD_WEBHOOK_CONFIRMED"),
    "daytrade": os.getenv("DISCORD_WEBHOOK_DAYTRADE"),
    "swing": os.getenv("DISCORD_WEBHOOK_SWING"),
    "csp": os.getenv("DISCORD_WEBHOOK_CSP"),
    "health": os.getenv("DISCORD_WEBHOOK_HEALTH"),
    "reports": os.getenv("DISCORD_WEBHOOK_REPORTS"),
    "scan_summary": os.getenv("DISCORD_WEBHOOK_SCAN_SUMMARY"),
    "closed_ledger": os.getenv("DISCORD_WEBHOOK_CLOSED_LEDGER"),
    "open_ledger": os.getenv("DISCORD_WEBHOOK_OPEN_LEDGER"),
    "profit_summary": os.getenv("DISCORD_WEBHOOK_PROFIT_SUMMARY"),
    "performance": os.getenv("DISCORD_WEBHOOK_PERFORMANCE"),
}
WEBHOOK_ENV_NAMES = {
    "confirmed": "DISCORD_WEBHOOK_CONFIRMED",
    "daytrade": "DISCORD_WEBHOOK_DAYTRADE",
    "swing": "DISCORD_WEBHOOK_SWING",
    "csp": "DISCORD_WEBHOOK_CSP",
    "health": "DISCORD_WEBHOOK_HEALTH",
    "reports": "DISCORD_WEBHOOK_REPORTS",
    "scan_summary": "DISCORD_WEBHOOK_SCAN_SUMMARY",
    "closed_ledger": "DISCORD_WEBHOOK_CLOSED_LEDGER",
    "open_ledger": "DISCORD_WEBHOOK_OPEN_LEDGER",
    "profit_summary": "DISCORD_WEBHOOK_PROFIT_SUMMARY",
    "performance": "DISCORD_WEBHOOK_PERFORMANCE",
}


@dataclass
class _AlertRequest:
    webhook: str
    title: str
    description: str
    color: int
    fields: list[dict] | None
    alert_type: str
    alert_key: str | None
    symbol: str | None
    price: float | None
    payload: dict[str, Any] | None
    cross_stream_key: str | None
    route: str
    queued_id: int | None = None


_DELIVERY_LOCK = threading.RLock()
_SENT_AT: deque[float] = deque()
_RATE_WINDOW_SECONDS = 60.0
def _prune_sent(now: float) -> None:
    _SENT_AT.clear()
    _SENT_AT.extend(
        recent_discord_delivery_attempts(now - _RATE_WINDOW_SECONDS)
    )


def _queue(request: _AlertRequest) -> None:
    request.queued_id = enqueue_discord_alert(
        {
            "route": request.route,
            "title": request.title,
            "description": request.description,
            "color": request.color,
            "fields": request.fields,
            "alert_type": request.alert_type,
            "alert_key": request.alert_key,
            "symbol": request.symbol,
            "price": request.price,
            "payload": request.payload,
            "cross_stream_key": request.cross_stream_key,
        }
    )
    print(
        f"[yellow]Queued Discord alert for rate limit: {request.title} "
        f"(queue #{request.queued_id})[/yellow]"
    )


def _notify_scan_summary_delivery_failure(title: str, reason: str) -> None:
    try:
        send_alert(
            title="Scan summary delivery error",
            description=f"{title}: {reason}",
            color=0xFFAA00,
            alert_type="health",
            alert_key=f"scan-summary-delivery-error:{title}",
        )
    except Exception as exc:
        print(f"[red]Could not report scan-summary delivery failure: {type(exc).__name__}[/red]")


def _deliver(request: _AlertRequest) -> bool:
    now = time.time()
    _prune_sent(now)
    record_discord_delivery_attempt(now)
    _SENT_AT.append(now)
    embed = {
        "title": request.title,
        "description": request.description,
        "color": request.color,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": f"Personal Trading Alerts · {request.alert_type}"},
    }
    if request.fields:
        embed["fields"] = request.fields

    try:
        response = requests.post(
            request.webhook,
            json={"embeds": [embed], "allowed_mentions": {"parse": []}},
            timeout=10,
        )
    except requests.RequestException as exc:
        print(
            f"[red]Failed to send alert {request.title}: "
            f"{type(exc).__name__} (request details withheld)[/red]"
        )
        return False

    if response.status_code not in (200, 204):
        print(
            f"[red]Discord error {response.status_code} for "
            f"{request.title} (response body withheld)[/red]"
        )
        return False

    if request.alert_key:
        log_alert(
            request.alert_key,
            request.alert_type,
            request.symbol,
            request.title,
            request.payload,
            request.price,
            request.cross_stream_key,
        )
    print(f"[green]✓ Alert sent: {request.title}[/green]")
    return True


def flush_alert_queue() -> int:
    """Deliver queued alerts as rate-limit slots become available."""
    delivered = 0
    with _DELIVERY_LOCK:
        while True:
            _prune_sent(time.time())
            if len(_SENT_AT) >= MAX_DISCORD_ALERTS_PER_MINUTE:
                break
            queued_alerts = queued_discord_alerts(limit=1)
            if not queued_alerts:
                break
            queued = queued_alerts[0]
            webhook = WEBHOOKS.get(queued["route"])
            if not webhook:
                env_name = WEBHOOK_ENV_NAMES.get(queued["route"], queued["route"])
                print(
                    f"[red]Dropped queued alert {queued['title']}: "
                    f"{env_name} is not configured[/red]"
                )
                if queued["route"] == "scan_summary":
                    _notify_scan_summary_delivery_failure(
                        queued["title"], f"{env_name} is not configured"
                    )
                remove_queued_discord_alert(queued["id"])
                continue
            request = _AlertRequest(
                webhook=webhook,
                title=queued["title"],
                description=queued["description"],
                color=queued["color"],
                fields=queued["fields"],
                alert_type=queued["alert_type"],
                alert_key=queued["alert_key"],
                symbol=queued["symbol"],
                price=queued["price"],
                payload=queued["payload"],
                cross_stream_key=queued["cross_stream_key"],
                route=queued["route"],
                queued_id=queued["id"],
            )
            if _deliver(request):
                remove_queued_discord_alert(queued["id"])
                delivered += 1
            else:
                if queued["route"] == "scan_summary":
                    _notify_scan_summary_delivery_failure(
                        queued["title"], "Discord delivery failed; queued for retry"
                    )
                break
    return delivered


def seconds_until_alert_capacity() -> float | None:
    with _DELIVERY_LOCK:
        if not queued_discord_alerts(limit=1):
            return None
        now = time.time()
        _prune_sent(now)
        if len(_SENT_AT) < MAX_DISCORD_ALERTS_PER_MINUTE:
            return 0.0
        return max(0.0, _SENT_AT[0] + _RATE_WINDOW_SECONDS - now)


def send_alert(
    title: str,
    description: str,
    color: int = 0x00FF00,
    fields: list[dict] | None = None,
    alert_type: str = "health",
    alert_key: str | None = None,
    symbol: str | None = None,
    price: float | None = None,
    payload: dict | None = None,
    skip_dedupe: bool = False,
    stream: str | None = None,
    cross_stream_key: str | None = None,
) -> bool:
    route = stream or ("confirmed" if alert_type == "flow" else alert_type)
    webhook = WEBHOOKS.get(route)
    if not webhook:
        env_name = WEBHOOK_ENV_NAMES.get(route, route)
        print(f"[red]{env_name} not found in .env[/red]")
        return False

    request = _AlertRequest(
        webhook=webhook,
        title=title,
        description=description,
        color=color,
        fields=fields,
        alert_type=alert_type,
        alert_key=alert_key,
        symbol=symbol,
        price=price,
        payload=payload,
        cross_stream_key=cross_stream_key,
        route=route,
    )
    with _DELIVERY_LOCK:
        if not skip_dedupe:
            is_duplicate = (
                recently_sent_seconds(alert_key, DAY_COOLDOWN_SEC)
                if alert_key and route == "daytrade"
                else bool(
                    alert_key
                    and recently_sent(
                        alert_key, ALERT_COOLDOWN_HOURS.get(route, 6)
                    )
                )
            )
            cross_duplicate = bool(
                cross_stream_key
                and recently_sent_cross_stream(
                    cross_stream_key, CROSS_STREAM_DEDUPE_SEC
                )
            )
            pending_duplicate = pending_discord_alert_duplicate(
                alert_key, cross_stream_key
            )
            if is_duplicate or cross_duplicate or pending_duplicate:
                print(f"[yellow]Skipped duplicate: {alert_key or title}[/yellow]")
                return False

        _prune_sent(time.time())
        if (
            queued_discord_alerts(limit=1)
            or len(_SENT_AT) >= MAX_DISCORD_ALERTS_PER_MINUTE
        ):
            _queue(request)
            return True
        return _deliver(request)


def _webhook_messages_url(webhook: str, message_id: str | None = None) -> str:
    parts = urlsplit(webhook)
    path = parts.path.rstrip("/")
    if message_id:
        path += f"/messages/{message_id}"
    query = parts.query
    if not message_id and "wait=" not in query:
        query = f"{query}&wait=true" if query else "wait=true"
    return urlunsplit((parts.scheme, parts.netloc, path, query, parts.fragment))


def edit_or_create_webhook_message(
    route: str,
    title: str,
    description: str,
    color: int,
    message_id: str | None,
) -> tuple[str | None, bool]:
    """Edit a dedicated webhook message, creating a replacement if editing fails."""
    webhook = WEBHOOKS.get(route)
    if not webhook:
        env_name = WEBHOOK_ENV_NAMES.get(route, route)
        print(f"[red]{env_name} not found in .env[/red]")
        return None, False

    embed = {
        "title": title,
        "description": description,
        "color": color,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "Personal Trading Alerts · status board"},
    }
    payload = {"embeds": [embed], "allowed_mentions": {"parse": []}}
    if message_id:
        try:
            response = requests.patch(
                _webhook_messages_url(webhook, message_id),
                json=payload,
                timeout=10,
            )
        except requests.RequestException as exc:
            print(f"[yellow]Could not edit {route} board: {type(exc).__name__}[/yellow]")
            return None, False
        else:
            if response.status_code in (200, 204):
                return message_id, True
            if response.status_code not in (404, 410):
                print(
                    f"[yellow]Could not edit {route} board: HTTP "
                    f"{response.status_code}; keeping stored message ID[/yellow]"
                )
                return None, False
            print(
                f"[yellow]Could not edit {route} board: HTTP "
                f"{response.status_code}; creating replacement[/yellow]"
            )

    try:
        response = requests.post(
            _webhook_messages_url(webhook),
            json=payload,
            timeout=10,
        )
    except requests.RequestException as exc:
        print(f"[red]Could not create {route} board: {type(exc).__name__}[/red]")
        return None, False
    if response.status_code not in (200, 204):
        print(
            f"[red]Could not create {route} board: HTTP "
            f"{response.status_code}[/red]"
        )
        return None, False
    try:
        body = response.json()
    except ValueError:
        print(f"[red]Discord did not return an ID for the new {route} board[/red]")
        return None, False
    new_id = str(body.get("id") or "")
    if not new_id:
        print(f"[red]Discord returned no ID for the new {route} board[/red]")
        return None, False
    return new_id, True


def delete_webhook_message(route: str, message_id: str) -> bool:
    webhook = WEBHOOKS.get(route)
    if not webhook:
        return False
    try:
        response = requests.delete(
            _webhook_messages_url(webhook, message_id),
            timeout=10,
        )
    except requests.RequestException as exc:
        print(f"[yellow]Could not remove obsolete {route} board: {type(exc).__name__}[/yellow]")
        return False
    if response.status_code in (200, 204, 404):
        return True
    print(
        f"[yellow]Could not remove obsolete {route} board: "
        f"HTTP {response.status_code}[/yellow]"
    )
    return False
