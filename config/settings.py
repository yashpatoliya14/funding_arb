"""
Runtime settings pulled from environment variables (.env). Never hardcode
API keys in source files — use a .env file (see .env.example) and load it
with python-dotenv, which run_dummy.py / run_live.py already do.
"""

import os
import json

# --- Legacy per-exchange symbols (used as FALLBACK if scanner can't reach an exchange) ---
# The engine now discovers symbols dynamically via list_instruments(), but
# these are kept for backward compatibility and as a sanity-check reference.
DELTA_SYMBOL = os.getenv("DELTA_SYMBOL", "BTCUSD")
BINANCE_SYMBOL = os.getenv("BINANCE_SYMBOL", "BTCUSDT")
BYBIT_SYMBOL = os.getenv("BYBIT_SYMBOL", "BTCUSDT")

# --- All exchange pair combinations (funding_leg, hedge_leg) ---
# Every pair is checked concurrently each funding cycle.
EXCHANGE_PAIRS = [
    ("delta", "binance"),
    ("delta", "bybit"),
    ("binance", "bybit"),
]

# --- Position sizing ---
TRADE_QUANTITY = float(os.getenv("TRADE_QUANTITY", "0.001"))   # in base asset units (legacy fallback)
REQUESTED_LEVERAGE = int(os.getenv("REQUESTED_LEVERAGE", "10"))

# Fixed notional per trade in INR — overrides TRADE_QUANTITY when > 0.
# Quantity = FIXED_NOTIONAL_INR / mark_price.
FIXED_NOTIONAL_INR = float(os.getenv("FIXED_NOTIONAL_INR", "10000"))

# --- Multi-coin scanning ---
# JSON list of base asset names to scan, e.g. '["BTC","ETH","SOL"]'
# Empty string or "[]" → use default whitelist from constants.py
_wl_raw = os.getenv("COIN_WHITELIST", "")
COIN_WHITELIST_OVERRIDE = json.loads(_wl_raw) if _wl_raw.strip().startswith("[") else None

SCAN_ALL_COINS = os.getenv("SCAN_ALL_COINS", "false").lower() == "true"

# --- Loop pacing ---
MAIN_LOOP_INTERVAL_SEC = int(os.getenv("MAIN_LOOP_INTERVAL_SEC", "10"))

# --- Strategy economics (overridable per environment) ---
# Minimum net edge (%) after amortized costs required to enter a trade.
# Lower this (even negative) in paper mode to exercise the execution pipeline.
MIN_NET_EDGE_PCT = float(os.getenv("MIN_NET_EDGE_PCT", "0.05"))

# A single funding snapshot rarely beats a full round-trip fee. Funding arb is
# only profitable if the delta-neutral position is HELD across several snapshots
# so entry/exit costs amortize. This is the number of snapshots we expect to
# hold, used both to amortize costs in the entry decision AND to bound the hold.
EXPECTED_HOLD_SNAPSHOTS = int(os.getenv("EXPECTED_HOLD_SNAPSHOTS", "8"))

# While holding, exit early if the net funding edge (%) for the held coin drops
# below this. 0.0 = exit as soon as funding stops being in our favour.
FUNDING_EXIT_THRESHOLD_PCT = float(os.getenv("FUNDING_EXIT_THRESHOLD_PCT", "0.0"))

# --- API credentials ---
DELTA_API_KEY = os.getenv("DELTA_API_KEY", "")
DELTA_API_SECRET = os.getenv("DELTA_API_SECRET", "")

BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")

BYBIT_API_KEY = os.getenv("BYBIT_API_KEY", "")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET", "")

# --- Telegram ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_ENABLED = os.getenv("TELEGRAM_ENABLED", "true").lower() == "true"
