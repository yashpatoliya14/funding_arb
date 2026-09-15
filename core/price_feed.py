"""
Requirement #1 (fetch every 10s) & #13 (websocket over polling where possible).

Delta, Binance, and Bybit all support public websocket ticker streams. All
three feed into the SAME PriceFeed interface so engine.py doesn't care which
transport is underneath.

This module keeps a background thread per exchange updating a shared
in-memory cache; engine.py just reads `feed.latest[symbol]`, which is either
the last websocket push or the last poll — always <= 10s stale, satisfying
requirement #1 even for the polling fallback.
"""

import json
import threading
import time
import logging
from typing import Dict, Optional

from config.constants import PRICE_POLL_INTERVAL_SEC, BASE_URLS
from exchanges.base import ExchangeClient, Ticker

log = logging.getLogger("price_feed")


class PriceFeed:
    def __init__(self):
        self.latest: Dict[str, Ticker] = {}   # key: f"{exchange}:{symbol}"
        self._threads = []
        self._stop = threading.Event()

    def _key(self, exchange: str, symbol: str) -> str:
        return f"{exchange}:{symbol}"

    def get(self, exchange: str, symbol: str) -> Optional[Ticker]:
        return self.latest.get(self._key(exchange, symbol))

    def stop(self):
        self._stop.set()

    # ---------------- polling fallback (works for ANY client, any exchange) ----------------

    def start_polling(self, client: ExchangeClient, symbol: str,
                       interval_sec: int = PRICE_POLL_INTERVAL_SEC):
        def _loop():
            while not self._stop.is_set():
                try:
                    ticker = client.get_ticker(symbol)
                    self.latest[self._key(client.name, symbol)] = ticker
                except Exception as e:
                    log.warning("Poll failed for %s:%s -> %s", client.name, symbol, e)
                self._stop.wait(interval_sec)
        t = threading.Thread(target=_loop, daemon=True, name=f"poll-{client.name}-{symbol}")
        t.start()
        self._threads.append(t)

    # ---------------- websocket: Delta (documented public channel) ----------------

    def start_delta_ws(self, symbol: str):
        """Delta's public WS is Socket.IO-free plain JSON over a standard
        websocket — subscribe to the 'v2/ticker' channel. Falls back to
        polling automatically if the socket drops, since the poll thread for
        the same symbol can run concurrently as a safety net (harmless
        redundancy — last-write-wins into self.latest)."""
        try:
            import websocket  # websocket-client package
        except ImportError:
            log.warning("websocket-client not installed; run `pip install websocket-client`. "
                        "Falling back to polling for Delta.")
            return

        ws_url = BASE_URLS["delta"]["ws"]

        def on_message(ws, message):
            try:
                data = json.loads(message)
                if data.get("type") == "v2/ticker" and data.get("symbol") == symbol:
                    self.latest[self._key("delta", symbol)] = Ticker(
                        symbol=symbol,
                        best_bid=float(data.get("best_bid", data.get("mark_price", 0))),
                        best_ask=float(data.get("best_ask", data.get("mark_price", 0))),
                        mark_price=float(data.get("mark_price", 0)),
                        funding_rate=float(data.get("funding_rate", 0)) if data.get("funding_rate") else None,
                        next_funding_time_ms=None,
                    )
            except Exception as e:
                log.warning("Delta WS parse error: %s", e)

        def on_open(ws):
            ws.send(json.dumps({
                "type": "subscribe",
                "payload": {"channels": [{"name": "v2/ticker", "symbols": [symbol]}]},
            }))

        def _run():
            while not self._stop.is_set():
                try:
                    ws = websocket.WebSocketApp(ws_url, on_message=on_message, on_open=on_open)
                    ws.run_forever(ping_interval=20)
                except Exception as e:
                    log.warning("Delta WS crashed, reconnecting in 5s: %s", e)
                if not self._stop.is_set():
                    time.sleep(5)

        t = threading.Thread(target=_run, daemon=True, name="ws-delta")
        t.start()
        self._threads.append(t)

    # ---------------- websocket: Binance Futures (public stream) ----------------

    def start_binance_ws(self, symbol: str):
        """Binance Futures public WS stream for mark price + funding rate.
        Stream: wss://fstream.binance.com/ws/<symbol_lower>@markPrice
        Also subscribes to bookTicker for bid/ask updates."""
        try:
            import websocket
        except ImportError:
            log.warning("websocket-client not installed. Falling back to polling for Binance.")
            return

        ws_url = BASE_URLS["binance"]["ws"]
        sym_lower = symbol.lower()
        # Combined stream for mark price + book ticker
        stream_url = f"{ws_url}/stream?streams={sym_lower}@markPrice/{sym_lower}@bookTicker"

        def on_message(ws, message):
            try:
                wrapper = json.loads(message)
                stream = wrapper.get("stream", "")
                data = wrapper.get("data", {})

                prev = self.latest.get(self._key("binance", symbol))

                if "markPrice" in stream:
                    mark_price = float(data.get("p", 0))  # mark price
                    funding_rate = float(data.get("r", 0)) if data.get("r") else None
                    next_funding_ms = int(data.get("T", 0)) or None

                    self.latest[self._key("binance", symbol)] = Ticker(
                        symbol=symbol,
                        best_bid=prev.best_bid if prev else mark_price,
                        best_ask=prev.best_ask if prev else mark_price,
                        mark_price=mark_price,
                        funding_rate=funding_rate,
                        next_funding_time_ms=next_funding_ms,
                    )
                elif "bookTicker" in stream:
                    best_bid = float(data.get("b", 0))
                    best_ask = float(data.get("a", 0))

                    self.latest[self._key("binance", symbol)] = Ticker(
                        symbol=symbol,
                        best_bid=best_bid,
                        best_ask=best_ask,
                        mark_price=prev.mark_price if prev else (best_bid + best_ask) / 2,
                        funding_rate=prev.funding_rate if prev else None,
                        next_funding_time_ms=prev.next_funding_time_ms if prev else None,
                    )
            except Exception as e:
                log.warning("Binance WS parse error: %s", e)

        def _run():
            while not self._stop.is_set():
                try:
                    ws = websocket.WebSocketApp(stream_url, on_message=on_message)
                    ws.run_forever(ping_interval=20)
                except Exception as e:
                    log.warning("Binance WS crashed, reconnecting in 5s: %s", e)
                if not self._stop.is_set():
                    time.sleep(5)

        t = threading.Thread(target=_run, daemon=True, name="ws-binance")
        t.start()
        self._threads.append(t)

    # ---------------- websocket: Bybit V5 (public linear ticker) ----------------

    def start_bybit_ws(self, symbol: str):
        """Bybit V5 public WebSocket for linear perpetual tickers.
        Endpoint: wss://stream.bybit.com/v5/public/linear
        Subscribe to: tickers.<symbol>"""
        try:
            import websocket
        except ImportError:
            log.warning("websocket-client not installed. Falling back to polling for Bybit.")
            return

        ws_url = BASE_URLS["bybit"]["ws"] + "/v5/public/linear"

        def on_message(ws, message):
            try:
                msg = json.loads(message)
                topic = msg.get("topic", "")
                if f"tickers.{symbol}" not in topic:
                    return
                data = msg.get("data", {})
                if not data:
                    return

                prev = self.latest.get(self._key("bybit", symbol))

                # Bybit sends snapshot (full) and delta (partial) updates
                mark_price = float(data.get("markPrice", 0)) if data.get("markPrice") else (prev.mark_price if prev else 0)
                funding_rate = float(data.get("fundingRate", 0)) if data.get("fundingRate") else (prev.funding_rate if prev else None)
                next_funding_ms = int(data.get("nextFundingTime", 0)) if data.get("nextFundingTime") else (prev.next_funding_time_ms if prev else None)
                best_bid = float(data.get("bid1Price", 0)) if data.get("bid1Price") else (prev.best_bid if prev else mark_price)
                best_ask = float(data.get("ask1Price", 0)) if data.get("ask1Price") else (prev.best_ask if prev else mark_price)

                self.latest[self._key("bybit", symbol)] = Ticker(
                    symbol=symbol,
                    best_bid=best_bid,
                    best_ask=best_ask,
                    mark_price=mark_price,
                    funding_rate=funding_rate,
                    next_funding_time_ms=next_funding_ms,
                )
            except Exception as e:
                log.warning("Bybit WS parse error: %s", e)

        def on_open(ws):
            ws.send(json.dumps({
                "op": "subscribe",
                "args": [f"tickers.{symbol}"],
            }))

        def _run():
            while not self._stop.is_set():
                try:
                    ws = websocket.WebSocketApp(ws_url, on_message=on_message, on_open=on_open)
                    ws.run_forever(ping_interval=20)
                except Exception as e:
                    log.warning("Bybit WS crashed, reconnecting in 5s: %s", e)
                if not self._stop.is_set():
                    time.sleep(5)

        t = threading.Thread(target=_run, daemon=True, name="ws-bybit")
        t.start()
        self._threads.append(t)
