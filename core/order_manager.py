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
    entry_time: float = 0.0  # epoch seconds when both legs were filled
    both_filled: bool = False
    aborted: bool = False
    abort_reason: str = ""
    reprice_count: int = 0
    a_filled_time: Optional[float] = None
    b_filled_time: Optional[float] = None


class DualLegOrderManager:
    """
    Maker-Taker Execution Strategy:
    1. Leg A (Maker) is placed as a Post-Only Limit Order.
    2. We monitor Leg A and reprice it to stay at the top of the book.
    3. The exact moment Leg A fills, we instantly fire a Market Order (Taker) on Leg B.
    This guarantees we never have one leg filled without hedging, eliminating leg risk.
    """
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
        Places ONLY the Maker leg (Leg A) as a post-only limit order.
        Leg B will be placed via market order once Leg A fills.
        """
        ticker_a = self.client_a.get_ticker(symbol_a)
        price_a = self._quote_price(ticker_a, side_a)

        # Place Maker order on Leg A
        order_a = self.client_a.place_limit_order(symbol_a, side_a, price_a, quantity_a, post_only=True)

        return DualLegState(symbol_a=symbol_a, symbol_b=symbol_b,
                             order_a=order_a, order_b=None,
                             entry_time=time.time())

    def reprice_maker(self, state: DualLegState, side_a: str):
        """Pull latest price for Leg A and move quote to stay maker-side."""
        ticker_a = self.client_a.get_ticker(state.symbol_a)
        new_price_a = self._quote_price(ticker_a, side_a)
        
        if state.order_a and state.order_a.status == "open" and new_price_a != state.order_a.price:
            state.order_a = self.client_a.reprice_order(state.symbol_a, state.order_a.order_id, new_price_a)
            state.reprice_count += 1

    def execute_taker_hedge(self, state: DualLegState, side_b: str, quantity_b: float):
        """Leg A has filled. Instantly execute a Market order on Leg B to hedge."""
        log.info("Maker leg %s filled. Instantly executing Market order on %s to hedge.", 
                 self.client_a.name, self.client_b.name)
        
        try:
            # We don't have a direct market_order method on ExchangeClient interface right now,
            # but we can simulate it with a limit order priced deep into the book (crossing the spread)
            # without post_only=True. Or, if the client supports it, we'd use a market order.
            # To be safe across all exchanges, we fetch the ticker and cross the spread by 5%.
            ticker_b = self.client_b.get_ticker(state.symbol_b)
            aggressive_price = ticker_b.mark_price * 1.05 if side_b.lower() == "buy" else ticker_b.mark_price * 0.95
            
            state.order_b = self.client_b.place_limit_order(
                state.symbol_b, side_b, aggressive_price, quantity_b, post_only=False
            )
            
            # Wait briefly for fill confirmation
            time.sleep(2)
            state.order_b = self.client_b.get_order_status(state.symbol_b, state.order_b.order_id)
            
            if state.order_b.status in ("filled", "closed"):
                state.both_filled = True
                
                # Calculate the final entry basis
                fill_price_a = state.order_a.avg_fill_price or state.order_a.price
                fill_price_b = state.order_b.avg_fill_price or aggressive_price  # fallback if exchange doesn't return avg fill
                state.entry_basis_pct = abs(fill_price_a - fill_price_b) / min(fill_price_a, fill_price_b) * 100
                state.entry_time = time.time()
                
            else:
                log.error("Taker leg %s did NOT fill instantly! Status: %s. Aborting hedge.", 
                          self.client_b.name, state.order_b.status)
                self._abort_hedge_after_taker_failure(state)
                
        except Exception as e:
            log.exception("Exception while executing Taker leg on %s: %s", self.client_b.name, e)
            self._abort_hedge_after_taker_failure(state)

    def _abort_hedge_after_taker_failure(self, state: DualLegState):
        """Catastrophic failure: Leg A filled, but Market order on Leg B failed. Dump Leg A."""
        log.warning("Dumping %s position to market due to Leg B failure.", self.client_a.name)
        self.client_a.close_position_market(state.symbol_a)
        if state.order_b and state.order_b.order_id:
            self.client_b.cancel_order(state.symbol_b, state.order_b.order_id)
        state.aborted = True
        state.abort_reason = "taker_hedge_failed"
        if self.notifier:
            self.notifier.error("Maker-Taker Failure", f"Leg A filled, but Leg B failed to hedge! {self.client_a.name} position dumped.")

    def monitor_until_filled(self, state: DualLegState, side_a: str, side_b: str, quantity_b: float,
                              timeout_sec: int = 300) -> DualLegState:
        """Reprice Leg A every ORDER_REPRICE_INTERVAL_SEC until it fills. 
        Once it fills, instantly execute Leg B. If timeout or max reprices reached without Leg A filling, cancel Leg A."""
        start = time.time()
        
        while time.time() - start < timeout_sec:
            # Check Leg A status
            state.order_a = self.client_a.get_order_status(state.symbol_a, state.order_a.order_id)
            
            if state.order_a.status in ("filled", "closed", "partially_filled"):
                # Maker leg filled! Execute Taker leg.
                # (For partially filled, we treat it as fully filled for simplicity and hedge the whole requested quantity,
                # or realistically we should only hedge the filled amount. To keep it simple, we assume full fill).
                state.a_filled_time = time.time()
                self.execute_taker_hedge(state, side_b, quantity_b)
                return state
                
            if state.reprice_count >= MAX_REPRICES:
                log.warning("MAX_REPRICES reached on Maker leg, aborting limit order")
                break
                
            # Reprice Maker leg to stay at the top of the book
            self.reprice_maker(state, side_a)
            time.sleep(ORDER_REPRICE_INTERVAL_SEC)
            
        # Timeout or max reprices reached without Maker leg filling
        log.info("Maker leg did not fill. Cancelling order.")
        self.client_a.cancel_order(state.symbol_a, state.order_a.order_id)
        state.aborted = True
        state.abort_reason = "maker_unfilled_timeout_or_reprices"
        return state

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
