from __future__ import annotations

import os
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
from rich import print

from src.config import ALERT_COOLDOWN_HOURS
from src.state import log_alert, recently_sent

load_dotenv()

DEFAULT_WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL")
WEBHOOKS = {
    "flow": os.getenv("DISCORD_WEBHOOK_FLOW") or DEFAULT_WEBHOOK,
    "swing": os.getenv("DISCORD_WEBHOOK_SWING") or DEFAULT_WEBHOOK,
    "csp": os.getenv("DISCORD_WEBHOOK_CSP") or DEFAULT_WEBHOOK,
    "health": os.getenv("DISCORD_WEBHOOK_HEALTH") or DEFAULT_WEBHOOK,
}


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
) -> bool:
    webhook = WEBHOOKS.get(alert_type) or DEFAULT_WEBHOOK
    if not webhook:
        print("[red]DISCORD_WEBHOOK_URL not found in .env[/red]")
        return False

    if alert_key and not skip_dedupe:
        cooldown = ALERT_COOLDOWN_HOURS.get(alert_type, 6)
        if recently_sent(alert_key, cooldown):
            print(f"[yellow]Skipped duplicate: {alert_key}[/yellow]")
            return False

    embed = {
        "title": title,
        "description": description,
        "color": color,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": f"Personal Trading Alerts · {alert_type}"},
    }
    if fields:
        embed["fields"] = fields

    try:
        response = requests.post(webhook, json={"embeds": [embed]}, timeout=10)
        if response.status_code not in (200, 204):
            print(f"[red]Discord error {response.status_code}: {response.text}[/red]")
            return False
    except Exception as e:
        print(f"[red]Failed to send alert: {e}[/red]")
        return False

    if alert_key:
        log_alert(alert_key, alert_type, symbol, title, payload, price)
    print(f"[green]✓ Alert sent: {title}[/green]")
    return True
