from __future__ import annotations

import math
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


import polars as pl
from rich import print

from src.config import (
    DAY_DTE_MAX,
    DAY_DTE_MIN,
    DAY_DTE_TIER_0_1_MAX,
    FLOW_FOLLOWUP_TAPE_WINDOW_SEC,
    DAY_MIN_PREMIUM_0_1,
    DAY_MIN_PREMIUM_2_7,
    FLOW_MAX_ALERTS_PER_SCAN,
    FLOW_MAX_DTE,
    FLOW_MAX_EXPIRATIONS,
    FLOW_MAX_DELTA,
    FLOW_MAX_SPREAD_PCT,
    FLOW_MIN_AGGRESSOR_PCT,
    FLOW_MIN_DELTA,
    FLOW_MIN_DTE,
    FLOW_MIN_OI,
    FLOW_MIN_PREMIUM,
    FLOW_RISK_REWARD_RATIO,
    FLOW_MIN_VOLUME,
    FLOW_STRIKE_RANGE_PCT,
    FLOW_TICKERS,
    FLOW_VOLUME_OI_RATIO,
)
from src.discord_alerts import send_alert
from src.discord_alerts import flush_alert_queue
from src.data import ET, _snapshot_timestamp
from src.options_data import (
    get_expirations,
    get_greeks_chain,
    get_oi_chain,
    get_ohlc_chain,
    get_option_chain,
    get_trade_quotes,
    get_underlying_price,
    join_on_contract,
)
from src.swing_signals import (
    build_context,
    confirmed_flow_level,
    underlying_invalidation,
)
from src.state import log_funnel_scan
from src.util import as_float, as_int, first_present, normalize_right


def _classify_aggressor(trades: pl.DataFrame | None) -> dict:
    buy_prem = sell_prem = mid_prem = 0.0
    buy_vol = sell_vol = 0
    if trades is None or trades.is_empty():
        return {
            "buy_premium": 0.0,
            "sell_premium": 0.0,
            "buy_pct": 0.0,
            "contracts": 0,
            "side": "UNKNOWN",
        }

    for row in trades.iter_rows(named=True):
        price = as_float(first_present(row, "price", "trade_price", "last", "close"))
        size = as_int(first_present(row, "size", "trade_size", "volume", "quantity"))
        bid = as_float(first_present(row, "bid", "bid_price"))
        ask = as_float(first_present(row, "ask", "ask_price"))
        if price <= 0 or size <= 0:
            continue
        premium = price * size * 100
        if ask > 0 and price >= ask:
            buy_prem += premium
            buy_vol += size
        elif bid > 0 and price <= bid:
            sell_prem += premium
            sell_vol += size
        else:
            mid_prem += premium

    total = buy_prem + sell_prem + mid_prem
    buy_pct = (buy_prem / total) if total else 0.0
    if buy_pct >= FLOW_MIN_AGGRESSOR_PCT:
        side = "BUY"
    elif total and sell_prem / total >= FLOW_MIN_AGGRESSOR_PCT:
        side = "SELL"
    else:
        side = "MIXED"
    return {
        "buy_premium": buy_prem,
        "sell_premium": sell_prem,
        "buy_pct": buy_pct,
        "buy_volume": buy_vol,
        "sell_volume": sell_vol,
        "contracts": buy_vol + sell_vol,
        "side": side,
        "total_premium": total,
    }


def _technical_confluence(ctx: dict | None, right: str) -> tuple[bool, str]:
    if not ctx:
        return False, "no technicals"
    trend = ctx.get("trend", "NEUTRAL")
    nearby_levels = ctx.get("nearby_levels") or []
    rs = ctx.get("rs")
    if right == "CALL":
        trend_aligned = trend in {"UPTREND", "NEUTRAL"} and ctx.get("ok_to_own", False)
        rs_aligned = rs is not None and rs >= 0
    else:
        trend_aligned = trend in {"DOWNTREND", "NEUTRAL"}
        rs_aligned = rs is not None and rs <= 0

    notes = [trend.lower()]
    if nearby_levels:
        notes.append("near " + ", ".join(nearby_levels[:3]))
    if rs is not None:
        notes.append(f"RS {rs:+.1f}%")
    rs_opposed = rs is not None and not rs_aligned
    aligned = trend_aligned and not rs_opposed and bool(nearby_levels or rs_aligned)
    return aligned, "; ".join(notes)


def _flow_confirmation(
    tech: dict | None, right: str, price: float, stream: str
) -> dict | None:
    if stream == "confirmed":
        return confirmed_flow_level(tech, right, price)
    if not tech:
        return None

    level_name = "prior_day_high" if right == "CALL" else "prior_day_low"
    level = as_float(tech.get(level_name))
    confirmed = price > level if right == "CALL" else 0 < price < level
    if not confirmed or level <= 0:
        return None
    return {
        "price": round(level, 2),
        "level": level_name,
        "direction": "BULLISH" if right == "CALL" else "BEARISH",
    }


def _premium_threshold(stream: str, dte: int) -> float:
    if stream == "confirmed":
        return FLOW_MIN_PREMIUM
    return (
        DAY_MIN_PREMIUM_0_1
        if dte <= DAY_DTE_TIER_0_1_MAX
        else DAY_MIN_PREMIUM_2_7
    )


def get_opposing_flow_summary(
    symbol: str,
    original_right: str,
    stream: str,
    now: datetime,
    tech: dict | None,
    underlying_price: float,
) -> dict:
    """Summarize fresh aggressive buys in opposite-right contracts over 15 minutes."""
    if stream not in {"confirmed", "daytrade"}:
        raise ValueError("stream must be 'confirmed' or 'daytrade'")

    opposite_right = "PUT" if original_right == "CALL" else "CALL"
    min_dte = DAY_DTE_MIN if stream == "daytrade" else FLOW_MIN_DTE
    max_dte = DAY_DTE_MAX if stream == "daytrade" else FLOW_MAX_DTE
    cutoff = (
        now.astimezone(timezone.utc).timestamp()
        - FLOW_FOLLOWUP_TAPE_WINDOW_SEC
    )
    window_start = datetime.fromtimestamp(cutoff, timezone.utc)
    total_buy_premium = 0.0
    classified_premium = 0.0
    top_contract: dict | None = None

    expirations = get_expirations(symbol, max_dte=max_dte, min_dte=min_dte)
    if stream == "confirmed":
        expirations = expirations[:FLOW_MAX_EXPIRATIONS]

    for expiration in expirations:
        try:
            dte = (date.fromisoformat(expiration) - now.astimezone(ET).date()).days
        except ValueError:
            continue
        if not min_dte <= dte <= max_dte:
            continue

        chain = get_option_chain(symbol, expiration)
        if chain is None or chain.is_empty() or "right" not in chain.columns:
            continue
        greeks = get_greeks_chain(symbol, expiration, stock_price=underlying_price)
        merged = join_on_contract(chain, greeks)
        if merged is None:
            continue

        for row in merged.iter_rows(named=True):
            right = normalize_right(first_present(row, "right"))
            if right != opposite_right:
                continue
            strike = as_float(first_present(row, "strike"))
            if strike <= 0:
                continue

            trades = get_trade_quotes(symbol, expiration, strike, opposite_right)
            if trades is None or trades.is_empty():
                continue
            fresh_rows = []
            for trade in trades.iter_rows(named=True):
                timestamp = _snapshot_timestamp(trade)
                if timestamp is None:
                    continue
                timestamp_utc = timestamp.astimezone(timezone.utc)
                age = (now.astimezone(timezone.utc) - timestamp_utc).total_seconds()
                if (
                    timestamp.astimezone(ET).date() == now.astimezone(ET).date()
                    and 0 <= age <= FLOW_FOLLOWUP_TAPE_WINDOW_SEC
                ):
                    fresh_rows.append(trade)
            if not fresh_rows:
                continue

            aggressors = _classify_aggressor(pl.DataFrame(fresh_rows))
            buy_premium = as_float(aggressors.get("buy_premium"))
            classified = as_float(aggressors.get("buy_premium")) + as_float(
                aggressors.get("sell_premium")
            )
            total_buy_premium += buy_premium
            classified_premium += classified
            if buy_premium <= 0:
                continue

            bid = as_float(first_present(row, "bid"))
            ask = as_float(first_present(row, "ask"))
            mid = (bid + ask) / 2 if bid > 0 and ask > 0 else None
            spread_pct = (
                ((ask - bid) / mid) * 100
                if mid and ask > 0 and bid >= 0
                else None
            )
            delta_value = first_present(row, "delta", "delta_mid", "delta_ask", "delta_bid")
            try:
                delta = float(delta_value) if delta_value is not None else None
            except (TypeError, ValueError):
                delta = None
            if delta is not None and not math.isfinite(delta):
                delta = None
            candidate = {
                "right": opposite_right,
                "strike": strike,
                "expiration": expiration,
                "dte": dte,
                "buy_premium": buy_premium,
                "mid": mid,
                "delta": delta,
                "spread_pct": spread_pct,
            }
            if top_contract is None or buy_premium > top_contract["buy_premium"]:
                top_contract = candidate

    buy_share = total_buy_premium / classified_premium if classified_premium else 0.0
    aligned, tech_note = _technical_confluence(tech, opposite_right)
    confirmation = _flow_confirmation(
        tech, opposite_right, underlying_price, stream
    )
    return {
        "right": opposite_right,
        "buy_premium": total_buy_premium,
        "classified_premium": classified_premium,
        "buy_share": buy_share,
        "top_contract": top_contract,
        "confirmed": aligned and confirmation is not None,
        "tech_note": tech_note,
        "confirmation": confirmation,
        "window_start": window_start,
    }


def scan_symbol(
    symbol: str,
    tech: dict | None,
    stream: str = "confirmed",
    funnel: dict[str, int] | None = None,
) -> list[dict]:
    if stream not in {"confirmed", "daytrade"}:
        raise ValueError("stream must be 'confirmed' or 'daytrade'")

    counts = funnel if funnel is not None else {}
    counts["available"] = int(tech is not None)
    for stage in (
        "contracts",
        "dte",
        "delta",
        "strike",
        "volume_oi",
        "premium",
        "buy_pct",
        "trend",
        "confirmation",
    ):
        counts.setdefault(stage, 0)

    day_trade = stream == "daytrade"
    min_dte = DAY_DTE_MIN if day_trade else FLOW_MIN_DTE
    max_dte = DAY_DTE_MAX if day_trade else FLOW_MAX_DTE
    price = get_underlying_price(symbol)
    if not price:
        counts["available"] = 0
        print(f"  No price for {symbol}")
        return []

    stream_label = "DAY-TRADE" if day_trade else "CONFIRMED"
    print(f"\nScanning {stream_label} {symbol} @ ${price:.2f}")
    results: list[dict] = []
    expirations = get_expirations(symbol, max_dte=max_dte, min_dte=min_dte)
    if not expirations:
        counts["available"] = 0
    if not day_trade:
        expirations = expirations[:FLOW_MAX_EXPIRATIONS]

    for exp in expirations:
        quotes = get_option_chain(symbol, exp)
        oi_df = get_oi_chain(symbol, exp)
        ohlc = get_ohlc_chain(symbol, exp)
        greeks = get_greeks_chain(symbol, exp, stock_price=price)
        merged = join_on_contract(quotes, oi_df, ohlc, greeks)
        if merged is None:
            counts["available"] = 0
            continue
        counts["contracts"] += len(merged)
        dte = (date.fromisoformat(exp) - date.today()).days
        if not min_dte <= dte <= max_dte:
            continue

        counts["dte"] += len(merged)
        candidates: list[dict] = []
        for row in merged.iter_rows(named=True):
            bid = as_float(first_present(row, "bid"))
            ask = as_float(first_present(row, "ask"))
            strike = as_float(first_present(row, "strike"))
            right = normalize_right(first_present(row, "right"))
            oi = as_int(first_present(row, "open_interest", "oi"))
            volume = as_int(first_present(row, "volume", "count"))
            delta = as_float(first_present(row, "delta", "delta_mid"))
            last = as_float(first_present(row, "close", "price", "last"))
            mid = (bid + ask) / 2 if bid > 0 and ask > 0 else last
            if not FLOW_MIN_DELTA <= abs(delta) <= FLOW_MAX_DELTA:
                continue
            counts["delta"] += 1
            if mid <= 0 or strike <= 0 or not right:
                continue
            otm_pct = (
                ((strike - price) / price) * 100
                if right == "CALL"
                else ((price - strike) / price) * 100
            )
            if abs(otm_pct) > FLOW_STRIKE_RANGE_PCT:
                continue
            counts["strike"] += 1

            spread_pct = ((ask - bid) / mid) * 100 if mid > 0 and ask > 0 else 999
            vol_oi = (volume / oi) if oi else (volume if volume else 0)
            premium_est = volume * mid * 100

            if (
                volume >= FLOW_MIN_VOLUME
                and oi >= FLOW_MIN_OI
                and vol_oi >= FLOW_VOLUME_OI_RATIO
                and spread_pct <= FLOW_MAX_SPREAD_PCT
            ):
                counts["volume_oi"] += 1
                candidates.append(
                    {
                        "symbol": symbol,
                        "right": right,
                        "strike": strike,
                        "expiration": exp,
                        "dte": dte,
                        "delta": delta,
                        "mid": round(mid, 2),
                        "bid": bid,
                        "ask": ask,
                        "oi": oi,
                        "volume": volume,
                        "vol_oi": round(vol_oi, 2),
                        "premium_est": round(premium_est, 0),
                        "spread_pct": round(spread_pct, 1),
                        "otm_pct": round(otm_pct, 1),
                        "underlying": price,
                    }
                )

        for item in candidates:
            trades = get_trade_quotes(symbol, exp, item["strike"], item["right"])
            agg = _classify_aggressor(trades)
            item.update(agg)
            if agg["buy_premium"] < _premium_threshold(stream, dte):
                continue
            counts["premium"] += 1
            if agg["buy_pct"] < FLOW_MIN_AGGRESSOR_PCT:
                continue
            counts["buy_pct"] += 1

            aligned, tech_note = _technical_confluence(tech, item["right"])
            if not aligned:
                continue
            counts["trend"] += 1
            confirmation = _flow_confirmation(tech, item["right"], price, stream)
            if not confirmation:
                continue
            counts["confirmation"] += 1
            item["stream"] = stream
            item["tech_aligned"] = aligned
            item["tech_note"] = tech_note
            item["confirmation"] = confirmation
            direction = "BULLISH" if item["right"] == "CALL" else "BEARISH"
            item["invalidation"] = underlying_invalidation(
                tech, direction, reference_price=price
            )
            if not item["invalidation"]:
                continue
            risk_distance = abs(price - item["invalidation"]["price"])
            if risk_distance <= 0:
                continue
            target_price = (
                price + FLOW_RISK_REWARD_RATIO * risk_distance
                if direction == "BULLISH"
                else price - FLOW_RISK_REWARD_RATIO * risk_distance
            )
            item["underlying_target_2r"] = round(target_price, 2)
            item["risk_reward_ratio"] = FLOW_RISK_REWARD_RATIO
            item["conditions"] = [
                f"aggressive buy premium ${int(item['buy_premium']):,}",
                f"vol/OI {item['vol_oi']}x",
                f"aggressor BUY {item['buy_pct']:.0%}",
                f"underlying broke {confirmation['level']}",
            ]
            results.append(item)

    results.sort(
        key=lambda x: (
            x["buy_premium"],
            x["buy_pct"],
        ),
        reverse=True,
    )
    return results


def run_flow_scan(send_alerts: bool = True) -> list[dict]:
    print("Running CONFIRMED and DAY-TRADE options-flow scans...\n")
    all_results: list[dict] = []
    tech_cache: dict[str, dict | None] = {}

    stream_results: dict[str, list[dict]] = {}
    for stream in ("confirmed", "daytrade"):
        results: list[dict] = []
        for symbol in FLOW_TICKERS:
            counts: dict[str, int] = {}
            if symbol not in tech_cache:
                try:
                    tech_cache[symbol] = build_context(symbol)
                except Exception:
                    tech_cache[symbol] = None
            try:
                symbol_results = scan_symbol(
                    symbol, tech_cache[symbol], stream=stream, funnel=counts
                )
            except Exception as exc:
                print(f"[red]Flow scan failed for {stream} {symbol}: {exc}[/red]")
                symbol_results = []
                counts["available"] = 0
                for stage in (
                    "contracts", "dte", "delta", "strike", "volume_oi", "premium",
                    "buy_pct", "trend", "confirmation",
                ):
                    counts.setdefault(stage, 0)
            funnel_counts = {
                stage: count for stage, count in counts.items()
                if stage != "available"
            }
            try:
                log_funnel_scan(
                    stream,
                    symbol,
                    funnel_counts,
                    data_available=bool(counts.get("available")),
                )
            except Exception as exc:
                print(f"[red]Could not persist {stream} funnel for {symbol}: {exc}[/red]")
            results.extend(symbol_results)
            print(
                f"[cyan]{stream.upper()} {symbol} funnel[/cyan] "
                f"contracts={funnel_counts.get('contracts', 0)} "
                f"DTE={funnel_counts.get('dte', 0)} "
                f"delta={funnel_counts.get('delta', 0)} "
                f"strike={funnel_counts.get('strike', 0)} "
                f"volume/OI={funnel_counts.get('volume_oi', 0)} "
                f"premium={funnel_counts.get('premium', 0)} "
                f"buy%={funnel_counts.get('buy_pct', 0)} "
                f"trend={funnel_counts.get('trend', 0)} "
                f"confirmation={funnel_counts.get('confirmation', 0)}"
            )
            for result in symbol_results:
                tag = " + TECH" if result["tech_aligned"] else ""
                print(
                    f"  {result['symbol']} {result['strike']}{result['right'][0]} "
                    f"{result['expiration']} | BUY {result['buy_pct']:.0%} | "
                    f"prem ${int(result['buy_premium']):,} | "
                    f"vol/OI {result['vol_oi']}x{tag}"
                )
        stream_results[stream] = sorted(
            results,
            key=lambda item: (
                item["buy_premium"],
                item["buy_pct"],
            ),
            reverse=True,
        )[:FLOW_MAX_ALERTS_PER_SCAN]

    all_results = [item for results in stream_results.values() for item in results]
    if send_alerts:
        for item in all_results:
            color = 0x00FF00 if item["right"] == "CALL" else 0xFF0000
            emoji = "🟢" if item["right"] == "CALL" else "🔴"
            boost = " · CONFLUENCE" if item["tech_aligned"] else ""
            stream = item["stream"]
            stream_label = "DAY-TRADE" if stream == "daytrade" else "CONFIRMED"
            title = (
                f"{emoji} {stream_label} {item['right']} setup — "
                f"{item['symbol']} ${item['strike']}{item['right'][0]}{boost}"
            )
            premium = int(item["buy_premium"])
            option_suggestion = (
                "long calls" if item["right"] == "CALL" else "long puts"
            )
            description = (
                f"Suggestion: consider {option_suggestion} on {item['symbol']}.\n"
                f"**Setup:** {item['right']} option candidate; underlying "
                f"confirmed above/below {item['confirmation']['level']} "
                f"${item['confirmation']['price']:.2f}\n"
                f"**Contract:** ${item['strike']}{item['right'][0]} "
                f"{item['expiration']} ({item['dte']} DTE) · "
                f"Delta {item['delta']:+.2f}\n"
                f"**Option entry reference:** ${item['mid']:.2f} mid "
                f"(Bid ${item['bid']:.2f} / Ask ${item['ask']:.2f})\n"
                f"**Max loss:** ~${item['mid'] * 100:,.0f} per contract "
                f"if bought near mid; no position size assumed\n"
                f"**Aggressive-buy premium:** ${premium:,}\n"
                f"**Volume:** {item['volume']:,} vs OI {item['oi']:,} ({item['vol_oi']}x)\n"
                f"**OTM:** {item['otm_pct']}% · **Underlying:** ${item['underlying']:.2f}\n"
                f"**Tape + tech:** {', '.join(item['conditions'])}\n"
                f"**Technical:** {item['tech_note']}\n"
                f"**2R underlying reference target:** "
                f"${item['underlying_target_2r']:.2f}\n"
                f"**Risk:Reward (underlying):** "
                f"1:{item['risk_reward_ratio']:.1f}"
            )
            invalidation = item.get("invalidation")
            if invalidation:
                description += (
                    f"\n**Underlying invalidation:** {invalidation['condition']} "
                    f"${invalidation['price']:.2f} ({invalidation['level']})"
                )
            alert_key_prefix = "flow" if stream == "confirmed" else "daytrade"
            send_alert(
                title=title,
                description=description,
                color=color,
                alert_type="flow",
                alert_key=f"{alert_key_prefix}:{item['symbol']}:{item['expiration']}:{item['strike']}:{item['right']}:{date.today()}",
                symbol=item["symbol"],
                price=item["underlying"],
                payload=item,
                stream=stream,
                cross_stream_key=(
                    f"flow-contract:{item['symbol']}:{item['expiration']}:"
                    f"{item['strike']}:{item['right']}"
                ),
            )

        flush_alert_queue()

    return all_results


if __name__ == "__main__":
    run_flow_scan(send_alerts=True)
