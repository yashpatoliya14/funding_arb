# Funding Rate Arbitrage Bot — Delta / CoinSwitch / Shark

Cross-exchange funding-rate capture bot: checks **all three exchange pair
combinations** concurrently (shark↔coinswitch, coinswitch↔delta, delta↔shark),
holds a hedged position across each qualifying pair through a funding snapshot,
collects the funding payment, closes.

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
# fill in .env with your real API keys, symbols, and Telegram bot token
```

### Telegram bot (for requirement #10 notifications)
1. Message `@BotFather` on Telegram → `/newbot` → copy the token into `.env`.
2. Send your new bot any message, then visit
   `https://api.telegram.org/bot<TOKEN>/getUpdates` to find your `chat_id`.

## Running

**Always start here — forward-test with fake money, real prices, real fill logic:**
```bash
python run_dummy.py
```
This launches **3 concurrent engines** (one per exchange pair) via asyncio.
Watch `logs/engine.log` and your Telegram chat for at least a few funding
cycles (3x/day) before considering live money. Check that:
- The sanity check passes for all 3 pairs (6 exchange legs total).
- Each pair's log lines are prefixed with its label (e.g. `[shark↔coinswitch]`).
- Entries happen ~20 min before 05:30 / 13:30 / 21:30 IST, not at random times.
- Simulated P&L direction matches what you'd expect given the funding sign.

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
                │ each engine has:
                ├── core/price_feed.py      (req #1, #13 — 10s poll or websocket)
                ├── core/spread_calc.py     (req #2, #6 — fee/tax-adjusted edge)
                ├── core/leverage_sync.py   (req #5 — common leverage both exchanges)
                ├── core/funding_window.py  (req #9 — 20-min entry lead, post-snapshot close)
                ├── core/order_manager.py   (req #7, #8 — synced post-only orders, leg-risk, basis-drift)
                └── core/telegram_notify.py (req #10)
                │
                ▼
    exchanges/{delta,coinswitch,shark}_client.py   OR   exchanges/simulator.py
        (identical interface — engine.py never knows which one it's talking to)
```

## Known gaps to close before scaling size

- **Shark WebSocket**: not wired up (REST polling only) — their docs
  reference a listen-key private WS pattern but the handshake wasn't fully
  available when this was built. Confirm at
  https://docs.sharkexchange.in/#web-sockets and wire it into
  `core/price_feed.py` if you need sub-10s data specifically from Shark.
- **Delta leverage endpoint path** (`/v2/products/{symbol}/orders/leverage`)
  should be double-checked against your Delta API version — leverage-setting
  endpoints have moved before across Delta API revisions.
- **Cross-exchange symbol mapping** (`BTCUSD` on Delta vs `BTCUSDT` on
  CoinSwitch/Shark) means you're hedging BTC/USD exposure against BTC/USDT —
  fine in practice since USDT tracks USD closely, but it's a small extra
  basis source worth knowing about.
- **No partial-fill handling beyond leg-risk close** — if an order partially
  fills, the current logic treats "any fill" as risk-relevant but doesn't
  try to true up the remaining unfilled quantity. For your stated approach
  (close immediately on any mismatch) this is intentional, but worth knowing.
