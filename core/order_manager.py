import math
import time
import logging
from dataclasses import dataclass
from typing import Optional

from exchanges.base import ExchangeClient, OrderResult
from config.constants import ORDER_REPRICE_INTERVAL_SEC, MAX_BASIS_DRIFT_PCT, MAX_REPRICES

log = logging.getLogger("order_manager")


@dataclass
class DualLegState:
    symbol_a: str
    symbol_b: str
    order_a: Optional[OrderResult] = None
    order_b: Optional[OrderResult] = None
    entry_basis_pct: Optional[float] = None
    entry_time: float = 0.0  # epoch seconds when orders were placed
    both_filled: bool = False
    aborted: bool = False
    abort_reason: str = ""
    reprice_count: int = 0
    a_filled_time: Optional[float] = None
    b_filled_time: Optional[float] = None


class DualLegOrderManager:
    def __init__(self, client_a: ExchangeClient, client_b: ExchangeClient, notifier=None):
        self.client_a = client_a
        self.client_b = client_b
        self.notifier = notifier

    def _quote_price(self, ticker, side: str) -> float:
        """Post-only: sit at the best bid (buy) or best ask (sell), never cross."""
        return ticker.best_bid if side.lower() == "buy" else ticker.best_ask

    def open_hedge(self, symbol_a: str, side_a: str, symbol_b: str, side_b: str,
                    quantity_a: float, quantity_b: float) -> DualLegState:
        """
        Places both legs as post-only limit orders sitting at the current
        best bid/ask (maker side), records the entry basis, and returns the
        state object the caller should keep polling via monitor_until_filled().
        """
        ticker_a = self.client_a.get_ticker(symbol_a)
        ticker_b = self.client_b.get_ticker(symbol_b)

        price_a = self._quote_price(ticker_a, side_a)
        price_b = self._quote_price(ticker_b, side_b)

        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            future_a = executor.submit(self.client_a.place_limit_order, symbol_a, side_a, price_a, quantity_a, True)
            future_b = executor.submit(self.client_b.place_limit_order, symbol_b, side_b, price_b, quantity_b, True)
            
            order_a = future_a.result()
            order_b = future_b.result()

        entry_basis_pct = abs(price_a - price_b) / min(price_a, price_b) * 100

        return DualLegState(symbol_a=symbol_a, symbol_b=symbol_b,
                             order_a=order_a, order_b=order_b,
                             entry_basis_pct=entry_basis_pct,
                             entry_time=time.time())

    def reprice_both(self, state: DualLegState, side_a: str, side_b: str):
        """Requirement #8: pull latest price, move both quotes to stay maker-side."""
        ticker_a = self.client_a.get_ticker(state.symbol_a)
        ticker_b = self.client_b.get_ticker(state.symbol_b)

        new_price_a = self._quote_price(ticker_a, side_a)
        new_price_b = self._quote_price(ticker_b, side_b)
        
        repriced = False

        if state.order_a and state.order_a.status == "open" and new_price_a != state.order_a.price:
            state.order_a = self.client_a.reprice_order(state.symbol_a, state.order_a.order_id, new_price_a)
            repriced = True
        if state.order_b and state.order_b.status == "open" and new_price_b != state.order_b.price:
            state.order_b = self.client_b.reprice_order(state.symbol_b, state.order_b.order_id, new_price_b)
            repriced = True
            
        if repriced:
            state.reprice_count += 1

    def check_leg_risk(self, state: DualLegState) -> bool:
        """Requirement #7: if one leg filled and the other didn't after a fair
        chance to reprice, close the filled leg immediately rather than sit
        unhedged. Returns True if it took action (caller should treat the
        hedge attempt as aborted)."""
        status_a = self.client_a.get_order_status(state.symbol_a, state.order_a.order_id)
        status_b = self.client_b.get_order_status(state.symbol_b, state.order_b.order_id)
        state.order_a, state.order_b = status_a, status_b

        # A filled or partially filled is considered filled for leg risk check logic
        a_filled = status_a.status in ("filled", "closed", "partially_filled")
        b_filled = status_b.status in ("filled", "closed", "partially_filled")

        if a_filled and not state.a_filled_time:
            state.a_filled_time = time.time()
        if b_filled and not state.b_filled_time:
            state.b_filled_time = time.time()

        if a_filled and b_filled:
            state.both_filled = True
            return False

        now = time.time()
        # Give the second leg 15 seconds to fill/reprice before aborting
        grace_period_sec = 15.0

        if a_filled and not b_filled:
            if now - state.a_filled_time > grace_period_sec:
                log.warning("Leg risk: %s filled, %s did not after grace period — closing %s",
                            self.client_a.name, self.client_b.name, self.client_a.name)
                self.client_b.cancel_order(state.symbol_b, state.order_b.order_id)
                self.client_a.close_position_market(state.symbol_a)
                state.aborted = True
                state.abort_reason = "leg_risk_a_only"
                if self.notifier:
                    self.notifier.leg_risk(
                        state.symbol_a, state.symbol_b,
                        self.client_a.name, self.client_b.name,
                        self.client_a.name, self.client_b.name,
                        state.symbol_a, state.symbol_b,
                    )
                return True
            else:
                log.info("Leg risk: %s filled, waiting for %s (%.1fs elapsed)", self.client_a.name, self.client_b.name, now - state.a_filled_time)

        if b_filled and not a_filled:
            if now - state.b_filled_time > grace_period_sec:
                log.warning("Leg risk: %s filled, %s did not after grace period — closing %s",
                            self.client_b.name, self.client_a.name, self.client_b.name)
                self.client_a.cancel_order(state.symbol_a, state.order_a.order_id)
                self.client_b.close_position_market(state.symbol_b)
                state.aborted = True
                state.abort_reason = "leg_risk_b_only"
                if self.notifier:
                    self.notifier.leg_risk(
                        state.symbol_a, state.symbol_b,
                        self.client_a.name, self.client_b.name,
                        self.client_b.name, self.client_a.name,
                        state.symbol_b, state.symbol_a,
                    )
                return True
            else:
                log.info("Leg risk: %s filled, waiting for %s (%.1fs elapsed)", self.client_b.name, self.client_a.name, now - state.b_filled_time)

        return False

    def check_basis_drift(self, state: DualLegState) -> bool:
        """Requirement #6 kill-switch: if the two-exchange price gap has moved
        against you beyond MAX_BASIS_DRIFT_PCT since entry, close both legs now."""
        if not state.both_filled:
            return False
        ticker_a = self.client_a.get_ticker(state.symbol_a)
        ticker_b = self.client_b.get_ticker(state.symbol_b)
        current_basis_pct = abs(ticker_a.mark_price - ticker_b.mark_price) / min(
            ticker_a.mark_price, ticker_b.mark_price) * 100
        drift = current_basis_pct - (state.entry_basis_pct or 0)

        if drift > MAX_BASIS_DRIFT_PCT:
            log.warning("Basis drift %.4f%% exceeds limit %.4f%% — closing both legs",
                        drift, MAX_BASIS_DRIFT_PCT)
            self.client_a.close_position_market(state.symbol_a)
            self.client_b.close_position_market(state.symbol_b)
            state.aborted = True
            state.abort_reason = "basis_drift"
            if self.notifier:
                self.notifier.basis_drift_stop(
                    state.symbol_a, state.symbol_b,
                    self.client_a.name, self.client_b.name,
                    drift, state.entry_basis_pct or 0, current_basis_pct,
                )
            return True
        return False

    def close_both(self, state: DualLegState):
        # Compute current basis before closing
        current_basis_pct = None
        try:
            ticker_a = self.client_a.get_ticker(state.symbol_a)
            ticker_b = self.client_b.get_ticker(state.symbol_b)
            current_basis_pct = abs(ticker_a.mark_price - ticker_b.mark_price) / min(
                ticker_a.mark_price, ticker_b.mark_price) * 100
        except Exception:
            pass

        self.client_a.close_position_market(state.symbol_a)
        self.client_b.close_position_market(state.symbol_b)

        if self.notifier:
            hold_seconds = time.time() - state.entry_time if state.entry_time else None
            self.notifier.exit(
                symbol_a=state.symbol_a,
                symbol_b=state.symbol_b,
                exchange_a=self.client_a.name,
                exchange_b=self.client_b.name,
                entry_basis_pct=state.entry_basis_pct,
                current_basis_pct=current_basis_pct,
                hold_seconds=hold_seconds,
            )

    def monitor_until_filled(self, state: DualLegState, side_a: str, side_b: str,
                              timeout_sec: int = 300) -> DualLegState:
        """Reprice every ORDER_REPRICE_INTERVAL_SEC, checking for leg risk each cycle,
        until both legs fill, one leg risk-triggers an abort, or timeout."""
        start = time.time()
        while time.time() - start < timeout_sec:
            if self.check_leg_risk(state):
                return state
            if state.both_filled:
                return state
                
            if state.reprice_count >= MAX_REPRICES:
                log.warning("MAX_REPRICES reached, aborting limit orders")
                break
                
            self.reprice_both(state, side_a, side_b)
            time.sleep(ORDER_REPRICE_INTERVAL_SEC)
            
        # timeout or max reprices without both filling -> treat as leg risk, clean up whichever side filled
        self.check_leg_risk(state)
        if not state.both_filled and not state.aborted:
            self.client_a.cancel_order(state.symbol_a, state.order_a.order_id)
            self.client_b.cancel_order(state.symbol_b, state.order_b.order_id)
            state.aborted = True
            state.abort_reason = "max_reprices_or_timeout"
        return state
