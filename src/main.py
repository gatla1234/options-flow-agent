from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, time as dt_time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rich import print

from src.config import (
    FLOW_POLL_SECONDS,
    REPORT_EOD_TIME,
    REPORT_MORNING_TIME,
    CSP_AFTERNOON_END_TIME,
    SCAN_SUMMARY_TIME,
)
from src.csp import scan_csps
from src.discord_alerts import (
    flush_alert_queue,
    seconds_until_alert_capacity,
    send_alert,
)
from src.followups import (
    run_csp_average_followups,
    run_flow_decision_followups,
    run_price_followups,
    run_scheduled_followups,
    run_swing_average_followups,
)
from src.closed_ledger import (
    evaluate_scheduled_eod_closures,
    flush_pending_closed_alerts,
)
from src.flow import run_flow_scan
from src.grade import grade_open_alerts
from src.market import (
    is_eod_window,
    is_rth,
    is_trading_day,
    now_et,
    options_close_time,
)
from src.market_reports import build_market_report
from src.open_board import update_open_board
from src.performance_reports import (
    run_daily_profit_summary,
    run_weekly_performance_report,
)
from src.scan_summary import run_daily_scan_summary
from src.state import get_flag, set_flag
from src.swing_signals import check_swing_tickers

MORNING_REPORT_TIME = dt_time.fromisoformat(REPORT_MORNING_TIME)
EOD_REPORT_TIME = dt_time.fromisoformat(REPORT_EOD_TIME)
SCAN_SUMMARY_RUN_TIME = dt_time.fromisoformat(SCAN_SUMMARY_TIME)
CSP_AFTERNOON_END = dt_time.fromisoformat(CSP_AFTERNOON_END_TIME)


def _health(message: str) -> None:
    send_alert(
        title="Heartbeat",
        description=message,
        color=0x808080,
        alert_type="health",
        alert_key=f"health:{now_et().date()}",
        skip_dedupe=False,
    )


def run_flow() -> None:
    run_flow_scan(send_alerts=True)


def run_swing() -> None:
    check_swing_tickers(send_alerts=True)


def run_csp() -> None:
    scan_csps(send_to_discord=True)


def run_eod() -> None:
    run_swing()
    run_swing_average_followups()
    evaluate_scheduled_eod_closures(now_et())
    grade_open_alerts()


def run_scheduled_csp(when: datetime | None = None) -> bool:
    when = when or now_et()
    if not is_trading_day(when.date()):
        return False

    today = str(when.date())
    windows = (
        ("csp_morning", dt_time(10, 15), dt_time(11, 0), "morning"),
        (
            "csp_afternoon",
            dt_time(15, 30),
            min(CSP_AFTERNOON_END, options_close_time(when.date())),
            "afternoon",
        ),
    )
    for flag, start, end, label in windows:
        if start <= when.time() < end and get_flag(flag) != today:
            print(f"Running {label} CSP scan...")
            run_csp()
            run_csp_average_followups(when)
            set_flag(flag, today)
            return True
    return False


def run_scheduled_report(report_type: str, when: datetime | None = None) -> bool:
    when = when or now_et()
    if not is_trading_day(when.date()):
        return False

    today = str(when.date())
    if report_type == "morning":
        flag = "market_report_morning"
        in_window = MORNING_REPORT_TIME <= when.time() < dt_time(9, 30)
        title = f"Pre-market macro & watchlist — {today}"
    elif report_type == "eod":
        flag = "market_report_eod"
        in_window = EOD_REPORT_TIME <= when.time() <= dt_time(16, 45)
        title = f"End-of-day market summary — {today}"
    else:
        raise ValueError("report_type must be 'morning' or 'eod'")

    if not in_window or get_flag(flag) == today:
        return False

    run_scheduled_followups(when)
    description = build_market_report(report_type, when)
    sent = send_alert(
        title=title,
        description=description,
        color=0x3498DB,
        alert_type="health",
        alert_key=f"market-report:{report_type}:{today}",
        payload={"report_type": report_type, "date": today},
        stream="reports",
    )
    if sent:
        set_flag(flag, today)
    return sent


def _sleep_with_alert_queue(seconds: float) -> None:
    flush_alert_queue()
    queue_wait = seconds_until_alert_capacity()
    time.sleep(min(seconds, queue_wait) if queue_wait is not None else seconds)


def run_once(kind: str) -> None:
    print(f"[cyan]One-shot {kind} at {now_et():%Y-%m-%d %H:%M ET}[/cyan]")
    if kind in {"all", "flow"}:
        run_flow()
    if kind in {"all", "swing"}:
        run_swing()
    if kind in {"all", "csp"}:
        run_csp()
    if kind == "grade":
        grade_open_alerts(min_age_hours=0)


def loop() -> None:
    print(f"[cyan]Loop started {now_et():%Y-%m-%d %H:%M ET}[/cyan]")
    print("Keep the Mac awake for scheduled reports and market scans (caffeinate -dimsu).")
    while True:
        now = now_et()
        today = str(now.date())
        try:
            flush_alert_queue()
            flush_pending_closed_alerts(now)
            if is_rth(now):
                if get_flag("open_ping") != today:
                    _health(f"Market open scan running · {now:%H:%M ET}")
                    set_flag("open_ping", today)
                run_scheduled_csp(now)
                run_flow()
                run_flow_decision_followups(now)
                run_price_followups(now)
                update_open_board(now)
                _sleep_with_alert_queue(FLOW_POLL_SECONDS)
            elif is_eod_window(now):
                run_scheduled_report("eod", now)
                run_scheduled_csp(now)
                if get_flag("eod_scan") != today:
                    print("Running EOD swing + CSP...")
                    run_eod()
                    set_flag("eod_scan", today)
                    _health(f"EOD swing/CSP pass finished · {now:%H:%M ET}")
                update_open_board(now)
                _sleep_with_alert_queue(60)
            elif is_trading_day(now.date()) and now.time() < dt_time(9, 0):
                update_open_board(now)
                _sleep_with_alert_queue(30)
            elif is_trading_day(now.date()) and now.time() < dt_time(9, 30):
                run_scheduled_report("morning", now)
                update_open_board(now)
                _sleep_with_alert_queue(30)
            elif is_trading_day(now.date()) and now.time() >= SCAN_SUMMARY_RUN_TIME:
                if get_flag("eod_scan") != today:
                    print("Running missed after-close swing pass before scan summary...")
                    run_eod()
                    set_flag("eod_scan", today)
                run_daily_scan_summary(now)
                run_daily_profit_summary(now)
                run_weekly_performance_report(now)
                update_open_board(now)
                _sleep_with_alert_queue(120)
            else:
                update_open_board(now)
                _sleep_with_alert_queue(120)
        except KeyboardInterrupt:
            print("Stopped.")
            return
        except Exception as e:
            print(f"[red]Loop error: {e}[/red]")
            send_alert(
                title="Scanner error",
                description=str(e),
                color=0xFFAA00,
                alert_type="health",
                alert_key=f"error:{today}:{type(e).__name__}",
            )
            _sleep_with_alert_queue(30)


def main() -> None:
    parser = argparse.ArgumentParser(description="Personal trading alerts")
    parser.add_argument(
        "--once",
        nargs="?",
        const="all",
        choices=["all", "flow", "swing", "csp", "grade"],
        help="Run a single pass (works after hours for testing)",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help=(
            "Run reports at 9:00/16:30 ET, flow in RTH, scheduled CSP, "
            "EOD swing, and the 17:00 ET scan summary"
        ),
    )
    args = parser.parse_args()
    if args.loop:
        loop()
    else:
        run_once(args.once or "all")


if __name__ == "__main__":
    main()
