# Options Flow Agent

A Python market-scanning and Discord alerting service for options flow, swing
setups, and cash-secured puts. It is an informational research tool: it does
not connect to a brokerage and never places or routes trades.

## Requirements

- Python 3.11 or newer
- ThetaData credentials for market data
- Finnhub API key for underlying quotes and company/market headlines
- Discord incoming webhooks for the channels you want to use

The scanner uses ThetaData for option chains, option trade data, Greeks,
open interest, and daily bars. Finnhub is used for underlying quotes and news.
If Finnhub is unavailable, the scanner may use ThetaData snapshots; during
regular trading hours, quotes must be current-session and no more than five
minutes old. Stale or untimestamped intraday quotes are rejected.

## Install and configure

From the repository root, create and activate a virtual environment, then
install the project dependencies:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Copy `.env.example` to `.env` and set your own credentials and webhook URLs:

```sh
cp .env.example .env
```

Required variable names:

```dotenv
THETADATA_API_KEY=
FINNHUB_API_KEY=
DISCORD_WEBHOOK_CONFIRMED=
DISCORD_WEBHOOK_DAYTRADE=
DISCORD_WEBHOOK_SWING=
DISCORD_WEBHOOK_CSP=
DISCORD_WEBHOOK_HEALTH=
DISCORD_WEBHOOK_REPORTS=
DISCORD_WEBHOOK_SCAN_SUMMARY=
DISCORD_WEBHOOK_CLOSED_LEDGER=
DISCORD_WEBHOOK_OPEN_LEDGER=
DISCORD_WEBHOOK_PROFIT_SUMMARY=
DISCORD_WEBHOOK_PERFORMANCE=
```

Each webhook is routed only to its matching channel. Missing webhooks do not
fall back to another channel. Keep `.env` private; it is ignored by Git.
`.env.example` contains placeholders only.

## Run

Run a single scan pass:

```sh
python -m src.main --once
```

Choose a single scanner or grading pass:

```sh
python -m src.main --once flow
python -m src.main --once swing
python -m src.main --once csp
python -m src.main --once grade
```

Start the application (continuous scheduler):

```sh
cd /Users/vineelamuddana/options-flow-agent
caffeinate -dimsu venv/bin/python run.py --loop
```

Run these two lines from any directory. The first changes into the project
root; the second uses the project virtualenv, keeps the Mac awake with
`caffeinate -dimsu`, and starts the loop. Equivalent command with the virtual
environment already activated:

```sh
python -m src.main --loop
```

The continuous loop uses America/New_York time. On trading days it polls the
flow streams every 45 seconds during regular market hours, runs CSP scans once
in each 10:15–11:00 a.m. and 3:30–4:15 p.m. window, sends the pre-market report
at 9:00 a.m. and end-of-day report at 4:30 p.m., and runs the swing scan and
grades alerts at least 20 hours old after the close. At 5:00 p.m. it posts the
daily per-ticker flow, swing, and CSP funnel/alert summary to the dedicated
scan-summary channel. It also checks original-alert price follow-ups during
market hours and news/expiration follow-ups in report windows. Weekends and
configured market holidays are skipped. Scheduled early closes end the
underlying session at 1:00 p.m. ET and the options board at 1:15 p.m.; the
afternoon CSP window is skipped if it would begin after that close. The
explicit holiday and early-close calendar in `src/market.py` is maintained
through 2027 and must be updated before each subsequent calendar year. Keep the
process running and the computer awake for scheduled alerts; the start command
above already handles the `caffeinate` part, so leave that terminal open while
the loop is active.

The open-alert board updates every 15 minutes until 4:15 p.m. ET (1:15 p.m.
on configured early-close days). The daily
profit summary runs at 5:15 p.m.; the weekly performance report runs Friday at
5:30 p.m. ET, or on that week's last trading day when Friday is a holiday.

The loop stores alert history, follow-up events, delivery queue entries,
delivery attempts, and schedule flags in `data/alerts.db`. That database is
local runtime state and is ignored by Git.

## Alert behavior

- **CONFIRMED** and **DAY-TRADE** options-flow alerts are separate streams and
  retain their stream labels. Each stream has its own channel and cooldown;
  cross-stream contract/direction deduplication is also applied.
- Swing and CSP alerts include underlying reference levels. CSP alerts explain
  assignment exposure and display estimated maximum loss at expiration.
- Signal alerts and follow-ups include a suggestion aligned with the signal's
  direction. Follow-ups reference the original alert and use its recorded alert
  price as entry for flow and swing; CSP follow-ups preserve the original
  underlying price and put details. Flow follow-ups can reverse, exit, or
  suggest equal-size averaging; swing and CSP follow-ups can suggest averaging
  when their configured guards pass.
- Discord delivery is rate-limited. Excess messages are persisted in a queue
  and retried; delivery failures are logged without printing webhook URLs.
- The dedicated closed-ledger channel records one immutable hypothetical
  closure per alert when a configured closing rule is reached. Option
  expiration status is inferred mechanically from the expiration-day close,
  not verified against brokerage exercise or assignment records. Configure
  `DISCORD_WEBHOOK_CLOSED_LEDGER`; a missing webhook is reported to system
  health and never falls back to another channel.
- The open-alert board edits its dedicated `open-ledger` webhook message every
  15 minutes from 9:30 a.m. through 4:15 p.m. ET and refreshes once after the
  close; its message IDs are stored in SQLite. Daily profit summaries run at
  5:15 p.m. ET, and weekly performance reports run Fridays at 5:30 p.m. ET (or
  the last trading day of a holiday-shortened week). Configure
  `DISCORD_WEBHOOK_OPEN_LEDGER`, `DISCORD_WEBHOOK_PROFIT_SUMMARY`, and
  `DISCORD_WEBHOOK_PERFORMANCE` for their separate channels.

## Configuration and limitations

Watchlists, scan thresholds, cooldowns, report times, and other operational
settings are centralized in `src/config.py`. The scanner depends on the
availability, freshness, and accuracy of third-party market data. Option
prints, Greeks, open interest, and quotes may be delayed, incomplete, or
unavailable; missing decision-critical data may prevent an alert. Reports do
not infer options activity or implied-volatility changes when configured data
cannot verify them. Suggestions follow the detected signal and the displayed
prices, risk figures, and targets are estimates.

## Tests

Run the mock-based test suite without live API keys or Discord delivery:

```sh
python -m unittest discover -s tests -v
```
