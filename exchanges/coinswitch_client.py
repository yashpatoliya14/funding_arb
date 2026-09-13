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
from exchanges.base import ExchangeClient, Ticker, OrderResult, Position


class CoinswitchClient(ExchangeClient):
    name = "coinswitch"

    def __init__(self, api_key: str, secret_key: str):
        self.api_key = api_key
        self.secret_key_bytes = bytes.fromhex(secret_key)
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
        secret = ed25519.Ed25519PrivateKey.from_private_bytes(self.secret_key_bytes)
        signature = secret.sign(message.encode("utf-8")).hex()
        headers = {
            "Content-Type": "application/json",
            "X-AUTH-APIKEY": self.api_key,
            "X-AUTH-SIGNATURE": signature,
            "X-AUTH-EPOCH": epoch,
        }
        return headers, decoded_path

    def _request(self, method: str, path: str, params: dict = None, body: dict = None):
        headers, decoded_path = self._sign(method, path, params)
        url = self.base_url + decoded_path
        resp = self.session.request(method, url, headers=headers,
                                     data=json.dumps(body) if body else None, timeout=10)
        resp.raise_for_status()
        return resp.json()

    # ---------------- public/market data ----------------

    def get_ticker(self, symbol: str) -> Ticker:
        data = self._request("GET", "/trade/api/v2/futures/ticker", params={"symbol": symbol})
        result = data.get("data", data)
        return Ticker(
            symbol=symbol,
            best_bid=float(result.get("bid", result.get("last_price", 0))),
            best_ask=float(result.get("ask", result.get("last_price", 0))),
            mark_price=float(result.get("mark_price", result.get("last_price", 0))),
            funding_rate=float(result.get("funding_rate", 0)) if result.get("funding_rate") else None,
            next_funding_time_ms=result.get("next_funding_time"),
        )

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
