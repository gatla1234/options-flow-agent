# Project Build Prompt

Use this prompt when asking an AI coding agent to continue building this
repository. Keep it aligned with the current implementation: update the
“Current project baseline” and “Project update log” whenever project behavior,
configuration, dependencies, or architecture materially changes.

## Reusable prompt

```text
You are continuing work on this existing Python repository. First inspect its
source, tests, configuration, and Git status. Treat the repository as the
source of truth; do not replace working code with a fresh implementation.
Preserve unrelated user changes, avoid committing secrets, and make precise,
tested changes for the requested task. Do not assume that the current prompt
below supersedes newer source code or user requirements.

PROJECT PURPOSE AND SAFETY
- Maintain a personal market-scanning and alerting system for options flow,
  swing signals, and cash-secured puts (CSPs).
- This is an informational alerting tool, not a brokerage or automated-trading
  system. Never place, route, or simulate live orders. Do not represent alerts
  or calculated levels as guaranteed, personalized financial advice.
- Clearly label prices, invalidations, and targets as market-data-based
  references, not execution instructions. CSP messaging must explain assignment
  risk and potential substantial losses.
- Never hard-code API keys, webhook URLs, or other credentials. Read secrets
  from environment variables or a local .env file; keep secret files out of Git.
- Fail closed when a required, decision-critical price or data source is
  missing, stale, malformed, or inconsistent. Do not generate a qualified
  alert from a success-shaped fallback.

TECHNOLOGY AND EXTERNAL SERVICES
- Use Python with type annotations, Polars, python-dotenv, requests, Rich, and
  SQLite, following the existing project patterns and requirements.
- ThetaData supplies historical stock data, option expirations/chains, Greeks,
  open interest, OHLC, and option trade/quote history.
- Finnhub may provide underlying quotes. During regular trading hours, accept
  only a quote from the current America/New_York date whose age is no more than
  300 seconds. ThetaData current-day snapshots may be used as fallback.
  Never substitute a prior-session close for a required intraday price.
- Discord webhooks deliver alerts. Support DISCORD_WEBHOOK_URL and optional
  DISCORD_WEBHOOK_FLOW, DISCORD_WEBHOOK_SWING, DISCORD_WEBHOOK_CSP, and
  DISCORD_WEBHOOK_HEALTH. Never print webhook URLs or API keys.
- Required environment variables: THETADATA_API_KEY, FINNHUB_API_KEY, and
  DISCORD_WEBHOOK_URL. Store placeholders only in .env.example.

WATCHLISTS AND BENCHMARKS
- Flow: SPY, QQQ, IWM, SMH, IBIT, GLD.
- Swing: TQQQ, TNA, SOXL, BITX.
- CSP: BITX, SOXL, TQQQ.
- Relative-strength benchmarks: SOXL→SMH, TQQQ→QQQ, BITX→IBIT, TNA→IWM.
- Keep watchlists and thresholds centralized in configuration rather than
  duplicating them across scanners.

OPTIONS-FLOW SCANNER
- Scan flow during regular trading hours on the configured 45-second interval.
- Consider 7–21 DTE expirations, up to two expirations.
- Filter contracts to absolute delta 0.40–0.55, strikes within 12% of the
  underlying, volume of at least 200, open interest of at least 50, volume/OI
  of at least 1.5, and bid/ask spread of at most 12%.
- Classify option prints using their trade price relative to bid and ask:
  at/above ask is buy-side, at/below bid is sell-side, and between them is
  mid-market/unclassified. Calculate premium as price × contracts × 100.
- Require at least $30,000 aggressive-buy premium and at least 60% buy premium
  for a BUY classification. Snapshot volume × midpoint is only an estimate and
  must never substitute for qualifying tape evidence.
- Require technical confluence and an underlying level confirmation. Calls
  require the underlying to break above prior-day or 20-day resistance; puts
  require a break below prior-day or 20-day support. Do not qualify a setup
  without the necessary technical context or confirmation.
- Include the contract, expiration/DTE, delta, bid/ask/midpoint, volume, OI,
  volume/OI, OTM percentage, qualifying tape premium, technical notes,
  underlying invalidation, and 2R underlying reference target in alerts.
- Rank candidates using technical alignment and qualifying buy premium; cap
  flow alerts at four per scan and deduplicate repeats.

SWING SCANNER
- Use recent daily OHLCV history (configured lookback: 80 days) to calculate
  9-day and 21-day EMAs, prior-day high/low/close, 20-day high/low, and
  20-day average volume excluding the current bar.
- Calculate five-day relative strength against the configured benchmark where
  data is available.
- Detect strong uptrends, support bounces, strong downtrends, and breakdowns
  using EMA structure, volume, relative strength, and nearby levels. The
  configured strong/decent volume thresholds are 1.5×/1.2×.
- Only return signals with a valid invalidation on the correct side of price
  and positive risk distance. Report a 2R underlying level only as a reference
  for reassessment, not a guaranteed take-profit.
- Include direction, live/reference price, trend, EMAs, volume, relative
  strength, nearby levels, invalidation, and target in deduplicated alerts.

CASH-SECURED-PUT SCANNER
- Require usable technical context and an acceptable-to-own-shares condition
  before evaluating CSPs. Skip the symbol if that context is unavailable.
- Consider expirations 5–45 DTE, up to four expirations.
- Consider puts below the underlying and 0–12% OTM, with premium at least
  0.4% of strike, spread no greater than 10%, and delta from -0.35 to -0.15
  when delta is available. Follow existing code behavior for missing Greeks;
  do not invent a delta value.
- Evaluate support at the 21-day EMA, prior low, and 20-day low; prefer strikes
  within 3% of support.
- Calculate premium, premium percentage, annualized yield, cash required,
  maximum profit, maximum loss at expiration, and reward-to-risk. Rank using
  annualized yield, a 1.25× nearby-support preference, and a 1.15× uptrend
  preference.
- Return at most five candidates per symbol and alert on at most the top three
  overall. Include expiration/DTE, premium, return figures, cash requirement,
  OTM percentage, delta/IV when available, support, trend/ownership filter,
  bid/ask, and invalidation.

ALERTS, STATE, AND RUNTIME
- Persist sent alerts, payloads, prices, timestamps, outcomes, grading times,
  and scheduler flags in SQLite. Use parameterized SQL and safe schema/index
  initialization.
- Cooldowns: flow 4 hours, swing 20 hours, CSP 20 hours, health 12 hours.
  Enforce the configured Discord rate limit of eight alerts per minute.
- Persist an alert only after Discord confirms successful delivery. Surface
  HTTP failures and request exceptions; do not report success on failure.
- Use America/New_York time. Run CSP once in each configured 10:15–11:00 and
  15:30–16:00 trading-day window. During regular trading hours, poll flow.
  In the end-of-day window, run the daily swing scan and grade eligible alerts
  once per trading day. Send market-open and end-of-day health heartbeats.
- Respect weekends and the maintained market holiday calendar. Document its
  maintenance expectations and test the relevant dates and scheduling windows.
- Preserve the existing CLI behavior for one-shot all/flow/swing/CSP/grade
  scans and continuous loop mode.
- Grade alerts after the configured minimum age (20 hours by default) using
  underlying price movement and alert direction. Handle missing prices/symbols
  and invalid stored payloads without corrupting state.

ENGINEERING AND DELIVERY
- Keep responsibilities in the existing modules for configuration, market and
  options data, technical signals, flow, CSP, Discord, SQLite state, grading,
  and runtime scheduling. Reuse existing helpers and avoid unnecessary
  abstractions or dependencies.
- Keep provider calls behind testable functions; use HTTP timeouts and useful
  non-secret diagnostics.
- Add or update documentation for setup, environment variables, installation,
  one-shot and loop usage, scheduling, data limitations, and no-trading scope
  when those subjects change.
- Add/update mock-based tests; tests must not require live credentials, market
  data, Discord, or brokerage access.
- At minimum, test tape classification and premium thresholds; flow technical,
  delta, and contract filters; fresh/stale quote behavior and intraday
  fail-closed behavior; swing signal/invalidation/2R calculations; CSP filters
  and expiration risk calculations; scheduler windows/once-per-day behavior;
  and alert deduplication, delivery failure, and SQLite persistence.
- Run the narrowest relevant tests, then the full test suite when appropriate.
  Report exactly what ran and whether it passed. Do not claim unrun validation.
- Review Git status and diffs before editing and before committing. Never
  discard unrelated user work. Stage and commit only the changes the user
  authorized; do not push unrelated or unreviewed work. If a push is requested,
  verify the target branch and remote, report the commit being pushed, and
  confirm the push result.

When completing a project change, update the baseline and append an entry to
the “Project update log” below with the date, a concise summary, relevant
files/behavior, validation run, and commit/push reference if applicable. Keep
historical entries; do not rewrite them to imply that old validation applies
to new code.
```

## Current project baseline

- Project: personal Python options-flow, swing, and CSP alert scanner.
- Main implementation: `src/`; CLI/runtime entry points: `run.py` and
  `src/main.py`.
- Providers and outputs: ThetaData, optional Finnhub quotes, Discord webhooks.
- Persistent state: SQLite database under `data/`.
- Tests: `unittest` tests under `tests/`, including flow, data freshness,
  invalidation, risk/reward, and scheduling coverage.
- Core watchlists and strategy thresholds are configured in `src/config.py`.
- Runtime timezone: America/New_York.
- This file is a maintained prompt and project log, not a specification that
  overrides actual code, tests, or newer user decisions.

## Project update log

| Date | Update | Validation | Git reference |
| --- | --- | --- | --- |
| 2026-10-26 | Created this reusable prompt and baseline; captured current updates for fresh quote handling, confirmed flow setups, risk/reward references, CSP scheduling, and test coverage. | `venv/bin/python -m unittest discover -s tests -v` (28 tests passed); `venv/bin/python -m compileall -q src tests`; `git diff --check`. | Commit/push pending |
