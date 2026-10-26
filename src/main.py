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

from src.config import FLOW_POLL_SECONDS
from src.csp import scan_csps
from src.discord_alerts import send_alert
from src.flow import run_flow_scan
from src.grade import grade_open_alerts
from src.market import is_eod_window, is_rth, is_trading_day, now_et
from src.state import get_flag, set_flag
from src.swing_signals import check_swing_tickers


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
    print("Keep the Mac awake during RTH (caffeinate -dimsu) or this process dies on sleep.")
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
                time.sleep(FLOW_POLL_SECONDS)
            elif is_eod_window(now):
                if get_flag("eod_scan") != today:
                    print("Running EOD swing + CSP...")
                    run_eod()
                    set_flag("eod_scan", today)
                    _health(f"EOD swing/CSP pass finished · {now:%H:%M ET}")
                time.sleep(60)
            else:
                if is_trading_day(now.date()) and now.hour < 9:
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
        help="Run forever: flow in RTH, swing+CSP after the close",
    )
    args = parser.parse_args()
    if args.loop:
        loop()
    else:
        run_once(args.once or "all")


if __name__ == "__main__":
    main()
