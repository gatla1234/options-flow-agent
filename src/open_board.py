from __future__ import annotations

from datetime import datetime, time, timezone
from typing import Any

from rich import print

from src.config import OPEN_BOARD_MESSAGE_LIMIT, OPEN_BOARD_UPDATE_MINUTES
from src.discord_alerts import (
    WEBHOOKS,
    delete_webhook_message,
    edit_or_create_webhook_message,
    send_alert,
)
from src.market import ET, is_trading_day, options_close_time
from src.performance_data import SCAN_GROUPS, mark_open_alert
from src.state import (
    get_flag,
    remove_tracker_message_id,
    set_flag,
    set_tracker_message_id,
    tracked_alert_rows,
    tracker_message_ids,
)
from src.util import as_float

BASIS = (
    "Basis: alert price = entry, 1 contract / 1 share, mid-to-mid, before "
    "commissions and spreads. This tracks alert performance, not your brokerage account."
)


def _money(value: Any, suffix: str = "") -> str:
    if value is None:
        return "unavailable"
    number = as_float(value)
    return f"{'-' if number < 0 else '+'}${abs(number):,.2f}{suffix}"


def _price(value: Any) -> str:
    return f"${as_float(value):.2f}" if value is not None else "price unavailable"


def _percent(value: Any) -> str:
    return f"{as_float(value) * 100:.0f}%" if value is not None else "unavailable"


def _created_sort_key(mark: dict[str, Any]) -> tuple[datetime, int]:
    try:
        created = datetime.fromisoformat(str(mark.get("created_at")))
    except (TypeError, ValueError):
        created = datetime.max.replace(tzinfo=timezone.utc)
    if created.tzinfo is None:
        created = created.replace(tzinfo=ET)
    return created.astimezone(timezone.utc), int(mark["id"])


def _alert_line(mark: dict[str, Any]) -> str:
    alert_id = mark["id"]
    symbol = mark["symbol"]
    direction = mark["direction"]
    days_open = (
        f"{mark['days_open']} days"
        if mark["days_open"] is not None
        else "days open unavailable"
    )
    pnl = _money(mark["pnl"])
    pnl_pct = (
        f"{mark['pnl_pct']:+.1f}%"
        if mark["pnl_pct"] is not None
        else "unavailable"
    )
    target_text = (
        f"${mark['target']:.2f} ({_percent(mark['target_progress'])} progress)"
        if mark["target"] is not None
        else "unavailable"
    )
    invalidation_text = (
        f"${mark['invalidation']:.2f} ({mark['invalidation_status']})"
        if mark["invalidation"] is not None
        else "unavailable"
    )
    flags = []
    if (
        mark["invalidation_progress"] is not None
        and mark["invalidation_progress"] >= 0.75
    ):
        flags.append("⚠ NEAR INVALIDATION")
    if mark["target_progress"] is not None and mark["target_progress"] >= 0.75:
        flags.append("🎯 NEAR 2R")
    flag_text = f" · {' · '.join(flags)}" if flags else ""

    if mark["scan_type"] == "flow":
        payload = mark["payload"]
        strike = mark["strike"]
        right = str(payload.get("right") or "option").upper()
        contract = (
            f"${strike:g}{right[0]} exp {payload.get('expiration', 'unavailable')}"
            if strike is not None and right
            else "contract unavailable"
        )
        option_text = (
            f"{_price(mark['entry_contract'])} → {_price(mark['current_contract'])}"
        )
        underlying_text = (
            f"{_price(mark['entry_underlying'])} → "
            f"{_price(mark['current_underlying'])}"
        )
        return (
            f"Alert #{alert_id} {symbol} {direction} {contract} · option "
            f"{option_text} · P&L {_money(mark['pnl'])} ({pnl_pct}) · "
            f"underlying {underlying_text} · 2R {target_text} · "
            f"invalidation {invalidation_text} · {days_open}{flag_text}"
        )
    if mark["scan_type"] == "swing":
        entry = mark["entry_underlying"]
        current = mark["current_underlying"]
        return (
            f"Alert #{alert_id} {symbol} {direction} · close "
            f"{_price(entry)} → {_price(current)} · P&L/share {_money(mark['pnl'])} "
            f"({pnl_pct}) · 2R {target_text} · invalidation "
            f"{invalidation_text} · {days_open}{flag_text}"
        )

    premium_entry = mark["entry_contract"]
    premium_current = mark["current_contract"]
    captured = mark["pnl_pct"]
    underlying = mark["current_underlying"]
    if underlying is None:
        strike_status = "price unavailable"
    elif mark["strike"] is None:
        strike_status = "strike unavailable"
    else:
        strike_status = (
            f"${underlying:.2f} {'above' if underlying > mark['strike'] else 'at/below'} "
            f"${mark['strike']:.2f} strike"
        )
    csp_header = (
        f"Alert #{alert_id} {symbol} "
        f"${mark['strike']:g}" if mark["strike"] is not None else
        f"Alert #{alert_id} {symbol} strike unavailable"
    )
    premium_text = (
        f"{csp_header} put exp "
        f"{mark['expiration'] or 'unavailable'} · premium "
        f"{_price(premium_entry)} → {_price(premium_current)} · "
        f"P&L {_money(mark['pnl'])} · max profit captured "
        f"{captured:.1f}% / ${mark['max_profit']:,.2f}"
        if captured is not None and mark["max_profit"] is not None
        else f"{csp_header} put exp "
        f"{mark['expiration'] or 'unavailable'} · premium "
        f"{_price(premium_entry)} → {_price(premium_current)} · "
        f"P&L {pnl} · max profit captured unavailable"
    )
    return premium_text + (
        f" · underlying {strike_status} · cash required "
        f"{_money(mark['cash_required'])} · {days_open}{flag_text}"
    )


def render_open_board(
    marks: list[dict[str, Any]],
    updated_at: datetime,
    message_limit: int = OPEN_BOARD_MESSAGE_LIMIT,
    market_closed: bool = False,
) -> dict[str, str]:
    local_now = updated_at.astimezone(ET)
    options_unavailable = sum(
        mark["pnl"] is None
        for mark in marks
        if mark["scan_type"] in {"flow", "csp"}
    )
    swing_unavailable = sum(
        mark["pnl"] is None for mark in marks if mark["scan_type"] == "swing"
    )
    options_suffix = (
        f" ({options_unavailable} unavailable excluded)"
        if options_unavailable
        else ""
    )
    swing_suffix = (
        f" ({swing_unavailable} unavailable excluded)"
        if swing_unavailable
        else ""
    )
    total_options = sum(
        mark["pnl"] or 0
        for mark in marks
        if mark["scan_type"] in {"flow", "csp"}
    )
    total_swing = sum(
        mark["pnl"] or 0 for mark in marks if mark["scan_type"] == "swing"
    )
    header = (
        f"Last updated: {local_now:%Y-%m-%d %H:%M ET} · "
        f"{len(marks)} open alerts · options total: {_money(total_options)} "
        f"per contract{options_suffix} · swing total: "
        f"{_money(total_swing)} per share{swing_suffix}"
    )
    footer = BASIS
    if market_closed:
        footer += f"\nMarket closed — last update {local_now:%H:%M ET}"
    if not marks:
        return {
            "all": (
                f"{header}\nNo open alerts — all alerts are closed. "
                f"See closed-ledger for history.\n{footer}"
            )
        }

    groups: dict[str, list[str]] = {key: [] for key, _ in SCAN_GROUPS}
    for mark in sorted(marks, key=_created_sort_key):
        groups.setdefault(mark["group"], []).append(_alert_line(mark))
    sections = [
        f"## {label}\n" + ("\n".join(groups[key]) if groups[key] else "None open.")
        for key, label in SCAN_GROUPS
    ]
    complete = f"{header}\n" + "\n\n".join(sections) + f"\n{footer}"
    if len(complete) <= message_limit:
        return {"all": complete}

    # Keep each scan-type block intact in its own edited webhook message.
    rendered: dict[str, str] = {}
    for (key, label), section in zip(SCAN_GROUPS, sections):
        body = f"{header}\n{section}\n{footer}"
        if len(body) <= message_limit:
            rendered[key] = body
            continue
        lines = section.splitlines()
        chunks: list[list[str]] = []
        current: list[str] = []
        for line in lines[1:]:
            proposed = (
                f"{header}\n## {label} (999/999)\n"
                + "\n".join([*current, line])
                + f"\n{footer}"
            )
            if current and len(proposed) > message_limit:
                chunks.append(current)
                current = [line]
            else:
                current.append(line)
        if current:
            chunks.append(current)
        for index, chunk in enumerate(chunks, 1):
            part_key = key if len(chunks) == 1 else f"{key}:{index}"
            rendered[part_key] = (
                f"{header}\n## {label} ({index}/{len(chunks)})\n"
                + "\n".join(chunk)
                + f"\n{footer}"
            )
    return rendered


def _missing_webhook_once(now: datetime) -> None:
    day = now.astimezone(ET).date().isoformat()
    flag = f"open-ledger-missing-webhook:{day}"
    if get_flag(flag):
        return
    send_alert(
        title="Open ledger webhook missing",
        description=(
            "DISCORD_WEBHOOK_OPEN_LEDGER is missing or empty; "
            "the open-alert tracker was skipped."
        ),
        color=0xFFAA00,
        alert_type="health",
        alert_key=flag,
    )
    set_flag(flag, "sent")


def update_open_board(now: datetime | None = None) -> bool:
    now = now or datetime.now(ET)
    local_now = now.astimezone(ET)
    if not WEBHOOKS.get("open_ledger"):
        _missing_webhook_once(local_now)
        return False
    if not is_trading_day(local_now.date()):
        return False

    board_close = options_close_time(local_now.date())
    market_open = time(9, 30) <= local_now.time() < board_close
    after_close = local_now.time() >= board_close
    if not market_open:
        if not after_close and tracker_message_ids():
            return False
        if get_flag("open_board_close_update") == local_now.date().isoformat():
            return False
        market_closed = True
    else:
        market_closed = False
        last_update = get_flag("open_board_last_update")
        if last_update:
            try:
                last_time = datetime.fromisoformat(last_update)
            except ValueError:
                print("[yellow]Ignoring invalid open-board update timestamp[/yellow]")
            else:
                elapsed = (local_now - last_time.astimezone(ET)).total_seconds()
                if last_time.astimezone(ET).date() == local_now.date() and elapsed < OPEN_BOARD_UPDATE_MINUTES * 60:
                    return False

    marks = [mark_open_alert(row, local_now) for row in tracked_alert_rows()]
    rendered = render_open_board(marks, local_now, market_closed=market_closed)
    existing = tracker_message_ids()
    wanted_keys = {f"open_board:{key}" for key in rendered}
    success = True
    for key, description in rendered.items():
        storage_key = f"open_board:{key}"
        old_id = existing.get(storage_key)
        title = "Open alert tracker" if key == "all" else f"Open alert tracker — {key}"
        message_id, sent = edit_or_create_webhook_message(
            "open_ledger", title, description, 0x5865F2, old_id
        )
        if not sent or message_id is None:
            success = False
            continue
        set_tracker_message_id(storage_key, message_id)
    if success:
        for storage_key, message_id in existing.items():
            if storage_key.startswith("open_board:") and storage_key not in wanted_keys:
                if delete_webhook_message("open_ledger", message_id):
                    remove_tracker_message_id(storage_key)
        set_flag("open_board_last_update", local_now.isoformat())
        if market_closed and after_close:
            set_flag("open_board_close_update", local_now.date().isoformat())
    return success
