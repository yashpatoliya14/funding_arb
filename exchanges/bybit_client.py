"""
Bybit V5 Linear Perpetuals REST client.

Auth scheme (from bybit-exchange.github.io/docs/v5):
    For SIGNED endpoints:
        timestamp = current epoch ms
        recv_window = "5000" (default)
        param_str = timestamp + api_key + recv_window + (query_string or json_body)
        signature = HMAC-SHA256(api_secret, param_str).hexdigest()
        headers = {
            X-BAPI-API-KEY, X-BAPI-TIMESTAMP, X-BAPI-SIGN,
            X-BAPI-RECV-WINDOW, Content-Type
        }

    For public endpoints (instruments-info, tickers):
        No auth needed — freely accessible.

Key endpoints used here:
    GET  /v5/market/instruments-info?category=linear  -> all symbols + funding interval
         (paginated via nextPageCursor — can return 500+ symbols)
    GET  /v5/market/tickers?category=linear           -> funding rate, mark price, bid/ask
    POST /v5/order/create                              -> place order
    POST /v5/order/cancel                              -> cancel order
    GET  /v5/order/realtime                            -> order status
    GET  /v5/position/list                             -> positions
    POST /v5/position/set-leverage                     -> set leverage

Symbol format: unseparated uppercase, e.g. "BTCUSDT", "ETHUSDT".

⚠️ Public market data endpoints do NOT require API keys.
   Trading endpoints require both API key and secret.
   The instruments-info endpoint can return 500+ symbols — this client
   implements nextPageCursor pagination to capture them all.
"""

import hashlib
import hmac
import time
import json
import logging
import requests
from typing import List, Optional
from urllib.parse import urlencode

from config.constants import BASE_URLS, MAX_LEVERAGE
from exchanges.base import ExchangeClient, Ticker, OrderResult, Position, InstrumentInfo

log = logging.getLogger("bybit_client")


class BybitClient(ExchangeClient):
    name = "bybit"

    def __init__(self, api_key: str = "", api_secret: str = ""):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = BASE_URLS["bybit"]["rest"]
        self.session = requests.Session()
        self.recv_window = "5000"

    # ---------------- auth plumbing ----------------

    def _sign(self, timestamp: str, params_str: str) -> str:
        """Generate HMAC-SHA256 signature for Bybit V5."""
        param_str = timestamp + self.api_key + self.recv_window + params_str
        return hmac.new(
            self.api_secret.encode("utf-8"),
            param_str.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _signed_headers(self, timestamp: str, signature: str) -> dict:
        return {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-SIGN": signature,
            "X-BAPI-RECV-WINDOW": self.recv_window,
            "Content-Type": "application/json",
        }

    def _public_get(self, path: str, params: dict = None) -> dict:
        """Public endpoint — no auth needed."""
        url = self.base_url + path
        resp = self.session.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("retCode", 0) != 0:
            raise RuntimeError(f"Bybit API error: {data.get('retMsg', 'unknown')} (code={data.get('retCode')})")
        return data.get("result", data)

    def _signed_get(self, path: str, params: dict = None) -> dict:
        params = params or {}
        timestamp = str(int(time.time() * 1000))
        query_string = urlencode(params) if params else ""
        signature = self._sign(timestamp, query_string)
        headers = self._signed_headers(timestamp, signature)
        url = self.base_url + path
        resp = self.session.get(url, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("retCode", 0) != 0:
            raise RuntimeError(f"Bybit API error: {data.get('retMsg', 'unknown')}")
        return data.get("result", data)

    def _signed_post(self, path: str, body: dict = None) -> dict:
        body = body or {}
        timestamp = str(int(time.time() * 1000))
        body_str = json.dumps(body)
        signature = self._sign(timestamp, body_str)
        headers = self._signed_headers(timestamp, signature)
        url = self.base_url + path
        resp = self.session.post(url, data=body_str, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("retCode", 0) != 0:
            raise RuntimeError(f"Bybit API error: {data.get('retMsg', 'unknown')}")
        return data.get("result", data)

    # ---------------- public/market data ----------------

    def get_ticker(self, symbol: str) -> Ticker:
        """Fetch ticker including funding rate, mark price, bid/ask from
        /v5/market/tickers?category=linear&symbol=X"""
        result = self._public_get("/v5/market/tickers",
                                   params={"category": "linear", "symbol": symbol})
        items = result.get("list", [])
        if not items:
            raise RuntimeError(f"Bybit: no ticker data for {symbol}")
        t = items[0]

        funding_rate = float(t.get("fundingRate", 0)) if t.get("fundingRate") else None
        next_funding_ms = int(t.get("nextFundingTime", 0)) or None
        mark_price = float(t.get("markPrice", 0))
        best_bid = float(t.get("bid1Price", mark_price))
        best_ask = float(t.get("ask1Price", mark_price))

        return Ticker(
            symbol=symbol,
            best_bid=best_bid,
            best_ask=best_ask,
            mark_price=mark_price,
            funding_rate=funding_rate,
            next_funding_time_ms=next_funding_ms,
        )

    def list_instruments(self) -> List[InstrumentInfo]:
        """Fetch all linear perpetual instruments from Bybit V5.

        Implements cursor-based pagination since Bybit can return 500+ symbols
        and a single response may not contain all of them.
        """
        instruments = []
        cursor = None

        while True:
            params = {"category": "linear", "limit": "1000"}
            if cursor:
                params["cursor"] = cursor

            try:
                result = self._public_get("/v5/market/instruments-info", params=params)
            except Exception as e:
                log.warning("Bybit list_instruments page failed: %s", e)
                break

            items = result.get("list", [])
            for item in items:
                contract_type = item.get("contractType", "")
                status = item.get("status", "")
                if contract_type != "LinearPerpetual" or status != "Trading":
                    continue

                symbol = item.get("symbol", "")
                base = item.get("baseCoin", "").upper()
                quote = item.get("quoteCoin", "").upper()
                settle = item.get("settleCoin", "").upper()
                if not symbol or not base:
                    continue

                # Funding interval is in minutes
                funding_interval = int(item.get("fundingInterval", 480))

                # Extract precision info
                lot_filter = item.get("lotSizeFilter", {})
                price_filter = item.get("priceFilter", {})
                min_qty = float(lot_filter.get("minOrderQty", 0))
                tick_size = float(price_filter.get("tickSize", 0))

                instruments.append(InstrumentInfo(
                    symbol=symbol,
                    base_asset=base,
                    quote_asset=quote or settle or "USDT",
                    contract_type="perpetual",
                    min_quantity=min_qty,
                    tick_size=tick_size,
                    is_active=True,
                    funding_interval_minutes=funding_interval,
                ))

            # Check for next page
            next_cursor = result.get("nextPageCursor", "")
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor

        log.info("Bybit: discovered %d perpetual instruments", len(instruments))
        return instruments

    # ---------------- trading ----------------

    def set_leverage(self, symbol: str, leverage: int) -> int:
        capped = min(leverage, MAX_LEVERAGE.get("bybit", 100))
        try:
            self._signed_post("/v5/position/set-leverage", body={
                "category": "linear",
                "symbol": symbol,
                "buyLeverage": str(capped),
                "sellLeverage": str(capped),
            })
        except Exception as e:
            # Bybit returns error if leverage is already set to the same value
            if "not modified" not in str(e).lower():
                log.warning("Bybit set_leverage failed for %s: %s", symbol, e)
        return capped

    def place_limit_order(self, symbol: str, side: str, price: float,
                           quantity: float, post_only: bool = True) -> OrderResult:
        body = {
            "category": "linear",
            "symbol": symbol,
            "side": side.capitalize(),  # Bybit expects "Buy" or "Sell"
            "orderType": "Limit",
            "qty": str(quantity),
            "price": str(price),
            "timeInForce": "PostOnly" if post_only else "GTC",
        }
        data = self._signed_post("/v5/order/create", body=body)
        return OrderResult(
            exchange="bybit",
            order_id=str(data.get("orderId", "")),
            symbol=symbol,
            side=side.lower(),
            price=price,
            quantity=quantity,
            status="open",
        )

    def cancel_order(self, symbol: str, order_id: str) -> bool:
        try:
            self._signed_post("/v5/order/cancel", body={
                "category": "linear",
                "symbol": symbol,
                "orderId": order_id,
            })
            return True
        except Exception as e:
            log.warning("Bybit cancel_order failed: %s", e)
            return False

    def get_order_status(self, symbol: str, order_id: str) -> OrderResult:
        data = self._signed_get("/v5/order/realtime", params={
            "category": "linear",
            "symbol": symbol,
            "orderId": order_id,
        })
        orders = data.get("list", [])
        if not orders:
            return OrderResult(exchange="bybit", order_id=order_id, symbol=symbol,
                                side="", price=0, quantity=0, status="unknown")
        o = orders[0]
        status_map = {
            "New": "open", "PartiallyFilled": "partially_filled",
            "Filled": "filled", "Cancelled": "cancelled",
            "Rejected": "rejected", "Deactivated": "cancelled",
        }
        return OrderResult(
            exchange="bybit",
            order_id=str(o.get("orderId", order_id)),
            symbol=symbol,
            side=o.get("side", "").lower(),
            price=float(o.get("price", 0)),
            quantity=float(o.get("qty", 0)),
            status=status_map.get(o.get("orderStatus", ""), "unknown"),
            filled_quantity=float(o.get("cumExecQty", 0)),
            avg_fill_price=float(o.get("avgPrice", 0)),
        )

    def get_position(self, symbol: str) -> Optional[Position]:
        data = self._signed_get("/v5/position/list", params={
            "category": "linear",
            "symbol": symbol,
        })
        positions = data.get("list", [])
        for p in positions:
            if p.get("symbol") == symbol:
                size = float(p.get("size", 0))
                if size == 0:
                    continue
                side_str = p.get("side", "")
                return Position(
                    symbol=symbol,
                    side="long" if side_str == "Buy" else "short",
                    quantity=size,
                    entry_price=float(p.get("avgPrice", 0)),
                    leverage=int(float(p.get("leverage", 0))),
                )
        return None

    def close_position_market(self, symbol: str) -> OrderResult:
        pos = self.get_position(symbol)
        if pos is None:
            return OrderResult(exchange="bybit", order_id="", symbol=symbol, side="",
                                price=0, quantity=0, status="no_position")
        side = "Sell" if pos.side == "long" else "Buy"
        body = {
            "category": "linear",
            "symbol": symbol,
            "side": side,
            "orderType": "Market",
            "qty": str(pos.quantity),
            "reduceOnly": True,
        }
        data = self._signed_post("/v5/order/create", body=body)
        return OrderResult(
            exchange="bybit",
            order_id=str(data.get("orderId", "")),
            symbol=symbol,
            side=side.lower(),
            price=0,
            quantity=pos.quantity,
            status="filled",
        )

    def reprice_order(self, symbol: str, order_id: str, new_price: float,
                       side: str = "", quantity: float = 0) -> OrderResult:
        """Bybit V5 supports amend order — use it for in-place repricing."""
        try:
            body = {
                "category": "linear",
                "symbol": symbol,
                "orderId": order_id,
                "price": str(new_price),
            }
            if quantity:
                body["qty"] = str(quantity)
            data = self._signed_post("/v5/order/amend", body=body)
            return OrderResult(
                exchange="bybit",
                order_id=str(data.get("orderId", order_id)),
                symbol=symbol,
                side=side,
                price=new_price,
                quantity=quantity,
                status="open",
            )
        except Exception as e:
            log.warning("Bybit amend_order failed, falling back to cancel+replace: %s", e)
            self.cancel_order(symbol, order_id)
            return self.place_limit_order(symbol, side, new_price, quantity, post_only=True)
