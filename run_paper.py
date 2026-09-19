"""
PAPER TRADING entry point — uses REAL market data but SIMULATED execution.

All state (balances, positions, trades, instrument cache) is persisted to
SQLite at data/paper_trading.db. Restarting the bot restores capital and
trade history exactly where you left off.

This file mirrors run_live.py exactly in logic, but:
  1. No "YES-I-UNDERSTAND-THE-RISK" confirmation required.
  2. Exchange clients are wrapped in PaperExchangeClient (real prices, fake fills).
  3. Telegram messages are prefixed with 📝 [PAPER].
  4. A periodic summary of virtual P&L is sent to Telegram.
  5. Instruments are cached in SQLite (24h TTL) — no re-fetching on restart.

Usage:
    python run_paper.py            # Normal start (resumes from DB if exists)
    python run_paper.py --reset    # Wipe all paper state and start fresh
"""

import asyncio
import sys
import os
import time
from dotenv import load_dotenv

load_dotenv()

from config import settings  # noqa: E402
from exchanges.delta_client import DeltaClient  # noqa: E402
from exchanges.binance_client import BinanceClient  # noqa: E402
from exchanges.bybit_client import BybitClient  # noqa: E402
from exchanges.paper_client import PaperExchangeClient  # noqa: E402
from core.telegram_notify import TelegramNotifier  # noqa: E402
from core.paper_db import paper_db  # noqa: E402
from engine import FundingArbEngine, MultiPairRunner  # noqa: E402


# ---------------------------------------------------------------------------
# Paper-mode Telegram notifier: wraps the real one, adds [PAPER] prefix
# ---------------------------------------------------------------------------

class PaperTelegramNotifier(TelegramNotifier):
    """Wraps TelegramNotifier to prefix all messages with 📝 [PAPER]."""

    PREFIX = "📝 <b>[PAPER]</b> "

    def send(self, message: str):
        # Prefix every message so it's obvious this is paper trading
        super().send(f"{self.PREFIX}{message}")

    def startup(self, mode: str, exchange_pairs: list, scan_mode: str,
                leverage: int, notional_inr: float = 0, quantity: float = 0):
        """Override startup to show PAPER mode clearly."""
        super().startup(
            mode=f"PAPER ({mode})",
            exchange_pairs=exchange_pairs,
            scan_mode=scan_mode,
            leverage=leverage,
            notional_inr=notional_inr,
            quantity=quantity,
        )


# ---------------------------------------------------------------------------
# Config from env
# ---------------------------------------------------------------------------

PAPER_INITIAL_BALANCE = float(os.getenv("PAPER_INITIAL_BALANCE", "100000"))
PAPER_SLIPPAGE_PCT = float(os.getenv("PAPER_SLIPPAGE_PCT", "0.01"))
PAPER_SUMMARY_INTERVAL_SEC = int(os.getenv("PAPER_SUMMARY_INTERVAL_SEC", "1800"))


# ---------------------------------------------------------------------------
# Build paper-wrapped clients
# ---------------------------------------------------------------------------

def build_paper_clients():
    """Build real exchange clients for market data, then wrap them in
    PaperExchangeClient for simulated execution."""
    real_clients = {}
    paper_clients = {}

    # ---------- Delta ----------
    if settings.DELTA_API_KEY and settings.DELTA_API_SECRET:
        real_clients["delta"] = DeltaClient(settings.DELTA_API_KEY, settings.DELTA_API_SECRET)
    else:
        print("⚠️  Missing API credentials for Delta — Delta pairs will be skipped.")

    # ---------- Binance ----------
    real_clients["binance"] = BinanceClient(
        settings.BINANCE_API_KEY, settings.BINANCE_API_SECRET
    )

    # ---------- Bybit ----------
    real_clients["bybit"] = BybitClient(
        settings.BYBIT_API_KEY, settings.BYBIT_API_SECRET
    )

    if len(real_clients) < 2:
        print("Need at least 2 exchange clients for market data. Check your .env file.")
        sys.exit(1)

    # Wrap each real client in a paper client
    for ex_name, real_client in real_clients.items():
        paper_clients[ex_name] = PaperExchangeClient(
            real_client=real_client,
            exchange_name=ex_name,
            initial_balance=PAPER_INITIAL_BALANCE,
            slippage_pct=PAPER_SLIPPAGE_PCT,
        )

    return paper_clients


# ---------------------------------------------------------------------------
# Enhanced MultiPairRunner with periodic P&L reporting
# ---------------------------------------------------------------------------

class PaperMultiPairRunner(MultiPairRunner):
    """Extends MultiPairRunner to send periodic paper trading summaries."""

    def __init__(self, engines: list, paper_clients: dict,
                 notifier: PaperTelegramNotifier,
                 summary_interval_sec: int = 1800):
        super().__init__(engines)
        self._paper_clients = paper_clients
        self._notifier = notifier
        self._summary_interval = summary_interval_sec

    async def run_all(self):
        """Run engines + a periodic summary reporter."""
        tasks = [asyncio.create_task(e.run_forever()) for e in self.engines]
        tasks.append(asyncio.create_task(self._periodic_summary()))
        await asyncio.gather(*tasks)

    async def _periodic_summary(self):
        """Send a P&L summary to Telegram every summary_interval_sec."""
        while True:
            await asyncio.sleep(self._summary_interval)
            try:
                self._send_combined_summary()
            except Exception as e:
                print(f"[paper summary error] {e}")

    def _send_combined_summary(self):
        """Aggregate P&L across all paper clients and send to Telegram.
        Skips sending if no trades have been placed and no positions are open."""
        # Check if there's any activity worth reporting
        has_activity = False
        for ex_name, client in self._paper_clients.items():
            stats = client.get_db_stats()
            if stats.get("total_trades", 0) > 0 or client.open_positions:
                has_activity = True
                break
        if not has_activity:
            return

        total_pnl = 0.0
        total_trades = 0
        total_fees = 0.0
        lines = [
            "📊 <b>PAPER TRADING — Periodic Summary</b>",
            "━━━━━━━━━━━━━━━━",
            "",
        ]

        for ex_name, client in self._paper_clients.items():
            pnl = client.total_pnl
            total_pnl += pnl
            total_fees += client._total_fees_paid

            stats = client.get_db_stats()
            trades = stats.get("total_trades", 0)
            total_trades += trades

            # Unrealized P&L for open positions
            unrealized = 0.0
            for sym, pos in client.open_positions.items():
                try:
                    ticker = client.get_ticker(sym)
                    if pos.side == "long":
                        unrealized += (ticker.mark_price - pos.entry_price) * pos.quantity
                    else:
                        unrealized += (pos.entry_price - ticker.mark_price) * pos.quantity
                except Exception:
                    pass

            lines.append(
                f"<b>{ex_name}:</b> "
                f"₹{client.balance:,.2f} | "
                f"PnL ₹{pnl:+,.2f} | "
                f"UPnL ₹{unrealized:+,.2f} | "
                f"{trades} trades"
            )

        lines.extend([
            "",
            f"<b>Combined Net P&L:</b> ₹{total_pnl:+,.2f}",
            f"<b>Total Fees:</b> ₹{total_fees:,.2f}",
            f"<b>Total Trades:</b> {total_trades}",
        ])

        self._notifier.send("\n".join(lines))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # Handle --reset flag
    if "--reset" in sys.argv:
        print("🗑️  Resetting all paper trading state...")
        paper_db.clear_all()
        print("   Done. Starting fresh.\n")

    pairs_display = ", ".join(f"{f} <-> {h}" for f, h in settings.EXCHANGE_PAIRS)

    print("=" * 70)
    print("📝 PAPER TRADING MODE — NO real orders, NO real money.")
    print(f"   State persisted to: data/paper_trading.db")
    print(f"   Exchange pairs: {pairs_display}")
    print(f"   Scanning: {'ALL coins' if settings.SCAN_ALL_COINS else 'whitelist'}")
    print(f"   Virtual balance/exchange: ₹{PAPER_INITIAL_BALANCE:,.0f}")
    print(f"   Slippage: {PAPER_SLIPPAGE_PCT}%")
    if settings.FIXED_NOTIONAL_INR > 0:
        print(f"   Notional/trade: ₹{settings.FIXED_NOTIONAL_INR:.0f}")
    else:
        print(f"   Quantity/trade: {settings.TRADE_QUANTITY}")
    print(f"   Leverage: {settings.REQUESTED_LEVERAGE}x")
    print(f"   P&L summary every: {PAPER_SUMMARY_INTERVAL_SEC // 60} min")
    print("=" * 70)

    # Check if we have prior state
    prior_stats = paper_db.get_trade_stats()
    if prior_stats.get("total_trades", 0) > 0:
        print(f"\n📦 Restoring prior session:")
        print(f"   Total trades: {prior_stats['total_trades']}")
        print(f"   Closed: {prior_stats['closed_trades']} "
              f"({prior_stats['wins']}W / {prior_stats['losses']}L)")
        print(f"   Realized P&L: ₹{prior_stats['total_pnl']:+,.2f}")
        print(f"   (Use --reset to start fresh)\n")
    else:
        print("\n🆕 Starting fresh session.\n")

    print("Starting paper trading... (no confirmation needed)\n")

    paper_clients = build_paper_clients()
    notifier = PaperTelegramNotifier(
        settings.TELEGRAM_BOT_TOKEN, settings.TELEGRAM_CHAT_ID,
        enabled=settings.TELEGRAM_ENABLED,
    )

    engines = []
    for funding_ex, hedge_ex in settings.EXCHANGE_PAIRS:
        if funding_ex not in paper_clients or hedge_ex not in paper_clients:
            print(f"  [SKIP] Missing client for {funding_ex} or {hedge_ex}")
            continue

        engine = FundingArbEngine(
            client_a=paper_clients[funding_ex],
            client_b=paper_clients[hedge_ex],
            exchange_name_a=funding_ex,
            exchange_name_b=hedge_ex,
            quantity=settings.TRADE_QUANTITY,
            leverage=settings.REQUESTED_LEVERAGE,
            notifier=notifier,
            dry_run_label="PAPER",
            all_clients=paper_clients,
        )
        engines.append(engine)

    if not engines:
        print("No exchange pairs configured — nothing to run.")
        sys.exit(1)

    # Sanity check (read-only, uses real market data)
    print(f"Running sanity check for {len(engines)} exchange pairs...")
    working_engines = []
    failed_pairs = []
    for engine in engines:
        if not engine.sanity_check():
            print(f"  [FAIL] {engine.pair_label} — skipping")
            failed_pairs.append(engine.pair_label)
        else:
            print(f"  [OK]   {engine.pair_label}")
            working_engines.append(engine)

    if not working_engines:
        print("All sanity checks FAILED — see logs/engine.log.")
        notifier.error("run_paper.sanity_check",
                       "All exchange pairs failed sanity check.")
        sys.exit(1)

    if failed_pairs:
        print(f"\n⚠️  {len(failed_pairs)} pair(s) failed: {', '.join(failed_pairs)}")
        print(f"   Proceeding with {len(working_engines)} working pair(s).\n")

    print(f"📝 {len(working_engines)} pairs active. Ctrl+C to stop.\n")

    # Send startup to Telegram
    active_pairs = [(e.exchange_name_a, e.exchange_name_b) for e in working_engines]
    notifier.startup(
        mode="PAPER",
        exchange_pairs=active_pairs,
        scan_mode="ALL coins" if settings.SCAN_ALL_COINS else "whitelist",
        leverage=settings.REQUESTED_LEVERAGE,
        notional_inr=settings.FIXED_NOTIONAL_INR,
        quantity=settings.TRADE_QUANTITY,
    )

    # Show restored state in Telegram
    if prior_stats.get("total_trades", 0) > 0:
        notifier.send(
            f"📦 <b>SESSION RESTORED</b>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"Prior trades: {prior_stats['total_trades']}\n"
            f"Win/Loss: {prior_stats['wins']}W / {prior_stats['losses']}L\n"
            f"Realized P&L: ₹{prior_stats['total_pnl']:+,.2f}"
        )

    if failed_pairs:
        notifier.send(
            f"⚠️ <b>PAIRS SKIPPED</b>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"{', '.join(failed_pairs)}\n"
            f"(sanity check failed)"
        )

    runner = PaperMultiPairRunner(
        working_engines, paper_clients, notifier,
        summary_interval_sec=PAPER_SUMMARY_INTERVAL_SEC,
    )

    try:
        asyncio.run(runner.run_all())
    except KeyboardInterrupt:
        print("\n\n📝 Paper trading stopped. Final summary:\n")
        for ex_name, client in paper_clients.items():
            summary = client.get_summary()
            print(summary.replace("<b>", "").replace("</b>", "").replace("&amp;", "&"))
            print()
        # Send final summary to Telegram
        try:
            runner._send_combined_summary()
            notifier.send("🛑 <b>PAPER TRADING STOPPED</b> — State saved to DB. "
                         "Restart to resume from where you left off.")
        except Exception:
            pass


if __name__ == "__main__":
    main()
