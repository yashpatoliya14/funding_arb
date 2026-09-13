import pytest
from exchanges.simulator import SimulatedClient
from exchanges.base import Ticker
from unittest.mock import MagicMock

class TestPaperSimulator:
    def setup_method(self):
        self.mock_real = MagicMock()
        self.mock_real.name = "real_mock"
        self.sim = SimulatedClient(self.mock_real)
        
    def test_order_creation_zero_fill(self):
        # Best bid = 100, best ask = 101.
        self.mock_real.get_ticker.return_value = Ticker(
            symbol="BTCUSDT", best_bid=100.0, best_ask=101.0, mark_price=100.5,
            funding_rate=0.0, next_funding_time_ms=0
        )
        # Place buy order at 99 -> should not fill
        res = self.sim.place_limit_order("BTCUSDT", "buy", 99.0, 1.0)
        assert res.status == "open"
        assert res.filled_quantity == 0.0
        
        # Ensure position is not created
        assert self.sim.get_position("BTCUSDT") is None

    def test_full_fill_and_movement(self):
        # Best bid = 100, best ask = 101.
        self.mock_real.get_ticker.return_value = Ticker(
            symbol="BTCUSDT", best_bid=100.0, best_ask=101.0, mark_price=100.5,
            funding_rate=0.0, next_funding_time_ms=0
        )
        
        # Place buy order at 99.
        res = self.sim.place_limit_order("BTCUSDT", "buy", 99.0, 1.0)
        assert res.status == "open"
        
        # Market moves down, best ask becomes 98 (crosses our bid of 99)
        self.mock_real.get_ticker.return_value = Ticker(
            symbol="BTCUSDT", best_bid=97.0, best_ask=98.0, mark_price=97.5,
            funding_rate=0.0, next_funding_time_ms=0
        )
        
        # Re-evaluating status should trigger fill
        status_res = self.sim.get_order_status("BTCUSDT", res.order_id)
        assert status_res.status == "filled"
        assert status_res.filled_quantity == 1.0
        
        pos = self.sim.get_position("BTCUSDT")
        assert pos is not None
        assert pos.side == "long"
        assert pos.quantity == 1.0
        
    def test_order_cancellation(self):
        self.mock_real.get_ticker.return_value = Ticker(
            symbol="BTCUSDT", best_bid=100.0, best_ask=101.0, mark_price=100.5,
            funding_rate=0.0, next_funding_time_ms=0
        )
        res = self.sim.place_limit_order("BTCUSDT", "buy", 99.0, 1.0)
        
        self.sim.cancel_order("BTCUSDT", res.order_id)
        status = self.sim.get_order_status("BTCUSDT", res.order_id)
        assert status.status == "cancelled"
        
    def test_order_replacement(self):
        self.mock_real.get_ticker.return_value = Ticker(
            symbol="BTCUSDT", best_bid=100.0, best_ask=101.0, mark_price=100.5,
            funding_rate=0.0, next_funding_time_ms=0
        )
        res = self.sim.place_limit_order("BTCUSDT", "buy", 99.0, 1.0)
        
        # Reprice to 102 -> should instantly fill because it crosses best ask (101.0)
        rep = self.sim.reprice_order("BTCUSDT", res.order_id, 102.0)
        assert rep.status == "filled"
        assert rep.price == 102.0
        assert rep.filled_quantity == 1.0

    def test_live_paper_isolation(self):
        # 29.21 Live/Paper Isolation Test
        # Ensure that calling sim methods does NOT call real client mutating methods
        self.mock_real.get_ticker.return_value = Ticker(
            symbol="BTC", best_bid=100.0, best_ask=101.0, mark_price=100.5,
            funding_rate=0.0, next_funding_time_ms=0
        )
        self.sim.place_limit_order("BTC", "buy", 99.0, 1.0)
        self.mock_real.place_limit_order.assert_not_called()
        
        res = self.sim.open_orders.copy().popitem()[1]
        self.sim.cancel_order("BTC", res["symbol"]) # passing order_id, wait, key is order_id
        self.mock_real.cancel_order.assert_not_called()
        
        # Fake a fill
        first_key = list(self.sim.open_orders.keys())[0]
        self.sim.open_orders[first_key]["status"] = "filled"
        self.sim.open_orders[first_key]["filled"] = 1.0
        self.sim.open_orders[first_key]["fill_price"] = 99.0
        self.sim._apply_fill(self.sim.open_orders[first_key])
        
        self.sim.close_position_market("BTC")
        self.mock_real.close_position_market.assert_not_called()
