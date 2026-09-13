"""
LIVE trading entry point — places REAL orders with REAL money on REAL
exchanges. Requires explicit confirmation every time it's started, and runs
engine.sanity_check() first, same as run_dummy.py, before touching an order.

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
from exchanges.coinswitch_client import CoinswitchClient  # noqa: E402
from exchanges.shark_client import SharkClient  # noqa: E402
from core.telegram_notify import TelegramNotifier  # noqa: E402
from engine import FundingArbEngine, MultiPairRunner  # noqa: E402

CONFIRM_PHRASE = "YES-I-UNDERSTAND-THE-RISK"


def build_clients():
    """Build one client per exchange. Credentials are required for live mode."""
    clients = {}

    if settings.DELTA_API_KEY and settings.DELTA_API_SECRET:
        clients["delta"] = DeltaClient(settings.DELTA_API_KEY, settings.DELTA_API_SECRET)
    else:
        print("Missing API credentials for delta — check your .env file.")
        sys.exit(1)

    if settings.COINSWITCH_API_KEY and settings.COINSWITCH_API_SECRET:
        clients["coinswitch"] = CoinswitchClient(settings.COINSWITCH_API_KEY, settings.COINSWITCH_API_SECRET)
    else:
        print("Missing API credentials for coinswitch — check your .env file.")
        sys.exit(1)

    if settings.SHARK_API_KEY and settings.SHARK_API_SECRET:
        clients["shark"] = SharkClient(settings.SHARK_API_KEY, settings.SHARK_API_SECRET)
    else:
        print("Missing API credentials for shark — check your .env file.")
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
    print("Have you run run_dummy.py successfully and reviewed logs/engine.log? "
          "Have you re-verified the fee/leverage numbers in config/constants.py "
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
    all_ok = True
    for engine in engines:
        if not engine.sanity_check():
            print(f"  [FAIL] Sanity check FAILED for {engine.pair_label}")
            all_ok = False
        else:
            print(f"  [OK] {engine.pair_label} OK")

    if not all_ok:
        print("One or more sanity checks FAILED — see logs/engine.log. Will NOT start live trading.")
        notifier.error("run_live.sanity_check", "Failed pre-flight sanity check — engine not started.")
        sys.exit(1)

    print(f"All {len(engines)} pairs passed. Starting LIVE loop. Ctrl+C to stop.")

    # Send startup confirmation to Telegram
    notifier.startup(
        mode="LIVE",
        exchange_pairs=settings.EXCHANGE_PAIRS,
        scan_mode="ALL coins" if settings.SCAN_ALL_COINS else "whitelist",
        leverage=settings.REQUESTED_LEVERAGE,
        notional_inr=settings.FIXED_NOTIONAL_INR,
        quantity=settings.TRADE_QUANTITY,
    )

    runner = MultiPairRunner(engines)
    asyncio.run(runner.run_all())


if __name__ == "__main__":
    main()
