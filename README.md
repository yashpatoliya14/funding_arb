# Funding Rate Arbitrage Bot — Delta / Binance / Bybit

Cross-exchange funding-rate capture bot with **multi-coin scanning**: discovers
all supported perpetual futures across **all three exchanges**, matches them by
base asset, computes the **full cost-adjusted P&L** for every opportunity, and
executes only when the net profit clears the configured threshold.

Runs all exchange pair combinations concurrently
(delta↔binance, delta↔bybit, binance↔bybit), holds a hedged position
through a funding snapshot, collects the payment, closes.

## Key Features

- **Multi-coin scanning** — automatically discovers BTC, ETH, SOL, and all
  whitelisted coins across exchanges; no hardcoded symbols
- **Full cost model** — both-leg funding, maker fees + 18% GST (Delta only),
  bid/ask spread cost, configurable slippage buffer — all computed before any trade
- **Atomic dual-leg execution** — post-only orders on both exchanges,
  synchronized repricing, leg-risk and basis-drift kill-switches
- **Direct exchange APIs** — uses Binance `premiumIndex` / `exchangeInfo` /
  `fundingInfo` and Bybit V5 `tickers` / `instruments-info` directly for
  funding rates and intervals — no third-party data aggregator needed
- **Live Trading Only** — places real orders directly on the exchanges.

> See [`docs/`](docs/) for architecture, cost model details, and configuration reference.

## What's real vs. what needs your verification

This was built against each exchange's **official published API docs** as of
Sep 2026 (endpoint paths, auth schemes, request/response shapes are real, not
guessed). But some things WILL change between "this compiles" and "this is
safe to run with real money," and only you can close that gap:

1. **API response field names drift between versions.** I wrote each client
   against the documented shape, but exchanges rename fields (`filled_size`
   vs `filledQty` etc.) without always updating docs same-day. Run
   `run_live.py`'s sanity check and read the raw JSON it logs before trusting it.
2. **Fee/leverage numbers in `config/constants.py` are researched, not
   fetched live from your account.** Your actual tier may differ (referral
   codes, volume tiers, promos). Re-check against your account dashboard.
3. **Currency mismatch** — Delta quotes in INR/USD, while Binance/Bybit quote
   in USDT. Cross-exchange basis and spread calculations across different
   quote currencies need FX conversion for accurate P&L in production.

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

## API Endpoints Used

| Exchange | Symbols | Funding Rate | Funding Interval |
|----------|---------|-------------|------------------|
| **Delta** | `/v2/products` | `/v2/tickers/{symbol}` | Fixed 8h (IST schedule) |
| **Binance** | `/fapi/v1/exchangeInfo` | `/fapi/v1/premiumIndex` | `/fapi/v1/fundingInfo` |
| **Bybit** | `/v5/market/instruments-info` | `/v5/market/tickers` | Same instruments API |

> **Note**: Binance and Bybit market data endpoints (symbols, funding rates,
> prices) are **public** and do not require API keys. API keys are only needed
> for trading operations (placing orders, setting leverage, etc.).

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

**Scan funding rates (no trading):**
```bash
python match_coins.py
```
This scans all three exchange pairs and prints the top funding arbitrage
opportunities with full cost breakdown. No API keys needed for Binance/Bybit.

**Live trading:**
```bash
python run_live.py
```
This requires typing a confirmation phrase and runs a sanity check
against your real accounts (read-only calls) before placing a single order.
This launches **3 concurrent engines** (one per exchange pair) via asyncio.
Watch `logs/engine.log` and your Telegram chat to monitor:
- The sanity check passes for all 3 pairs (6 exchange legs total).
- Multi-coin scan discovers matching coins across both exchanges.
- Cost breakdowns in the log show realistic fee/spread/slippage numbers.
- Entries happen ~20 min before 05:30 / 13:30 / 21:30 IST, not at random times.
- Only positive-edge opportunities trigger execution.

## How the pieces fit together

```
run_live.py
        │
        ▼
    MultiPairRunner  ── asyncio.gather() over 3 concurrent engines
        │
        ├── FundingArbEngine (delta ↔ binance)
        ├── FundingArbEngine (delta ↔ bybit)
        └── FundingArbEngine (binance ↔ bybit)
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
    exchanges/{delta,binance,bybit}_client.py
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
│   ├── binance_client.py     # Binance USDⓈ-M Futures REST client
│   ├── bybit_client.py       # Bybit V5 Linear Perpetuals REST client
├── docs/
│   ├── architecture.md       # System architecture & data flow
│   ├── cost_model.md         # Full cost model reference
│   └── configuration.md      # All configuration options
├── engine.py                 # Main orchestrator
├── run_live.py               # Live trading entry point
├── match_coins.py            # Funding rate scanner (read-only)
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

- **Delta leverage endpoint path** (`/v2/products/{symbol}/orders/leverage`)
  should be double-checked against your Delta API version — leverage-setting
  endpoints have moved before across Delta API revisions.
- **Currency mismatch** — Delta uses INR/USD while Binance/Bybit use USDT.
  For accurate cross-exchange spread and basis calculations, an FX conversion
  layer may be needed in production.
- **No partial-fill handling beyond leg-risk close** — if an order partially
  fills, the current logic treats "any fill" as risk-relevant but doesn't
  try to true up the remaining unfilled quantity. For your stated approach
  (close immediately on any mismatch) this is intentional, but worth knowing.
- **Bybit pagination** — the `list_instruments()` client implements
  `nextPageCursor` pagination to handle Bybit's 500+ symbol responses.
  Verify this works correctly with your API access level.
