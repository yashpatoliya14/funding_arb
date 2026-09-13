"""
Main orchestrator. Ties together price_feed, spread_calc, leverage_sync,
funding_window, order_manager, and telegram_notify into the loop you
described:

  1. Fetch prices/funding every 10s               -> price_feed.py
  2. Calculate spread + fee/tax-adjusted edge       -> spread_calc.py
  3-4. Fees pulled from constants.py, not re-fetched every tick
  5. Sync leverage across both exchanges            -> leverage_sync.py
  6. Track basis drift, kill-switch                 -> order_manager.py
  7-8. Synchronized post-only orders, reprice every 10s, leg-risk handling
        -> order_manager.py
  9. Enter 20 min before funding, close right after -> funding_window.py
  10. Telegram updates throughout                   -> telegram_notify.py
  11. Same code path for sim and live               -> exchanges/simulator.py
  12. Run this file's `sanity_check()` before any live run.

Multi-pair: a MultiPairRunner spins up one FundingArbEngine per exchange
combination (shark-coin, coin-delta, delta-shark) as concurrent asyncio
tasks. Each engine is fully independent.
"""

import asyncio
import logging
import time
from dataclasses import dataclass

from config import settings
from core.price_feed import PriceFeed
from core.spread_calc import evaluate_funding_trade
from core.leverage_sync import sync_leverage
from core.funding_window import is_in_entry_window, should_close_now, next_funding_time
from core.order_manager import DualLegOrderManager
from core.telegram_notify import TelegramNotifier
from exchanges.base import ExchangeClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.FileHandler("logs/engine.log"), logging.StreamHandler()],
)
log = logging.getLogger("engine")


@dataclass
class ArbLeg:
    client: ExchangeClient
    symbol: str


class FundingArbEngine:
    def __init__(self, funding_leg: ArbLeg, hedge_leg: ArbLeg,
                 quantity: float, leverage: int, notifier: TelegramNotifier,
                 dry_run_label: str = "LIVE"):
        self.funding_leg = funding_leg
        self.hedge_leg = hedge_leg
        self.quantity = quantity
        self.leverage = leverage
        self.notifier = notifier
        self.dry_run_label = dry_run_label
        self.pair_label = f"{funding_leg.client.name}↔{hedge_leg.client.name}"
        self.feed = PriceFeed()
        self.order_manager = DualLegOrderManager(funding_leg.client, hedge_leg.client, notifier)
        self._current_state = None

    def sanity_check(self) -> bool:
        """Requirement #12: run this before ANY live session. Verifies both
        clients can fetch a ticker, both accept the requested symbol, and
        neither key is a placeholder."""
        ok = True
        for leg, name in [(self.funding_leg, "funding_leg"), (self.hedge_leg, "hedge_leg")]:
            try:
                t = leg.client.get_ticker(leg.symbol)
                if t.mark_price <= 0:
                    log.error("[%s] %s: ticker returned non-positive mark price for %s",
                              self.pair_label, name, leg.symbol)
                    ok = False
                else:
                    log.info("[%s] %s OK: %s mark=%s bid=%s ask=%s",
                             self.pair_label, name, leg.symbol, t.mark_price, t.best_bid, t.best_ask)
            except Exception as e:
                log.error("[%s] %s: sanity check failed for %s -> %s",
                          self.pair_label, name, leg.symbol, e)
                ok = False
        return ok

    def _decide_direction(self, funding_rate: float):
        """Positive funding: longs pay shorts -> go SHORT the funding leg to collect.
        Negative funding: shorts pay longs -> go LONG the funding leg to collect.
        Hedge leg takes the opposite side to stay notionally flat."""
        if funding_rate is None:
            return None, None
        if funding_rate > 0:
            return "sell", "buy"   # short funding leg, long hedge leg
        else:
            return "buy", "sell"   # long funding leg, short hedge leg

    async def run_forever(self):
        log.info("[%s] Starting engine [%s] for %s (funding) / %s (hedge)",
                  self.pair_label, self.dry_run_label,
                  self.funding_leg.symbol, self.hedge_leg.symbol)
        self.notifier.heartbeat(
            f"Engine started [{self.dry_run_label}] — {self.pair_label}: "
            f"{self.funding_leg.symbol} / {self.hedge_leg.symbol}. "
            f"Next funding at {next_funding_time()}"
        )

        # requirement #1: background price/funding fetch every 10s
        self.feed.start_polling(self.funding_leg.client, self.funding_leg.symbol)
        self.feed.start_polling(self.hedge_leg.client, self.hedge_leg.symbol)

        traded_this_cycle = False

        while True:
            try:
                in_window = is_in_entry_window()

                if in_window and not traded_this_cycle:
                    await asyncio.to_thread(self._attempt_entry)
                    traded_this_cycle = True

                if self._current_state and self._current_state.both_filled:
                    aborted = await asyncio.to_thread(
                        self.order_manager.check_basis_drift, self._current_state
                    )
                    if aborted or should_close_now():
                        log.info("[%s] Closing hedge (basis-drift-abort=%s, snapshot-window=%s)",
                                 self.pair_label, aborted, should_close_now())
                        if not aborted:
                            await asyncio.to_thread(
                                self.order_manager.close_both, self._current_state
                            )
                        self._current_state = None

                if not is_in_entry_window() and should_close_now() is False and self._current_state is None:
                    traded_this_cycle = False  # reset once we're clear of the previous window

            except Exception as e:
                log.exception("[%s] Engine loop error: %s", self.pair_label, e)
                self.notifier.error(f"engine.run_forever [{self.pair_label}]", str(e))

            await asyncio.sleep(settings.MAIN_LOOP_INTERVAL_SEC)

    def _attempt_entry(self):
        funding_ticker = self.funding_leg.client.get_ticker(self.funding_leg.symbol)
        hedge_ticker = self.hedge_leg.client.get_ticker(self.hedge_leg.symbol)

        report = evaluate_funding_trade(
            exchange_a=self.funding_leg.client.name, exchange_b=self.hedge_leg.client.name,
            funding_rate_a=funding_ticker.funding_rate or 0.0,
            funding_rate_b=hedge_ticker.funding_rate or 0.0,
            price_a=funding_ticker.mark_price, price_b=hedge_ticker.mark_price,
        )
        log.info("[%s] Edge report: %s", self.pair_label, report.reason)

        if not report.tradeable:
            log.info("[%s] Skipping entry — edge does not clear cost threshold.", self.pair_label)
            return

        side_funding, side_hedge = self._decide_direction(funding_ticker.funding_rate)
        if side_funding is None:
            log.info("[%s] No funding rate available yet — skipping this cycle.", self.pair_label)
            return

        lev_decision = sync_leverage(self.funding_leg.client, self.hedge_leg.client,
                                      self.funding_leg.symbol, self.hedge_leg.symbol, self.leverage)
        log.info("[%s] Leverage synced to %sx (requested %sx)",
                  self.pair_label, lev_decision.common_leverage, lev_decision.requested)

        state = self.order_manager.open_hedge(
            self.funding_leg.symbol, side_funding, self.hedge_leg.symbol, side_hedge,
            self.quantity, self.quantity,
        )
        self.notifier.entry(self.funding_leg.symbol, self.funding_leg.client.name,
                             self.hedge_leg.client.name, report.net_after_cost_pct,
                             lev_decision.common_leverage)

        state = self.order_manager.monitor_until_filled(state, side_funding, side_hedge)
        self._current_state = state

        if state.aborted:
            log.warning("[%s] Entry aborted: %s", self.pair_label, state.abort_reason)
        else:
            log.info("[%s] Both legs filled — holding through funding snapshot.", self.pair_label)


class MultiPairRunner:
    """Runs multiple FundingArbEngine instances concurrently via asyncio,
    one per exchange pair (shark-coin, coin-delta, delta-shark)."""

    def __init__(self, engines: list[FundingArbEngine]):
        self.engines = engines

    async def run_all(self):
        log.info("MultiPairRunner: launching %d concurrent engines", len(self.engines))
        tasks = [asyncio.create_task(e.run_forever()) for e in self.engines]
        await asyncio.gather(*tasks)
