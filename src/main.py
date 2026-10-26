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

from src.config import FLOW_POLL_SECONDS, REPORT_EOD_TIME, REPORT_MORNING_TIME
from src.csp import scan_csps
from src.discord_alerts import send_alert
from src.followups import run_price_followups, run_scheduled_followups
from src.flow import run_flow_scan
from src.grade import grade_open_alerts
from src.market import is_eod_window, is_rth, is_trading_day, now_et
from src.market_reports import build_market_report
from src.state import get_flag, set_flag
from src.swing_signals import check_swing_tickers

MORNING_REPORT_TIME = dt_time.fromisoformat(REPORT_MORNING_TIME)
EOD_REPORT_TIME = dt_time.fromisoformat(REPORT_EOD_TIME)


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
    grade_open_alerts()


def run_scheduled_csp(when: datetime | None = None) -> bool:
    when = when or now_et()
    if not is_trading_day(when.date()):
        return False

    today = str(when.date())
    windows = (
        ("csp_morning", dt_time(10, 15), dt_time(11, 0), "morning"),
        ("csp_afternoon", dt_time(15, 30), dt_time(16, 0), "afternoon"),
    )
    for flag, start, end, label in windows:
        if start <= when.time() < end and get_flag(flag) != today:
            print(f"Running {label} CSP scan...")
            run_csp()
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
    )
    if sent:
        set_flag(flag, today)
    return sent


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
            if is_rth(now):
                if get_flag("open_ping") != today:
                    _health(f"Market open scan running · {now:%H:%M ET}")
                    set_flag("open_ping", today)
                run_scheduled_csp(now)
                run_flow()
                run_price_followups(now)
                time.sleep(FLOW_POLL_SECONDS)
            elif is_eod_window(now):
                run_scheduled_report("eod", now)
                if get_flag("eod_scan") != today:
                    print("Running EOD swing + CSP...")
                    run_eod()
                    set_flag("eod_scan", today)
                    _health(f"EOD swing/CSP pass finished · {now:%H:%M ET}")
                time.sleep(60)
            elif is_trading_day(now.date()) and now.time() < dt_time(9, 0):
                time.sleep(30)
            elif is_trading_day(now.date()) and now.time() < dt_time(9, 30):
                run_scheduled_report("morning", now)
                time.sleep(30)
            else:
                time.sleep(120)
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
            time.sleep(30)


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
        help="Run reports at 9:00/16:30 ET, flow in RTH, scheduled CSP, and EOD swing",
    )
    args = parser.parse_args()
    if args.loop:
        loop()
    else:
        run_once(args.once or "all")


if __name__ == "__main__":
    main()
