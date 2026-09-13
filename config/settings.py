"""
Runtime settings pulled from environment variables (.env). Never hardcode
API keys in source files — use a .env file (see .env.example) and load it
with python-dotenv, which run_dummy.py / run_live.py already do.
"""

import os

# --- Symbols to trade on each exchange (their naming conventions differ) ---
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
TRADE_QUANTITY = float(os.getenv("TRADE_QUANTITY", "0.001"))   # in base asset units
REQUESTED_LEVERAGE = int(os.getenv("REQUESTED_LEVERAGE", "10"))

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
