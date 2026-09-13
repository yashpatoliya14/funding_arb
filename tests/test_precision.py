import pytest
from core.precision import round_price, round_quantity

class TestPrecision:
    def test_price_rounding_buy(self):
        # Tick size = 0.5. Current price = 100.7.
        # Buying (going long) -> we sit on bid, so we don't want to cross. We round DOWN to nearest tick.
        assert round_price(100.7, 0.5, "buy") == 100.5
        assert round_price(100.9, 0.5, "buy") == 100.5
        assert round_price(101.0, 0.5, "buy") == 101.0
        assert round_price(101.4, 0.5, "buy") == 101.0
        
    def test_price_rounding_sell(self):
        # Tick size = 0.5. Current price = 100.7.
        # Selling (going short) -> we sit on ask. Round UP to nearest tick.
        assert round_price(100.7, 0.5, "sell") == 101.0
        assert round_price(100.1, 0.5, "sell") == 100.5
        assert round_price(100.0, 0.5, "sell") == 100.0
        assert round_price(100.6, 0.5, "sell") == 101.0
        
    def test_quantity_rounding_down(self):
        # min_qty = 0.1, step_size = 0.1
        assert round_quantity(1.23, 0.1, 0.1) == 1.2
        assert round_quantity(1.29, 0.1, 0.1) == 1.2
        assert round_quantity(0.05, 0.1, 0.1) == 0.0 # Below min qty
        assert round_quantity(1.234, 0.01, 0.01) == 1.23
        
    def test_quantity_no_step(self):
        # step size = 0 uses min_qty
        assert round_quantity(1.23, 0.1, 0.0) == 1.2
        assert round_quantity(1.23, 0.2, 0.0) == 1.2
