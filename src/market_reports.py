from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

import requests
from rich import print

from src.config import (
    CSP_GAP_DOWN_ALERT_PCT,
    CSP_TICKERS,
    FINNHUB_MAX_QUOTE_AGE_SECONDS,
    REPORT_FLOW_TICKERS,
    REPORT_GAP_ALERT_PCT,
    SWING_TICKERS,
)
from src.data import FinnhubQuote, get_daily_bars, get_finnhub_quote
from src.flow import scan_symbol
from src.market import ET
from src.state import alerts_between
from src.swing_signals import build_context
from src.util import pct_change

CORE_TICKERS = ("SPY", "QQQ", "IWM", "SMH")
PREMARKET_START = time(4, 0)
MARKET_OPEN = time(9, 30)
EOD_QUOTE_START = time(15, 55)
EOD_QUOTE_MAX_AGE = timedelta(minutes=45)
MACRO_KEYWORDS = (
    "fed",
    "federal reserve",
    "fomc",
    "inflation",
    "cpi",
    "pce",
    "payroll",
    "jobs report",
    "unemployment",
    "treasury yield",
    "tariff",
    "trade war",
    "sanction",
    "geopolitical",
    "oil price",
    "crude",
    "earnings",
    "recession",
    "gdp",
    "consumer confidence",
    "rate cut",
    "rate hike",
)


def _quote_is_current(
    quote: FinnhubQuote | None, report_type: str, now: datetime
) -> bool:
    if quote is None:
        return False

    now_utc = now.astimezone(timezone.utc)
    quoted_at = quote.timestamp.astimezone(ET)
    age = now_utc - quote.timestamp.astimezone(timezone.utc)
    if quoted_at.date() != now.astimezone(ET).date() or age < timedelta(0):
        return False

    if report_type == "morning":
        return (
            PREMARKET_START <= quoted_at.time() < MARKET_OPEN
            and age.total_seconds() <= FINNHUB_MAX_QUOTE_AGE_SECONDS
        )

    return (
        EOD_QUOTE_START <= quoted_at.time() <= now.astimezone(ET).time()
        and age <= EOD_QUOTE_MAX_AGE
    )


def _get_report_quotes(
    report_type: str, now: datetime
) -> dict[str, FinnhubQuote | None]:
    symbols = dict.fromkeys(
        (*CORE_TICKERS, *SWING_TICKERS, *CSP_TICKERS, *REPORT_FLOW_TICKERS)
    )
    quotes: dict[str, FinnhubQuote | None] = {}
    for symbol in symbols:
        quote = get_finnhub_quote(symbol)
        quotes[symbol] = quote if _quote_is_current(quote, report_type, now) else None
    return quotes


def _fetch_macro_headlines(now: datetime, report_type: str) -> list[dict[str, Any]]:
    api_key = os.getenv("FINNHUB_API_KEY")
    if not api_key:
        print(
            "[yellow]Market report headlines unavailable: "
            "FINNHUB_API_KEY is not set[/yellow]"
        )
        return []

    try:
        response = requests.get(
            "https://finnhub.io/api/v1/news",
            params={"category": "general", "token": api_key},
            timeout=10,
        )
    except requests.RequestException as exc:
        print(f"[yellow]Market news request failed: {type(exc).__name__}[/yellow]")
        return []

    if response.status_code != 200:
        print(
            f"[yellow]Market news request failed: "
            f"HTTP {response.status_code}[/yellow]"
        )
        return []

    try:
        payload = response.json()
    except ValueError:
        print("[yellow]Finnhub returned invalid market news[/yellow]")
        return []
    if not isinstance(payload, list):
        print("[yellow]Finnhub returned an unexpected market news response[/yellow]")
        return []

    local_now = now.astimezone(ET)
    if report_type == "morning":
        cutoff = local_now - timedelta(hours=18)
    else:
        cutoff = local_now.replace(hour=9, minute=30, second=0, microsecond=0)

    headlines: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        headline = str(item.get("headline") or "").strip()
        timestamp = item.get("datetime")
        if not headline:
            continue
        try:
            published = datetime.fromtimestamp(
                float(timestamp), timezone.utc
            ).astimezone(ET)
        except (OSError, OverflowError, TypeError, ValueError):
            continue
        if not cutoff <= published <= local_now:
            continue
        search_text = f"{headline} {item.get('summary') or ''}".lower()
        if not any(keyword in search_text for keyword in MACRO_KEYWORDS):
            continue
        headlines.append(
            {
                "headline": headline,
                "source": str(item.get("source") or "Finnhub"),
                "published": published,
            }
        )

    headlines.sort(key=lambda item: item["published"], reverse=True)
    return headlines[:3]


def _diagnosis(headlines: list[dict[str, Any]], report_type: str) -> str:
    if not headlines:
        context = "pre-market" if report_type == "morning" else "today's"
        return (
            f"No verified macro headline was returned for {context} context, "
            "so there is no defensible single-catalyst explanation for the index move."
        )

    text = " ".join(item["headline"].lower() for item in headlines)
    if any(
        word in text
        for word in ("inflation", "cpi", "pce", "payroll", "jobs report", "gdp")
    ):
        return (
            "The reported data can reset expected Fed policy and Treasury yields, "
            "changing the discount rate investors apply to equities."
        )
    if any(
        word in text
        for word in ("fed", "federal reserve", "fomc", "rate cut", "rate hike")
    ):
        return (
            "The policy commentary can shift expected interest rates and Treasury "
            "yields, repricing equity valuations."
        )
    if any(
        word in text
        for word in ("tariff", "sanction", "geopolitical", "trade war", "crude", "oil")
    ):
        return (
            "The trade or supply-risk headlines can change expected input costs and "
            "risk appetite, particularly for cyclicals and energy-sensitive sectors."
        )
    if "earnings" in text:
        return (
            "The earnings headlines change profit expectations; moves in large "
            "index constituents can disproportionately affect SPY and QQQ."
        )
    return (
        "These headlines may affect expected growth, rates, or risk appetite; "
        "the price table shows whether investors are repricing those risks."
    )


def _format_macro_module(
    headlines: list[dict[str, Any]], report_type: str
) -> str:
    title = (
        "Pre-Market Macro Context"
        if report_type == "morning"
        else "Market-Moving Context"
    )
    lines = [f"## 1. {title}"]
    for item in headlines:
        lines.append(
            f"- **{item['headline']}** — {item['source']}, "
            f"{item['published']:%H:%M ET}"
        )
    if not headlines:
        lines.extend(
            [
                "- No qualifying macro headline was verified in the configured news feed.",
                "- No specific catalyst is attributed without a source-backed headline.",
            ]
        )
    elif len(headlines) == 1:
        lines.append("- No second qualifying macro headline was verified in the feed.")
    lines.append(f"\n**Diagnostic:** {_diagnosis(headlines, report_type)}")
    return "\n".join(lines)


def _format_price(value: float | None) -> str:
    return f"${value:,.2f}" if value is not None else "Unavailable"


def _format_matrix(
    quotes: dict[str, FinnhubQuote | None], report_type: str
) -> str:
    if report_type == "morning":
        title = "Pre-Market Price"
        change_title = "Overnight Change (%)"
        source_note = (
            "Finnhub quote timestamp must be current-session pre-market "
            "and no older than 5 minutes."
        )
    else:
        title = "Close / Last Price"
        change_title = "Day Change (%)"
        source_note = (
            "Finnhub quote timestamp must be from 3:55 p.m. ET or later "
            "on the current trading day."
        )
    lines = [
        "## 2. Core Watchlist Matrix (SPY, QQQ, IWM, SMH)",
        f"| Ticker | {title} | {change_title} |",
        "|---|---:|---:|",
    ]
    for symbol in CORE_TICKERS:
        quote = quotes.get(symbol)
        if quote is None or quote.previous_close is None:
            lines.append(f"| {symbol} | Unavailable | Unavailable |")
            continue
        change = pct_change(quote.price, quote.previous_close)
        change_display = f"{change:+.2f}%" if change is not None else "Unavailable"
        lines.append(f"| {symbol} | {_format_price(quote.price)} | {change_display} |")
    lines.append(f"\n_Data: {source_note} Stale prices are omitted._")
    return "\n".join(lines)


def _prior_day_levels(symbol: str, today: date) -> tuple[float, float] | None:
    bars = get_daily_bars(symbol, days=10)
    if bars is None or bars.is_empty() or not {"high", "low"}.issubset(bars.columns):
        return None

    sort_column = "created" if "created" in bars.columns else bars.columns[0]
    for row in reversed(bars.sort(sort_column).to_dicts()):
        raw_date = row.get("created", row.get(sort_column))
        try:
            if isinstance(raw_date, datetime):
                bar_date = raw_date.date()
            elif isinstance(raw_date, date):
                bar_date = raw_date
            else:
                bar_date = date.fromisoformat(str(raw_date)[:10])
        except (TypeError, ValueError):
            continue
        if bar_date < today:
            try:
                high, low = float(row["high"]), float(row["low"])
            except (TypeError, ValueError):
                return None
            if high > 0 and low > 0:
                return high, low
    return None


def _swing_summary(
    quotes: dict[str, FinnhubQuote | None], report_type: str, now: datetime
) -> str:
    observations: list[str] = []
    for symbol in SWING_TICKERS:
        quote = quotes.get(symbol)
        if quote is None or quote.previous_close is None:
            continue
        change = pct_change(quote.price, quote.previous_close)
        if change is None:
            continue
        level_text = ""
        levels = _prior_day_levels(symbol, now.astimezone(ET).date())
        if levels:
            prior_high, prior_low = levels
            if quote.price > prior_high:
                level_text = " above prior-day high"
            elif quote.price < prior_low:
                level_text = " below prior-day low"
        label = "gap" if report_type == "morning" else "day move"
        observations.append(f"{symbol} {label} {change:+.2f}%{level_text}")

    if not observations:
        return "No fresh same-session prices available for the leveraged swing list."
    return "; ".join(observations[:4]) + "."


def _flow_summary(
    report_type: str,
    quotes: dict[str, FinnhubQuote | None],
) -> str:
    if report_type == "morning":
        gaps: list[str] = []
        for symbol in REPORT_FLOW_TICKERS:
            quote = quotes.get(symbol)
            if quote is None or quote.previous_close is None:
                continue
            change = pct_change(quote.price, quote.previous_close)
            if change is not None and abs(change) >= REPORT_GAP_ALERT_PCT:
                gaps.append(f"{symbol} {change:+.2f}%")
        gap_note = (
            "Large underlying gaps: " + ", ".join(gaps) + "."
            if gaps
            else "No verified underlying gap of 2% or more."
        )
        return (
            "No verified pre-market options-tape feed is available from the "
            "configured providers; options-flow anomalies cannot be confirmed "
            f"before 9:30 a.m. ET. {gap_note}"
        )

    qualifying: list[dict[str, Any]] = []
    failures: list[str] = []
    for symbol in REPORT_FLOW_TICKERS:
        if quotes.get(symbol) is None:
            failures.append(symbol)
            continue
        try:
            context = build_context(symbol)
            qualifying.extend(scan_symbol(symbol, context))
        except Exception as exc:
            print(
                f"[yellow]End-of-day flow scan failed for {symbol}: "
                f"{type(exc).__name__}[/yellow]"
            )
            failures.append(symbol)

    qualifying.sort(key=lambda item: item.get("buy_premium", 0), reverse=True)
    if qualifying:
        summaries = [
            f"{item['symbol']} ${item['strike']}{item['right'][0]} "
            f"{item['expiration']} (${item['buy_premium']:,.0f} ask-side premium)"
            for item in qualifying[:2]
        ]
        result = "Confirmed setups: " + "; ".join(summaries) + "."
    else:
        result = "No qualifying confirmed flow setup found in the report watchlist."
    if failures:
        result += " Incomplete data for: " + ", ".join(failures) + "."
    return result


def _csp_summary(
    report_type: str,
    quotes: dict[str, FinnhubQuote | None],
    now: datetime,
) -> str:
    if report_type == "morning":
        falling: list[str] = []
        for symbol in CSP_TICKERS:
            quote = quotes.get(symbol)
            if quote is None or quote.previous_close is None:
                continue
            change = pct_change(quote.price, quote.previous_close)
            if change is not None and change <= -CSP_GAP_DOWN_ALERT_PCT:
                falling.append(f"{symbol} {change:+.2f}%")
        note = (
            "Gap-down watch: " + ", ".join(falling) + "."
            if falling
            else f"No verified CSP-underlying gap-down of {CSP_GAP_DOWN_ALERT_PCT:.1f}% or more."
        )
        return (
            f"{note} Pre-market IV repricing cannot be verified: option quotes "
            "may be stale before 9:30 a.m. ET; recheck premiums after the options open."
        )

    start = datetime.combine(now.astimezone(ET).date(), time.min, ET)
    end = start + timedelta(days=1)
    alerts = alerts_between("csp", start, end)
    if not alerts:
        return (
            "No successfully delivered CSP alerts recorded today. Current IV "
            "change is not independently verified by this report."
        )
    names = [
        str(item.get("title") or item.get("symbol") or "CSP alert")
        for item in alerts[:3]
    ]
    return (
        "CSP alerts delivered today: " + "; ".join(names) + ". "
        "These are prior scan results, not a fresh post-close IV quote."
    )


def build_market_report(report_type: str, now: datetime | None = None) -> str:
    if report_type not in {"morning", "eod"}:
        raise ValueError("report_type must be 'morning' or 'eod'")
    now = now or datetime.now(ET)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    quotes = _get_report_quotes(report_type, now)
    headlines = _fetch_macro_headlines(now, report_type)
    module_one = _format_macro_module(headlines, report_type)
    module_two = _format_matrix(quotes, report_type)
    module_three_title = (
        "Specific Trading Alerts & Momentum Segments"
        if report_type == "morning"
        else "End-of-Day Trading & Momentum Summary"
    )
    module_three = "\n".join(
        [
            f"## 3. {module_three_title}",
            f"- **Options Flow Volatility (IBIT, GLD, SLV, USO):** "
            f"{_flow_summary(report_type, quotes)}",
            f"- **Swing Trailing Momentum (TQQQ, TNA, SOXL, BITX):** "
            f"{_swing_summary(quotes, report_type, now)}",
            f"- **CSP Screening ({', '.join(CSP_TICKERS)}):** "
            f"{_csp_summary(report_type, quotes, now)}",
        ]
    )
    return "\n---\n".join((module_one, module_two, module_three))
