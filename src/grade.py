from __future__ import annotations

import json
from datetime import datetime, timezone

from rich import print

from src.data import get_live_price
from src.state import grade_alert, ungraded_alerts
from src.util import as_float, pct_change


def grade_open_alerts(min_age_hours: float = 20.0) -> None:
    """Mark stored alerts with the move since they fired. Review later to tighten filters."""
    now = datetime.now(timezone.utc)
    rows = ungraded_alerts()
    if not rows:
        print("No ungraded alerts.")
        return

    for row in rows:
        created = datetime.fromisoformat(row["created_at"])
        age_hours = (now - created).total_seconds() / 3600
        if age_hours < min_age_hours:
            continue
        payload = {}
        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, json.JSONDecodeError):
            payload = {}
        if payload.get("is_followup"):
            continue
        symbol = row["symbol"]
        if not symbol:
            continue
        live = get_live_price(symbol)
        entry = as_float(row["price_at_alert"])
        if not live or not entry:
            continue
        move = pct_change(live, entry)
        direction = str(payload.get("direction") or payload.get("right") or "").upper()
        if "PUT" in direction or direction == "BEARISH":
            helpful = move is not None and move < 0
        else:
            helpful = move is not None and move > 0
        outcome = (
            f"{'helpful' if helpful else 'fade'} {move:+.2f}% "
            f"(entry {entry:.2f} -> {live:.2f})"
        )
        grade_alert(row["id"], outcome)
        print(f"{symbol} {row['alert_type']}: {outcome}")
