import pytest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from core.funding_window import (
    is_in_entry_window, seconds_until_entry_window, should_close_now, next_funding_time
)
from core.coin_scanner import CoinScanner
from exchanges.base import Ticker
from unittest.mock import MagicMock

IST = ZoneInfo("Asia/Kolkata")

class TestFundingTiming:
    def test_funding_timing_windows(self, monkeypatch):
        # 29.12 Funding-Timing Tests
        # Let's say next funding is at 13:30 IST today.
        # ENTRY_LEAD_MINUTES = 20
        import core.funding_window
        monkeypatch.setattr(core.funding_window, "ENTRY_LEAD_MINUTES", 20)
        
        # Test 21 minutes before (13:09)
        dt_21m = datetime(2026, 9, 13, 13, 9, tzinfo=IST)
        assert is_in_entry_window(dt_21m) is False
        assert seconds_until_entry_window(dt_21m) == 60
        
        # Test 20 minutes before (13:10)
        dt_20m = datetime(2026, 9, 13, 13, 10, tzinfo=IST)
        assert is_in_entry_window(dt_20m) is True
        assert seconds_until_entry_window(dt_20m) == 0
        
        # Test 19 minutes before (13:11)
        dt_19m = datetime(2026, 9, 13, 13, 11, tzinfo=IST)
        assert is_in_entry_window(dt_19m) is True
        
        # Test 1 minute before (13:29)
        dt_1m = datetime(2026, 9, 13, 13, 29, tzinfo=IST)
        assert is_in_entry_window(dt_1m) is True
        
        # Test exactly at funding (13:30)
        dt_exact = datetime(2026, 9, 13, 13, 30, tzinfo=IST)
        # Entry window ends strictly before the funding time
        assert is_in_entry_window(dt_exact) is False
        
        # Test after funding (13:31)
        dt_after = datetime(2026, 9, 13, 13, 31, tzinfo=IST)
        assert is_in_entry_window(dt_after) is False
        
    def test_post_snapshot_close(self, monkeypatch):
        import core.funding_window
        monkeypatch.setattr(core.funding_window, "POST_SNAPSHOT_CLOSE_DELAY_SEC", 30)
        
        # Funding happened at 13:30.
        # At 13:30:15
        dt_15s = datetime(2026, 9, 13, 13, 30, 15, tzinfo=IST)
        assert should_close_now(dt_15s) is True
        
        # At 13:30:45 (outside strict delay, but within 60s safety margin)
        dt_45s = datetime(2026, 9, 13, 13, 30, 45, tzinfo=IST)
        assert should_close_now(dt_45s) is True
        
        # At 13:32:00 (outside margin)
        dt_120s = datetime(2026, 9, 13, 13, 32, 0, tzinfo=IST)
        assert should_close_now(dt_120s) is False

class TestScanners:
    def test_refresh_instruments(self):
        # 29.13 30-Minute Scanner Tests
        # Verify 3 exchanges queried, common identified, etc.
        scanner = CoinScanner()
        
        mock_a = MagicMock()
        mock_a.name = "A"
        mock_b = MagicMock()
        mock_b.name = "B"
        
        from exchanges.base import InstrumentInfo
        # A has BTC, ETH, SOL
        mock_a.list_instruments.return_value = [
            InstrumentInfo("BTCUSDT", "BTC", "USDT", "perpetual", 0.01, 0.1, True),
            InstrumentInfo("ETHUSDT", "ETH", "USDT", "perpetual", 0.01, 0.1, True),
            InstrumentInfo("SOLUSDT", "SOL", "USDT", "perpetual", 1, 0.01, True),
        ]
        # B has BTC, SOL, MATIC
        mock_b.list_instruments.return_value = [
            InstrumentInfo("BTC-USDT", "BTC", "USDT", "perpetual", 0.01, 0.1, True),
            InstrumentInfo("SOL-USDT", "SOL", "USDT", "perpetual", 1, 0.01, True),
            InstrumentInfo("MATIC-USDT", "MATIC", "USDT", "perpetual", 10, 0.001, True),
        ]
        
        smap = scanner.refresh_instruments({"A": mock_a, "B": mock_b})
        
        common = smap.common_coins("A", "B")
        assert common == ["BTC", "SOL"]
        
    def test_scan_funding_rates(self):
        # 29.14 One-Minute Funding Monitor Tests
        scanner = CoinScanner()
        mock_a = MagicMock()
        mock_a.name = "A"
        mock_b = MagicMock()
        mock_b.name = "B"
        
        # Setup symbol map directly
        scanner._symbol_map.mapping = {
            "BTC": {"A": "BTCUSDT", "B": "BTC-USDT"}
        }
        
        mock_a.get_ticker.return_value = Ticker("BTCUSDT", 100, 101, 100.5, 0.05, 0)
        mock_b.get_ticker.return_value = Ticker("BTC-USDT", 100, 101, 100.5, -0.05, 0)
        
        snapshots = scanner.scan_funding_rates({"A": mock_a, "B": mock_b}, "A", "B")
        
        assert len(snapshots) == 2
        assert snapshots[0].funding_rate == 0.05
        assert snapshots[1].funding_rate == -0.05
        
        # Test opportunity evaluation
        opp = scanner.find_best_opportunity({"A": mock_a, "B": mock_b}, "A", "B")
        assert opp is not None
        assert opp.base_asset == "BTC"
        assert opp.net_funding_pct > 0
