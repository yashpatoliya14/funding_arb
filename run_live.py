"""
LIVE trading entry point — places REAL orders with REAL money on REAL
exchanges. Requires explicit confirmation every time it's started, and runs
engine.sanity_check() first, same as run_dummy.py, before touching an order.

Multi-pair: builds one engine per exchange combination (shark-coin, coin-delta,
delta-shark) and runs them ALL concurrently via asyncio.

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
from engine import FundingArbEngine, ArbLeg, MultiPairRunner  # noqa: E402

EXCHANGE_MAP = {
    "delta": (DeltaClient, settings.DELTA_API_KEY, settings.DELTA_API_SECRET, settings.DELTA_SYMBOL),
    "coinswitch": (CoinswitchClient, settings.COINSWITCH_API_KEY, settings.COINSWITCH_API_SECRET,
                   settings.COINSWITCH_SYMBOL),
    "shark": (SharkClient, settings.SHARK_API_KEY, settings.SHARK_API_SECRET, settings.SHARK_SYMBOL),
}

CONFIRM_PHRASE = "YES-I-UNDERSTAND-THE-RISK"


def build_live_leg(exchange_name: str) -> ArbLeg:
    cls, key, secret, symbol = EXCHANGE_MAP[exchange_name]
    if not key or not secret:
        print(f"Missing API credentials for {exchange_name} — check your .env file.")
        sys.exit(1)
    client = cls(key, secret)
    return ArbLeg(client=client, symbol=symbol)


def main():
    pairs_display = ", ".join(f"{f}↔{h}" for f, h in settings.EXCHANGE_PAIRS)

    print("=" * 70)
    print("LIVE MODE — this places REAL orders with REAL money.")
    print(f"Exchange pairs: {pairs_display}")
    print(f"Quantity: {settings.TRADE_QUANTITY} | Leverage: {settings.REQUESTED_LEVERAGE}x")
    print("Have you run run_dummy.py successfully and reviewed logs/engine.log? "
          "Have you re-verified the fee/leverage numbers in config/constants.py "
          "against your own account dashboards?")
    print("=" * 70)
    answer = input(f"Type '{CONFIRM_PHRASE}' to proceed: ").strip()
    if answer != CONFIRM_PHRASE:
        print("Confirmation phrase did not match. Aborting.")
        sys.exit(1)

    notifier = TelegramNotifier(settings.TELEGRAM_BOT_TOKEN, settings.TELEGRAM_CHAT_ID,
                                 enabled=settings.TELEGRAM_ENABLED)

    engines = []
    for funding_ex, hedge_ex in settings.EXCHANGE_PAIRS:
        funding_leg = build_live_leg(funding_ex)
        hedge_leg = build_live_leg(hedge_ex)

        engine = FundingArbEngine(
            funding_leg=funding_leg, hedge_leg=hedge_leg,
            quantity=settings.TRADE_QUANTITY, leverage=settings.REQUESTED_LEVERAGE,
            notifier=notifier, dry_run_label="LIVE",
        )
        engines.append(engine)

    print(f"Running sanity check for {len(engines)} exchange pairs (read-only calls only)...")
    all_ok = True
    for engine in engines:
        if not engine.sanity_check():
            print(f"  ✗ Sanity check FAILED for {engine.pair_label}")
            all_ok = False
        else:
            print(f"  ✓ {engine.pair_label} OK")

    if not all_ok:
        print("One or more sanity checks FAILED — see logs/engine.log. Will NOT start live trading.")
        notifier.error("run_live.sanity_check", "Failed pre-flight sanity check — engine not started.")
        sys.exit(1)

    print(f"All {len(engines)} pairs passed. Starting LIVE loop. Ctrl+C to stop.")
    runner = MultiPairRunner(engines)
    asyncio.run(runner.run_all())


if __name__ == "__main__":
    main()
