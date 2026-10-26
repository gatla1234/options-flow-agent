from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "alerts.db"


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_key TEXT NOT NULL,
            alert_type TEXT NOT NULL,
            symbol TEXT,
            title TEXT,
            payload TEXT,
            price_at_alert REAL,
            created_at TEXT NOT NULL,
            outcome TEXT,
            graded_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS flags (
            name TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS followup_events (
            original_alert_id INTEGER NOT NULL,
            event_key TEXT NOT NULL,
            sent_at TEXT NOT NULL,
            PRIMARY KEY (original_alert_id, event_key)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_alerts_key_created ON alerts(alert_key, created_at)"
    )
    conn.commit()
    return conn


def recently_sent(alert_key: str, cooldown_hours: float) -> bool:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=cooldown_hours)
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM alerts WHERE alert_key = ? AND created_at >= ? LIMIT 1",
            (alert_key, cutoff.isoformat()),
        ).fetchone()
    return row is not None


def log_alert(
    alert_key: str,
    alert_type: str,
    symbol: str | None,
    title: str,
    payload: dict[str, Any] | None = None,
    price_at_alert: float | None = None,
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO alerts (
                alert_key, alert_type, symbol, title, payload, price_at_alert, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                alert_key,
                alert_type,
                symbol,
                title,
                json.dumps(payload or {}),
                price_at_alert,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()


def get_flag(name: str) -> str | None:
    with _connect() as conn:
        row = conn.execute("SELECT value FROM flags WHERE name = ?", (name,)).fetchone()
    return row[0] if row else None


def set_flag(name: str, value: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO flags(name, value) VALUES(?, ?) ON CONFLICT(name) DO UPDATE SET value = excluded.value",
            (name, value),
        )
        conn.commit()


def ungraded_alerts(alert_type: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT id, alert_type, symbol, payload, price_at_alert, created_at FROM alerts WHERE outcome IS NULL"
    params: tuple = ()
    if alert_type:
        sql += " AND alert_type = ?"
        params = (alert_type,)
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def alerts_between(
    alert_type: str, start: datetime, end: datetime
) -> list[dict[str, Any]]:
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, alert_type, symbol, title, payload, price_at_alert, created_at
            FROM alerts
            WHERE alert_type = ? AND created_at >= ? AND created_at < ?
            ORDER BY created_at
            """,
            (
                alert_type,
                start.astimezone(timezone.utc).isoformat(),
                end.astimezone(timezone.utc).isoformat(),
            ),
        ).fetchall()
    return [dict(row) for row in rows]


def followup_alerts_since(cutoff: datetime) -> list[dict[str, Any]]:
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, alert_type, symbol, title, payload, price_at_alert, created_at
            FROM alerts
            WHERE alert_type IN ('flow', 'swing', 'csp') AND created_at >= ?
            ORDER BY created_at
            """,
            (cutoff.astimezone(timezone.utc).isoformat(),),
        ).fetchall()
    return [dict(row) for row in rows]


def followup_event_sent(original_alert_id: int, event_key: str) -> bool:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM followup_events
            WHERE original_alert_id = ? AND event_key = ?
            """,
            (original_alert_id, event_key),
        ).fetchone()
    return row is not None


def mark_followup_event_sent(original_alert_id: int, event_key: str) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO followup_events (
                original_alert_id, event_key, sent_at
            ) VALUES (?, ?, ?)
            """,
            (
                original_alert_id,
                event_key,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()


def grade_alert(alert_id: int, outcome: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE alerts SET outcome = ?, graded_at = ? WHERE id = ?",
            (outcome, datetime.now(timezone.utc).isoformat(), alert_id),
        )
        conn.commit()
