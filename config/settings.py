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
COINSWITCH_SYMBOL = os.getenv("COINSWITCH_SYMBOL", "BTCUSDT")
SHARK_SYMBOL = os.getenv("SHARK_SYMBOL", "BTCUSDT")

# --- All three exchange pair combinations (funding_leg, hedge_leg) ---
# Every pair is checked concurrently each funding cycle.
EXCHANGE_PAIRS = [
    ("shark", "coinswitch"),
    ("coinswitch", "delta"),
    ("delta", "shark"),
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

# --- API credentials (leave blank for dummy/sim mode) ---
DELTA_API_KEY = os.getenv("DELTA_API_KEY", "")
DELTA_API_SECRET = os.getenv("DELTA_API_SECRET", "")

COINSWITCH_API_KEY = os.getenv("COINSWITCH_API_KEY", "")
COINSWITCH_API_SECRET = os.getenv("COINSWITCH_API_SECRET", "")

SHARK_API_KEY = os.getenv("SHARK_API_KEY", "")
SHARK_API_SECRET = os.getenv("SHARK_API_SECRET", "")

# --- Telegram ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_ENABLED = os.getenv("TELEGRAM_ENABLED", "true").lower() == "true"

# --- Simulator starting balance (paper trading only) ---
SIM_STARTING_BALANCE_INR = float(os.getenv("SIM_STARTING_BALANCE_INR", "100000"))
