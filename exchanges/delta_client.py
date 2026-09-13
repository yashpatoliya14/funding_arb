"""
Delta Exchange India REST client.

Auth scheme (verified from docs.delta.exchange, Sep 2026):
    signature_data = METHOD + TIMESTAMP + PATH + QUERY_STRING + BODY
    signature = HMAC-SHA256(api_secret, signature_data).hexdigest()
    headers = {api-key, timestamp, signature, User-Agent, Content-Type}

Key endpoints used here (from Delta's public docs / product_specs):
    GET  /v2/tickers/{symbol}
    GET  /v2/products/{symbol}        -> instrument info (tick size, funding interval)
    POST /v2/orders                    -> place order
    PUT  /v2/orders/{id}                -> edit order (used for repricing)
    DELETE /v2/orders/{id}               -> cancel order
    GET  /v2/orders/{id}
    GET  /v2/positions?product_id=...
    POST /v2/orders  (reduce_only market order for emergency close)
    POST /v2/products/{symbol}/orders/leverage  -> NOTE: verify exact path in your
         dashboard; Delta's leverage-set endpoint has moved between API versions
         in the past. TEST IN THEIR SANDBOX FIRST.

⚠️ This file is written against the DOCUMENTED contract shapes. Exact field
names in responses can drift between API versions — run test_connection()
against your own account before wiring it into the live engine.
"""

import hashlib
import hmac
import time
import requests

from config.constants import BASE_URLS, MAX_LEVERAGE
from exchanges.base import ExchangeClient, Ticker, OrderResult, Position


class DeltaClient(ExchangeClient):
    name = "delta"

    def __init__(self, api_key: str, api_secret: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = BASE_URLS["delta"]["rest"]
        self.session = requests.Session()

    # ---------------- auth plumbing ----------------

    def _sign(self, method: str, path: str, query_string: str = "", payload: str = ""):
        timestamp = str(int(time.time()))
        signature_data = method + timestamp + path + query_string + payload
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            signature_data.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        headers = {
            "api-key": self.api_key,
            "timestamp": timestamp,
            "signature": signature,
            "User-Agent": "funding-arb-bot",
            "Content-Type": "application/json",
        }
        return headers

    def _request(self, method: str, path: str, params: dict = None, body: dict = None):
        import json
        query_string = ""
        if params:
            # Delta expects the exact query string used in the signature to match
            # the one sent on the wire — build it manually rather than trusting
            # requests' own encoding order.
            query_string = "?" + "&".join(f"{k}={v}" for k, v in params.items())
        payload = json.dumps(body) if body else ""
        headers = self._sign(method, path, query_string, payload)
        url = self.base_url + path + query_string
        resp = self.session.request(method, url, headers=headers, data=payload, timeout=10)
        resp.raise_for_status()
        return resp.json()

    # ---------------- public/market data ----------------

    def get_ticker(self, symbol: str) -> Ticker:
        data = self._request("GET", f"/v2/tickers/{symbol}")["result"]
        return Ticker(
            symbol=symbol,
            best_bid=float(data.get("quotes", {}).get("best_bid", data.get("mark_price", 0))),
            best_ask=float(data.get("quotes", {}).get("best_ask", data.get("mark_price", 0))),
            mark_price=float(data.get("mark_price", 0)),
            funding_rate=float(data.get("funding_rate", 0)) if data.get("funding_rate") else None,
            next_funding_time_ms=None,  # Delta publishes this on the product spec / UI ticker; wire
                                         # this up to product_specs.rate_exchange_interval + last
                                         # settlement time if you need it programmatically.
        )

    # ---------------- trading ----------------

    def set_leverage(self, symbol: str, leverage: int) -> int:
        capped = min(leverage, MAX_LEVERAGE["delta"])
        # NOTE: verify this path against your Delta API version before live use.
        self._request("POST", f"/v2/products/{symbol}/orders/leverage", body={"leverage": capped})
        return capped

    def place_limit_order(self, symbol: str, side: str, price: float,
                           quantity: float, post_only: bool = True) -> OrderResult:
        body = {
            "product_symbol": symbol,
            "size": quantity,
            "side": side,
            "order_type": "limit_order",
            "limit_price": str(price),
            "post_only": post_only,
            "time_in_force": "gtc",
        }
        data = self._request("POST", "/v2/orders", body=body)["result"]
        return OrderResult(
            exchange="delta", order_id=str(data["id"]), symbol=symbol, side=side,
            price=price, quantity=quantity, status=data.get("state", "open"),
            filled_quantity=float(data.get("filled_size", 0) or 0),
        )

    def reprice_order(self, symbol: str, order_id: str, new_price: float) -> OrderResult:
        """Requirement #8: update the limit order to a new price instead of cancel+replace,
        which preserves your place-in-queue on some order types and is cheaper on rate limits."""
        data = self._request("PUT", f"/v2/orders/{order_id}",
                              body={"limit_price": str(new_price), "product_symbol": symbol})["result"]
        return OrderResult(
            exchange="delta", order_id=str(data["id"]), symbol=symbol,
            side=data.get("side", ""), price=new_price,
            quantity=float(data.get("size", 0)), status=data.get("state", "open"),
            filled_quantity=float(data.get("filled_size", 0) or 0),
        )

    def cancel_order(self, symbol: str, order_id: str) -> bool:
        self._request("DELETE", f"/v2/orders/{order_id}", body={"product_symbol": symbol})
        return True

    def get_order_status(self, symbol: str, order_id: str) -> OrderResult:
        data = self._request("GET", f"/v2/orders/{order_id}")["result"]
        return OrderResult(
            exchange="delta", order_id=str(data["id"]), symbol=symbol,
            side=data.get("side", ""), price=float(data.get("limit_price", 0) or 0),
            quantity=float(data.get("size", 0)), status=data.get("state", "unknown"),
            filled_quantity=float(data.get("filled_size", 0) or 0),
            avg_fill_price=float(data.get("average_fill_price", 0) or 0),
        )

    def get_position(self, symbol: str):
        data = self._request("GET", "/v2/positions", params={"product_symbol": symbol})["result"]
        if not data or float(data.get("size", 0)) == 0:
            return None
        size = float(data["size"])
        return Position(
            symbol=symbol, side="long" if size > 0 else "short",
            quantity=abs(size), entry_price=float(data.get("entry_price", 0)),
            leverage=int(data.get("leverage", 0) or 0),
        )

    def close_position_market(self, symbol: str) -> OrderResult:
        pos = self.get_position(symbol)
        if pos is None:
            return OrderResult(exchange="delta", order_id="", symbol=symbol, side="",
                                price=0, quantity=0, status="no_position")
        side = "sell" if pos.side == "long" else "buy"
        body = {
            "product_symbol": symbol, "size": pos.quantity, "side": side,
            "order_type": "market_order", "reduce_only": True,
        }
        data = self._request("POST", "/v2/orders", body=body)["result"]
        return OrderResult(
            exchange="delta", order_id=str(data["id"]), symbol=symbol, side=side,
            price=0, quantity=pos.quantity, status=data.get("state", "filled"),
        )
