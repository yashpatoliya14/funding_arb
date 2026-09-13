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
        self.mock_b.get_ticker.return_value = Ticker(
            symbol="BTC", best_bid=100.0, best_ask=101.0, mark_price=100.5,
            funding_rate=0.0, next_funding_time_ms=0
        )
        self.mock_a.place_limit_order.return_value = OrderResult(
            exchange="a", order_id="1", symbol="BTC", side="buy", price=100.0,
            quantity=1.0, status="open"
        )
        self.mock_b.place_limit_order.return_value = OrderResult(
            exchange="b", order_id="2", symbol="BTC", side="sell", price=101.0,
            quantity=1.0, status="open"
        )
        
        state = self.manager.open_hedge("BTC", "buy", "BTC", "sell", 1.0, 1.0)
        assert state.order_a.order_id == "1"
        assert state.order_b.order_id == "2"
        assert state.entry_basis_pct is not None
        
    def test_reprice_max_reprices(self, monkeypatch):
        # 29.11 Order Reprice Tests
        monkeypatch.setattr("core.order_manager.ORDER_REPRICE_INTERVAL_SEC", 0) # speed up
        monkeypatch.setattr("core.order_manager.MAX_REPRICES", 3)
        
        state = DualLegState(
            symbol_a="BTC", symbol_b="BTC",
            order_a=OrderResult("a", "1", "BTC", "buy", 100.0, 1.0, "open"),
            order_b=OrderResult("b", "2", "BTC", "sell", 101.0, 1.0, "open"),
        )
        
        # Simulate price moving
        def mock_get_ticker(*args, **kwargs):
            return Ticker("BTC", best_bid=102.0, best_ask=103.0, mark_price=102.5, funding_rate=0, next_funding_time_ms=0)
        
        self.mock_a.get_ticker.side_effect = mock_get_ticker
        self.mock_b.get_ticker.side_effect = mock_get_ticker
        
        self.mock_a.get_order_status.return_value = state.order_a
        self.mock_b.get_order_status.return_value = state.order_b
        
        def mock_reprice(sym, oid, new_price):
            return OrderResult("x", oid, sym, "buy", new_price, 1.0, "open")
            
        self.mock_a.reprice_order.side_effect = mock_reprice
        self.mock_b.reprice_order.side_effect = mock_reprice
        
        res_state = self.manager.monitor_until_filled(state, "buy", "sell", timeout_sec=5)
        
        assert res_state.aborted is True
        assert res_state.abort_reason == "max_reprices_or_timeout"
        assert self.mock_a.reprice_order.call_count == 3
        assert self.mock_b.reprice_order.call_count == 3

class TestPartialFill:
    def setup_method(self):
        self.mock_a = MagicMock()
        self.mock_b = MagicMock()
        self.mock_notifier = MagicMock()
        self.mock_a.name = "A"
        self.mock_b.name = "B"
        self.manager = DualLegOrderManager(self.mock_a, self.mock_b, self.mock_notifier)
        
    def test_one_leg_failure(self):
        # 29.10 Partial-Fill / One-Leg Failure Tests
        # Leg A = 100% filled, Leg B = 0% filled
        state = DualLegState(
            symbol_a="BTC", symbol_b="BTC",
            order_a=OrderResult("A", "1", "BTC", "buy", 100.0, 1.0, "open"),
            order_b=OrderResult("B", "2", "BTC", "sell", 101.0, 1.0, "open"),
        )
        
        # Status A is filled, B is open
        self.mock_a.get_order_status.return_value = OrderResult("A", "1", "BTC", "buy", 100.0, 1.0, "filled", filled_quantity=1.0)
        self.mock_b.get_order_status.return_value = OrderResult("B", "2", "BTC", "sell", 101.0, 1.0, "open", filled_quantity=0.0)
        
        # Manually set a_filled_time to be outside grace period
        state.a_filled_time = time.time() - 20.0
        
        aborted = self.manager.check_leg_risk(state)
        
        assert aborted is True
        assert state.aborted is True
        assert state.abort_reason == "leg_risk_a_only"
        
        # Assert hedge is flattened: cancel B, market close A
        self.mock_b.cancel_order.assert_called_with("BTC", "2")
        self.mock_a.close_position_market.assert_called_with("BTC")
        self.mock_notifier.leg_risk.assert_called()
        
    def test_partial_fill_risk(self):
        # Leg A = 40% filled, Leg B = 0%
        state = DualLegState(
            symbol_a="BTC", symbol_b="BTC",
            order_a=OrderResult("A", "1", "BTC", "buy", 100.0, 1.0, "open"),
            order_b=OrderResult("B", "2", "BTC", "sell", 101.0, 1.0, "open"),
        )
        
        self.mock_a.get_order_status.return_value = OrderResult("A", "1", "BTC", "buy", 100.0, 1.0, "partially_filled", filled_quantity=0.4)
        self.mock_b.get_order_status.return_value = OrderResult("B", "2", "BTC", "sell", 101.0, 1.0, "open", filled_quantity=0.0)
        
        # Manually set a_filled_time to be outside grace period
        state.a_filled_time = time.time() - 20.0
        
        aborted = self.manager.check_leg_risk(state)
        
        assert aborted is True
        assert state.aborted is True
        self.mock_a.close_position_market.assert_called_with("BTC")
