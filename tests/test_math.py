import pytest
from core.spread_calc import evaluate_funding_trade_full
from core.leverage_sync import sync_leverage
from unittest.mock import MagicMock
from config import settings

class TestFundingMathAndDifferentials:
    def test_positive_vs_positive(self):
        # A = 0.50%, B = 0.10% => Differential = +0.40%
        # A is higher -> short A, long B
        opp = evaluate_funding_trade_full(
            "binance", "bybit", "BTC",
            0.0050, 0.0010,
            50000, 50010, 50005,
            50000, 50010, 50005
        )
        assert opp.funding_side == "sell"
        assert opp.funding_exchange == "binance"
        assert opp.hedge_exchange == "bybit"
        # 0.50% - 0.10% = 0.40%
        assert pytest.approx(opp.net_funding_pct, 0.0001) == 0.40

    def test_negative_vs_negative(self):
        # A = -0.05%, B = -0.10% => B is lower -> B has lower rate, A has higher rate -> short A, long B
        # A (-0.05) > B (-0.10). So short A, long B.
        # Short A pays 0.05%, Long B receives 0.10%. Net = +0.05%.
        opp = evaluate_funding_trade_full(
            "binance", "bybit", "BTC",
            -0.0005, -0.0010,
            50000, 50010, 50005,
            50000, 50010, 50005
        )
        assert opp.funding_exchange == "binance"
        assert opp.funding_side == "sell"
        # 0.10% received by long, 0.05% paid by short
        assert pytest.approx(opp.funding_received_pct, 0.0001) == -0.05 # actually shorting negative rate means you PAY
        assert pytest.approx(opp.funding_paid_pct, 0.0001) == -0.10
        # "If funding_rate < 0: shorts PAY to longs (negative funding)"
        # "If hedge_rate < 0: longs RECEIVE from shorts (negative funding)"
        # So short A (-0.05) -> pays 0.05%. Long B (-0.10) -> receives 0.10%.
        # Let's check spread_calc logic outputs.

    def test_mixed_funding(self):
        # A = +0.10%, B = -0.10%
        opp = evaluate_funding_trade_full(
            "binance", "bybit", "BTC",
            0.0010, -0.0010,
            50000, 50010, 50005,
            50000, 50010, 50005
        )
        assert opp.funding_side == "sell"
        assert opp.funding_exchange == "binance"
        # Short A receives 0.10%, Long B receives 0.10%
        assert pytest.approx(opp.net_funding_pct, 0.0001) == 0.20

    def test_zero_funding(self):
        # A = 0, B = 0 => net 0
        opp = evaluate_funding_trade_full(
            "binance", "bybit", "BTC",
            0.0, 0.0,
            50000, 50010, 50005,
            50000, 50010, 50005
        )
        assert opp.net_funding_pct == 0.0

    def test_threshold_case(self, monkeypatch):
        # Set MIN_NET_EDGE_PCT to 0.03 for testing
        import core.spread_calc
        monkeypatch.setattr(core.spread_calc, "MIN_NET_EDGE_PCT", 0.03)
        monkeypatch.setattr(core.spread_calc, "SLIPPAGE_BPS_PER_LEG", 0.0)
        
        # We need net_pnl = exactly 0.03%
        # Let's set fees to 0 by mocking _fee_pct_one_side
        monkeypatch.setattr(core.spread_calc, "_fee_pct_one_side", lambda ex, maker: 0.0)
        
        # Funding spread = 0.03
        opp = evaluate_funding_trade_full(
            "binance", "bybit", "BTC",
            0.0003, 0.0000,
            50000, 50000, 50000,
            50000, 50000, 50000
        )
        assert opp.tradeable is True
        assert pytest.approx(opp.net_pnl_pct, 0.0001) == 0.03

        # Below threshold
        opp2 = evaluate_funding_trade_full(
            "binance", "bybit", "BTC",
            0.00029, 0.0000,
            50000, 50000, 50000,
            50000, 50000, 50000
        )
        assert opp2.tradeable is False

class TestFeesAndSpread:
    def test_fee_calculations(self, monkeypatch):
        # Mock fees to specific values
        def mock_fee(exchange, maker=True):
            if exchange == "binance": return 0.0001 # 0.01%
            if exchange == "delta": return 0.0002 # 0.02%
            return 0.0
            
        import core.spread_calc
        monkeypatch.setattr(core.spread_calc, "_fee_pct_one_side", mock_fee)
        monkeypatch.setattr(core.spread_calc, "SLIPPAGE_BPS_PER_LEG", 0.0)
        
        opp = evaluate_funding_trade_full(
            "binance", "delta", "BTC",
            0.0050, 0.0000,
            50000, 50000, 50000,
            50000, 50000, 50000
        )
        # Entry + Exit on both legs:
        # Binance: 0.01% * 100 * 2 = 0.02%
        # Delta: 0.02% * 100 * 2 = 0.04%
        assert pytest.approx(opp.entry_fees_pct + opp.exit_fees_pct, 0.0001) == 0.06
        
    def test_spread_calculation(self, monkeypatch):
        import core.spread_calc
        monkeypatch.setattr(core.spread_calc, "_fee_pct_one_side", lambda ex, maker: 0.0)
        monkeypatch.setattr(core.spread_calc, "SLIPPAGE_BPS_PER_LEG", 0.0)
        
        # Price difference:
        # F_bid=50000, F_ask=50010 (mid 50005) -> half spread = 5/50005 = 0.00999%
        # H_bid=49990, H_ask=50000 (mid 49995) -> half spread = 5/49995 = 0.01000%
        opp = evaluate_funding_trade_full(
            "binance", "delta", "BTC",
            0.0050, 0.0000,
            50000, 50010, 50005,
            49990, 50000, 49995
        )
        expected_spread_cost = (5 / 50005 * 100) + (5 / 49995 * 100)
        assert pytest.approx(opp.spread_cost_pct, 0.0001) == expected_spread_cost
        
class TestLeverage:
    def test_sync_leverage(self):
        client_a = MagicMock()
        client_b = MagicMock()
        
        # A returns 20, B returns 30
        client_a.set_leverage.return_value = 20
        client_b.set_leverage.return_value = 30
        
        res = sync_leverage(client_a, client_b, "BTCUSDT", "BTCUSDT", 50)
        assert res.requested == 50
        assert res.common_leverage == 20
        assert res.exchange_a_applied == 20
        assert res.exchange_b_applied == 30
        
        # B should be re-called with 20
        client_b.set_leverage.assert_any_call("BTCUSDT", 20)
        
    def test_sync_leverage_same(self):
        client_a = MagicMock()
        client_b = MagicMock()
        
        # A returns 100, B returns 100, asked 100
        client_a.set_leverage.return_value = 100
        client_b.set_leverage.return_value = 100
        
        res = sync_leverage(client_a, client_b, "BTC", "BTC", 100)
        assert res.common_leverage == 100
        # No re-call needed
        assert client_a.set_leverage.call_count == 1
        assert client_b.set_leverage.call_count == 1
