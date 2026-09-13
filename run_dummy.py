"""
Requirement #11 & #12: forward-test the ENTIRE engine with fake money but
real market data and real execution logic (fills simulated against real
bid/ask), then run engine.sanity_check() before you ever flip to run_live.py.

Multi-pair: builds one engine per exchange combination (shark-coin, coin-delta,
delta-shark) and runs them ALL concurrently via asyncio.

Usage:
    python run_dummy.py
"""

import asyncio
import sys
from dotenv import load_dotenv

load_dotenv()

from config import settings  # noqa: E402
from exchanges.delta_client import DeltaClient  # noqa: E402
from exchanges.coinswitch_client import CoinswitchClient  # noqa: E402
from exchanges.shark_client import SharkClient  # noqa: E402
from exchanges.simulator import SimulatedClient  # noqa: E402
from core.telegram_notify import TelegramNotifier  # noqa: E402
from engine import FundingArbEngine, ArbLeg, MultiPairRunner  # noqa: E402

EXCHANGE_MAP = {
    "delta": (DeltaClient, settings.DELTA_API_KEY, settings.DELTA_API_SECRET, settings.DELTA_SYMBOL),
    "coinswitch": (CoinswitchClient, settings.COINSWITCH_API_KEY, settings.COINSWITCH_API_SECRET,
                   settings.COINSWITCH_SYMBOL),
    "shark": (SharkClient, settings.SHARK_API_KEY, settings.SHARK_API_SECRET, settings.SHARK_SYMBOL),
}


def build_sim_leg(exchange_name: str) -> ArbLeg:
    cls, key, secret, symbol = EXCHANGE_MAP[exchange_name]
    # Market data still needs SOME credentials for private endpoints on some
    # exchanges' ticker calls; if the exchange's ticker endpoint is public
    # (all three of ours are), empty keys are fine for the read-only data
    # source feeding the simulator.
    real_client_for_data = cls(key or "dummy", secret or "dummy")
    sim_client = SimulatedClient(real_client_for_data, starting_balance_inr=settings.SIM_STARTING_BALANCE_INR)
    return ArbLeg(client=sim_client, symbol=symbol)


def main():
    notifier = TelegramNotifier(settings.TELEGRAM_BOT_TOKEN, settings.TELEGRAM_CHAT_ID,
                                 enabled=settings.TELEGRAM_ENABLED)

    engines = []
    for funding_ex, hedge_ex in settings.EXCHANGE_PAIRS:
        funding_leg = build_sim_leg(funding_ex)
        hedge_leg = build_sim_leg(hedge_ex)

        engine = FundingArbEngine(
            funding_leg=funding_leg, hedge_leg=hedge_leg,
            quantity=settings.TRADE_QUANTITY, leverage=settings.REQUESTED_LEVERAGE,
            notifier=notifier, dry_run_label="DUMMY/PAPER",
        )
        engines.append(engine)

    print(f"Running sanity check for {len(engines)} exchange pairs (paper trading — no real orders)...")
    all_ok = True
    for engine in engines:
        if not engine.sanity_check():
            print(f"  ✗ Sanity check FAILED for {engine.pair_label}")
            all_ok = False
        else:
            print(f"  ✓ {engine.pair_label} OK")

    if not all_ok:
        print("One or more sanity checks FAILED — see logs/engine.log. Fix before continuing.")
        sys.exit(1)

    print(f"All {len(engines)} pairs passed. Starting paper-trading loop. Ctrl+C to stop.")
    runner = MultiPairRunner(engines)
    asyncio.run(runner.run_all())


if __name__ == "__main__":
    main()
