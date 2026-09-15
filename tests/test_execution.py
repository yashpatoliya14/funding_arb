import pytest
from core.order_manager import DualLegOrderManager, DualLegState
from exchanges.base import OrderResult, Ticker
from unittest.mock import MagicMock
import time

class TestExecution:
    def setup_method(self):
        self.mock_a = MagicMock()
        self.mock_b = MagicMock()
        self.mock_notifier = MagicMock()
        self.manager = DualLegOrderManager(self.mock_a, self.mock_b, self.mock_notifier)
        
    def test_open_hedge_state(self):
        self.mock_a.get_ticker.return_value = Ticker(
            symbol="BTC", best_bid=100.0, best_ask=101.0, mark_price=100.5,
            funding_rate=0.0, next_funding_time_ms=0
        )
        self.mock_a.place_limit_order.return_value = OrderResult(
            exchange="a", order_id="1", symbol="BTC", side="buy", price=100.0,
            quantity=1.0, status="open"
        )
        
        state = self.manager.open_hedge("BTC", "buy", "BTC", "sell", 1.0, 1.0)
        assert state.order_a.order_id == "1"
        assert state.order_b is None  # Maker-taker: Leg B is not placed yet
        
    def test_reprice_max_reprices(self, monkeypatch):
        # 29.11 Order Reprice Tests
        monkeypatch.setattr("core.order_manager.ORDER_REPRICE_INTERVAL_SEC", 0) # speed up
        monkeypatch.setattr("core.order_manager.MAX_REPRICES", 3)
        
        state = DualLegState(
            symbol_a="BTC", symbol_b="BTC",
            order_a=OrderResult("a", "1", "BTC", "buy", 100.0, 1.0, "open"),
            order_b=None,
        )
        
        # Simulate price moving
        def mock_get_ticker(*args, **kwargs):
            return Ticker("BTC", best_bid=102.0, best_ask=103.0, mark_price=102.5, funding_rate=0, next_funding_time_ms=0)
        
        self.mock_a.get_ticker.side_effect = mock_get_ticker
        self.mock_a.get_order_status.return_value = state.order_a
        
        def mock_reprice(sym, oid, new_price):
            return OrderResult("x", oid, sym, "buy", new_price, 1.0, "open")
            
        self.mock_a.reprice_order.side_effect = mock_reprice
        
        res_state = self.manager.monitor_until_filled(state, "buy", "sell", 1.0, timeout_sec=5)
        
        assert res_state.aborted is True
        assert res_state.abort_reason == "maker_unfilled_timeout_or_reprices"
        assert self.mock_a.reprice_order.call_count == 3
        # Ensure it cancelled the Maker leg
        self.mock_a.cancel_order.assert_called_with("BTC", "1")

class TestPartialFill:
    def setup_method(self):
        self.mock_a = MagicMock()
        self.mock_b = MagicMock()
        self.mock_notifier = MagicMock()
        self.mock_a.name = "A"
        self.mock_b.name = "B"
        self.manager = DualLegOrderManager(self.mock_a, self.mock_b, self.mock_notifier)
        
    def test_taker_hedge_execution(self):
        # Leg A fills, Leg B should fire instantly
        state = DualLegState(
            symbol_a="BTC", symbol_b="BTC",
            order_a=OrderResult("A", "1", "BTC", "buy", 100.0, 1.0, "filled", filled_quantity=1.0),
            order_b=None,
        )
        
        self.mock_b.get_ticker.return_value = Ticker("BTC", best_bid=100.0, best_ask=101.0, mark_price=100.5, funding_rate=0, next_funding_time_ms=0)
        self.mock_b.place_limit_order.return_value = OrderResult("B", "2", "BTC", "sell", 101.0, 1.0, "open")
        self.mock_b.get_order_status.return_value = OrderResult("B", "2", "BTC", "sell", 101.0, 1.0, "filled", filled_quantity=1.0)
        
        self.manager.execute_taker_hedge(state, "sell", 1.0)
        
        assert state.both_filled is True
        assert state.entry_basis_pct is not None
        assert self.mock_b.place_limit_order.called

    def test_taker_leg_failure(self):
        # Leg A fills, but Leg B fails to fill
        state = DualLegState(
            symbol_a="BTC", symbol_b="BTC",
            order_a=OrderResult("A", "1", "BTC", "buy", 100.0, 1.0, "filled", filled_quantity=1.0),
            order_b=None,
        )
        
        self.mock_b.get_ticker.return_value = Ticker("BTC", best_bid=100.0, best_ask=101.0, mark_price=100.5, funding_rate=0, next_funding_time_ms=0)
        self.mock_b.place_limit_order.return_value = OrderResult("B", "2", "BTC", "sell", 101.0, 1.0, "open")
        self.mock_b.get_order_status.return_value = OrderResult("B", "2", "BTC", "sell", 101.0, 1.0, "open", filled_quantity=0.0)
        
        self.manager.execute_taker_hedge(state, "sell", 1.0)
        
        # Should abort and market close A
        assert state.aborted is True
        assert state.abort_reason == "taker_hedge_failed"
        self.mock_a.close_position_market.assert_called_with("BTC")
        self.mock_b.cancel_order.assert_called_with("BTC", "2")
