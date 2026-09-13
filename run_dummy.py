"""
Requirement #11 & #12: forward-test the ENTIRE engine with fake money but
real market data and real execution logic (fills simulated against real
bid/ask), then run engine.sanity_check() before you ever flip to run_live.py.

Multi-coin mode: each engine dynamically scans all supported coins on its
exchange pair, selects the best opportunity, verifies full cost profitability,
and paper-trades only if net P&L > threshold.

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
from engine import FundingArbEngine, MultiPairRunner  # noqa: E402

EXCHANGE_BUILDERS = {
    "delta": (DeltaClient, settings.DELTA_API_KEY, settings.DELTA_API_SECRET),
    "coinswitch": (CoinswitchClient, settings.COINSWITCH_API_KEY, settings.COINSWITCH_API_SECRET),
    "shark": (SharkClient, settings.SHARK_API_KEY, settings.SHARK_API_SECRET),
}


def build_sim_clients():
    """Build simulated clients wrapping real data sources for each exchange."""
    clients = {}
    for ex_name, (cls, key, secret) in EXCHANGE_BUILDERS.items():
        # Market data still needs SOME credentials for private endpoints on some
        # exchanges' ticker calls; if the exchange's ticker endpoint is public
        # (all three of ours are), empty keys are fine for the read-only data
        # source feeding the simulator.
        real_client = cls(key or "dummy", secret or "dummy")
        sim_client = SimulatedClient(real_client, starting_balance_inr=settings.SIM_STARTING_BALANCE_INR)
        clients[ex_name] = sim_client
    return clients


def main():
    notifier = TelegramNotifier(settings.TELEGRAM_BOT_TOKEN, settings.TELEGRAM_CHAT_ID,
                                 enabled=settings.TELEGRAM_ENABLED)

    clients = build_sim_clients()

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
            dry_run_label="DUMMY/PAPER",
            all_clients=clients,
        )
        engines.append(engine)

    if not engines:
        print("No exchange pairs configured — nothing to run.")
        sys.exit(1)

    print(f"Running sanity check for {len(engines)} exchange pairs (paper trading - no real orders)...")
    all_ok = True
    for engine in engines:
        if not engine.sanity_check():
            print(f"  [FAIL] Sanity check FAILED for {engine.pair_label}")
            all_ok = False
        else:
            print(f"  [OK] {engine.pair_label} OK")

    if not all_ok:
        print("One or more sanity checks FAILED — see logs/engine.log. Fix before continuing.")
        sys.exit(1)

    pairs_display = ", ".join(f"{f} <-> {h}" for f, h in settings.EXCHANGE_PAIRS)
    scan_mode = "ALL coins" if settings.SCAN_ALL_COINS else "whitelist"
    print(f"All {len(engines)} pairs passed. Multi-coin scan: {scan_mode}.")

    # Send startup confirmation to Telegram
    notifier.startup(
        mode="PAPER TRADING",
        exchange_pairs=settings.EXCHANGE_PAIRS,
        scan_mode=scan_mode,
        leverage=settings.REQUESTED_LEVERAGE,
        notional_inr=settings.FIXED_NOTIONAL_INR,
        quantity=settings.TRADE_QUANTITY,
    )

    print(f"Starting paper-trading loop. Ctrl+C to stop.")
    runner = MultiPairRunner(engines)
    asyncio.run(runner.run_all())


if __name__ == "__main__":
    main()
