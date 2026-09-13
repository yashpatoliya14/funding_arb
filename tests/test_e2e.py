import pytest
import asyncio
from unittest.mock import MagicMock, patch
from engine import FundingArbEngine
from core.telegram_notify import TelegramNotifier
from exchanges.base import Ticker
from exchanges.simulator import SimulatedClient

class TestE2E:
    @pytest.mark.asyncio
    async def test_e2e_full_cycle(self):
        # 29.23 E2E with simulated client
        
        # Setup mocks
        mock_a_real = MagicMock()
        mock_a_real.name = "A"
        mock_b_real = MagicMock()
        mock_b_real.name = "B"
        
        sim_a = SimulatedClient(mock_a_real)
        sim_b = SimulatedClient(mock_b_real)
        
        notifier = MagicMock()
        
        engine = FundingArbEngine(
            client_a=sim_a, client_b=sim_b,
            exchange_name_a="A", exchange_name_b="B",
            quantity=1.0, leverage=10, notifier=notifier
        )
        
        from core.coin_scanner import ArbOpportunity
        mock_opp = ArbOpportunity(
            base_asset="BTC",
            funding_exchange="A",
            hedge_exchange="B",
            funding_symbol="BTCUSDT",
            hedge_symbol="BTC-USDT",
            funding_side="buy",
            hedge_side="sell",
            net_funding_pct=0.1,
            spread_cost_pct=0.01,
            slippage_cost_pct=0.01,
            total_cost_pct=0.04,
            entry_fees_pct=0.01,
            exit_fees_pct=0.01,
            funding_received_pct=0.15,
            funding_paid_pct=0.05,
            net_pnl_pct=0.06,
            quantity=1.0,
            notional_inr=10000,
            funding_rate=0.001,
            hedge_rate=-0.001,
            funding_bid=100.0,
            funding_ask=100.5,
            funding_mark=100.25,
            hedge_bid=100.0,
            hedge_ask=100.5,
            hedge_mark=100.25,
            tradeable=True,
            reason="Mock tradeable opp"
        )
        
        engine.scanner.find_best_opportunity = MagicMock(return_value=mock_opp)
        
        # Fresh prices should confirm the opportunity
        mock_a_real.get_ticker.return_value = Ticker("BTCUSDT", 100.0, 100.5, 100.25, 0.05, 0)
        mock_b_real.get_ticker.return_value = Ticker("BTC-USDT", 100.0, 100.5, 100.25, -0.05, 0)
        
        # We need evaluate_funding_trade_full to return a tradeable opportunity.
        # It's imported inside _attempt_entry. We can patch it.
        with patch("core.spread_calc.evaluate_funding_trade_full", return_value=mock_opp):
            with patch("core.order_manager.ORDER_REPRICE_INTERVAL_SEC", 0):
                # Ensure the simulated market instantly crosses the orders to fill them
                def side_effect_ticker_a(sym):
                    return Ticker(sym, 99.0, 99.5, 99.25, 0.05, 0)
                def side_effect_ticker_b(sym):
                    return Ticker(sym, 101.0, 101.5, 101.25, -0.05, 0)
                
                # Mock tick sequence: 1st time for setup, 2nd time for fill evaluation
                mock_a_real.get_ticker.side_effect = [
                    Ticker("BTCUSDT", 100.0, 100.5, 100.25, 0.05, 0), # for evaluate
                    Ticker("BTCUSDT", 100.0, 100.5, 100.25, 0.05, 0), # for quote
                    Ticker("BTCUSDT", 99.0, 99.5, 99.25, 0.05, 0), # crosses our bid of 100.0
                    Ticker("BTCUSDT", 99.0, 99.5, 99.25, 0.05, 0),
                    Ticker("BTCUSDT", 99.0, 99.5, 99.25, 0.05, 0),
                ]
                mock_b_real.get_ticker.side_effect = [
                    Ticker("BTC-USDT", 100.0, 100.5, 100.25, -0.05, 0), # for evaluate
                    Ticker("BTC-USDT", 100.0, 100.5, 100.25, -0.05, 0), # for quote
                    Ticker("BTC-USDT", 101.0, 101.5, 101.25, -0.05, 0), # crosses our ask of 100.5
                    Ticker("BTC-USDT", 101.0, 101.5, 101.25, -0.05, 0),
                    Ticker("BTC-USDT", 101.0, 101.5, 101.25, -0.05, 0),
                ]
                
                engine._attempt_entry()
                
        assert engine._current_state is not None
        assert engine._current_state.both_filled is True
        
        pos_a = sim_a.get_position("BTCUSDT")
        pos_b = sim_b.get_position("BTC-USDT")
        
        assert pos_a is not None
        assert pos_b is not None
        
        # One long, one short
        assert pos_a.side != pos_b.side
