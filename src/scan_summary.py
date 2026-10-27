from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from typing import Any

from rich import print

from src.config import (
    CSP_TICKERS,
    FLOW_TICKERS,
    SCAN_SUMMARY_MESSAGE_LIMIT,
    SCAN_SUMMARY_TIME,
    SWING_TICKERS,
)
from src.discord_alerts import WEBHOOKS, send_alert
from src.market import ET, is_trading_day
from src.state import (
    funnel_scans_between,
    get_flag,
    scan_summary_alerts_between,
    set_flag,
)
from src.util import as_float

_SECTIONS = (
    ("confirmed", "CONFIRMED FLOW", FLOW_TICKERS,
     ("contracts", "dte", "delta", "strike", "volume_oi", "premium", "buy_pct", "trend", "confirmation")),
    ("daytrade", "DAY-TRADE FLOW", FLOW_TICKERS,
     ("contracts", "dte", "delta", "strike", "volume_oi", "premium", "buy_pct", "trend", "confirmation")),
    (
        "swing",
        "SWING",
        SWING_TICKERS,
        ("candidates", "trend", "volume", "relative_strength", "confirmation", "risk_reward"),
    ),
    ("csp", "CSP", CSP_TICKERS,
     ("candidates", "contract", "max_loss", "otm", "premium", "spread", "delta")),
)
_STAGE_NAMES = {
    "dte": "DTE gate",
    "contracts": "DTE gate",
    "delta": "delta gate",
    "strike": "strike gate",
    "volume_oi": "volume/OI gate",
    "premium": "premium gate",
    "buy_pct": "buy% gate",
    "trend": "trend gate",
    "confirmation": "confirmation gate",
    "candidates": "candidates",
    "contract": "contract gate",
    "max_loss": "max-loss gate",
    "ownership": "ownership gate",
    "otm": "OTM gate",
    "spread": "spread gate",
    "signal": "signal criteria",
    "relative_strength": "relative-strength gate",
    "risk_reward": "risk/reward gate",
}


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=ET)
    return start, start + timedelta(days=1)


def _section_for_alert(alert: dict[str, Any]) -> str | None:
    payload = alert.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    if alert["alert_type"] == "flow":
        stream = payload.get("stream")
        if stream in {"confirmed", "daytrade"}:
            return stream
        title = str(alert.get("title") or "").upper()
        return "daytrade" if "DAY-TRADE" in title else "confirmed"
    if alert["alert_type"] in {"swing", "csp"}:
        return alert["alert_type"]
    return None


def _followup_kind(payload: dict[str, Any]) -> str:
    event = str(payload.get("followup_event") or "").lower()
    if "reverse" in event:
        return "REVERSE"
    if "average" in event:
        return "AVERAGE"
    if "invalidation" in event or "exit" in event:
        return "EXIT"
    if "2r" in event or "target" in event:
        return "2R"
    if "strike" in event or "assignment" in event:
        return "STRIKE"
    if "news" in event:
        return "NEWS"
    if "expiration" in event:
        return "EXPIRATION"
    return "OTHER"


def _direction(payload: dict[str, Any], alert_type: str) -> str:
    right = payload.get("right")
    if right:
        return str(right).upper()
    if alert_type == "csp":
        return "PUT"
    direction = payload.get("direction")
    if direction:
        return str(direction).upper()
    return "unavailable"


def _contract(payload: dict[str, Any], alert_type: str) -> str:
    strike = payload.get("strike")
    expiration = payload.get("expiration")
    right = str(payload.get("right") or ("PUT" if alert_type == "csp" else "")).upper()
    if strike is None:
        return "underlying" if alert_type == "swing" else "contract unavailable"
    if right not in {"CALL", "PUT"}:
        return "contract unavailable"
    strike_value = as_float(strike)
    if strike_value <= 0:
        return "contract unavailable"
    suffix = "C" if right == "CALL" else "P"
    contract = f"${strike_value:g}{suffix}"
    if expiration:
        contract += f" exp {expiration}"
    return contract


def _alert_time(created_at: str) -> str:
    try:
        value = datetime.fromisoformat(created_at)
        if value.tzinfo is None:
            return "time unavailable"
        return value.astimezone(ET).strftime("%H:%M ET")
    except (TypeError, ValueError):
        return "time unavailable"


def _ticker_alert_data(
    alerts: list[dict[str, Any]], section: str, symbol: str
) -> tuple[list[str], Counter[str], int]:
    original_lines: list[str] = []
    followup_kinds: Counter[str] = Counter()
    followup_count = 0
    for alert in alerts:
        if _section_for_alert(alert) != section or alert.get("symbol") != symbol:
            continue
        payload = alert.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        alert_type = str(alert.get("alert_type") or "")
        number = alert["id"]
        when = _alert_time(str(alert.get("created_at") or ""))
        if payload.get("is_followup"):
            kind = _followup_kind(payload)
            followup_kinds[kind] += 1
            followup_count += 1
            original_lines.append(
                f"Follow-up #{number}: {kind} · "
                f"{_direction(payload, alert_type)} · "
                f"{_contract(payload, alert_type)} · {when}"
            )
        else:
            direction = _direction(payload, alert_type)
            contract = _contract(payload, alert_type)
            original_lines.append(
                f"Alert #{number}: {direction} · {contract} · {when}"
            )
    return original_lines, followup_kinds, followup_count


def _top_blockers(counts: dict[str, int], stages: tuple[str, ...]) -> list[str]:
    drops: list[tuple[int, str, int]] = []
    for previous, stage in zip(stages, stages[1:]):
        entered = max(0, int(counts.get(previous, 0)))
        passed = max(0, int(counts.get(stage, 0)))
        blocked = max(0, entered - passed)
        if blocked:
            drops.append((blocked, stage, entered))
    ownership_blocked = max(
        0,
        int(counts.get("ownership_entered", 0)) - int(counts.get("ownership", 0)),
    )
    if ownership_blocked:
        drops.append((ownership_blocked, "ownership", int(counts["ownership_entered"])))
    drops.sort(
        key=lambda item: (
            -item[0],
            stages.index(item[1]) if item[1] in stages else -1,
        )
    )
    return [
        f"{_STAGE_NAMES[stage]} blocked {blocked} of {entered}"
        for blocked, stage, entered in drops[:3]
    ]


def _ticker_block(
    section: str,
    label: str,
    symbol: str,
    counts: dict[str, int] | None,
    alerts: list[dict[str, Any]],
    stages: tuple[str, ...],
    alert_data_available: bool,
) -> tuple[list[str], int, int]:
    if counts is None or not alert_data_available:
        return [f"{symbol} {label}: data unavailable"], 0, 0

    candidate_count = max(0, int(counts.get(stages[0], 0)))
    alert_lines, followup_kinds, followup_count = _ticker_alert_data(
        alerts, section, symbol
    )
    lines = [
        f"{symbol} {label}:",
        (
            f"Candidates considered: {candidate_count} setup patterns"
            if section == "swing"
            else f"Candidates considered: {candidate_count}"
        ),
    ]
    blockers = _top_blockers(counts, stages)
    lines.append(
        "Top blocking filters: "
        + ("; ".join(blockers) if blockers else "none observed")
    )
    signal_alert_lines = [line for line in alert_lines if line.startswith("Alert #")]
    lines.append(f"Alerts fired: {len(signal_alert_lines)}")
    if signal_alert_lines:
        lines.extend(signal_alert_lines)
    else:
        lines.append(f"{symbol} {label}: no alerts")
    kind_summary = ", ".join(
        f"{count} {kind}" for kind, count in sorted(followup_kinds.items())
    )
    lines.append(
        f"Follow-ups: {followup_count}"
        + (f" ({kind_summary})" if kind_summary else "")
    )
    lines.extend(line for line in alert_lines if line.startswith("Follow-up #"))
    return lines, candidate_count, followup_count


def _split_block(lines: list[str], limit: int) -> list[list[str]]:
    chunks: list[list[str]] = []
    current: list[str] = []
    current_length = 0
    for line in lines:
        if len(line) > limit:
            if current:
                chunks.append(current)
                current, current_length = [], 0
            for offset in range(0, len(line), limit):
                chunks.append([line[offset:offset + limit]])
            continue
        line_length = len(line) + (1 if current else 0)
        if current and current_length + line_length > limit:
            chunks.append(current)
            current, current_length = [], 0
        if current:
            current_length += 1
        current.append(line)
        current_length += len(line)
    if current:
        chunks.append(current)
    return chunks


def _pack_messages(
    section_title: str,
    blocks: list[list[str]],
    limit: int,
    global_header: str | None = None,
) -> list[str]:
    messages: list[str] = []
    prefix = [section_title]
    if global_header:
        prefix.insert(0, global_header)
    current = prefix.copy()
    first_prefix = prefix.copy()
    split_overhead = len(section_title) + 45

    def flush() -> None:
        if len(current) > len(prefix):
            messages.append("\n".join(current))

    for block in blocks:
        heading = block[0] if block else ""
        piece_groups = _split_block(block, max(1, limit - split_overhead))
        for index, pieces in enumerate(piece_groups):
            if len(current) > len(prefix):
                projected = len("\n".join(current + pieces))
                if projected > limit:
                    flush()
                    current[:] = [section_title]
            group = pieces
            if index:
                group = [f"{heading} (continued)", *pieces]
            if len("\n".join(current + group)) > limit:
                if global_header and not messages and len(current) == len(prefix):
                    messages.append("\n".join(current))
                    current[:] = [section_title]
                else:
                    flush()
                    current[:] = first_prefix if not messages else [section_title]
            current.extend(group)
    flush()
    if not messages:
        messages.append("\n".join(prefix))
    return messages


def build_daily_scan_summary(day: date) -> dict[str, Any]:
    start, end = _day_bounds(day)
    funnel_query_ok = True
    alert_query_ok = True
    try:
        funnel_rows = funnel_scans_between(start, end)
    except Exception as exc:
        print(f"[red]Could not read funnel data for {day}: {type(exc).__name__}: {exc}[/red]")
        funnel_rows = []
        funnel_query_ok = False
    try:
        alert_rows = scan_summary_alerts_between(start, end)
    except Exception as exc:
        print(f"[red]Could not read alert data for {day}: {type(exc).__name__}: {exc}[/red]")
        alert_rows = None
        alert_query_ok = False
    funnel_data: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in funnel_rows:
        funnel_data[(row["scan_type"], row["symbol"])].append(row)

    totals: dict[str, dict[str, int]] = {}
    section_messages: dict[str, list[str]] = {}
    missing_alert_data = False
    missing_funnel_data = not funnel_query_ok
    total_alerts = sum(
        1
        for row in alert_rows or []
        if not isinstance(row.get("payload"), dict)
        or not row["payload"].get("is_followup")
    )
    total_followups = sum(
        1
        for row in alert_rows or []
        if isinstance(row.get("payload"), dict)
        and row["payload"].get("is_followup")
    )
    date_label = day.strftime("%a %b %d").replace(" 0", " ")
    if not alert_query_ok or alert_rows is None:
        header = f"Scan summary — {date_label}: alert totals unavailable across 4 scan types"
    else:
        header = (
            f"Scan summary — {date_label}: {total_alerts} alerts, "
            f"{total_followups} follow-ups across 4 scan types"
        )
    for section, label, tickers, stages in _SECTIONS:
        blocks: list[list[str]] = []
        section_candidates = section_alerts = section_followups = 0
        available_tickers = 0
        for symbol in tickers:
            runs = funnel_data.get((section, symbol), [])
            successful_runs = [row for row in runs if row["data_available"]]
            if not successful_runs:
                counts = None
            else:
                available_tickers += 1
                counts = {
                    stage: sum(int(row["counts"].get(stage, 0)) for row in successful_runs)
                    for stage in stages
                }
            if alert_rows is None:
                missing_alert_data = True
            ticker_label = label.replace(" FLOW", "")
            ticker_lines, candidates, followups = _ticker_block(
                section,
                ticker_label,
                symbol,
                counts,
                alert_rows or [],
                stages,
                alert_data_available=alert_query_ok and alert_rows is not None,
            )
            if counts is None:
                missing_funnel_data = True
            blocks.append(ticker_lines)
            section_candidates += candidates
            section_followups += followups
            section_alerts += sum(1 for line in ticker_lines if line.startswith("Alert #"))
        totals[section] = {
            "candidates": section_candidates,
            "alerts": section_alerts,
            "followups": section_followups,
        }
        if alert_rows is None:
            section_title = f"{label} · data unavailable (alert log missing)"
        elif available_tickers == 0:
            section_title = f"{label} · data unavailable (funnel data missing)"
        else:
            total_note = (
                "Totals (available ticker data only)"
                if available_tickers < len(tickers)
                else "Totals"
            )
            section_title = (
                f"{label} · {total_note}: {section_candidates} candidates, "
                f"{section_alerts} alerts, {section_followups} follow-ups"
            )
        section_messages[section] = _pack_messages(
            section_title,
            blocks,
            SCAN_SUMMARY_MESSAGE_LIMIT,
            global_header=header if section == "confirmed" else None,
        )

    return {
        "date": day.isoformat(),
        "header": header,
        "totals": totals,
        "messages": section_messages,
        "alerts_available": alert_rows is not None,
        "missing_alert_data": missing_alert_data,
        "missing_funnel_data": missing_funnel_data,
        "has_scanner_data": any(row["data_available"] for row in funnel_rows),
    }


def _health_error(day: date, message: str) -> None:
    try:
        send_alert(
            title="Scan summary error",
            description=message,
            color=0xFFAA00,
            alert_type="health",
            alert_key=f"scan-summary-error:{day.isoformat()}",
            skip_dedupe=False,
        )
    except Exception as exc:
        print(f"[red]Could not notify system-health channel: {exc}[/red]")


def run_daily_scan_summary(when: datetime | None = None) -> bool:
    when = when or datetime.now(ET)
    if when.tzinfo is None:
        when = when.replace(tzinfo=ET)
    local_when = when.astimezone(ET)
    day = local_when.date()
    if not is_trading_day(day) or local_when.time() < time.fromisoformat(SCAN_SUMMARY_TIME):
        return False
    daily_flag = f"scan_summary:{day.isoformat()}"
    if get_flag(daily_flag) is not None:
        return False

    if not WEBHOOKS.get("scan_summary"):
        _health_error(day, "DISCORD_WEBHOOK_SCAN_SUMMARY is missing or empty; summary skipped.")
        set_flag(daily_flag, "skipped_missing_webhook")
        return False

    try:
        summary = build_daily_scan_summary(day)
    except Exception as exc:
        _health_error(day, f"Could not build daily scan summary: {type(exc).__name__}: {exc}")
        return False
    if not summary["has_scanner_data"]:
        notice = (
            f"Scan summary — {day.strftime('%a %b %d').replace(' 0', ' ')}: "
            "scanner data missing; daily summary not generated."
        )
        try:
            sent = send_alert(
                title="Daily scan summary — data missing",
                description=notice,
                color=0xFFAA00,
                alert_type="health",
                stream="scan_summary",
                skip_dedupe=True,
            )
        except Exception as exc:
            sent = False
            _health_error(day, f"Could not post missing-data notice: {type(exc).__name__}: {exc}")
        if sent:
            set_flag(daily_flag, "skipped_missing_data")
            _health_error(day, "No scanner funnel data was recorded; daily summary was not generated.")
        return sent

    success = True
    for section, label, _, _ in _SECTIONS:
        for index, description in enumerate(summary["messages"][section]):
            sent_flag = f"{daily_flag}:{section}:{index}"
            if get_flag(sent_flag) is not None:
                continue
            try:
                sent = send_alert(
                    title=f"Daily scan summary — {label}",
                    description=description,
                    color=0x5865F2,
                    alert_type="health",
                    alert_key=None,
                    stream="scan_summary",
                    skip_dedupe=True,
                )
            except Exception as exc:
                sent = False
                _health_error(
                    day,
                    f"Failed to post {label} scan summary: {type(exc).__name__}: {exc}",
                )
            if sent:
                set_flag(sent_flag, "sent")
            else:
                success = False
                _health_error(day, f"Failed to post {label} scan summary message {index + 1}.")
    if success:
        set_flag(daily_flag, "sent")
        if summary["missing_alert_data"]:
            _health_error(day, "Alert log data is unavailable for one or more scan summary sections.")
        if summary["missing_funnel_data"]:
            _health_error(day, "Funnel data is missing for one or more scan summary tickers.")
    return success
