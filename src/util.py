from __future__ import annotations

from typing import Any


def as_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def as_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def first_present(row: dict, *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in row and row[key] is not None:
            return row[key]
    return default


def normalize_right(value: Any) -> str:
    raw = str(value or "").strip().upper()
    if raw in {"C", "CALL"}:
        return "CALL"
    if raw in {"P", "PUT"}:
        return "PUT"
    return raw


def pct_change(new: float, old: float) -> float | None:
    if old in (0, None) or new is None:
        return None
    return ((new - old) / old) * 100


def annualized_yield(premium_pct: float, dte: int) -> float:
    if dte <= 0:
        return 0.0
    return premium_pct * (365.0 / dte)
