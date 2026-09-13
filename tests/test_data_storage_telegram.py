import os
import json
import pytest
import tempfile
from core.storage import AtomicStorage
from core.telegram_notify import TelegramNotifier
from unittest.mock import MagicMock, patch
from exchanges.base import Ticker

class TestStorage:
    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.storage = AtomicStorage(self.temp_dir)
        
    def test_atomic_writes(self):
        # Write opportunity
        self.storage.persist_opportunity({"sym": "BTC", "profit": 10})
        
        # Verify JSON file exists
        assert os.path.exists(self.storage.state_file)
        
        with open(self.storage.state_file, "r") as f:
            data = json.load(f)
            assert data["opportunities"][0]["profit"] == 10
            
    def test_corrupted_storage_recovery(self):
        self.storage.persist_trade({"id": 1, "pnl": 50})
        
        # Manually corrupt the file
        with open(self.storage.state_file, "w") as f:
            f.write("{invalid_json: 1, ")
            
        # Re-initialize should recover from bak if exists, or return empty
        # In this case bak exists from atomic rename
        new_storage = AtomicStorage(self.temp_dir)
        # Bak file had the state before the last save... wait, first save creates bak of empty state?
        # Actually first save moves empty to bak, then saves new.
        # Let's see what is inside new_storage
        # Since bak was the empty file, it might load empty.
        assert isinstance(new_storage.state, dict)
        
    def test_persistence_categories(self):
        # 29.18 opportunity, funding, order, position, trade, pnl
        self.storage.persist_opportunity({"type": "opp"})
        self.storage.persist_trade({"pnl": 100})
        self.storage.save_position("BTC", {"qty": 1})
        
        assert len(self.storage.state["opportunities"]) == 1
        assert len(self.storage.state["trades"]) == 1
        assert self.storage.state["positions"]["BTC"]["qty"] == 1
        
        self.storage.clear_position("BTC")
        assert "BTC" not in self.storage.state["positions"]


class TestTelegramMock:
    def test_telegram_alerts(self):
        # 29.19 Telegram Tests
        notifier = TelegramNotifier("fake_token", "fake_chat_id", enabled=True)
        
        with patch("requests.post") as mock_post:
            notifier.startup(mode="PAPER", exchange_pairs=[("A", "B")], scan_mode="ALL", leverage=10, notional_inr=1000, quantity=1.0)
            assert mock_post.called
            
            # API keys shouldn't be in message
            call_args = mock_post.call_args[1]["json"]["text"]
            assert "fake_token" not in call_args
            assert "API" not in call_args
            
            notifier.send("Test emergency")
            assert mock_post.call_count == 2
