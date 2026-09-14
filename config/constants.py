"""
Static constants: fees, leverage caps, base URLs, funding schedules.

WHY THIS IS A SEPARATE FILE (your requirement #3/#4):
Fees/leverage rarely change intraday. Fetching them from the exchange on every
10-second loop tick wastes an API call and adds latency for no benefit. We look
them up ONCE here (researched from official docs / fee pages as of Sep 2026)
and the engine reads from this file instead.

⚠️ VERIFY BEFORE GOING LIVE:
Fee tiers change with your 30-day volume, and promos/referral codes change
the effective rate. Before running with real money, log into each exchange's
fee-schedule page (or call the account/fee endpoint once, manually) and correct
the numbers below. Wrong fee assumptions here silently eat your funding edge.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class FeeSchedule:
    maker: float   # as a fraction, e.g. 0.0002 = 0.02%
    taker: float
    source_note: str


# ---------------------------------------------------------------------------
# Fees (researched Sep 2026 — RECONFIRM against your account's actual tier)
# ---------------------------------------------------------------------------
FEES = {
    "delta": FeeSchedule(
        maker=0.0002,   # 0.02%
        taker=0.0005,   # 0.05%
        source_note="Delta Exchange India futures/perp fee schedule, Sep 2026",
    ),
    "coinswitch": FeeSchedule(
        maker=0.0002,   # 0.02%
        taker=0.0005,   # 0.05%
        source_note="CoinSwitch PRO Futures default tier, Sep 2026 — tiered by volume, reconfirm",
    ),
    "shark": FeeSchedule(
        maker=0.00016,  # 0.016%
        taker=0.00040,  # 0.040%
        source_note="Shark Exchange advertised futures fees, Sep 2026",
    ),
}

# GST is charged ON TOP of trading fees for Indian crypto exchanges (18%).
# Delta explicitly charges 18% GST on fees; assume the same applies elsewhere
# unless you confirm otherwise for CoinSwitch/Shark.
GST_ON_FEES = 0.18

# ---------------------------------------------------------------------------
# Leverage caps per exchange (platform-wide ceiling; per-symbol may be lower —
# always cross-check with instrument_info / exchangeInfo at runtime, since a
# specific pair like an altcoin usually has a much lower cap than the
# headline "up to Nx" marketing number).
# ---------------------------------------------------------------------------
MAX_LEVERAGE = {
    "delta": 100,       # up to 100x on majors, less on alts — check instrument_info
    "coinswitch": 25,   # CoinSwitch PRO Futures — reconfirm, some sources say up to 50x
    "shark": 100,       # Shark advertises up to 150x on some pairs — check exchangeInfo
}

# ---------------------------------------------------------------------------
# Base URLs (from official docs, Sep 2026)
# ---------------------------------------------------------------------------
BASE_URLS = {
    "delta": {
        "rest": "https://api.india.delta.exchange",
        "ws": "wss://socket.india.delta.exchange",
    },
    "coinswitch": {
        "rest": "https://coinswitch.co",
        "ws": "wss://ws.coinswitch.co",
        "ws_namespace": "/exchange_2",
        "ws_path": "/pro/realtime-rates-socket/futures/exchange_2",
    },
    "shark": {
        "rest": "https://api.sharkexchange.in",
        # Shark's public websocket (using Socket.IO).
        "ws": "https://fawss.sharkexchange.in/",
    },
}

# ---------------------------------------------------------------------------
# Funding schedule
# ---------------------------------------------------------------------------
# Delta Exchange: funding is EXCHANGED (not just accrued) at fixed snapshot
# times, effective 8-Sep-2025. You only need to be in position AT the
# snapshot instant — no need for second-by-second precision on entry.
DELTA_FUNDING_TIMES_IST = ["05:30", "13:30", "21:30"]

# CoinSwitch / Shark: standard 8-hour perpetual funding cycle is industry
# default (00:00 / 08:00 / 16:00 UTC = 05:30 / 13:30 / 21:30 IST) — but
# CONFIRM per-symbol via each exchange's instrument/contract-info endpoint,
# since some alt pairs use shorter intervals.
DEFAULT_FUNDING_TIMES_IST = ["05:30", "13:30", "21:30"]

# ---------------------------------------------------------------------------
# Strategy thresholds (tune these — they are starting points, not gospel)
# ---------------------------------------------------------------------------
ENTRY_LEAD_MINUTES = 20          # enter this many minutes before funding snapshot
POST_SNAPSHOT_CLOSE_DELAY_SEC = 60   # wait this long after snapshot, then close both legs
PRICE_POLL_INTERVAL_SEC = 10     # requirement #1
ORDER_REPRICE_INTERVAL_SEC = 10  # requirement #7/#8
MAX_REPRICES = 10
MAX_BASIS_DRIFT_PCT = 0.15       # kill-switch: close both legs if basis moves against you by this %
MIN_NET_EDGE_PCT = 0.05          # don't enter unless funding edge clears round-trip cost by this margin (%)

# ---------------------------------------------------------------------------
# Slippage & execution cost parameters
# ---------------------------------------------------------------------------
SLIPPAGE_BPS_PER_LEG = 0.0002   # 0.02% per leg — conservative for majors, may need tuning for alts

# ---------------------------------------------------------------------------
# Multi-coin scanning configuration
# ---------------------------------------------------------------------------
# Whitelist of base assets to scan. If empty AND SCAN_ALL_COINS is False,
# falls back to BTC only for backward compatibility.
COIN_WHITELIST = ["BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "AVAX", "LINK", "DOT", "MATIC"]

# If True, ignores COIN_WHITELIST and scans every perpetual the exchange offers.
SCAN_ALL_COINS = False

# ---------------------------------------------------------------------------
# Position sizing for multi-coin mode
# ---------------------------------------------------------------------------
# Fixed notional per trade in INR. Quantity is computed as notional / mark_price.
# Set to 0 to fall back to the legacy TRADE_QUANTITY (fixed base-asset units).
FIXED_NOTIONAL_INR = 10000

# ---------------------------------------------------------------------------
# Volume filter (minimum 24h volume to consider a coin tradeable)
# ---------------------------------------------------------------------------
MIN_VOLUME_24H = 0   # 0 = no filter; set to e.g. 1_000_000 to skip illiquid alts

