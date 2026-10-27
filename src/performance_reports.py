from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

from src.config import (
    PERFORMANCE_REPORT_TIME,
    PROFIT_SUMMARY_TIME,
    SCAN_SUMMARY_MESSAGE_LIMIT,
)
from src.discord_alerts import WEBHOOKS, send_alert
from src.market import ET, is_trading_day
from src.performance_data import (
    SCAN_GROUPS,
    local_date,
    mark_open_alert,
)
from src.state import (
    all_alert_creation_dates,
    closed_alert_records,
    discord_alert_delivery_status,
    get_flag,
    set_flag,
    tracked_alert_rows,
)

BASIS = (
    "Hypothetical — alert price = entry, 1 contract / 1 share, before "
    "commissions and spreads. Not brokerage P&L."
)


def _value(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _amount(value: Any, suffix: str) -> str:
    number = _value(value)
    if number is None:
        return "unavailable"
    sign = "-" if number < 0 else "+"
    return f"{sign}${abs(number):,.2f}{suffix}"


def _asset_type(record: dict[str, Any]) -> str:
    scan_type = str(record.get("scan_type") or "").upper()
    return "swing" if scan_type == "SWING" else "options"


def _record_is_stale(record: dict[str, Any]) -> bool:
    return str(record.get("closing_reason") or "").upper() == "STALE"


def _scan_type(record: dict[str, Any]) -> str:
    scan_type = str(record.get("scan_type") or "").upper()
    if scan_type in {"CONFIRMED FLOW", "DAY-TRADE FLOW", "SWING", "CSP"}:
        return scan_type
    alert_type = str(record.get("scan_type") or "").lower()
    return {
        "flow": "CONFIRMED FLOW",
        "swing": "SWING",
        "csp": "CSP",
    }.get(alert_type, alert_type.upper() or "UNKNOWN")


def _daily_sections(
    marks: list[dict[str, Any]],
    closed: list[dict[str, Any]],
    today: date,
) -> tuple[list[str], dict[str, dict[str, float]], int, int]:
    todays_closed = [
        item for item in closed if local_date(item.get("close_date")) == today
    ]
    stale = [item for item in closed if _record_is_stale(item)]
    closed_nonstale = [item for item in todays_closed if not _record_is_stale(item)]
    lines = ["## OPEN"]
    if marks:
        for mark in marks:
            identity = (
                f"Alert #{mark['id']} {mark['symbol']} "
                f"({_scan_type({'scan_type': mark['group'].replace('confirmed', 'CONFIRMED FLOW').replace('daytrade', 'DAY-TRADE FLOW').upper()})})"
            )
            if mark["entry_contract"] is None:
                lines.append(f"{identity}: entry unavailable")
                continue
            if mark["pnl"] is None:
                lines.append(f"{identity}: price unavailable; excluded from totals")
                continue
            unit = "per share" if mark["scan_type"] == "swing" else "per contract"
            progress = []
            if mark["scan_type"] in {"flow", "swing"}:
                progress.append(
                    f"{mark['target_progress'] * 100:.1f}% toward 2R"
                    if mark["target_progress"] is not None
                    else "2R progress unavailable"
                )
                progress.append(
                    f"invalidation {mark['invalidation_status']}"
                    + (
                        f" at ${mark['invalidation']:.2f}"
                        + (
                            f" ({abs(mark['current_underlying'] - mark['invalidation']):.2f} away)"
                            if mark["current_underlying"] is not None
                            else ""
                        )
                        if mark["invalidation"] is not None
                        else ""
                    )
                )
            else:
                progress.append(
                    f"{mark['pnl_pct']:.1f}% max profit captured"
                    if mark["pnl_pct"] is not None
                    else "max-profit progress unavailable"
                )
            lines.append(
                f"{identity}: unrealized {_amount(mark['pnl'], '')} {unit}; "
                f"{mark['days_open'] if mark['days_open'] is not None else 'unavailable'} "
                f"days open; {'; '.join(progress)}"
            )
    else:
        lines.append("None open.")

    lines.append("## CLOSED-BY-RULE")
    if closed_nonstale:
        for item in closed_nonstale:
            pnl = _value(item.get("pnl"))
            unit = "per share" if _asset_type(item) == "swing" else "per contract"
            lines.append(
                f"Alert #{item.get('alert_id')} {_scan_type(item)} "
                f"{item.get('ticker', 'unavailable')}: "
                f"{item.get('closing_reason', 'unavailable')} · "
                f"P&L {_amount(pnl, '')} {unit}"
            )
    else:
        lines.append("No alerts closed today.")

    lines.append("## STALE")
    if stale:
        for item in stale:
            lines.append(
                f"Alert #{item.get('alert_id')} {item.get('ticker', 'unavailable')}: "
                f"P&L {_amount(item.get('pnl'), '')} per share as of last close; "
                "excluded from win rate and combined totals"
            )
    else:
        lines.append("None.")

    totals = {
        "options": {"open": 0.0, "closed": 0.0, "combined": 0.0},
        "swing": {"open": 0.0, "closed": 0.0, "combined": 0.0},
    }
    for mark in marks:
        asset = "swing" if mark["scan_type"] == "swing" else "options"
        pnl = _value(mark.get("pnl"))
        if pnl is not None:
            totals[asset]["open"] += pnl
    for item in closed_nonstale:
        pnl = _value(item.get("pnl"))
        if pnl is not None:
            totals[_asset_type(item)]["closed"] += pnl
    for asset in totals:
        totals[asset]["combined"] = totals[asset]["open"] + totals[asset]["closed"]

    lines.append("## TOTALS")
    for asset, unit in (("options", "per-contract $"), ("swing", "per-share $")):
        values = totals[asset]
        lines.append(
            f"{asset.title()} ({unit}): open {_amount(values['open'], '')} · "
            f"closed today {_amount(values['closed'], '')} · "
            f"combined {_amount(values['combined'], '')}"
        )
    winning = sum(1 for item in closed_nonstale if (_value(item.get("pnl")) or 0) > 0)
    lines.append(
        f"Win rate today: {winning}/{len(closed_nonstale)} closed-by-rule alerts"
    )
    lines.append(BASIS)
    return lines, totals, winning, len(closed_nonstale)


def _split_lines(lines: list[str], limit: int) -> list[str]:
    messages: list[str] = []
    current: list[str] = []
    for line in lines:
        proposed = "\n".join([*current, line])
        if current and len(proposed) > limit:
            messages.append("\n".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        messages.append("\n".join(current))
    return messages


def build_daily_profit_summary(
    now: datetime,
    marks: list[dict[str, Any]] | None = None,
    closed: list[dict[str, Any]] | None = None,
) -> list[str]:
    local_now = now.astimezone(ET)
    today = local_now.date()
    if marks is None:
        marks = [mark_open_alert(row, local_now) for row in tracked_alert_rows()]
    if closed is None:
        closed = closed_alert_records()
    lines, totals, _, _ = _daily_sections(marks, closed, today)
    header = (
        f"Daily profit summary — {today.isoformat()} · "
        f"{len(marks)} open alerts · options "
        f"{_amount(totals['options']['combined'], '')} per contract · swing "
        f"{_amount(totals['swing']['combined'], '')} per share"
    )
    messages = _split_lines([header, *lines], SCAN_SUMMARY_MESSAGE_LIMIT)
    return messages


def _notify_missing_webhook_once(route: str, flag: str) -> None:
    if get_flag(flag):
        return
    env_var = {
        "profit_summary": "DISCORD_WEBHOOK_PROFIT_SUMMARY",
        "performance": "DISCORD_WEBHOOK_PERFORMANCE",
    }[route]
    send_alert(
        title=f"{route.replace('_', ' ').title()} webhook missing",
        description=f"{env_var} is missing or empty; this report was skipped.",
        color=0xFFAA00,
        alert_type="health",
        alert_key=flag,
    )
    set_flag(flag, "sent")


def run_daily_profit_summary(now: datetime | None = None) -> bool:
    now = now or datetime.now(ET)
    local_now = now.astimezone(ET)
    if (
        not is_trading_day(local_now.date())
        or local_now.time() < time.fromisoformat(PROFIT_SUMMARY_TIME)
    ):
        return False
    today = local_now.date().isoformat()
    flag = f"profit-summary:{today}"
    if get_flag(flag):
        return False
    if not WEBHOOKS.get("profit_summary"):
        _notify_missing_webhook_once(
            "profit_summary", f"profit-summary-missing-webhook:{today}"
        )
        set_flag(flag, "skipped")
        return False
    messages = build_daily_profit_summary(local_now)
    success = True
    for index, message in enumerate(messages, 1):
        alert_key = f"profit-summary:{today}:{index}"
        part_flag = f"{flag}:part:{index}"
        if get_flag(part_flag):
            continue
        delivery_status = discord_alert_delivery_status(alert_key)
        if delivery_status == "delivered":
            set_flag(part_flag, "sent")
            continue
        if delivery_status == "queued":
            success = False
            continue
        sent = send_alert(
            title=f"Daily profit summary — {today}" + (
                f" ({index}/{len(messages)})" if len(messages) > 1 else ""
            ),
            description=message,
            color=0x3498DB,
            alert_type="health",
            alert_key=alert_key,
            stream="profit_summary",
        )
        delivery_status = discord_alert_delivery_status(alert_key)
        if delivery_status == "delivered" or (sent and delivery_status is None):
            set_flag(part_flag, "sent")
        elif delivery_status == "queued":
            success = False
        else:
            success = False
    if success:
        set_flag(flag, "sent")
    return success


def _date_range_records(
    records: list[dict[str, Any]],
    start: date | None,
    end: date,
) -> list[dict[str, Any]]:
    result = []
    for record in records:
        closed = local_date(record.get("close_date"))
        if closed is None or closed > end or (start is not None and closed < start):
            continue
        result.append(record)
    return result


def _period_values(
    closed: list[dict[str, Any]],
    marks: list[dict[str, Any]],
    start: date | None,
    end: date,
) -> dict[str, dict[str, float | int]]:
    period = _date_range_records(closed, start, end)
    values = {
        "options": {
            "realized": 0.0,
            "unrealized": 0.0,
            "combined": 0.0,
            "realized_unavailable": 0,
            "unrealized_unavailable": 0,
        },
        "swing": {
            "realized": 0.0,
            "unrealized": 0.0,
            "combined": 0.0,
            "realized_unavailable": 0,
            "unrealized_unavailable": 0,
        },
    }
    for record in period:
        pnl = _value(record.get("pnl"))
        asset = _asset_type(record)
        if pnl is None:
            values[asset]["realized_unavailable"] += 1
        else:
            values[asset]["realized"] += pnl
    for mark in marks:
        pnl = _value(mark.get("pnl"))
        asset = "swing" if mark["scan_type"] == "swing" else "options"
        if pnl is None:
            values[asset]["unrealized_unavailable"] += 1
        else:
            values[asset]["unrealized"] += pnl
    for asset in values:
        values[asset]["combined"] = (
            values[asset]["realized"] + values[asset]["unrealized"]
        )
    return values


def _win_rate(records: list[dict[str, Any]]) -> tuple[int, int]:
    return (
        sum(1 for item in records if (_value(item.get("pnl")) or 0) > 0),
        len(records),
    )


def _monthly_lines(
    records: list[dict[str, Any]], first_live: date | None, today: date
) -> list[str]:
    if first_live is None:
        return ["Monthly realized: no data"]
    cursor = date(first_live.year, 1, 1)
    lines = ["Monthly realized:"]
    while cursor <= today:
        month_records = []
        for record in records:
            closed = local_date(record.get("close_date"))
            if closed and closed.year == cursor.year and closed.month == cursor.month:
                month_records.append(record)
        if cursor < date(first_live.year, first_live.month, 1):
            lines.append(f"{cursor:%Y-%m}: no data")
        else:
            options = sum(
                _value(item.get("pnl")) or 0
                for item in month_records
                if _asset_type(item) == "options"
            )
            swing = sum(
                _value(item.get("pnl")) or 0
                for item in month_records
                if _asset_type(item) == "swing"
            )
            options_unavailable = sum(
                _value(item.get("pnl")) is None
                for item in month_records
                if _asset_type(item) == "options"
            )
            swing_unavailable = sum(
                _value(item.get("pnl")) is None
                for item in month_records
                if _asset_type(item) == "swing"
            )
            lines.append(
                f"{cursor:%Y-%m}: options {_amount(options, '')} per contract · "
                f"swing {_amount(swing, '')} per share"
                + (
                    f" · unavailable P&L excluded: options {options_unavailable}, "
                    f"swing {swing_unavailable}"
                    if options_unavailable or swing_unavailable
                    else ""
                )
            )
        cursor = (
            date(cursor.year + 1, 1, 1)
            if cursor.month == 12
            else date(cursor.year, cursor.month + 1, 1)
        )
    return lines


def build_weekly_performance_report(
    now: datetime,
    marks: list[dict[str, Any]] | None = None,
    closed: list[dict[str, Any]] | None = None,
    creation_dates: list[str] | None = None,
) -> list[str]:
    local_now = now.astimezone(ET)
    today = local_now.date()
    if marks is None:
        marks = [mark_open_alert(row, local_now) for row in tracked_alert_rows()]
    if closed is None:
        closed = closed_alert_records()
    if creation_dates is None:
        creation_dates = all_alert_creation_dates()
    first_live = min(
        (d for d in (local_date(value) for value in creation_dates) if d is not None),
        default=None,
    )
    year_start = date(today.year, 1, 1)
    ytd_closed = _date_range_records(closed, year_start, today)
    all_closed = _date_range_records(closed, None, today)
    ytd = _period_values(ytd_closed, marks, year_start, today)
    overall = _period_values(all_closed, marks, None, today)
    lines = [f"Weekly performance — week ending {today.isoformat()}"]
    for title, values in (("YTD", ytd), ("Overall", overall)):
        lines.append(f"## {title}")
        for asset, unit in (("options", "per-contract $"), ("swing", "per-share $")):
            item = values[asset]
            realized_unavailable = item["realized_unavailable"]
            unrealized_unavailable = item["unrealized_unavailable"]
            realized_unavailable_text = (
                f" ({realized_unavailable} unavailable excluded)"
                if realized_unavailable
                else ""
            )
            unrealized_unavailable_text = (
                f" ({unrealized_unavailable} unavailable excluded)"
                if unrealized_unavailable
                else ""
            )
            lines.append(
                f"{asset.title()} ({unit}): realized "
                f"{_amount(item['realized'], '')}{realized_unavailable_text} · "
                f"unrealized {_amount(item['unrealized'], '')}"
                f"{unrealized_unavailable_text} · "
                f"combined {_amount(item['combined'], '')}"
            )

    lines.append("## Per-scan-type")
    for _, label in SCAN_GROUPS:
        label_upper = label.upper()
        matching = [item for item in ytd_closed if _scan_type(item) == label_upper]
        wins, count = _win_rate(matching)
        pnl = sum(_value(item.get("pnl")) or 0 for item in matching)
        missing_pnl = sum(_value(item.get("pnl")) is None for item in matching)
        unit = "per share" if label_upper == "SWING" else "per contract"
        lines.append(
            f"{label}: YTD realized {_amount(pnl, '')} {unit}; "
            f"win rate {wins}/{count}"
            + (f"; {missing_pnl} unavailable P&L excluded" if missing_pnl else "")
        )
        all_matching = [item for item in all_closed if _scan_type(item) == label_upper]
        all_wins, all_count = _win_rate(all_matching)
        all_pnl = sum(_value(item.get("pnl")) or 0 for item in all_matching)
        all_missing_pnl = sum(
            _value(item.get("pnl")) is None for item in all_matching
        )
        lines.append(
            f"{label}: overall realized {_amount(all_pnl, '')} {unit}; "
            f"win rate {all_wins}/{all_count}"
            + (
                f"; {all_missing_pnl} unavailable P&L excluded"
                if all_missing_pnl
                else ""
            )
        )
    total_wins, total_count = _win_rate(ytd_closed)
    all_wins, all_count = _win_rate(all_closed)
    lines.append(f"Overall win rate: YTD {total_wins}/{total_count}; all-time {all_wins}/{all_count}")
    lines.extend(_monthly_lines(all_closed, first_live, today))
    lines.append(BASIS)
    header = lines[0]
    return _split_lines([header, *lines[1:]], SCAN_SUMMARY_MESSAGE_LIMIT)


def last_trading_day_of_week(day: date) -> date | None:
    monday = day - timedelta(days=day.weekday())
    sessions = [
        monday + timedelta(days=offset)
        for offset in range(5)
        if is_trading_day(monday + timedelta(days=offset))
    ]
    return sessions[-1] if sessions else None


def run_weekly_performance_report(now: datetime | None = None) -> bool:
    now = now or datetime.now(ET)
    local_now = now.astimezone(ET)
    report_day = last_trading_day_of_week(local_now.date())
    if (
        report_day is None
        or report_day != local_now.date()
        or local_now.time() < time.fromisoformat(PERFORMANCE_REPORT_TIME)
    ):
        return False
    week_key = report_day.isocalendar()
    flag = f"performance-report:{week_key.year}:{week_key.week}"
    if get_flag(flag):
        return False
    if not WEBHOOKS.get("performance"):
        _notify_missing_webhook_once(
            "performance",
            f"performance-missing-webhook:{week_key.year}:{week_key.week}",
        )
        set_flag(flag, "skipped")
        return False
    messages = build_weekly_performance_report(local_now)
    success = True
    for index, message in enumerate(messages, 1):
        alert_key = f"performance:{week_key.year}:{week_key.week}:{index}"
        part_flag = f"{flag}:part:{index}"
        if get_flag(part_flag):
            continue
        delivery_status = discord_alert_delivery_status(alert_key)
        if delivery_status == "delivered":
            set_flag(part_flag, "sent")
            continue
        if delivery_status == "queued":
            success = False
            continue
        sent = send_alert(
            title=f"Weekly performance — week ending {report_day.isoformat()}"
            + (f" ({index}/{len(messages)})" if len(messages) > 1 else ""),
            description=message,
            color=0x3498DB,
            alert_type="health",
            alert_key=alert_key,
            stream="performance",
        )
        delivery_status = discord_alert_delivery_status(alert_key)
        if delivery_status == "delivered" or (sent and delivery_status is None):
            set_flag(part_flag, "sent")
        elif delivery_status == "queued":
            success = False
        else:
            success = False
    if success:
        set_flag(flag, "sent")
    return success
