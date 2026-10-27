from __future__ import annotations

import math
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


import polars as pl
from rich import print

from src.config import (
    CSP_MAX_DELTA,
    CSP_MAX_DTE,
    CSP_MAX_EXPIRATIONS,
    CSP_MAX_ALERTS,
    CSP_MAX_CANDIDATES_PER_SYMBOL,
    CSP_MAX_OTM_PCT,
    CSP_MAX_SPREAD_PCT,
    CSP_MIN_DELTA,
    CSP_MIN_DTE,
    CSP_MIN_PREMIUM_PCT,
    CSP_NEAR_SUPPORT_PCT,
    CSP_SUPPORT_RANKING_WEIGHT,
    CSP_TICKERS,
    CSP_UPTREND_RANKING_WEIGHT,
)
from src.discord_alerts import send_alert
from src.options_data import (
    get_expirations,
    get_greeks_chain,
    get_option_chain,
    get_underlying_price,
    join_on_contract,
)
from src.swing_signals import build_context, underlying_invalidation
from src.state import log_funnel_scan
from src.util import annualized_yield, as_float, first_present, normalize_right


def _support_levels(tech: dict | None) -> list[tuple[str, float]]:
    if not tech:
        return []
    levels = tech.get("levels") or {}
    out = []
    for name in ("ema_slow", "low_20", "prior_low"):
        value = as_float(levels.get(name))
        if value > 0:
            out.append((name, value))
    return out


def _near_support(strike: float, supports: list[tuple[str, float]]) -> tuple[bool, str]:
    best = None
    for name, level in supports:
        distance = abs(strike - level) / level * 100
        if distance <= CSP_NEAR_SUPPORT_PCT and (best is None or distance < best[0]):
            best = (distance, name, level)
    if not best:
        return False, "no nearby support"
    return True, f"{best[1]} ${best[2]:.2f} ({best[0]:.1f}% away)"


def find_csp_opportunities(
    symbol: str,
    tech: dict | None,
    funnel: dict[str, int] | None = None,
) -> list[dict]:
    counts = funnel if funnel is not None else {}
    counts["available"] = int(tech is not None)
    counts["ownership_entered"] = int(tech is not None)
    counts["ownership"] = 0
    for stage in (
        "candidates", "contract", "max_loss", "otm", "premium", "spread", "delta"
    ):
        counts.setdefault(stage, 0)
    if not tech:
        print(f"  Skip {symbol}: technical context unavailable")
        return []
    if not tech.get("ok_to_own"):
        print(f"  Skip {symbol}: would not want to own shares ({tech.get('trend')})")
        return []
    counts["ownership"] = 1

    price = get_underlying_price(symbol)
    if price is None or price <= 0:
        counts["available"] = 0
        print(f"  No price for {symbol}")
        return []

    print(f"  Underlying: ${price:.2f}")
    opportunities: list[dict] = []
    expirations = [
        exp
        for exp in get_expirations(symbol, max_dte=CSP_MAX_DTE, min_dte=CSP_MIN_DTE)
    ][:CSP_MAX_EXPIRATIONS]
    if not expirations:
        counts["available"] = 0
    supports = _support_levels(tech)

    for exp_str in expirations:
        try:
            dte = (date.fromisoformat(exp_str) - date.today()).days
            chain = get_option_chain(symbol, exp_str)
            greeks = get_greeks_chain(symbol, exp_str, stock_price=price)
            merged = join_on_contract(chain, greeks)
            if merged is None:
                counts["available"] = 0
                continue

            if "right" in merged.columns:
                puts = merged.filter(
                    pl.col("right").cast(pl.String).str.to_uppercase().is_in(["PUT", "P"])
                )
            else:
                puts = merged

            for row in puts.iter_rows(named=True):
                strike = as_float(first_present(row, "strike"))
                bid = as_float(first_present(row, "bid"))
                ask = as_float(first_present(row, "ask"))
                right = normalize_right(first_present(row, "right", default="PUT"))
                if right != "PUT":
                    continue
                counts["candidates"] += 1
                if bid <= 0.01 or strike <= 0 or strike >= price:
                    continue
                counts["contract"] += 1

                premium = (bid + ask) / 2 if ask > 0 else bid
                max_profit = premium * 100
                max_loss = (strike - premium) * 100
                if max_loss <= 0:
                    continue
                counts["max_loss"] += 1
                reward_risk_ratio = max_profit / max_loss
                mid = premium
                spread_pct = ((ask - bid) / mid) * 100 if mid > 0 and ask > 0 else 999
                otm_pct = ((price - strike) / price) * 100
                premium_pct = (premium / strike) * 100
                if not 0 < otm_pct <= CSP_MAX_OTM_PCT:
                    continue
                counts["otm"] += 1
                if premium_pct < CSP_MIN_PREMIUM_PCT:
                    continue
                counts["premium"] += 1
                if spread_pct > CSP_MAX_SPREAD_PCT:
                    continue
                counts["spread"] += 1
                delta_value = first_present(
                    row, "delta", "delta_mid", "delta_ask", "delta_bid"
                )
                try:
                    delta = float(delta_value) if delta_value is not None else None
                except (TypeError, ValueError):
                    delta = None
                if delta is not None and not math.isfinite(delta):
                    delta = None
                iv = as_float(first_present(row, "implied_vol", "iv", "iv_mid", "volatility"))
                near, support_note = _near_support(strike, supports)
                ann = annualized_yield(premium_pct, dte)
                cash = strike * 100

                delta_ok = delta is None or CSP_MIN_DELTA <= delta <= CSP_MAX_DELTA

                if not delta_ok:
                    continue
                counts["delta"] += 1

                score = ann
                if near:
                    score *= CSP_SUPPORT_RANKING_WEIGHT
                if tech and tech.get("trend") == "UPTREND":
                    score *= CSP_UPTREND_RANKING_WEIGHT

                opportunities.append(
                    {
                        "symbol": symbol,
                        "strike": strike,
                        "expiration": exp_str,
                        "dte": dte,
                        "premium": round(premium, 2),
                        "premium_pct": round(premium_pct, 2),
                        "annualized": round(ann, 1),
                        "otm_pct": round(otm_pct, 2),
                        "bid": bid,
                        "ask": ask,
                        "spread_pct": round(spread_pct, 1),
                        "delta": round(delta, 3) if delta is not None else None,
                        "iv": round(iv * 100, 1) if 0 < iv < 5 else (round(iv, 1) if iv else None),
                        "cash_required": cash,
                        "max_profit": round(max_profit, 2),
                        "max_loss": round(max_loss, 2),
                        "reward_risk_ratio": round(reward_risk_ratio, 4),
                        "underlying": price,
                        "support_note": support_note,
                        "near_support": near,
                        "invalidation": underlying_invalidation(
                            tech, "BULLISH", reference_price=price
                        ),
                        "ok_to_own": bool(tech.get("ok_to_own") if tech else True),
                        "trend": tech.get("trend") if tech else "n/a",
                        "score": score,
                    }
                )
        except Exception as e:
            counts["available"] = 0
            print(f"  Error on {exp_str}: {e}")
            continue

    opportunities.sort(key=lambda x: x["score"], reverse=True)
    return opportunities[:CSP_MAX_CANDIDATES_PER_SYMBOL]


def scan_csps(send_to_discord: bool = True) -> list[dict]:
    print("Scanning for Cash-Secured Put opportunities...\n")
    all_ops: list[dict] = []

    for symbol in CSP_TICKERS:
        print(f"Checking {symbol}...")
        counts: dict[str, int] = {}
        try:
            tech = build_context(symbol)
        except Exception as exc:
            print(f"[red]CSP context failed for {symbol}: {exc}[/red]")
            tech = None
        try:
            ops = find_csp_opportunities(symbol, tech, funnel=counts)
        except Exception as exc:
            print(f"[red]CSP scan failed for {symbol}: {exc}[/red]")
            ops = []
            counts["available"] = 0
        funnel_counts = {
            stage: count for stage, count in counts.items() if stage != "available"
        }
        try:
            log_funnel_scan(
                "csp",
                symbol,
                funnel_counts,
                data_available=bool(counts.get("available")),
            )
        except Exception as exc:
            print(f"[red]Could not persist CSP funnel for {symbol}: {exc}[/red]")
        print(
            f"[cyan]CSP {symbol} funnel[/cyan] "
            + " ".join(
                f"{stage}={funnel_counts.get(stage, 0)}"
                for stage in (
                    "candidates", "contract", "max_loss", "otm",
                    "premium", "spread", "delta",
                )
            )
        )
        all_ops.extend(ops)
        for op in ops:
            delta_display = f"{op['delta']:.3f}" if op["delta"] is not None else "unavailable"
            print(
                f"  → {op['symbol']} ${op['strike']}P {op['expiration']} | "
                f"{op['premium_pct']}% / {op['annualized']}% ann | "
                f"OTM {op['otm_pct']}% | Δ {delta_display} | {op['support_note']}"
            )

    if not all_ops:
        print("\nNo CSP opportunities found with current filters.")
        return []

    print(f"\nFound {len(all_ops)} total opportunities.")
    if send_to_discord:
        top = sorted(all_ops, key=lambda x: x["score"], reverse=True)[
            :CSP_MAX_ALERTS
        ]
        for op in top:
            title = f"💰 CSP — {op['symbol']} ${op['strike']}P"
            iv_line = f"**IV:** {op['iv']}\n" if op["iv"] is not None else ""
            delta_line = (
                f"**Delta:** {op['delta']:.3f}\n"
                if op["delta"] is not None
                else "**Delta:** unavailable (Greeks not retrieved)\n"
            )
            strike_text = f"${op['strike']:g}"
            description = (
                f"Suggestion: consider selling this cash-secured put if you are "
                f"OK owning {op['symbol']} shares at {strike_text}.\n"
                f"**Expiration:** {op['expiration']} ({op['dte']} DTE)\n"
                f"**Premium:** ${op['premium']} ({op['premium_pct']}% of strike)\n"
                f"**Max reward:risk at expiration:** "
                f"1:{op['max_loss'] / op['max_profit']:.1f} "
                f"(${op['max_profit']:,.0f} max profit : ${op['max_loss']:,.0f} max loss)\n"
                "**Assignment risk:** If assigned, you may have to buy 100 shares "
                "per contract at the strike. A decline below the strike can cause "
                "losses beyond the premium received; the displayed maximum loss "
                "assumes the shares fall to zero, before fees.\n"
                f"**Annualized:** {op['annualized']}%\n"
                f"**Cash required:** ${op['cash_required']:,.0f}\n"
                f"**OTM:** {op['otm_pct']}% · **Underlying:** ${op['underlying']:.2f}\n"
                f"{delta_line}{iv_line}"
                f"**Support:** {op['support_note']}\n"
                f"**Own-shares filter:** {op['trend']} · ok_to_own={op['ok_to_own']}\n"
                f"**Bid/Ask:** ${op['bid']} / ${op['ask']} ({op['spread_pct']}%)"
            )
            invalidation = op.get("invalidation")
            if invalidation:
                description += (
                    f"\n**Underlying invalidation:** below "
                    f"${invalidation['price']:.2f} ({invalidation['level']})"
                )
            send_alert(
                title=title,
                description=description,
                color=0x0099FF,
                alert_type="csp",
                alert_key=f"csp:{op['symbol']}:{op['expiration']}:{op['strike']}",
                symbol=op["symbol"],
                price=op["underlying"],
                payload=op,
            )
    return all_ops


if __name__ == "__main__":
    scan_csps(send_to_discord=True)
