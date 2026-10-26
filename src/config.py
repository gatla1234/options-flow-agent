# src/config.py

# ===== Universe =====
FLOW_TICKERS = [
    "SPY", "QQQ", "IWM", "SMH", "IBIT", "GLD",
]
SWING_TICKERS = ["TQQQ", "TNA", "SOXL", "BITX"]
CSP_TICKERS = ["BITX", "SOXL", "TQQQ"]

# Relative-strength benchmarks for leveraged names
SWING_BENCHMARKS = {
    "SOXL": "SMH",
    "TQQQ": "QQQ",
    "BITX": "IBIT",
    "TNA": "IWM",
}

# ===== Swing Trade =====
LOOKBACK_DAYS = 80
VOLUME_LOOKBACK = 20
VOLUME_MULTIPLIER = 1.5
EMA_FAST = 9
EMA_SLOW = 21
RS_LOOKBACK = 5
LEVEL_NEAR_PCT = 1.5

# ===== Cash-Secured Put =====
CSP_MAX_DTE = 45
CSP_MIN_DTE = 5
CSP_MAX_EXPIRATIONS = 4
CSP_MIN_PREMIUM_PCT = 0.4
CSP_MAX_OTM_PCT = 12.0
CSP_MIN_DELTA = -0.35
CSP_MAX_DELTA = -0.15
CSP_MAX_SPREAD_PCT = 10.0
CSP_NEAR_SUPPORT_PCT = 3.0

# ===== Options Flow =====
# Only recommend contracts in the selected DTE and absolute-delta range.
FLOW_MAX_DTE = 21
FLOW_MIN_DTE = 7
FLOW_MAX_EXPIRATIONS = 2
FLOW_MIN_DELTA = 0.40
FLOW_MAX_DELTA = 0.55
FLOW_STRIKE_RANGE_PCT = 12
FLOW_MIN_PREMIUM = 30_000
FLOW_MIN_VOLUME = 200
FLOW_VOLUME_OI_RATIO = 1.5
FLOW_MIN_OI = 50
FLOW_MIN_AGGRESSOR_PCT = 0.60
FLOW_MAX_SPREAD_PCT = 12.0
FLOW_POLL_SECONDS = 45
FLOW_MAX_ALERTS_PER_SCAN = 4

# Underlying quotes used during regular trading hours must be recent.
FINNHUB_MAX_QUOTE_AGE_SECONDS = 300

# ===== Scheduled market reports =====
REPORT_MORNING_TIME = "09:00"
REPORT_EOD_TIME = "16:30"
REPORT_FLOW_TICKERS = ["IBIT", "GLD", "SLV", "USO"]
REPORT_GAP_ALERT_PCT = 2.0
CSP_GAP_DOWN_ALERT_PCT = 1.5
FOLLOWUP_MAX_AGE_DAYS = 45
FOLLOWUP_NEWS_LOOKBACK_HOURS = 24
FOLLOWUP_PRICE_POLL_SECONDS = 300

# ===== Alerts / runtime =====
ALERT_COOLDOWN_HOURS = {
    "flow": 4,
    "swing": 20,
    "csp": 20,
    "health": 12,
}
MAX_DISCORD_ALERTS_PER_MINUTE = 8
