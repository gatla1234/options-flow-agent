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
- Include a suggestion aligned with each signal's existing direction. Keep
  prices, invalidations, and targets labeled as reference levels, and explain
  assignment risk and potential losses for CSPs.
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
  than 300 seconds. Apply the same current-date and 300-second freshness checks
  to the ThetaData snapshot fallback; if neither source provides a fresh quote,
  reject the candidate. Never substitute a prior-session close for an
  intraday price.
- Route alerts through dedicated environment-configured Discord webhooks:
  DISCORD_WEBHOOK_CONFIRMED, DISCORD_WEBHOOK_DAYTRADE,
  DISCORD_WEBHOOK_SWING, DISCORD_WEBHOOK_CSP, DISCORD_WEBHOOK_HEALTH,
  DISCORD_WEBHOOK_REPORTS, DISCORD_WEBHOOK_SCAN_SUMMARY, and
  DISCORD_WEBHOOK_CLOSED_LEDGER, DISCORD_WEBHOOK_OPEN_LEDGER,
  DISCORD_WEBHOOK_PROFIT_SUMMARY, and DISCORD_WEBHOOK_PERFORMANCE.
  Route morning and
  end-of-day reports through the
  reports webhook. Do not silently route a missing channel webhook to another
  channel.
- Read THETADATA_API_KEY, FINNHUB_API_KEY, and Discord webhook values from the
  environment. Give API requests timeouts and useful errors without logging
  secrets.

DEFAULT WATCHLISTS
- Flow: SPY, QQQ, IWM, SMH, IBIT, SOXL.
- Swing: TQQQ, TNA, SOXL, BITX.
- CSP: BITX, SOXL, TQQQ.
- Relative-strength benchmarks: SOXL vs SMH, TQQQ vs QQQ, BITX vs IBIT,
  TNA vs IWM.
- Keep symbols and thresholds centralized and configurable.

OPTIONS-FLOW SCANNER
- Scan during regular trading hours every 45 seconds.
- Run two separate streams on the configured flow watchlist. CONFIRMED scans
  2–21 DTE across up to two expirations, keeps its four-hour cooldown, and
  retains its existing $30,000 aggressive-buy premium threshold. DAY-TRADE
  scans every expiration from 0–7 DTE, uses a $100,000 buy-premium threshold
  for 0–1 DTE and $50,000 for 2–7 DTE, and uses a 60-minute cooldown.
- Both streams use absolute delta 0.40–0.55; strikes within 12% of the
  underlying; volume at least 200; open interest at least 50; volume/OI at
  least 1.5; spread no greater than 12%; and aggressive-buy premium share of
  at least 60%.
- Classify a print at/above ask as a buy, at/below bid as a sell, and a print
  between bid and ask as unclassified. Premium is price × contracts × 100.
- Apply each stream's aggressive-buy premium threshold and require at least
  60% buy premium. Snapshot volume × midpoint is not a substitute for tape
  evidence.
- Require aligned technical context in both streams. CONFIRMED requires calls
  above prior-day or 20-day resistance and puts below prior-day or 20-day
  support. DAY-TRADE requires calls above the prior day's high and puts below
  the prior day's low, from completed daily bars. Reject candidates when
  required data or confirmation is absent.
- Include contract, expiration/DTE, delta, bid/ask/midpoint, volume, OI,
  volume/OI, OTM percentage, tape premium, technical notes, invalidation, and
  a 2R underlying reference target. Rank qualifying candidates by
  aggressive-buy tape premium descending, then buy-premium share descending.
  Cap alerts at four per stream per scan.
- Include "Suggestion: consider long calls on <TICKER>." for calls and
  "Suggestion: consider long puts on <TICKER>." for puts.
- Label alerts CONFIRMED CALL/PUT or DAY-TRADE CALL/PUT; never label a
  DAY-TRADE alert CONFIRMED. Log per-stream counts through DTE, delta,
  volume/OI, premium, buy percentage, trend, and confirmation stages.
- Keep each stream's repeat-alert cooldown (4 hours for CONFIRMED and
  60 minutes for DAY-TRADE). Also prevent a contract+direction alert in one
  stream from triggering in the other stream for 60 minutes.

SWING SCANNER
- Use about 80 days of daily OHLCV. Calculate 9- and 21-day EMAs, prior-day
  high/low/close, 20-day high/low, and 20-day average volume excluding the
  current bar.
- Calculate five-day relative strength against the configured benchmark when
  data is available.
- Detect strong uptrends, support bounces, strong downtrends, and breakdowns
  using EMA structure, volume, relative strength, and nearby levels. Default
  strong/decent volume thresholds: 1.5×/1.2×, configured centrally.
- Require a defined invalidation on the correct side of price and a minimum 2R
  reference target. Include signal direction, price, trend, EMAs, volume,
  relative strength, nearby levels, and invalidation.
- Suggest considering a long position for bullish signals and a short position
  for bearish signals.

CASH-SECURED-PUT SCANNER
- Only scan when technical context is available and the underlying passes an
  acceptable-to-own-shares filter.
- Consider 5–45 DTE, up to four expirations. Filter puts to strikes below the
  underlying, up to 12% OTM, premium at least 0.4% of strike, spread at most
  10%, and delta -0.35 to -0.15 when available.
- If Greeks are unavailable, skip the delta filter. Mark delta as unavailable
  in the candidate record and alert; never display a delta value that was not
  retrieved.
- Evaluate support at the 21-day EMA, prior low, and 20-day low; prefer strikes
  within 3% of support.
- Calculate premium, annualized yield, cash required, maximum profit, maximum
  loss at expiration, and reward-to-risk. Prefer nearby support (1.25×) and an
  uptrend (1.15×) when ranking; configure both ranking weights centrally.
- Return up to five candidates per symbol and alert on the top three overall;
  configure both caps centrally.
  Include expiration/DTE, premium, yield, cash requirement, OTM percentage,
  delta/IV when available, support, bid/ask, trend, and invalidation.
- Include a suggestion to sell the cash-secured put only if the user is willing
  to own shares at the strike.

ALERTS AND SCHEDULING
- Persist successfully sent alerts, payloads, prices, timestamps, grading
  outcomes, and scheduler flags in SQLite using parameterized SQL.
- Use per-stream flow cooldowns of 4 hours for CONFIRMED and 60 minutes for
  DAY-TRADE, plus the 60-minute cross-stream contract+direction dedupe rule.
  Configure cross-stream dedupe duration centrally, separately from stream
  cooldowns.
  Use cooldowns of 20 hours for swing and CSP and 12 hours for health. Limit
  Discord alerts to eight per minute.
- Cap flow results at four per stream per scan. If a scan would exceed the
  eight-per-minute Discord limit, queue excess alerts for the next minute
  window, sending CONFIRMED alerts first. Log every alert dropped instead of
  sent; never silently discard one.
- Persist an alert only after Discord confirms success; surface delivery
  failures explicitly.
- Use America/New_York time. During regular hours, poll flow and run CSP scans
  once in each 10:15–11:00 and 15:30–16:00 window. After the close, run the
  swing scan and grade eligible alerts once per trading day.
- Deliver a concise, three-section pre-market report at 9:00 a.m. ET and a
  similar end-of-day report at 4:30 p.m. ET. Include source-backed market
  headlines and a four-index price/change table. Reject stale or wrong-session
  prices instead of showing the previous close as pre-market data.
- At 5:00 p.m. ET on each trading day, after the end-of-day scan and report,
  post per-ticker, per-scan-type funnel counts, blocker stages, alerts, and
  follow-ups to the dedicated scan-summary webhook. Never fall back to another
  channel if DISCORD_WEBHOOK_SCAN_SUMMARY is missing; send one system-health
  note and skip. Label missing/stale scanner data instead of inventing counts,
  and split oversized scan-type reports without truncation.
- Maintain a separate closed-alert ledger routed only to
  DISCORD_WEBHOOK_CLOSED_LEDGER. Persist closure state by original SQLite alert
  number and post at most one immutable record for invalidation, 2R target,
  expiration, CSP 90% premium decay, or swing staleness after 20 trading days.
  Include required audit fields and hypothetical P&L; never include open
  alerts. A missing ledger webhook must produce one system-health note and
  must not fall back to another channel. Infer expiration outcomes from
  expiration-day intrinsic value; do not claim brokerage exercise or
  assignment status.
- Maintain a single edited open-alert tracker in the dedicated
  DISCORD_WEBHOOK_OPEN_LEDGER channel. Persist webhook message IDs, refresh
  every 15 minutes from 9:30 a.m. through 4:15 p.m. ET, and keep the last
  closing marks visible outside market hours. Split by scan type only when the
  message limit requires it. Include current P&L, target/invalidation progress,
  and status flags; never include closed alerts or suggestions.
- At 5:15 p.m. ET each trading day, post a dedicated profit summary to
  DISCORD_WEBHOOK_PROFIT_SUMMARY with open, closed-by-rule, stale, and separate
  options/swing totals. At 5:30 p.m. ET on the last trading day of each week,
  post YTD and all-time realized/unrealized performance to
  DISCORD_WEBHOOK_PERFORMANCE. Use persisted closed-ledger records and current
  open marks; never combine options per-contract dollars with swing per-share
  dollars. Each missing webhook gets one health note and no fallback post.
- Keep pre-market option-flow and IV claims conservative: if the configured
  feeds cannot verify current-session options trades or IV, state that clearly
  and do not infer an IV spike from an underlying gap.
- Treat the alert-time price as entry for every follow-up. Flow uses the
  recorded alert price; swing uses the signal-day close; CSP records the
  alert-time underlying price plus the original put's strike, expiration, and
  premium. State these entry details in follow-ups, compute all related math
  from them, and do not use assumed-entry or unknown-position-status wording.
- On the existing 45-second regular-hours flow scan, evaluate current-session
  flow alerts using fresh quotes and the scanner's aggressive-trade
  classification. Reverse when opposing 15-minute aggressive premium reaches
  the stream's premium threshold, opposing premium is at least 60% of
  classified premium, and opposing trend/price confirmation passes. Otherwise
  exit for broken invalidation or qualifying unconfirmed opposing flow.
  Otherwise, suggest an equal-size average after a 50% retracement toward
  intact invalidation only when the original contract has at least 7 DTE.
  Allow one follow-up per rule per alert per session.
- On the existing after-close swing run, suggest an equal-size average after a
  50% direction-neutral retracement toward intact invalidation, using later
  daily closes only. Restrict to alerts no older than five trading days and
  send at most once per alert.
- During existing CSP scan windows, suggest selling an additional put after a
  50% retracement toward the original strike while still above it, only when
  the original put has at least 7 DTE and a fresh candidate passes standard CSP
  filters. Prefer the original strike and expiration; otherwise use the
  highest-ranked qualifying candidate. Report total premium, additional cash
  requirement, and correct per-leg or blended breakevens; send at most once.
- Preserve existing invalidation/2R/strike follow-ups, adding the recorded
  entry. Route each follow-up to the original alert's stream channel. Also
  check for company headlines and approaching expirations in scheduled report
  windows. Include an event-appropriate suggestion; never infer that a
  headline invalidates a setup.
- Respect weekends, full market holidays, and scheduled early closes. Stop
  underlying scans at the applicable close, account for the ETF options
  session extension, and skip scheduled scans that would occur after an early
  close. Maintain the explicit exchange calendar before each new year. Send
  market-open and end-of-day health alerts.
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
