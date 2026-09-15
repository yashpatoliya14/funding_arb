"""
LIVE trading entry point — places REAL orders with REAL money on REAL
exchanges. Requires explicit confirmation every time it's started, and runs
engine.sanity_check() first before touching an order.

Multi-coin mode: each engine dynamically scans all supported coins on its
exchange pair each funding cycle, selects the best opportunity, verifies
full cost profitability, and trades only if net P&L > threshold.

Usage:
    python run_live.py
    (then type YES-I-UNDERSTAND-THE-RISK when prompted)
"""

import asyncio
import sys
from dotenv import load_dotenv

load_dotenv()

from config import settings  # noqa: E402
from exchanges.delta_client import DeltaClient  # noqa: E402
from exchanges.binance_client import BinanceClient  # noqa: E402
from exchanges.bybit_client import BybitClient  # noqa: E402
from core.telegram_notify import TelegramNotifier  # noqa: E402
from engine import FundingArbEngine, MultiPairRunner  # noqa: E402

CONFIRM_PHRASE = "YES-I-UNDERSTAND-THE-RISK"


def build_clients():
    """Build one client per exchange.

    Delta requires API keys (Indian exchange, all endpoints need auth).
    Binance and Bybit: market data works without keys, but trading requires them.
    For scan-only mode, Binance/Bybit can run without keys.
    """
    clients = {}

    if settings.DELTA_API_KEY and settings.DELTA_API_SECRET:
        clients["delta"] = DeltaClient(settings.DELTA_API_KEY, settings.DELTA_API_SECRET)
    else:
        print("⚠️  Missing API credentials for Delta — Delta pairs will be skipped.")

    # Binance: works without keys for market data (scanning), needs keys for trading
    clients["binance"] = BinanceClient(
        settings.BINANCE_API_KEY, settings.BINANCE_API_SECRET
    )
    if not settings.BINANCE_API_KEY:
        print("ℹ️  Binance running in scan-only mode (no API key). Trading disabled.")

    # Bybit: works without keys for market data (scanning), needs keys for trading
    clients["bybit"] = BybitClient(
        settings.BYBIT_API_KEY, settings.BYBIT_API_SECRET
    )
    if not settings.BYBIT_API_KEY:
        print("ℹ️  Bybit running in scan-only mode (no API key). Trading disabled.")

    if len(clients) < 2:
        print("Need at least 2 exchange clients to run. Check your .env file.")
        sys.exit(1)

    return clients


def main():
    pairs_display = ", ".join(f"{f} <-> {h}" for f, h in settings.EXCHANGE_PAIRS)

    print("=" * 70)
    print("LIVE MODE — this places REAL orders with REAL money.")
    print(f"Exchange pairs: {pairs_display}")
    print(f"Multi-coin scanning: {'ALL coins' if settings.SCAN_ALL_COINS else 'whitelist'}")
    print(f"Notional per trade: ₹{settings.FIXED_NOTIONAL_INR:.0f}" if settings.FIXED_NOTIONAL_INR > 0
          else f"Quantity: {settings.TRADE_QUANTITY}")
    print(f"Leverage: {settings.REQUESTED_LEVERAGE}x")
    print("Have you re-verified the fee/leverage numbers in config/constants.py "
          "against your own account dashboards?")
    print("=" * 70)
    answer = input(f"Type '{CONFIRM_PHRASE}' to proceed: ").strip()
    if answer != CONFIRM_PHRASE:
        print("Confirmation phrase did not match. Aborting.")
        sys.exit(1)

    clients = build_clients()
    notifier = TelegramNotifier(settings.TELEGRAM_BOT_TOKEN, settings.TELEGRAM_CHAT_ID,
                                 enabled=settings.TELEGRAM_ENABLED)

    engines = []
    for funding_ex, hedge_ex in settings.EXCHANGE_PAIRS:
        if funding_ex not in clients or hedge_ex not in clients:
            print(f"  [SKIP] Missing client for {funding_ex} or {hedge_ex}")
            continue

        engine = FundingArbEngine(
            client_a=clients[funding_ex],
            client_b=clients[hedge_ex],
            exchange_name_a=funding_ex,
            exchange_name_b=hedge_ex,
            quantity=settings.TRADE_QUANTITY,
            leverage=settings.REQUESTED_LEVERAGE,
            notifier=notifier,
            dry_run_label="LIVE",
            all_clients=clients,
        )
        engines.append(engine)

    if not engines:
        print("No exchange pairs configured — nothing to run.")
        sys.exit(1)

    print(f"Running sanity check for {len(engines)} exchange pairs (read-only calls only)...")
    working_engines = []
    failed_pairs = []
    for engine in engines:
        if not engine.sanity_check():
            print(f"  [FAIL] Sanity check FAILED for {engine.pair_label} — skipping this pair")
            failed_pairs.append(engine.pair_label)
        else:
            print(f"  [OK] {engine.pair_label} OK")
            working_engines.append(engine)

    if not working_engines:
        print("All sanity checks FAILED — nothing to run. See logs/engine.log.")
        notifier.error("run_live.sanity_check", "All exchange pairs failed sanity check — engine not started.")
        sys.exit(1)

    if failed_pairs:
        print(f"WARNING: {len(failed_pairs)} pair(s) failed and will be skipped: {', '.join(failed_pairs)}")
        print(f"Proceeding with {len(working_engines)} working pair(s).")

    print(f"{len(working_engines)} pairs active. Starting LIVE loop. Ctrl+C to stop.")

    # Send startup confirmation to Telegram
    active_pairs = [(e.exchange_name_a, e.exchange_name_b) for e in working_engines]
    notifier.startup(
        mode="LIVE",
        exchange_pairs=active_pairs,
        scan_mode="ALL coins" if settings.SCAN_ALL_COINS else "whitelist",
        leverage=settings.REQUESTED_LEVERAGE,
        notional_inr=settings.FIXED_NOTIONAL_INR,
        quantity=settings.TRADE_QUANTITY,
    )
    if failed_pairs:
        notifier.send(
            f"⚠️ <b>PAIRS SKIPPED</b>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"{', '.join(failed_pairs)}\n"
            f"(sanity check failed — will retry on next restart)"
        )

    runner = MultiPairRunner(working_engines)
    asyncio.run(runner.run_all())


if __name__ == "__main__":
    main()
