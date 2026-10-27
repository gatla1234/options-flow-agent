from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import polars as pl
from rich import print

from src.config import (
    EMA_FAST,
    EMA_SLOW,
    LEVEL_NEAR_PCT,
    RS_LOOKBACK,
    SWING_BENCHMARKS,
    SWING_TICKERS,
    VOLUME_DECENT_MULTIPLIER,
    VOLUME_LOOKBACK,
    VOLUME_MULTIPLIER,
)
from src.data import ET, get_daily_bars, get_live_price
from src.discord_alerts import send_alert
from src.market import is_rth
from src.state import log_funnel_scan
from src.util import as_float, pct_change


def _prep(df: pl.DataFrame) -> pl.DataFrame | None:
    needed = {"close", "high", "low", "volume"}
    if df is None or df.is_empty() or not needed.issubset(set(df.columns)):
        return None
    sort_col = "created" if "created" in df.columns else df.columns[0]
    df = df.sort(sort_col)
    if len(df) < VOLUME_LOOKBACK + 5:
        return None
    return df.with_columns(
        [
            pl.col("close").ewm_mean(span=EMA_FAST).alias("ema_fast"),
            pl.col("close").ewm_mean(span=EMA_SLOW).alias("ema_slow"),
            pl.col("volume").shift(1).rolling_mean(window_size=VOLUME_LOOKBACK).alias("avg_volume"),
            pl.col("high").shift(1).rolling_max(window_size=20).alias("high_20"),
            pl.col("low").shift(1).rolling_min(window_size=20).alias("low_20"),
        ]
    )


def _bar_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _latest_completed_bar(df: pl.DataFrame) -> dict | None:
    if "created" not in df.columns:
        return None
    today = datetime.now(ET).date()
    for row in reversed(df.to_dicts()):
        bar_date = _bar_date(row.get("created"))
        if bar_date is not None and bar_date < today:
            return row
    return None


def _near(price: float, level: float | None, pct: float = LEVEL_NEAR_PCT) -> bool:
    if not level:
        return False
    return abs((price - level) / level) * 100 <= pct


def _return_pct(df: pl.DataFrame, lookback: int) -> float | None:
    if len(df) <= lookback:
        return None
    now = as_float(df.tail(1)["close"][0])
    then = as_float(df.tail(lookback + 1).head(1)["close"][0])
    return pct_change(now, then)


def underlying_invalidation(
    ctx: dict | None, direction: str, reference_price: float | None = None
) -> dict | None:
    if not ctx:
        return None

    price = reference_price or as_float(ctx.get("live"))
    if price <= 0:
        return None

    levels = ctx.get("levels") or {}
    if direction == "BULLISH":
        candidates = [
            (name, as_float(levels.get(name)))
            for name in ("ema_slow", "prior_low", "low_20")
        ]
        valid = [(name, level) for name, level in candidates if 0 < level < price]
        if not valid:
            return None
        name, level = max(valid, key=lambda item: item[1])
        return {"price": round(level, 2), "level": name, "condition": "below"}

    if direction == "BEARISH":
        candidates = [
            (name, as_float(levels.get(name)))
            for name in ("ema_slow", "prior_high", "high_20")
        ]
        valid = [(name, level) for name, level in candidates if level > price]
        if not valid:
            return None
        name, level = min(valid, key=lambda item: item[1])
        return {"price": round(level, 2), "level": name, "condition": "above"}

    return None


def confirmed_flow_level(
    ctx: dict | None, right: str, price: float
) -> dict | None:
    if not ctx or price <= 0:
        return None

    levels = ctx.get("levels") or {}
    if right == "CALL":
        resistances = [
            (name, as_float(levels.get(name)))
            for name in ("prior_high", "high_20")
        ]
        valid = [(name, level) for name, level in resistances if 0 < level < price]
        if not valid:
            return None
        name, level = max(valid, key=lambda item: item[1])
        return {"price": round(level, 2), "level": name, "direction": "BULLISH"}

    if right == "PUT":
        supports = [
            (name, as_float(levels.get(name)))
            for name in ("prior_low", "low_20")
        ]
        valid = [(name, level) for name, level in supports if level > price]
        if not valid:
            return None
        name, level = min(valid, key=lambda item: item[1])
        return {"price": round(level, 2), "level": name, "direction": "BEARISH"}

    return None


def build_context(symbol: str) -> dict | None:
    df = _prep(get_daily_bars(symbol))
    if df is None:
        return None

    last = df.tail(1).to_dicts()[0]
    prev = df.tail(2).head(1).to_dicts()[0]
    during_rth = is_rth()
    live = get_live_price(symbol, require_current_day=during_rth)
    if live is None:
        if during_rth:
            return None
        live = as_float(last["close"])

    avg_volume = as_float(last.get("avg_volume"))
    volume = as_float(last.get("volume"))
    volume_ratio = (volume / avg_volume) if avg_volume else 0.0
    ema_fast = as_float(last.get("ema_fast"))
    ema_slow = as_float(last.get("ema_slow"))

    levels = {
        "prior_high": as_float(prev.get("high")),
        "prior_low": as_float(prev.get("low")),
        "prior_close": as_float(prev.get("close")),
        "ema_fast": ema_fast,
        "ema_slow": ema_slow,
        "high_20": as_float(last.get("high_20")),
        "low_20": as_float(last.get("low_20")),
    }
    prior_completed_bar = _latest_completed_bar(df)
    nearby = [name for name, level in levels.items() if _near(live, level)]

    benchmark = SWING_BENCHMARKS.get(symbol)
    rs = None
    if benchmark:
        bench_df = _prep(get_daily_bars(benchmark))
        if bench_df is not None:
            sym_ret = _return_pct(df, RS_LOOKBACK)
            bench_ret = _return_pct(bench_df, RS_LOOKBACK)
            if sym_ret is not None and bench_ret is not None:
                rs = round(sym_ret - bench_ret, 2)

    trend = "NEUTRAL"
    if live > ema_fast > ema_slow:
        trend = "UPTREND"
    elif live < ema_fast < ema_slow:
        trend = "DOWNTREND"

    ok_to_own = trend != "DOWNTREND" and (levels["low_20"] == 0 or live >= levels["low_20"] * 0.97)

    return {
        "symbol": symbol,
        "close": as_float(last["close"]),
        "live": live,
        "ema_fast": round(ema_fast, 2),
        "ema_slow": round(ema_slow, 2),
        "volume": int(volume),
        "avg_volume": int(avg_volume),
        "volume_ratio": round(volume_ratio, 2),
        "levels": levels,
        "prior_day_high": (
            as_float(prior_completed_bar.get("high"))
            if prior_completed_bar
            else None
        ),
        "prior_day_low": (
            as_float(prior_completed_bar.get("low"))
            if prior_completed_bar
            else None
        ),
        "nearby_levels": nearby,
        "rs": rs,
        "benchmark": benchmark,
        "trend": trend,
        "ok_to_own": ok_to_own,
    }


def calculate_signals(
    ctx: dict, funnel: dict[str, int] | None = None
) -> dict | None:
    live = ctx["live"]
    ema_fast = ctx["ema_fast"]
    ema_slow = ctx["ema_slow"]
    volume_ratio = ctx["volume_ratio"]
    rs = ctx["rs"]
    nearby = ctx["nearby_levels"]
    levels = ctx["levels"]
    strong_volume = volume_ratio >= VOLUME_MULTIPLIER
    decent_volume = volume_ratio >= VOLUME_DECENT_MULTIPLIER
    bullish_rs = rs is None or rs >= 0
    bearish_rs = rs is None or rs <= 0

    distance_to_slow = ((live - ema_slow) / ema_slow) * 100 if ema_slow else 0
    uptrend_alignment = live > ema_fast > ema_slow
    downtrend_alignment = live < ema_fast < ema_slow
    support_price_confirmation = (
        -4.0 < distance_to_slow < 1.0
        and live > ema_fast * 0.98
        and (nearby or bullish_rs)
    )
    breakdown_price_confirmation = live < (levels["prior_low"] or live)

    if funnel is not None:
        paths = (
            (
                uptrend_alignment,
                strong_volume,
                bullish_rs,
                True,
                min(ema_slow, levels["prior_low"] or ema_slow),
                "BULLISH",
            ),
            (
                True,
                decent_volume,
                True,
                support_price_confirmation,
                min(ema_slow * 0.98, levels["low_20"] or ema_slow),
                "BULLISH",
            ),
            (
                downtrend_alignment,
                strong_volume,
                bearish_rs,
                True,
                max(ema_slow, levels["prior_high"] or ema_slow),
                "BEARISH",
            ),
            (
                ctx["trend"] != "UPTREND",
                strong_volume,
                True,
                breakdown_price_confirmation,
                levels["prior_high"] or ema_fast,
                "BEARISH",
            ),
        )
        stage_counts = {
            "candidates": len(paths),
            "trend": sum(1 for path in paths if path[0]),
            "volume": sum(1 for path in paths if path[0] and path[1]),
            "relative_strength": sum(
                1 for path in paths if path[0] and path[1] and path[2]
            ),
            "confirmation": sum(
                1
                for path in paths
                if path[0] and path[1] and path[2] and path[3]
            ),
            "risk_reward": sum(
                1
                for path in paths
                if path[0]
                and path[1]
                and path[2]
                and path[3]
                and (
                    live - path[4] > 0
                    if path[5] == "BULLISH"
                    else path[4] - live > 0
                )
            ),
        }
        funnel.update(stage_counts)

    if uptrend_alignment and strong_volume and bullish_rs:
        signal_type, direction = "STRONG UPTREND", "BULLISH"
        invalidation = min(ema_slow, levels["prior_low"] or ema_slow)
    elif support_price_confirmation and decent_volume:
        signal_type, direction = "SUPPORT BOUNCE", "BULLISH"
        invalidation = min(ema_slow * 0.98, levels["low_20"] or ema_slow)
    elif downtrend_alignment and strong_volume and bearish_rs:
        signal_type, direction = "STRONG DOWNTREND", "BEARISH"
        invalidation = max(ema_slow, levels["prior_high"] or ema_slow)
    elif (
        breakdown_price_confirmation
        and strong_volume
        and ctx["trend"] != "UPTREND"
    ):
        signal_type, direction = "BREAKDOWN", "BEARISH"
        invalidation = levels["prior_high"] or ema_fast
    else:
        return None

    risk_distance = (
        live - invalidation if direction == "BULLISH" else invalidation - live
    )
    if risk_distance <= 0:
        return None
    target = live + 2 * risk_distance if direction == "BULLISH" else live - 2 * risk_distance

    confluence = []
    if nearby:
        confluence.append("levels: " + ", ".join(nearby))
    if rs is not None:
        confluence.append(f"RS vs {ctx['benchmark']}: {rs:+.1f}%")
    confluence.append(f"volume {volume_ratio:.2f}x")
    confluence.append(ctx["trend"].lower())

    return {
        **ctx,
        "type": signal_type,
        "direction": direction,
        "invalidation": round(float(invalidation), 2),
        "target_2r": round(float(target), 2),
        "risk_reward_ratio": 2.0,
        "confluence": confluence,
    }


def format_swing_description(signal: dict) -> str:
    rs_line = ""
    if signal.get("rs") is not None:
        rs_line = f"**RS vs {signal['benchmark']}:** {signal['rs']:+.1f}% (5d)\n"
    levels = ", ".join(signal["nearby_levels"]) if signal["nearby_levels"] else "none nearby"
    suggestion = (
        f"Suggestion: consider long {signal['symbol']}."
        if signal["direction"] == "BULLISH"
        else f"Suggestion: consider short {signal['symbol']}."
    )
    return (
        f"{suggestion}\n"
        f"**Direction:** {signal['direction']}\n"
        f"**Price:** ${signal['live']:.2f} (EOD ${signal['close']:.2f})\n"
        f"**9 EMA:** ${signal['ema_fast']} · **21 EMA:** ${signal['ema_slow']}\n"
        f"**Volume:** {signal['volume']:,} ({signal['volume_ratio']}x avg)\n"
        f"{rs_line}"
        f"**Nearby levels:** {levels}\n"
        f"**Confluence:** {'; '.join(signal['confluence'])}\n"
        f"**Underlying invalidation:** ${signal['invalidation']}\n"
        f"**2R underlying reference target:** "
        f"${signal['target_2r']:.2f}\n"
        f"**Risk:Reward (underlying):** 1:2.0"
    )


def check_swing_tickers(send_alerts: bool = True) -> list[dict]:
    print("Checking swing tickers...\n")
    signals: list[dict] = []

    for symbol in SWING_TICKERS:
        counts: dict[str, int] = {}
        data_available = False
        funnel_persist_attempted = False
        try:
            ctx = build_context(symbol)
            if not ctx:
                print(f"{symbol}: not enough data")
                signal = None
            else:
                data_available = True
                signal = calculate_signals(ctx, funnel=counts)
            print(f"{symbol}:")
            if ctx:
                print(f"  Live:       ${ctx['live']:.2f}")
                print(f"  9/21 EMA:   ${ctx['ema_fast']} / ${ctx['ema_slow']}")
                print(f"  Volume:     {ctx['volume_ratio']}x  Trend: {ctx['trend']}")
                print(f"  RS:         {ctx['rs']}")
            print(f"  Signal:     {signal['type'] if signal else 'No'}")
            print("-" * 40)

            try:
                funnel_persist_attempted = True
                log_funnel_scan(
                    "swing",
                    symbol,
                    counts,
                    data_available=data_available,
                )
            except Exception as exc:
                print(f"[red]Could not persist swing funnel for {symbol}: {exc}[/red]")
            print(
                f"[cyan]Swing {symbol} funnel[/cyan] "
                f"candidates={counts.get('candidates', 0)} "
                f"trend={counts.get('trend', 0)} "
                f"volume={counts.get('volume', 0)} "
                f"relative-strength={counts.get('relative_strength', 0)} "
                f"confirmation={counts.get('confirmation', 0)} "
                f"risk/reward={counts.get('risk_reward', 0)}"
            )
            if not signal:
                continue
            signals.append(signal)
            if send_alerts:
                color = 0x00FF00 if signal["direction"] == "BULLISH" else 0xFF0000
                emoji = "🚀" if signal["direction"] == "BULLISH" else "📉"
                send_alert(
                    title=f"{emoji} {signal['type']} — {symbol}",
                    description=format_swing_description(signal),
                    color=color,
                    alert_type="swing",
                    alert_key=f"swing:{symbol}:{signal['type']}:{signal['direction']}",
                    symbol=symbol,
                    price=signal["close"],
                    payload=signal,
                )
        except Exception as e:
            print(f"Error checking {symbol}: {e}")
            if not funnel_persist_attempted:
                try:
                    log_funnel_scan("swing", symbol, counts, data_available=False)
                except Exception as log_exc:
                    print(f"[red]Could not persist swing funnel for {symbol}: {log_exc}[/red]")

    return signals


if __name__ == "__main__":
    check_swing_tickers()
