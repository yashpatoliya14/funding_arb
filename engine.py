"""
Main orchestrator. Ties together coin_scanner, price_feed, spread_calc,
leverage_sync, funding_window, order_manager, and telegram_notify.

Multi-coin upgrade:
  1. At startup, scan all supported coins across all exchanges
  2. Each funding cycle, find the BEST opportunity (highest net P&L coin)
  3. Calculate ALL costs (fees, spread, slippage, both-leg funding)
  4. Execute only if final net profit > configured threshold
  5. Both legs executed atomically as much as exchanges allow

Flow per cycle:
  1. Refresh instrument lists (cached, re-fetched every 5 min)
  2. Scan funding rates for all common coins on this exchange pair
  3. Rank by net P&L after full cost deduction
  4. If best opportunity clears threshold AND we're in the entry window:
     a. Sync leverage across both exchanges for the selected coin
     b. Place synchronized post-only orders on both legs
     c. Monitor fills, reprice every 10s
     d. Hold through funding snapshot, close right after
  5. Telegram updates throughout
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Dict, Optional

from config import settings
from core.coin_scanner import CoinScanner, ArbOpportunity
from core.spread_calc import evaluate_funding_trade
from core.leverage_sync import sync_leverage
from core.funding_window import (
    is_in_entry_window, should_close_now, next_funding_time,
    most_recent_funding_time, seconds_until_entry_window,
)
from core.order_manager import DualLegOrderManager
from core.telegram_notify import TelegramNotifier
from exchanges.base import ExchangeClient

import os

os.makedirs("logs", exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.FileHandler("logs/engine.log", encoding="utf-8"), logging.StreamHandler()],
)
log = logging.getLogger("engine")


@dataclass
class ArbLeg:
    client: ExchangeClient
    symbol: str


class FundingArbEngine:
    """Runs funding arbitrage between two exchanges.

    Multi-coin mode (default): scans all common coins each cycle, picks
    the best opportunity, and trades it dynamically.

    Legacy mode: if constructed with fixed symbols, behaves like the
    original single-coin engine.
    """

    def __init__(self, client_a: ExchangeClient, client_b: ExchangeClient,
                 exchange_name_a: str, exchange_name_b: str,
                 quantity: float, leverage: int, notifier: TelegramNotifier,
                 dry_run_label: str = "LIVE",
                 fixed_symbol_a: str = "", fixed_symbol_b: str = "",
                 all_clients: Dict[str, ExchangeClient] = None):
        self.client_a = client_a
        self.client_b = client_b
        self.exchange_name_a = exchange_name_a
        self.exchange_name_b = exchange_name_b
        self.quantity = quantity
        self.leverage = leverage
        self.notifier = notifier
        self.dry_run_label = dry_run_label
        self.pair_label = f"{exchange_name_a} <-> {exchange_name_b}"
        # All clients dict for scanner (includes both + potentially others)
        self._all_clients = all_clients or {exchange_name_a: client_a, exchange_name_b: client_b}
        self.scanner = CoinScanner()
        self.order_manager = DualLegOrderManager(client_a, client_b, notifier)
        self._current_state = None
        self._current_opp: Optional[ArbOpportunity] = None
        self._window_notified = False
        # Hold-across-snapshots tracking
        self._held_snapshots = 0
        self._last_counted_funding = None   # datetime of the last snapshot we counted
        self._last_idle_log = 0.0           # throttle for "waiting for window" logs
        # Legacy fixed-symbol mode
        self._fixed_symbol_a = fixed_symbol_a
        self._fixed_symbol_b = fixed_symbol_b

    def sanity_check(self) -> bool:
        """Requirement #12: run this before ANY live session. Verifies both
        clients can fetch a ticker for at least one coin."""
        ok = True
        for client, ex_name in [(self.client_a, self.exchange_name_a),
                                 (self.client_b, self.exchange_name_b)]:
            try:
                # Try to get instruments first
                instruments = client.list_instruments()
                if instruments:
                    test_sym = instruments[0].symbol
                    log.info("[%s] %s: found %d instruments, testing %s",
                             self.pair_label, ex_name, len(instruments), test_sym)
                else:
                    # Fall back to legacy symbol
                    test_sym = self._legacy_symbol(ex_name)
                    log.warning("[%s] %s: no instruments returned, using fallback %s",
                                self.pair_label, ex_name, test_sym)

                t = client.get_ticker(test_sym)
                if t.mark_price <= 0:
                    log.error("[%s] %s: ticker returned non-positive mark price for %s",
                              self.pair_label, ex_name, test_sym)
                    ok = False
                else:
                    log.info("[%s] %s OK: %s mark=%s bid=%s ask=%s funding=%s",
                             self.pair_label, ex_name, test_sym, t.mark_price,
                             t.best_bid, t.best_ask, t.funding_rate)
            except Exception as e:
                log.error("[%s] %s: sanity check failed -> %s", self.pair_label, ex_name, e)
                ok = False
        return ok

    def _legacy_symbol(self, exchange_name: str) -> str:
        return {
            "delta": settings.DELTA_SYMBOL,
            "binance": settings.BINANCE_SYMBOL,
            "bybit": settings.BYBIT_SYMBOL,
        }.get(exchange_name, "BTCUSDT")

    async def run_forever(self):
        log.info("[%s] Starting engine [%s] — multi-coin scanning enabled",
                  self.pair_label, self.dry_run_label)
        self.notifier.heartbeat(
            f"Engine started [{self.dry_run_label}] - {self.pair_label}. "
            f"Next funding at {next_funding_time().strftime('%I:%M %p IST')}"
        )

        # Initial instrument scan
        await asyncio.to_thread(
            self.scanner.refresh_instruments, self._all_clients, True
        )
        common = self.scanner.symbol_map.common_coins(
            self.exchange_name_a, self.exchange_name_b
        )
        log.info("[%s] Common coins: %s", self.pair_label, common)

        traded_this_cycle = False

        while True:
            try:
                # Periodically refresh instruments (cached internally)
                await asyncio.to_thread(
                    self.scanner.refresh_instruments, self._all_clients
                )

                in_window = is_in_entry_window()
                have_position = self._current_state is not None

                # ---- Entry: only in-window, once per cycle, and only if flat ----
                if in_window and not traded_this_cycle and not have_position:
                    # Notify once when the entry window opens
                    if not self._window_notified:
                        self.notifier.window_open(self.pair_label, next_funding_time().strftime("%I:%M %p IST"))
                        self._window_notified = True
                    # Retry scanning every loop tick until a trade is placed or the window closes
                    trade_placed = await asyncio.to_thread(self._attempt_entry)
                    if trade_placed:
                        traded_this_cycle = True
                        self._held_snapshots = 0
                        # Don't count the snapshot we entered just before as "held".
                        self._last_counted_funding = next_funding_time()
                elif not have_position:
                    # Idle: make it obvious the bot is alive and waiting, not stuck.
                    self._log_idle_wait()

                # ---- Hold across snapshots / exit ----
                if self._current_state and self._current_state.both_filled:
                    aborted = await asyncio.to_thread(
                        self.order_manager.check_basis_drift, self._current_state
                    )
                    if aborted:
                        log.info("[%s] Position closed by basis-drift kill-switch.", self.pair_label)
                        self._reset_position()
                    elif should_close_now():
                        # A funding snapshot just passed. Count it once, then decide
                        # whether to keep holding (collect more snapshots) or exit.
                        snap = most_recent_funding_time()
                        if snap != self._last_counted_funding:
                            self._last_counted_funding = snap
                            self._held_snapshots += 1
                            should_exit, reason = await asyncio.to_thread(self._should_exit_position)
                            log.info("[%s] Snapshot #%d collected. %s",
                                     self.pair_label, self._held_snapshots, reason)
                            if should_exit:
                                await asyncio.to_thread(
                                    self.order_manager.close_both, self._current_state
                                )
                                self._reset_position()

                # ---- Reset per-cycle entry gate once clear of the window ----
                if not is_in_entry_window() and should_close_now() is False and self._current_state is None:
                    traded_this_cycle = False  # reset once we're clear of the previous window
                    self._window_notified = False  # reset window notification for next cycle

            except Exception as e:
                log.exception("[%s] Engine loop error: %s", self.pair_label, e)
                self.notifier.error(f"engine.run_forever [{self.pair_label}]", str(e))

            await asyncio.sleep(settings.MAIN_LOOP_INTERVAL_SEC)

    def _attempt_entry(self) -> bool:
        """Scan all coins, find the best opportunity, verify costs, and execute.

        Returns True if a trade was actually placed, False otherwise.
        """
        # ---- Step 1: Find the best opportunity ----
        opp = self.scanner.find_best_opportunity(
            self._all_clients, self.exchange_name_a, self.exchange_name_b
        )

        if opp is None:
            common_count = len(self.scanner.symbol_map.common_coins(
                self.exchange_name_a, self.exchange_name_b
            ))
            log.info("[%s] No tradeable opportunity across %d coins — will retry.",
                     self.pair_label, common_count)
            return False

        log.info("[%s] Selected opportunity: %s", self.pair_label, opp.reason)

        if not opp.tradeable:
            log.info("[%s] Best opportunity does not clear cost threshold — will retry.",
                     self.pair_label)
            return False

        # ---- Step 2: Fresh price verification ----
        # Re-fetch tickers to ensure the opportunity is still valid right now
        try:
            funding_ticker = self._all_clients[opp.funding_exchange].get_ticker(opp.funding_symbol)
            hedge_ticker = self._all_clients[opp.hedge_exchange].get_ticker(opp.hedge_symbol)
        except Exception as e:
            log.warning("[%s] Failed to re-fetch tickers for verification: %s", self.pair_label, e)
            return False

        # Re-evaluate with fresh prices
        from core.spread_calc import evaluate_funding_trade_full
        fresh_opp = evaluate_funding_trade_full(
            exchange_a=opp.funding_exchange,
            exchange_b=opp.hedge_exchange,
            base_asset=opp.base_asset,
            funding_rate_a=funding_ticker.funding_rate or 0.0,
            funding_rate_b=hedge_ticker.funding_rate or 0.0,
            bid_a=funding_ticker.best_bid,
            ask_a=funding_ticker.best_ask,
            mark_a=funding_ticker.mark_price,
            bid_b=hedge_ticker.best_bid,
            ask_b=hedge_ticker.best_ask,
            mark_b=hedge_ticker.mark_price,
            symbol_a=opp.funding_symbol,
            symbol_b=opp.hedge_symbol,
        )

        log.info("[%s] Fresh verification: %s", self.pair_label, fresh_opp.reason)

        if not fresh_opp.tradeable:
            log.info("[%s] Opportunity no longer profitable after fresh price check — will retry.",
                     self.pair_label)
            return False

        # ---- Step 3: Notify and prepare ----
        self.notifier.entry_full(
            fresh_opp.base_asset, fresh_opp.funding_exchange,
            fresh_opp.hedge_exchange, fresh_opp
        )

        # ---- Step 4: Sync leverage ----
        funding_client = self._all_clients[fresh_opp.funding_exchange]
        hedge_client = self._all_clients[fresh_opp.hedge_exchange]

        lev_decision = sync_leverage(
            funding_client, hedge_client,
            fresh_opp.funding_symbol, fresh_opp.hedge_symbol,
            self.leverage
        )
        log.info("[%s] Leverage synced to %sx (requested %sx)",
                  self.pair_label, lev_decision.common_leverage, lev_decision.requested)

        # ---- Step 5: Execute both legs ----
        # Use the opportunity's computed quantity, or fall back to configured
        trade_qty = fresh_opp.quantity if fresh_opp.quantity > 0 else self.quantity

        # Temporarily swap order manager clients to match the opportunity direction
        om = DualLegOrderManager(funding_client, hedge_client, self.notifier)

        state = om.open_hedge(
            fresh_opp.funding_symbol, fresh_opp.funding_side,
            fresh_opp.hedge_symbol, fresh_opp.hedge_side,
            trade_qty, trade_qty,
        )

        from config.constants import ENTRY_LEAD_MINUTES
        state = om.monitor_until_filled(
            state, fresh_opp.funding_side, fresh_opp.hedge_side, trade_qty,
            timeout_sec=ENTRY_LEAD_MINUTES * 60
        )

        self._current_state = state
        self._current_opp = fresh_opp
        # Point the order_manager at the right clients for ongoing monitoring
        self.order_manager = om

        if state.aborted:
            log.warning("[%s] Entry aborted: %s", self.pair_label, state.abort_reason)
            self._current_state = None
            self._current_opp = None
            return False
        else:
            log.info("[%s] Both legs filled for %s — holding across up to %d funding snapshots.",
                     self.pair_label, fresh_opp.base_asset, settings.EXPECTED_HOLD_SNAPSHOTS)
            return True

    def _reset_position(self):
        """Clear all position state after a close/abort so the engine can
        re-enter on the next window."""
        self._current_state = None
        self._current_opp = None
        self._held_snapshots = 0

    def _should_exit_position(self) -> tuple[bool, str]:
        """Decide whether to close the held delta-neutral position.

        Exit when EITHER:
          - we've collected the expected number of snapshots (cost amortized), OR
          - the funding edge for the held coin has decayed below the exit
            threshold (holding longer would no longer be in our favour).
        """
        opp = self._current_opp
        if opp is None:
            return True, "no opportunity attached — closing"

        if self._held_snapshots >= settings.EXPECTED_HOLD_SNAPSHOTS:
            return True, (f"held {self._held_snapshots}/{settings.EXPECTED_HOLD_SNAPSHOTS} "
                          f"snapshots — target reached, closing")

        # Re-check the live funding edge for the direction we actually hold:
        # short `funding_symbol` on funding_exchange, long `hedge_symbol` on hedge_exchange.
        try:
            f_ticker = self._all_clients[opp.funding_exchange].get_ticker(opp.funding_symbol)
            h_ticker = self._all_clients[opp.hedge_exchange].get_ticker(opp.hedge_symbol)
        except Exception as e:
            # Can't verify — hold rather than churn fees on a transient error.
            return False, f"funding re-check failed ({e}) — holding"

        f_rate = f_ticker.funding_rate or 0.0
        h_rate = h_ticker.funding_rate or 0.0
        # Short collects funding on the funding leg; long pays on the hedge leg.
        net_funding_pct = (f_rate - h_rate) * 100

        if net_funding_pct < settings.FUNDING_EXIT_THRESHOLD_PCT:
            return True, (f"net funding edge {net_funding_pct:+.4f}% dropped below "
                          f"exit threshold {settings.FUNDING_EXIT_THRESHOLD_PCT}% — closing")

        return False, (f"net funding edge {net_funding_pct:+.4f}% still favourable — "
                       f"holding ({self._held_snapshots}/{settings.EXPECTED_HOLD_SNAPSHOTS})")

    def _log_idle_wait(self):
        """Throttled heartbeat log so it's clear the bot is alive and simply
        waiting for the next entry window (trades only fire near funding times)."""
        now = time.time()
        if now - self._last_idle_log < 300:  # at most once every 5 min
            return
        self._last_idle_log = now
        wait_sec = seconds_until_entry_window()
        nft = next_funding_time().strftime("%I:%M %p IST")
        if wait_sec > 0:
            mins = int(wait_sec // 60)
            log.info("[%s] Idle — entry window opens in ~%d min (funding at %s). No scan until then.",
                     self.pair_label, mins, nft)
        else:
            log.info("[%s] In entry window (funding at %s) — scanning for opportunities.",
                     self.pair_label, nft)


class MultiPairRunner:
    """Runs multiple FundingArbEngine instances concurrently via asyncio,
    one per exchange pair (delta-binance, delta-bybit, binance-bybit)."""

    def __init__(self, engines: list[FundingArbEngine]):
        self.engines = engines

    async def run_all(self):
        log.info("MultiPairRunner: launching %d concurrent engines", len(self.engines))
        tasks = [asyncio.create_task(e.run_forever()) for e in self.engines]
        await asyncio.gather(*tasks)
