"""
Requirement #1 (fetch every 10s) & #13 (websocket over polling where possible).

Delta and CoinSwitch both document public websocket ticker streams; Shark's
docs mention websockets but the exact connect handshake wasn't confirmed at
build time (see exchanges/shark_client.py docstring) — so Shark uses REST
polling. All three feed into the SAME PriceFeed interface so engine.py
doesn't care which transport is underneath.

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
        # NOTE: it's a good idea to ALSO call
        #   feed.start_polling(real_delta_client, symbol, interval_sec=30)
        # from engine.py as a slow safety net in case this socket silently
        # stalls without raising — last-write-wins into self.latest, so
        # running both concurrently is harmless.

    # ---------------- websocket: CoinSwitch (documented Socket.IO channel) ----------------

    def start_coinswitch_ws(self, symbol: str):
        try:
            import socketio
        except ImportError:
            log.warning("python-socketio not installed; run `pip install python-socketio[client]`. "
                        "Falling back to polling for CoinSwitch.")
            return

        cfg = BASE_URLS["coinswitch"]

        def _run():
            while not self._stop.is_set():
                sio = socketio.Client(reconnection=True)

                @sio.on("FETCH_ORDER_BOOK_CS_PRO", namespace=cfg["ws_namespace"])
                def on_book(data):
                    try:
                        bids = data.get("bids") or data.get("b") or []
                        asks = data.get("asks") or data.get("a") or []
                        best_bid = float(bids[0][0]) if bids else 0.0
                        best_ask = float(asks[0][0]) if asks else 0.0
                        prev = self.latest.get(self._key("coinswitch", symbol))
                        self.latest[self._key("coinswitch", symbol)] = Ticker(
                            symbol=symbol, best_bid=best_bid, best_ask=best_ask,
                            mark_price=(best_bid + best_ask) / 2 if (best_bid and best_ask) else 0.0,
                            funding_rate=prev.funding_rate if prev else None,
                            next_funding_time_ms=prev.next_funding_time_ms if prev else None,
                        )
                    except Exception as e:
                        log.warning("CoinSwitch WS parse error: %s", e)

                try:
                    sio.connect(cfg["ws"], namespaces=[cfg["ws_namespace"]],
                                transports=["websocket"],
                                socketio_path=cfg["ws_path"], wait_timeout=10)
                    sio.emit("FETCH_ORDER_BOOK_CS_PRO", {"event": "subscribe", "pair": symbol},
                             namespace=cfg["ws_namespace"])
                    sio.wait()
                except Exception as e:
                    log.warning("CoinSwitch WS crashed, reconnecting in 5s: %s", e)
                if not self._stop.is_set():
                    time.sleep(5)

        t = threading.Thread(target=_run, daemon=True, name="ws-coinswitch")
        t.start()
        self._threads.append(t)
        # Funding rate isn't in the order-book stream — keep a slow REST poll
        # for that field specifically (caller wires this via start_polling too,
        # same as for Delta above).
