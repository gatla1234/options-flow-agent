from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Generator

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "alerts.db"


@contextmanager
def _connect() -> Generator[sqlite3.Connection, None, None]:
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
            cross_stream_key TEXT,
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
        """
        CREATE TABLE IF NOT EXISTS closed_alert_ledger (
            alert_id INTEGER PRIMARY KEY,
            status TEXT NOT NULL,
            record TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tracker_messages (
            board_key TEXT PRIMARY KEY,
            message_id TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS funnel_scans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_type TEXT NOT NULL,
            symbol TEXT NOT NULL,
            counts TEXT NOT NULL,
            data_available INTEGER NOT NULL,
            scanned_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS discord_alert_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            route TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            color INTEGER NOT NULL,
            fields TEXT,
            alert_type TEXT NOT NULL,
            alert_key TEXT,
            symbol TEXT,
            price REAL,
            payload TEXT,
            cross_stream_key TEXT,
            queued_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS discord_delivery_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            attempted_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_discord_attempts_at "
        "ON discord_delivery_attempts(attempted_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_alerts_key_created ON alerts(alert_key, created_at)"
    )
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(alerts)").fetchall()
    }
    if "cross_stream_key" not in columns:
        conn.execute("ALTER TABLE alerts ADD COLUMN cross_stream_key TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_alerts_cross_stream_created "
        "ON alerts(cross_stream_key, created_at)"
    )
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()


def recently_sent(alert_key: str, cooldown_hours: float) -> bool:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=cooldown_hours)
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM alerts WHERE alert_key = ? AND created_at >= ? LIMIT 1",
            (alert_key, cutoff.isoformat()),
        ).fetchone()
    return row is not None


def recently_sent_seconds(alert_key: str, cooldown_seconds: float) -> bool:
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=cooldown_seconds)
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM alerts WHERE alert_key = ? AND created_at >= ? LIMIT 1",
            (alert_key, cutoff.isoformat()),
        ).fetchone()
    return row is not None


def recently_sent_cross_stream(
    cross_stream_key: str, cooldown_seconds: float
) -> bool:
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=cooldown_seconds)
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM alerts
            WHERE cross_stream_key = ? AND created_at >= ?
            LIMIT 1
            """,
            (cross_stream_key, cutoff.isoformat()),
        ).fetchone()
    return row is not None


def pending_discord_alert_duplicate(
    alert_key: str | None, cross_stream_key: str | None
) -> bool:
    keys = [key for key in (alert_key, cross_stream_key) if key is not None]
    if not keys:
        return False
    placeholders = ", ".join("?" for _ in keys)
    with _connect() as conn:
        row = conn.execute(
            f"""
            SELECT 1 FROM discord_alert_queue
            WHERE alert_key IN ({placeholders})
               OR cross_stream_key IN ({placeholders})
            LIMIT 1
            """,
            (*keys, *keys),
        ).fetchone()
    return row is not None


def enqueue_discord_alert(alert: dict[str, Any]) -> int:
    with _connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO discord_alert_queue (
                route, title, description, color, fields, alert_type, alert_key,
                symbol, price, payload, cross_stream_key, queued_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                alert["route"],
                alert["title"],
                alert["description"],
                alert["color"],
                json.dumps(alert["fields"]) if alert["fields"] is not None else None,
                alert["alert_type"],
                alert["alert_key"],
                alert["symbol"],
                alert["price"],
                json.dumps(alert["payload"]) if alert["payload"] is not None else None,
                alert["cross_stream_key"],
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)


def queued_discord_alerts(limit: int = 1) -> list[dict[str, Any]]:
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT * FROM discord_alert_queue
            ORDER BY CASE WHEN route = 'confirmed' THEN 0 ELSE 1 END, id
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    result = []
    for row in rows:
        alert = dict(row)
        alert["fields"] = json.loads(alert["fields"]) if alert["fields"] else None
        alert["payload"] = json.loads(alert["payload"]) if alert["payload"] else None
        result.append(alert)
    return result


def remove_queued_discord_alert(alert_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM discord_alert_queue WHERE id = ?", (alert_id,))
        conn.commit()


def recover_stale_closed_alert_claims(older_than: datetime) -> int:
    if older_than.tzinfo is None:
        raise ValueError("older_than must be timezone-aware")
    with _connect() as conn:
        cursor = conn.execute(
            """
            UPDATE closed_alert_ledger
            SET status = 'pending', updated_at = ?
            WHERE status = 'sending' AND updated_at < ?
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                older_than.astimezone(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
        return cursor.rowcount


def recent_discord_delivery_attempts(cutoff_epoch: float) -> list[float]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT attempted_at FROM discord_delivery_attempts
            WHERE attempted_at >= ?
            ORDER BY attempted_at
            """,
            (cutoff_epoch,),
        ).fetchall()
    return [float(row[0]) for row in rows]


def record_discord_delivery_attempt(attempted_at: float) -> None:
    cutoff = attempted_at - 24 * 60 * 60
    with _connect() as conn:
        conn.execute(
            "INSERT INTO discord_delivery_attempts(attempted_at) VALUES (?)",
            (attempted_at,),
        )
        conn.execute(
            "DELETE FROM discord_delivery_attempts WHERE attempted_at < ?",
            (cutoff,),
        )
        conn.commit()


def discord_alert_delivery_status(alert_key: str) -> str | None:
    with _connect() as conn:
        if conn.execute(
            "SELECT 1 FROM alerts WHERE alert_key = ? LIMIT 1", (alert_key,)
        ).fetchone():
            return "delivered"
        if conn.execute(
            """
            SELECT 1 FROM discord_alert_queue
            WHERE alert_key = ? LIMIT 1
            """,
            (alert_key,),
        ).fetchone():
            return "queued"
    return None


def log_alert(
    alert_key: str,
    alert_type: str,
    symbol: str | None,
    title: str,
    payload: dict[str, Any] | None = None,
    price_at_alert: float | None = None,
    cross_stream_key: str | None = None,
    created_at: datetime | None = None,
) -> None:
    timestamp = created_at or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        raise ValueError("created_at must be timezone-aware")
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO alerts (
                alert_key, alert_type, symbol, title, payload, price_at_alert,
                cross_stream_key, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                alert_key,
                alert_type,
                symbol,
                title,
                json.dumps(payload or {}),
                price_at_alert,
                cross_stream_key,
                timestamp.astimezone(timezone.utc).isoformat(),
            ),
        )
        conn.commit()


def log_funnel_scan(
    scan_type: str,
    symbol: str,
    counts: dict[str, int],
    data_available: bool,
    scanned_at: datetime | None = None,
) -> None:
    timestamp = scanned_at or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        raise ValueError("scanned_at must be timezone-aware")
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO funnel_scans (
                scan_type, symbol, counts, data_available, scanned_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                scan_type,
                symbol,
                json.dumps(counts),
                int(data_available),
                timestamp.astimezone(timezone.utc).isoformat(),
            ),
        )
        conn.commit()


def funnel_scans_between(start: datetime, end: datetime) -> list[dict[str, Any]]:
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT scan_type, symbol, counts, data_available, scanned_at
            FROM funnel_scans
            WHERE scanned_at >= ? AND scanned_at < ?
            ORDER BY scanned_at
            """,
            (
                start.astimezone(timezone.utc).isoformat(),
                end.astimezone(timezone.utc).isoformat(),
            ),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["counts"] = json.loads(item["counts"])
        item["data_available"] = bool(item["data_available"])
        result.append(item)
    return result


def scan_summary_alerts_between(
    start: datetime, end: datetime
) -> list[dict[str, Any]]:
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, alert_type, symbol, title, payload, created_at
            FROM alerts
            WHERE created_at >= ? AND created_at < ?
              AND alert_type IN ('flow', 'swing', 'csp')
            ORDER BY created_at, id
            """,
            (
                start.astimezone(timezone.utc).isoformat(),
                end.astimezone(timezone.utc).isoformat(),
            ),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["payload"] = json.loads(item["payload"] or "{}")
        result.append(item)
    return result


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


def save_closed_alert_record(alert_id: int, record: dict[str, Any]) -> dict[str, Any]:
    """Persist the first closure event so retries use the same immutable facts."""
    now = datetime.now(timezone.utc).isoformat()
    encoded = json.dumps(record)
    with _connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO closed_alert_ledger (
                alert_id, status, record, created_at, updated_at
            ) VALUES (?, 'pending', ?, ?, ?)
            """,
            (alert_id, encoded, now, now),
        )
        row = conn.execute(
            "SELECT record FROM closed_alert_ledger WHERE alert_id = ?",
            (alert_id,),
        ).fetchone()
        conn.commit()
    return json.loads(row[0])


def claim_closed_alert_record(alert_id: int) -> bool:
    """Atomically claim a pending ledger record before sending it."""
    with _connect() as conn:
        cursor = conn.execute(
            """
            UPDATE closed_alert_ledger
            SET status = 'sending', updated_at = ?
            WHERE alert_id = ? AND status = 'pending'
            """,
            (datetime.now(timezone.utc).isoformat(), alert_id),
        )
        conn.commit()
    return cursor.rowcount == 1


def mark_closed_alert_record_posted(alert_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            """
            UPDATE closed_alert_ledger
            SET status = 'posted', updated_at = ?
            WHERE alert_id = ?
            """,
            (datetime.now(timezone.utc).isoformat(), alert_id),
        )
        conn.commit()


def release_closed_alert_record(alert_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            """
            UPDATE closed_alert_ledger
            SET status = 'pending', updated_at = ?
            WHERE alert_id = ? AND status = 'sending'
            """,
            (datetime.now(timezone.utc).isoformat(), alert_id),
        )
        conn.commit()


def closed_alert_record(alert_id: int) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT status, record FROM closed_alert_ledger WHERE alert_id = ?",
            (alert_id,),
        ).fetchone()
    if row is None:
        return None
    result = json.loads(row[1])
    result["_status"] = row[0]
    return result


def pending_closed_alert_records() -> list[dict[str, Any]]:
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT alert_id, record
            FROM closed_alert_ledger
            WHERE status = 'pending'
            ORDER BY alert_id
            """
        ).fetchall()
    return [
        {"alert_id": row["alert_id"], "record": json.loads(row["record"])}
        for row in rows
    ]


def closed_alert_is_posted(alert_id: int) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM closed_alert_ledger WHERE alert_id = ? AND status = 'posted'",
            (alert_id,),
        ).fetchone()
    return row is not None


def closed_alert_is_recorded(alert_id: int) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM closed_alert_ledger WHERE alert_id = ?",
            (alert_id,),
        ).fetchone()
    return row is not None


def tracked_alert_rows() -> list[dict[str, Any]]:
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT a.id, a.alert_type, a.symbol, a.title, a.payload,
                   a.price_at_alert, a.created_at
            FROM alerts a
            LEFT JOIN closed_alert_ledger c ON c.alert_id = a.id
            WHERE a.alert_type IN ('flow', 'swing', 'csp')
              AND c.alert_id IS NULL
            ORDER BY a.created_at, a.id
            """
        ).fetchall()
    results: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        try:
            payload = json.loads(item.get("payload") or "{}")
        except (TypeError, json.JSONDecodeError):
            payload = {}
        if not isinstance(payload, dict) or payload.get("is_followup"):
            continue
        item["payload"] = payload
        results.append(item)
    return results


def closed_alert_records() -> list[dict[str, Any]]:
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT alert_id, status, record, created_at, updated_at
            FROM closed_alert_ledger
            ORDER BY alert_id
            """
        ).fetchall()
    records: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        record = json.loads(item.pop("record"))
        records.append({**record, "_status": item["status"]})
    return records


def all_alert_creation_dates() -> list[str]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT created_at, payload FROM alerts
            WHERE alert_type IN ('flow', 'swing', 'csp')
            ORDER BY created_at
            """
        ).fetchall()
    dates: list[str] = []
    for created_at, encoded_payload in rows:
        try:
            payload = json.loads(encoded_payload or "{}")
        except (TypeError, json.JSONDecodeError):
            payload = {}
        if isinstance(payload, dict) and payload.get("is_followup"):
            continue
        dates.append(str(created_at))
    return dates


def get_tracker_message_id(board_key: str) -> str | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT message_id FROM tracker_messages WHERE board_key = ?",
            (board_key,),
        ).fetchone()
    return str(row[0]) if row else None


def set_tracker_message_id(board_key: str, message_id: str) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO tracker_messages(board_key, message_id, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(board_key) DO UPDATE SET
                message_id = excluded.message_id,
                updated_at = excluded.updated_at
            """,
            (board_key, message_id, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()


def tracker_message_ids() -> dict[str, str]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT board_key, message_id FROM tracker_messages"
        ).fetchall()
    return {str(row[0]): str(row[1]) for row in rows}


def remove_tracker_message_id(board_key: str) -> None:
    with _connect() as conn:
        conn.execute(
            "DELETE FROM tracker_messages WHERE board_key = ?", (board_key,)
        )
        conn.commit()


def grade_alert(alert_id: int, outcome: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE alerts SET outcome = ?, graded_at = ? WHERE id = ?",
            (outcome, datetime.now(timezone.utc).isoformat(), alert_id),
        )
        conn.commit()
