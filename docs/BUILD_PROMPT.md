# Build Prompt

Use the prompt below to build this kind of project from scratch or give it to a
developer as a project brief. For a similar project, change the watchlists,
providers, thresholds, and scheduling details to fit the new requirements.

```text
Build a complete Python market-scanning and alerting system for options flow,
swing-trade signals, and cash-secured puts (CSPs).

If working in an existing repository, inspect its code, tests, configuration,
and Git status first. Preserve useful implementation and unrelated user work.
If starting from scratch, create a clear, testable project structure. In either
case, implement a working system, document setup and use, and run the tests.
Do not stop after presenting a plan.

PURPOSE AND SAFETY
- This is a research and alerting tool, not a brokerage or trading bot. Never
  place or route orders.
- Describe alerts as informational, not financial advice. Label prices,
  invalidations, and targets as reference levels, not execution instructions.
  Explain assignment risk and potential losses for CSPs.
- Never hard-code or expose API keys or webhook URLs. Load secrets from
  environment variables or a local .env file; provide placeholders only in
  .env.example and ensure secret files are ignored by Git.
- Fail closed when decision-critical data is missing, stale, or invalid.

TECHNOLOGY AND INTEGRATIONS
- Use Python with type hints, Polars, python-dotenv, requests, Rich, and SQLite.
- Use ThetaData for stock history, option expirations/chains, Greeks, open
  interest, OHLC, and option trade/quote history.
- Use Finnhub for underlying quotes when configured. During regular trading
  hours, require a quote from the current America/New_York date and no older
  than 300 seconds. A current-day ThetaData snapshot may be a fallback; never
  substitute a prior-session close for an intraday price.
- Send alerts with Discord webhooks. Support DISCORD_WEBHOOK_URL and optional
  DISCORD_WEBHOOK_FLOW, DISCORD_WEBHOOK_SWING, DISCORD_WEBHOOK_CSP, and
  DISCORD_WEBHOOK_HEALTH.
- Read THETADATA_API_KEY, FINNHUB_API_KEY, and Discord webhook values from the
  environment. Give API requests timeouts and useful errors without logging
  secrets.

DEFAULT WATCHLISTS
- Flow: SPY, QQQ, IWM, SMH, IBIT, GLD.
- Swing: TQQQ, TNA, SOXL, BITX.
- CSP: BITX, SOXL, TQQQ.
- Relative-strength benchmarks: SOXL vs SMH, TQQQ vs QQQ, BITX vs IBIT,
  TNA vs IWM.
- Keep symbols and thresholds centralized and configurable.

OPTIONS-FLOW SCANNER
- Scan during regular trading hours every 45 seconds.
- Consider 7–21 DTE, up to two expirations; absolute delta 0.40–0.55; strikes
  within 12% of the underlying; volume at least 200; open interest at least 50;
  volume/OI at least 1.5; spread no greater than 12%.
- Classify a print at/above ask as a buy, at/below bid as a sell, and a print
  between bid and ask as unclassified. Premium is price × contracts × 100.
- Require at least $30,000 aggressive-buy premium and at least 60% buy premium.
  Snapshot volume × midpoint is not a substitute for tape evidence.
- Require aligned technical context and an underlying level confirmation:
  calls above prior-day or 20-day resistance; puts below prior-day or 20-day
  support. Reject candidates when required data or confirmation is absent.
- Include contract, expiration/DTE, delta, bid/ask/midpoint, volume, OI,
  volume/OI, OTM percentage, tape premium, technical notes, invalidation, and
  a 2R underlying reference target. Rank qualifying candidates and cap alerts
  at four per scan.

SWING SCANNER
- Use about 80 days of daily OHLCV. Calculate 9- and 21-day EMAs, prior-day
  high/low/close, 20-day high/low, and 20-day average volume excluding the
  current bar.
- Calculate five-day relative strength against the configured benchmark when
  data is available.
- Detect strong uptrends, support bounces, strong downtrends, and breakdowns
  using EMA structure, volume, relative strength, and nearby levels. Default
  strong/decent volume thresholds: 1.5×/1.2×.
- Require a valid invalidation on the correct side of price and positive risk.
  Include signal direction, price, trend, EMAs, volume, relative strength,
  nearby levels, invalidation, and a 2R reference target.

CASH-SECURED-PUT SCANNER
- Only scan when technical context is available and the underlying passes an
  acceptable-to-own-shares filter.
- Consider 5–45 DTE, up to four expirations. Filter puts to strikes below the
  underlying, up to 12% OTM, premium at least 0.4% of strike, spread at most
  10%, and delta -0.35 to -0.15 when available.
- Evaluate support at the 21-day EMA, prior low, and 20-day low; prefer strikes
  within 3% of support.
- Calculate premium, annualized yield, cash required, maximum profit, maximum
  loss at expiration, and reward-to-risk. Prefer nearby support (1.25×) and an
  uptrend (1.15×) when ranking.
- Return up to five candidates per symbol and alert on the top three overall.
  Include expiration/DTE, premium, yield, cash requirement, OTM percentage,
  delta/IV when available, support, bid/ask, trend, and invalidation.

ALERTS AND SCHEDULING
- Persist successfully sent alerts, payloads, prices, timestamps, grading
  outcomes, and scheduler flags in SQLite using parameterized SQL.
- Use alert cooldowns of 4 hours for flow, 20 hours for swing and CSP, and
  12 hours for health. Limit Discord alerts to eight per minute.
- Persist an alert only after Discord confirms success; surface delivery
  failures explicitly.
- Use America/New_York time. During regular hours, poll flow and run CSP scans
  once in each 10:15–11:00 and 15:30–16:00 window. After the close, run the
  swing scan and grade eligible alerts once per trading day.
- Deliver a concise, three-section pre-market report at 9:00 a.m. ET and a
  similar end-of-day report at 4:30 p.m. ET. Include source-backed market
  headlines and a four-index price/change table. Reject stale or wrong-session
  prices instead of showing the previous close as pre-market data.
- Keep pre-market option-flow and IV claims conservative: if the configured
  feeds cannot verify current-session options trades or IV, state that clearly
  and do not infer an IV spike from an underlying gap.
- Monitor recent original flow, swing, and CSP alerts without assuming the user
  entered or still holds a position. During regular hours, check fresh
  underlying prices at most every five minutes and send a once-per-event
  follow-up when the original invalidation/2R reference or CSP strike is
  reached. Also check for new company headlines and approaching contract
  expirations in scheduled report windows. Label each message as a follow-up,
  identify the original alert, and say it is a review reminder—not a buy, sell,
  or hold instruction. Never infer that a headline invalidates a setup.
- Respect weekends and market holidays. Send market-open and end-of-day
  health alerts.
- Provide CLI modes for one-shot all/flow/swing/CSP/grade scans and a continuous
  loop.
- Grade alerts after 20 hours by comparing the underlying price with its alert
  price and direction. Handle missing prices and invalid stored payloads
  safely.

QUALITY AND ACCEPTANCE
- Separate configuration, data access, scanners, market hours, alert delivery,
  persistence, grading, and runtime scheduling into testable modules.
- Add mock-based automated tests that need no live API keys, market data, or
  Discord. Test quote freshness and intraday fail-closed behavior, flow filters
  and tape classification, swing signals and risk levels, CSP calculations,
  scheduler windows, alert deduplication, and delivery failures.
- Document installation, environment setup, command-line use, scheduling, data
  limitations, and the fact that the app never places trades.
- Run relevant tests and report the actual results. Do not claim checks passed
  unless they were run.
- Keep changes focused. Review Git status and diffs; never discard unrelated
  user changes or commit secrets. Commit or push only when requested.
```
