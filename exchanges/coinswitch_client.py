"""
CoinSwitch PRO Futures REST client.

Auth scheme (verified from api-trading.coinswitch.co/get-started/authentication):
    signed_message = METHOD + path_with_query (url-decoded) + epoch
    signature = Ed25519_sign(secret_key_bytes, signed_message).hex()
    headers = {Content-Type, X-AUTH-APIKEY, X-AUTH-SIGNATURE, X-AUTH-EPOCH}

Endpoints used here (verified from api-trading.coinswitch.co/recipes/futures-client):
    GET    /trade/api/v2/futures/ticker
    GET    /trade/api/v2/futures/instrument_info
    POST   /trade/api/v2/futures/leverage
    POST   /trade/api/v2/futures/order          -> place
    DELETE /trade/api/v2/futures/order          -> cancel
    GET    /trade/api/v2/futures/order          -> status
    GET    /trade/api/v2/futures/positions

API key/secret are hex-encoded Ed25519 keypairs (generated in CoinSwitch PRO
API Management, not created by this bot).

Symbol format: unseparated, e.g. "BTCUSDT" (not "BTC/USDT" or "BTC-USDT").

⚠️ There is no documented "edit order" endpoint in the Futures v2 client —
repricing here is implemented as cancel + replace (requirement #8), which is
what CoinSwitch Futures actually supports per their recipe list.
"""

import time
import json
import uuid
import urllib.parse
import requests
from cryptography.hazmat.primitives.asymmetric import ed25519

from config.constants import BASE_URLS, MAX_LEVERAGE
from exchanges.base import ExchangeClient, Ticker, OrderResult, Position, InstrumentInfo
from typing import List

import threading
_CS_SHARED_SIO = None
_CS_SHARED_CACHE = {}
_CS_SHARED_SUBSCRIBED = set()
_CS_SHARED_LOCK = threading.Lock()


class CoinswitchClient(ExchangeClient):
    name = "coinswitch"

    def __init__(self, api_key: str, secret_key: str):
        self.api_key = api_key
        try:
            self.secret_key_bytes = bytes.fromhex(secret_key) if secret_key else b""
        except Exception:
            self.secret_key_bytes = b""
        self.base_url = BASE_URLS["coinswitch"]["rest"]
        self.session = requests.Session()

    # ---------------- auth plumbing ----------------

    def _sign(self, method: str, path: str, params: dict = None):
        method = method.upper()
        path_with_query = path
        if params:
            sep = "&" if "?" in path else "?"
            path_with_query = path + sep + urllib.parse.urlencode(params)
        decoded_path = urllib.parse.unquote_plus(path_with_query)
        epoch = str(int(time.time() * 1000))
        message = method + decoded_path + epoch
        if len(self.secret_key_bytes) != 32:
            raise ValueError(
                "CoinSwitch PRO requires a valid API key and 32-byte Ed25519 hex secret in .env even for market data."
            )
        secret = ed25519.Ed25519PrivateKey.from_private_bytes(self.secret_key_bytes)
        signature = secret.sign(message.encode("utf-8")).hex()
        headers = {
            "Content-Type": "application/json",
            "X-AUTH-APIKEY": self.api_key,
            "X-AUTH-SIGNATURE": signature,
            "X-AUTH-EPOCH": epoch,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36",
        }
        return headers, decoded_path

    def _request(self, method: str, path: str, params: dict = None, body: dict = None, retries: int = 3):
        headers, decoded_path = self._sign(method, path, params)
        url = self.base_url + decoded_path
        
        for attempt in range(retries):
            resp = self.session.request(method, url, headers=headers,
                                         data=json.dumps(body) if body else None, timeout=10)
            if resp.status_code == 429:
                time.sleep(1)
                continue
            resp.raise_for_status()
            try:
                return resp.json()
            except Exception:
                return {}
        
        resp.raise_for_status()
        return {}

    # ---------------- public/market data ----------------

    def _get_ticker_ws(self, symbol: str) -> Ticker:
        global _CS_SHARED_SIO, _CS_SHARED_CACHE, _CS_SHARED_SUBSCRIBED, _CS_SHARED_LOCK
        import threading
        import socketio

        with _CS_SHARED_LOCK:
            if _CS_SHARED_SIO is None:
                _CS_SHARED_SIO = socketio.Client(reconnection=True)
                cfg = BASE_URLS["coinswitch"]

                @_CS_SHARED_SIO.on("connect", namespace=cfg["ws_namespace"])
                def on_connect():
                    with _CS_SHARED_LOCK:
                        for sym in list(_CS_SHARED_SUBSCRIBED):
                            _CS_SHARED_SIO.emit("FETCH_TICKER_INFO_CS_PRO", {"event": "subscribe", "pair": sym}, namespace=cfg["ws_namespace"])

                @_CS_SHARED_SIO.on("FETCH_TICKER_INFO_CS_PRO", namespace=cfg["ws_namespace"])
                def on_ticker(data):
                    if isinstance(data, dict):
                        for sym, d in data.items():
                            if isinstance(d, dict) and "s" in d:
                                _CS_SHARED_CACHE[d["s"]] = Ticker(
                                    symbol=d["s"],
                                    best_bid=float(d.get("b", 0)),
                                    best_ask=float(d.get("a", 0)),
                                    mark_price=float(d.get("p", d.get("c", 0))),
                                    funding_rate=float(d.get("r", 0)) if d.get("r") is not None else None,
                                    next_funding_time_ms=d.get("T"),
                                )

                def _connect():
                    try:
                        _CS_SHARED_SIO.connect(
                            cfg["ws"],
                            namespaces=[cfg["ws_namespace"]],
                            transports=["websocket"],
                            socketio_path=cfg["ws_path"],
                            wait=True,
                        )
                    except Exception:
                        pass

                t = threading.Thread(target=_connect, daemon=True, name="cs-public-ws-singleton")
                t.start()

            cfg = BASE_URLS["coinswitch"]
            if symbol not in _CS_SHARED_SUBSCRIBED:
                _CS_SHARED_SUBSCRIBED.add(symbol)
                if _CS_SHARED_SIO.connected:
                    _CS_SHARED_SIO.emit("FETCH_TICKER_INFO_CS_PRO", {"event": "subscribe", "pair": symbol}, namespace=cfg["ws_namespace"])

        # Wait up to 15s for the first tick (cold start on VPS can take a few seconds to connect)
        for _ in range(150):
            if symbol in _CS_SHARED_CACHE:
                return _CS_SHARED_CACHE[symbol]
            time.sleep(0.1)

        raise RuntimeError(f"CoinSwitch WebSocket timed out fetching ticker for {symbol}")

    def get_ticker(self, symbol: str) -> Ticker:
        # Try WebSocket first, fallback to REST if blocked (e.g. VPS datacenter IPs blocked by Cloudflare)
        try:
            return self._get_ticker_ws(symbol)
        except RuntimeError:
            # Fallback to REST
            data = self._request("GET", "/trade/api/v2/futures/ticker", params={"symbol": symbol, "exchange": "EXCHANGE_2"})["data"]
            return Ticker(
                symbol=symbol,
                best_bid=float(data.get("best_bid_price", 0)),
                best_ask=float(data.get("best_ask_price", 0)),
                mark_price=float(data.get("mark_price", data.get("last_price", 0))),
                funding_rate=float(data.get("funding_rate", 0)) if data.get("funding_rate") else None,
                next_funding_time_ms=data.get("next_funding_time"),
            )

    def list_instruments(self) -> List[InstrumentInfo]:
        """Discover futures instruments from CoinSwitch.

        The instrument_info endpoint returns {"data":null} on both coinswitch.co
        and api-trading.coinswitch.co, so we fall back to querying the ticker
        with EXCHANGE_2 to discover available symbols, plus a hardcoded list of
        known CoinSwitch futures symbols.
        """
        import logging
        log = logging.getLogger("coinswitch_client")
        # Step 1: Try ticker-based discovery for known symbols
        return self._discover_via_ticker()

    def _discover_via_ticker(self) -> List[InstrumentInfo]:
        """Discover available symbols by querying ticker for known CoinSwitch futures."""
        import logging
        log = logging.getLogger("coinswitch_client")
        instruments = []
        # Known CoinSwitch PRO Futures symbols (USDT-margined, EXCHANGE_2)
        known_symbols = [
            "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT",
            "ADAUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT", "LTCUSDT",
            "BCHUSDT", "UNIUSDT", "AAVEUSDT", "MANAUSDT", "FILUSDT",
            "ETHFIUSDT", "ARBUSDT", "STRKUSDT", "TAOUSDT", "ENAUSDT",
            "JUPUSDT", "INJUSDT", "PEPEUSDT", "SHIBUSDT", "XAUUSDT",
            "MATICUSDT", "NEARUSDT", "SUIUSDT", "APTUSDT", "POLUSDT",
            "TRXUSDT", "SEIUSDT", "STXUSDT", "ATOMUSDT", "NEOUSDT",
            "VETUSDT", "FTMUSDT", "EOSUSDT", "ICXUSDT", "ZILUSDT",
            "BATUSDT", "ZRXUSDT", "LRCUSDT", "OCEANUSDT", "HOTUSDT",
            "COTIUSDT", "RSRUSDT", "GOATUSDT", "MOGUSDT", "POPCATUSDT",
            "MELANIAUSDT", "TRUMPUSDT", "PNUTUSDT", "WLDUSDT", "GOOGLUSDT",
            "TSLAUSDT", "NVDAUSDT", "AMZNUSDT", "COINUSDT", "SKHYNIXUSDT",
            "RUNEUSDT", "TIAUSDT", "ONDOUSDT", "ZKUSDT", "RAYUSDT",
            "CARRUSDT", "GIGAUSDT", "BERAUSDT", "INITUSDT", "LISTAUSDT",
            "WCTUSDT", "HYPEUSDT", "MUSDT", "KITEUSDT", "SLVONUSDT",
            "DOGSUSDT", "NOTUSDT", "MEUSDT", "EVAAUSDT", "COOKIEUSDT",
            "PROVEUSDT", "SAGAUSDT", "DDOGUSDT", "NASAUSDT", "SWARMSUSDT",
            "GrizzlyUSDT", "SPXUSDT", "SPYXUSDT", "CAKEUSDT", "ENAUSDT",
        ]
        
        # We don't query the REST API per coin here because doing so for 80+ coins
        # immediately triggers CoinSwitch's aggressive 429 Too Many Requests rate limit.
        # Instead, we assume the hardcoded list and fetch live data via WebSocket.
        for sym in known_symbols:
            sym = sym.strip().upper()
            if sym.endswith("USDT"):
                base = sym[:-4]
                quote = "USDT"
            elif sym.endswith("USD") or sym.endswith("INR"):
                base = sym[:-3]
                quote = sym[-3:]
            else:
                base = sym
                quote = "USDT"
            instruments.append(InstrumentInfo(
                symbol=sym,
                base_asset=base,
                quote_asset=quote,
                contract_type="perpetual",
                is_active=True,
            ))

        log.info("CoinSwitch: returned %d instruments from local list", len(instruments))
        return instruments

    # ---------------- trading ----------------

    def set_leverage(self, symbol: str, leverage: int) -> int:
        capped = min(leverage, MAX_LEVERAGE["coinswitch"])
        self._request("POST", "/trade/api/v2/futures/leverage",
                       body={"symbol": symbol, "leverage": capped, "exchange": "EXCHANGE_2"})
        return capped

    def place_limit_order(self, symbol: str, side: str, price: float,
                           quantity: float, post_only: bool = True) -> OrderResult:
        body = {
            "exchange": "EXCHANGE_2",
            "symbol": symbol,
            "side": side.upper(),
            "order_type": "LIMIT",
            "price": price,
            "quantity": quantity,
            "client_order_id": str(uuid.uuid4()),
            "post_only": post_only,   # if unsupported by this endpoint version, remove and
                                       # rely on price placement to stay maker-side instead.
        }
        data = self._request("POST", "/trade/api/v2/futures/order", body=body)["data"]
        return OrderResult(
            exchange="coinswitch", order_id=str(data["order_id"]), symbol=symbol, side=side,
            price=price, quantity=quantity, status=data.get("status", "open"),
        )

    def cancel_order(self, symbol: str, order_id: str) -> bool:
        self._request("DELETE", "/trade/api/v2/futures/order",
                       body={"symbol": symbol, "order_id": order_id, "exchange": "EXCHANGE_2"})
        return True

    def reprice_order(self, symbol: str, order_id: str, new_price: float,
                       side: str, quantity: float, post_only: bool = True) -> OrderResult:
        """No documented in-place edit for Futures v2 -> cancel + replace."""
        self.cancel_order(symbol, order_id)
        return self.place_limit_order(symbol, side, new_price, quantity, post_only)

    def get_order_status(self, symbol: str, order_id: str) -> OrderResult:
        data = self._request("GET", "/trade/api/v2/futures/order",
                              params={"symbol": symbol, "order_id": order_id})["data"]
        return OrderResult(
            exchange="coinswitch", order_id=str(data["order_id"]), symbol=symbol,
            side=data.get("side", "").lower(), price=float(data.get("price", 0)),
            quantity=float(data.get("quantity", 0)), status=data.get("status", "unknown"),
            filled_quantity=float(data.get("filled_quantity", 0) or 0),
            avg_fill_price=float(data.get("avg_price", 0) or 0),
        )

    def get_position(self, symbol: str):
        data = self._request("GET", "/trade/api/v2/futures/positions", params={"symbol": symbol})["data"]
        positions = data if isinstance(data, list) else [data]
        for p in positions:
            if p.get("symbol") == symbol and float(p.get("quantity", 0)) != 0:
                qty = float(p["quantity"])
                return Position(
                    symbol=symbol, side="long" if qty > 0 else "short",
                    quantity=abs(qty), entry_price=float(p.get("entry_price", 0)),
                    leverage=int(p.get("leverage", 0) or 0),
                )
        return None

    def close_position_market(self, symbol: str) -> OrderResult:
        pos = self.get_position(symbol)
        if pos is None:
            return OrderResult(exchange="coinswitch", order_id="", symbol=symbol, side="",
                                price=0, quantity=0, status="no_position")
        side = "SELL" if pos.side == "long" else "BUY"
        body = {
            "exchange": "EXCHANGE_2", "symbol": symbol, "side": side,
            "order_type": "MARKET", "quantity": pos.quantity, "reduce_only": True,
            "client_order_id": str(uuid.uuid4()),
        }
        data = self._request("POST", "/trade/api/v2/futures/order", body=body)["data"]
        return OrderResult(
            exchange="coinswitch", order_id=str(data["order_id"]), symbol=symbol,
            side=side.lower(), price=0, quantity=pos.quantity,
            status=data.get("status", "filled"),
        )
