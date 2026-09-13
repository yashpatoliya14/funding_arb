# Funding Rate Arbitrage Bot — Delta / CoinSwitch / Shark

Cross-exchange funding-rate capture bot with **multi-coin scanning**: discovers
all supported perpetual futures across **all three exchanges**, matches them by
base asset, computes the **full cost-adjusted P&L** for every opportunity, and
executes only when the net profit clears the configured threshold.

Runs all exchange pair combinations concurrently
(shark↔coinswitch, coinswitch↔delta, delta↔shark), holds a hedged position
through a funding snapshot, collects the payment, closes.

## Key Features

- **Multi-coin scanning** — automatically discovers BTC, ETH, SOL, and all
  whitelisted coins across exchanges; no hardcoded symbols
- **Full cost model** — both-leg funding, maker fees + 18% GST, bid/ask
  spread cost, configurable slippage buffer — all computed before any trade
- **Atomic dual-leg execution** — post-only orders on both exchanges,
  synchronized repricing, leg-risk and basis-drift kill-switches
- **Real or paper trading** — identical code path; only the exchange client
  class changes (requirement #11)

> See [`docs/`](docs/) for architecture, cost model details, and configuration reference.

## What's real vs. what needs your verification

This was built against each exchange's **official published API docs** as of
Sep 2026 (endpoint paths, auth schemes, request/response shapes are real, not
guessed). But three things WILL change between "this compiles" and "this is
safe to run with real money," and only you can close that gap:

1. **API response field names drift between versions.** I wrote each client
   against the documented shape, but exchanges rename fields (`filled_size`
   vs `filledQty` etc.) without always updating docs same-day. Run
   `run_dummy.py`'s sanity check and read the raw JSON it logs before trusting it.
2. **Fee/leverage numbers in `config/constants.py` are researched, not
   fetched live from your account.** Your actual tier may differ (referral
   codes, volume tiers, promos). Re-check against your account dashboard.
3. **Shark Exchange is a small, newer platform** (launched ~2025, FIU-AML
   registered but not SEBI/RBI regulated). Independent reviews are mixed on
   execution quality. Start with minimum size here specifically, and confirm
   withdrawals work smoothly before scaling up.

## Setup

```bash
cd funding_arb
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# fill in .env with your real API keys and Telegram bot token
```

### Telegram bot (for requirement #10 notifications)
1. Message `@BotFather` on Telegram → `/newbot` → copy the token into `.env`.
2. Send your new bot any message, then visit
   `https://api.telegram.org/bot<TOKEN>/getUpdates` to find your `chat_id`.

## Configuration

All tuning knobs live in two places:

| Location | What goes here |
|----------|---------------|
| `.env` | API keys, sizing, coin whitelist override, Telegram credentials |
| `config/constants.py` | Fees, leverage caps, slippage, funding schedule, thresholds |

### Key `.env` variables

```bash
# Multi-coin scanning
COIN_WHITELIST=["BTC","ETH","SOL"]   # JSON list, or empty for default
SCAN_ALL_COINS=false                  # true = scan every perpetual

# Position sizing
FIXED_NOTIONAL_INR=10000             # ₹ per trade (0 = use TRADE_QUANTITY)
TRADE_QUANTITY=0.001                  # base asset units (legacy fallback)
REQUESTED_LEVERAGE=10
```

> Full configuration reference: [`docs/configuration.md`](docs/configuration.md)

## Running

**Always start here — forward-test with fake money, real prices, real fill logic:**
```bash
python run_dummy.py
```
This launches **3 concurrent engines** (one per exchange pair) via asyncio.
Watch `logs/engine.log` and your Telegram chat for at least a few funding
cycles (3×/day) before considering live money. Check that:
- The sanity check passes for all 3 pairs (6 exchange legs total).
- Multi-coin scan discovers matching coins across both exchanges.
- Cost breakdowns in the log show realistic fee/spread/slippage numbers.
- Entries happen ~20 min before 05:30 / 13:30 / 21:30 IST, not at random times.
- Only positive-edge opportunities trigger execution.

**Only after that, live trading:**
```bash
python run_live.py
```
This requires typing a confirmation phrase and re-runs the sanity check
against your real accounts (read-only calls) before placing a single order.

## How the pieces fit together

```
run_dummy.py / run_live.py
        │
        ▼
    MultiPairRunner  ── asyncio.gather() over 3 concurrent engines
        │
        ├── FundingArbEngine (shark ↔ coinswitch)
        ├── FundingArbEngine (coinswitch ↔ delta)
        └── FundingArbEngine (delta ↔ shark)
                │
                │ each engine, every cycle:
                ├── core/coin_scanner.py     (discover coins, match, rank by net P&L)
                ├── core/spread_calc.py      (full cost model: fees, spread, slippage)
                ├── core/leverage_sync.py    (common leverage both exchanges)
                ├── core/funding_window.py   (20-min entry lead, post-snapshot close)
                ├── core/order_manager.py    (synced post-only orders, leg-risk, basis-drift)
                ├── core/price_feed.py       (10s poll or websocket)
                └── core/telegram_notify.py  (entries, exits, scans, errors)
                │
                ▼
    exchanges/{delta,coinswitch,shark}_client.py   OR   exchanges/simulator.py
        (identical interface — engine.py never knows which one it's talking to)
```

## Project structure

```
funding_arb/
├── config/
│   ├── constants.py          # Fees, leverage caps, thresholds, whitelist
│   └── settings.py           # Env-driven runtime settings
├── core/
│   ├── coin_scanner.py       # Multi-coin discovery, matching, ranking
│   ├── spread_calc.py        # Full cost model + edge evaluation
│   ├── funding_window.py     # Funding schedule timing
│   ├── leverage_sync.py      # Cross-exchange leverage sync
│   ├── order_manager.py      # Dual-leg order execution
│   ├── price_feed.py         # REST polling + WebSocket feeds
│   └── telegram_notify.py    # Telegram notifications
├── exchanges/
│   ├── base.py               # Abstract ExchangeClient interface
│   ├── delta_client.py       # Delta Exchange India REST client
│   ├── coinswitch_client.py  # CoinSwitch PRO Futures REST client
│   ├── shark_client.py       # Shark Exchange REST client
│   └── simulator.py          # Paper-trading simulator
├── docs/
│   ├── architecture.md       # System architecture & data flow
│   ├── cost_model.md         # Full cost model reference
│   └── configuration.md      # All configuration options
├── engine.py                 # Main orchestrator
├── run_dummy.py              # Paper trading entry point
├── run_live.py               # Live trading entry point
├── .env.example              # Environment variable template
└── requirements.txt          # Python dependencies
```

## Testing

Testing is mandatory before any deployment. To run the complete automated test suite, use pytest:

```bash
pytest -q
```

To run tests with coverage:

```bash
pytest --cov=. --cov-report=term-missing
```

Make sure all tests pass.

## Known gaps to close before scaling size

- **Shark WebSocket**: not wired up (REST polling only) — their docs
  reference a listen-key private WS pattern but the handshake wasn't fully
  available when this was built. Confirm at
  https://docs.sharkexchange.in/#web-sockets and wire it into
  `core/price_feed.py` if you need sub-10s data specifically from Shark.
- **Delta leverage endpoint path** (`/v2/products/{symbol}/orders/leverage`)
  should be double-checked against your Delta API version — leverage-setting
  endpoints have moved before across Delta API revisions.
- **No partial-fill handling beyond leg-risk close** — if an order partially
  fills, the current logic treats "any fill" as risk-relevant but doesn't
  try to true up the remaining unfilled quantity. For your stated approach
  (close immediately on any mismatch) this is intentional, but worth knowing.
- **Instrument discovery endpoints** — the `list_instruments()` endpoints on
  each exchange should be verified against your account. If an exchange
  returns no instruments, the system falls back to the legacy symbol from `.env`.
