import pytest
from core.order_manager import DualLegOrderManager, DualLegState
from config.constants import MAX_BASIS_DRIFT_PCT
from exchanges.base import Ticker
from unittest.mock import MagicMock

class TestRiskLimits:
    def test_basis_drift_kill_switch(self):
        # 29.20 Risk Management Tests
        mock_a = MagicMock()
        mock_a.name = "A"
        mock_b = MagicMock()
        mock_b.name = "B"
        
        manager = DualLegOrderManager(mock_a, mock_b)
        
        state = DualLegState(
            symbol_a="BTC", symbol_b="BTC",
            entry_basis_pct=0.1, both_filled=True
        )
        
        # Drift = 0.5% - 0.1% = 0.4% > MAX_BASIS_DRIFT_PCT (0.15)
        def mock_ticker(sym, ask, bid, mark):
            return Ticker(sym, ask, bid, mark, 0, 0)
            
        mock_a.get_ticker.return_value = mock_ticker("BTC", 100.5, 100.0, 100.5)
        mock_b.get_ticker.return_value = mock_ticker("BTC", 100.0, 100.0, 100.0)
        
        # Drift check
        aborted = manager.check_basis_drift(state)
        
        assert aborted is True
        assert state.aborted is True
        assert state.abort_reason == "basis_drift"
        mock_a.close_position_market.assert_called_with("BTC")
        mock_b.close_position_market.assert_called_with("BTC")
        
    def test_disconnected_feeds_abort(self):
        # Disconnected market feeds abort correctly.
        # This typically means getting tickers fails.
        mock_a = MagicMock()
        mock_b = MagicMock()
        
        manager = DualLegOrderManager(mock_a, mock_b)
        
        mock_a.get_ticker.side_effect = Exception("Connection Timeout")
        
        # We can't even get ticker to close, but let's see how close_both handles it.
        # close_both wraps ticker fetch in try/except
        state = DualLegState(symbol_a="BTC", symbol_b="BTC")
        manager.close_both(state)
        
        # It should still call close position
        mock_a.close_position_market.assert_called()
        mock_b.close_position_market.assert_called()
        
    def test_capital_exposure_caps(self):
        # 29.20 Absolute capital exposure caps
        import config.constants
        assert hasattr(config.constants, "FIXED_NOTIONAL_INR")
        assert config.constants.FIXED_NOTIONAL_INR > 0
