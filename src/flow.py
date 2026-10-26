from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


import polars as pl
from rich import print

from src.config import (
    FLOW_MAX_ALERTS_PER_SCAN,
    FLOW_MAX_DTE,
    FLOW_MAX_EXPIRATIONS,
    FLOW_MAX_SPREAD_PCT,
    FLOW_MIN_AGGRESSOR_PCT,
    FLOW_MIN_OI,
    FLOW_MIN_PREMIUM,
    FLOW_MIN_VOLUME,
    FLOW_STRIKE_RANGE_PCT,
    FLOW_TICKERS,
    FLOW_VOLUME_OI_RATIO,
)
from src.discord_alerts import send_alert
from src.options_data import (
    get_expirations,
    get_oi_chain,
    get_ohlc_chain,
    get_option_chain,
    get_trade_quotes,
    get_underlying_price,
    join_on_contract,
)
from src.swing_signals import build_context
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


def scan_symbol(symbol: str, tech: dict | None) -> list[dict]:
    price = get_underlying_price(symbol)
    if not price:
        print(f"  No price for {symbol}")
        return []

    print(f"\nScanning {symbol} @ ${price:.2f}")
    results: list[dict] = []
    expirations = get_expirations(symbol, max_dte=FLOW_MAX_DTE)[:FLOW_MAX_EXPIRATIONS]

    for exp in expirations:
        quotes = get_option_chain(symbol, exp)
        oi_df = get_oi_chain(symbol, exp)
        ohlc = get_ohlc_chain(symbol, exp)
        merged = join_on_contract(quotes, oi_df, ohlc)
        if merged is None:
            continue

        candidates: list[dict] = []
        for row in merged.iter_rows(named=True):
            bid = as_float(first_present(row, "bid"))
            ask = as_float(first_present(row, "ask"))
            strike = as_float(first_present(row, "strike"))
            right = normalize_right(first_present(row, "right"))
            oi = as_int(first_present(row, "open_interest", "oi"))
            volume = as_int(first_present(row, "volume", "count"))
            last = as_float(first_present(row, "close", "price", "last"))
            mid = (bid + ask) / 2 if bid > 0 and ask > 0 else last
            if mid <= 0 or strike <= 0 or not right:
                continue

            otm_pct = ((strike - price) / price) * 100 if right == "CALL" else ((price - strike) / price) * 100
            if abs(otm_pct) > FLOW_STRIKE_RANGE_PCT:
                continue

            spread_pct = ((ask - bid) / mid) * 100 if mid > 0 and ask > 0 else 999
            vol_oi = (volume / oi) if oi else (volume if volume else 0)
            premium_est = volume * mid * 100

            if (
                volume >= FLOW_MIN_VOLUME
                and oi >= FLOW_MIN_OI
                and vol_oi >= FLOW_VOLUME_OI_RATIO
                and spread_pct <= FLOW_MAX_SPREAD_PCT
            ):
                candidates.append(
                    {
                        "symbol": symbol,
                        "right": right,
                        "strike": strike,
                        "expiration": exp,
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

        candidates.sort(key=lambda x: x["premium_est"], reverse=True)
        for item in candidates[:6]:
            trades = get_trade_quotes(symbol, exp, item["strike"], item["right"])
            agg = _classify_aggressor(trades)
            item.update(agg)
            if (
                agg["side"] != "BUY"
                or agg["buy_premium"] < FLOW_MIN_PREMIUM
            ):
                continue

            aligned, tech_note = _technical_confluence(tech, item["right"])
            if not aligned:
                continue
            item["tech_aligned"] = aligned
            item["tech_note"] = tech_note
            item["conditions"] = [
                f"aggressive buy premium ${int(item['buy_premium']):,}",
                f"vol/OI {item['vol_oi']}x",
                f"aggressor BUY {item['buy_pct']:.0%}",
                "technical confluence",
            ]
            results.append(item)

    results.sort(key=lambda x: x["buy_premium"], reverse=True)
    return results[:4]


def run_flow_scan(send_alerts: bool = True) -> list[dict]:
    print("Running options flow scan (trades + aggressor + vol/OI)...\n")
    all_results: list[dict] = []
    tech_cache: dict[str, dict | None] = {}

    for symbol in FLOW_TICKERS:
        if symbol not in tech_cache:
            try:
                tech_cache[symbol] = build_context(symbol)
            except Exception:
                tech_cache[symbol] = None
        res = scan_symbol(symbol, tech_cache[symbol])
        all_results.extend(res)
        for r in res:
            tag = " + TECH" if r["tech_aligned"] else ""
            print(
                f"  {r['symbol']} {r['strike']}{r['right'][0]} {r['expiration']} | "
                f"BUY {r['buy_pct']:.0%} | prem ${int(r['buy_premium']):,} | "
                f"vol/OI {r['vol_oi']}x{tag}"
            )

    if send_alerts and all_results:
        ranked = sorted(
            all_results,
            key=lambda x: (
                int(x.get("tech_aligned") or 0),
                x["buy_premium"],
            ),
            reverse=True,
        )[:FLOW_MAX_ALERTS_PER_SCAN]
        for item in ranked:
            color = 0x00FF00 if item["right"] == "CALL" else 0xFF0000
            emoji = "🟢" if item["right"] == "CALL" else "🔴"
            boost = " · CONFLUENCE" if item["tech_aligned"] else ""
            title = f"{emoji} Aggressive {item['right']} buy — {item['symbol']} {item['strike']}{item['right'][0]}{boost}"
            premium = int(item["buy_premium"])
            description = (
                f"**Type:** {item['right']} BUY (aggressor {item['buy_pct']:.0%})\n"
                f"**Strike / Exp:** ${item['strike']} / {item['expiration']}\n"
                f"**Premium:** ${premium:,} (est tape ${int(item['premium_est']):,})\n"
                f"**Volume:** {item['volume']:,} vs OI {item['oi']:,} ({item['vol_oi']}x)\n"
                f"**Mid:** ${item['mid']} (Bid {item['bid']} / Ask {item['ask']})\n"
                f"**OTM:** {item['otm_pct']}% · **Underlying:** ${item['underlying']:.2f}\n"
                f"**Tape + tech:** {', '.join(item['conditions'])}\n"
                f"**Technical:** {item['tech_note']}"
            )
            send_alert(
                title=title,
                description=description,
                color=color,
                alert_type="flow",
                alert_key=f"flow:{item['symbol']}:{item['expiration']}:{item['strike']}:{item['right']}:{date.today()}",
                symbol=item["symbol"],
                price=item["underlying"],
                payload=item,
            )

    return all_results


if __name__ == "__main__":
    run_flow_scan(send_alerts=True)
