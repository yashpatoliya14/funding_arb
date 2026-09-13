import pytest
from unittest.mock import patch, MagicMock
from exchanges.delta_client import DeltaClient

class TestExchangeAdapters:
    def test_delta_signature(self):
        client = DeltaClient("key", "secret")
        
        # Override time to test deterministic signature
        with patch("time.time", return_value=1234567890):
            # method + timestamp + path + query_string + payload
            # GET + 1234567890 + /v2/tickers/BTCUSDT +  + 
            headers = client._sign("GET", "/v2/tickers/BTCUSDT")
            assert headers["api-key"] == "key"
            assert headers["timestamp"] == "1234567890"
            assert "signature" in headers
            
    def test_delta_get_ticker(self):
        client = DeltaClient("key", "secret")
        with patch.object(client.session, "request") as mock_req:
            mock_resp = MagicMock()
            mock_resp.json.return_value = {
                "result": {
                    "quotes": {"best_bid": 100, "best_ask": 101},
                    "mark_price": 100.5,
                    "funding_rate": 0.001
                }
            }
            mock_req.return_value = mock_resp
            
            ticker = client.get_ticker("BTCUSDT")
            assert ticker.symbol == "BTCUSDT"
            assert ticker.best_bid == 100
            assert ticker.best_ask == 101
            assert ticker.funding_rate == 0.001
            
    def test_delta_list_instruments(self):
        client = DeltaClient("key", "secret")
        with patch.object(client.session, "request") as mock_req:
            mock_resp = MagicMock()
            mock_resp.json.return_value = {
                "result": [
                    {
                        "symbol": "BTCUSD",
                        "contract_type": "perpetual_futures",
                        "state": "live",
                        "underlying_asset": {"symbol": "BTC"},
                        "quoting_asset": {"symbol": "USD"},
                        "min_size": 1,
                        "tick_size": 0.5
                    },
                    {
                        "symbol": "ETHUSD",
                        "contract_type": "futures", # should be skipped
                        "state": "live"
                    }
                ]
            }
            mock_req.return_value = mock_resp
            
            instruments = client.list_instruments()
            assert len(instruments) == 1
            assert instruments[0].symbol == "BTCUSD"
            assert instruments[0].base_asset == "BTC"
