"""
Binance USDⓈ-M Futures REST client.

Auth scheme (from developers.binance.com):
    For SIGNED endpoints:
        query_string = all params as key=value&key=value (sorted)
        signature = HMAC-SHA256(api_secret, query_string).hexdigest()
        add signature=XXX to query params
        headers = {X-MBX-APIKEY: api_key}

    For public endpoints (exchangeInfo, premiumIndex, fundingInfo, bookTicker):
        No auth needed — these are freely accessible.

Key endpoints used here:
    GET  /fapi/v1/exchangeInfo          -> all symbols (filter contractType=PERPETUAL, status=TRADING)
    GET  /fapi/v1/fundingInfo           -> funding interval per symbol (fundingIntervalHours)
    GET  /fapi/v1/premiumIndex          -> current funding rate, mark price, next funding time
    GET  /fapi/v1/ticker/bookTicker     -> best bid/ask
    POST /fapi/v1/order                 -> place order
    DELETE /fapi/v1/order               -> cancel order
    GET  /fapi/v2/account               -> positions
    POST /fapi/v1/leverage              -> set leverage

Symbol format: unseparated uppercase, e.g. "BTCUSDT", "ETHUSDT".

⚠️ Public market data endpoints do NOT require API keys.
   Trading endpoints require both API key and secret.
"""

import hashlib
import hmac
import time
import logging
import requests
from typing import List, Optional
from urllib.parse import urlencode

from config.constants import BASE_URLS, MAX_LEVERAGE
from exchanges.base import ExchangeClient, Ticker, OrderResult, Position, InstrumentInfo

log = logging.getLogger("binance_client")


class BinanceClient(ExchangeClient):
    name = "binance"

    def __init__(self, api_key: str = "", api_secret: str = ""):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = BASE_URLS["binance"]["rest"]
        self.session = requests.Session()
        # Cache funding interval data (symbol -> interval_hours)
        self._funding_intervals: dict = {}

    # ---------------- auth plumbing ----------------

    def _sign(self, params: dict) -> dict:
        """Add timestamp and HMAC-SHA256 signature to params."""
        params["timestamp"] = str(int(time.time() * 1000))
        query_string = urlencode(params)
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        params["signature"] = signature
        return params

    def _headers(self, signed: bool = False) -> dict:
        headers = {"Content-Type": "application/json"}
        if signed and self.api_key:
            headers["X-MBX-APIKEY"] = self.api_key
        return headers

    def _public_get(self, path: str, params: dict = None) -> dict:
        """Public endpoint — no auth needed."""
        url = self.base_url + path
        resp = self.session.get(url, params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def _signed_get(self, path: str, params: dict = None) -> dict:
        params = dict(params or {})
        params = self._sign(params)
        url = self.base_url + path
        resp = self.session.get(url, params=params,
                                headers=self._headers(signed=True), timeout=10)
        resp.raise_for_status()
        return resp.json()

    def _signed_post(self, path: str, params: dict = None) -> dict:
        params = dict(params or {})
        params = self._sign(params)
        url = self.base_url + path
        resp = self.session.post(url, params=params,
                                 headers=self._headers(signed=True), timeout=10)
        resp.raise_for_status()
        return resp.json()

    def _signed_delete(self, path: str, params: dict = None) -> dict:
        params = dict(params or {})
        params = self._sign(params)
        url = self.base_url + path
        resp = self.session.delete(url, params=params,
                                    headers=self._headers(signed=True), timeout=10)
        resp.raise_for_status()
        return resp.json()

    # ---------------- public/market data ----------------

    def _fetch_funding_intervals(self):
        """Fetch fundingIntervalHours for all symbols from /fapi/v1/fundingInfo."""
        try:
            data = self._public_get("/fapi/v1/fundingInfo")
            for item in data:
                sym = item.get("symbol", "")
                hours = item.get("fundingIntervalHours", 8)
                self._funding_intervals[sym] = int(hours)
            log.info("Binance: cached funding intervals for %d symbols", len(self._funding_intervals))
        except Exception as e:
            log.warning("Binance: failed to fetch fundingInfo: %s", e)

    def get_ticker(self, symbol: str) -> Ticker:
        """Fetch current funding rate + mark price from premiumIndex,
        and best bid/ask from bookTicker."""
        # Premium index: funding rate, mark price, next funding time
        premium = self._public_get("/fapi/v1/premiumIndex", params={"symbol": symbol})

        # Book ticker: best bid/ask
        book = self._public_get("/fapi/v1/ticker/bookTicker", params={"symbol": symbol})

        funding_rate = float(premium.get("lastFundingRate", 0))
        mark_price = float(premium.get("markPrice", 0))
        next_funding_ms = int(premium.get("nextFundingTime", 0)) or None

        best_bid = float(book.get("bidPrice", mark_price))
        best_ask = float(book.get("askPrice", mark_price))

        # Get funding interval from cache
        interval_minutes = self._funding_intervals.get(symbol, 8) * 60  # default 8h

        return Ticker(
            symbol=symbol,
            best_bid=best_bid,
            best_ask=best_ask,
            mark_price=mark_price,
            funding_rate=funding_rate,
            next_funding_time_ms=next_funding_ms,
            funding_interval_minutes=interval_minutes,
        )

    def list_instruments(self) -> List[InstrumentInfo]:
        """Fetch all PERPETUAL, TRADING symbols from Binance USDⓈ-M Futures."""
        # First, refresh funding interval cache
        self._fetch_funding_intervals()

        try:
            data = self._public_get("/fapi/v1/exchangeInfo")
            symbols = data.get("symbols", [])
            instruments = []
            for s in symbols:
                contract_type = s.get("contractType", "")
                status = s.get("status", "")
                if contract_type != "PERPETUAL" or status != "TRADING":
                    continue
                symbol = s.get("symbol", "")
                base = s.get("baseAsset", "").upper()
                quote = s.get("quoteAsset", "").upper()
                if not symbol or not base:
                    continue

                # Extract min quantity and tick size from filters
                min_qty = 0.0
                tick_size = 0.0
                for f in s.get("filters", []):
                    if f.get("filterType") == "LOT_SIZE":
                        min_qty = float(f.get("minQty", 0))
                    elif f.get("filterType") == "PRICE_FILTER":
                        tick_size = float(f.get("tickSize", 0))

                interval_hours = self._funding_intervals.get(symbol, 8)

                instruments.append(InstrumentInfo(
                    symbol=symbol,
                    base_asset=base,
                    quote_asset=quote,
                    contract_type="perpetual",
                    min_quantity=min_qty,
                    tick_size=tick_size,
                    is_active=True,
                    funding_interval_minutes=interval_hours * 60,
                ))
            log.info("Binance: discovered %d perpetual instruments", len(instruments))
            return instruments
        except Exception as e:
            log.warning("Binance list_instruments failed: %s", e)
            return []

    # ---------------- trading ----------------

    def set_leverage(self, symbol: str, leverage: int) -> int:
        capped = min(leverage, MAX_LEVERAGE.get("binance", 125))
        try:
            self._signed_post("/fapi/v1/leverage",
                              params={"symbol": symbol, "leverage": capped})
        except Exception as e:
            log.warning("Binance set_leverage failed for %s: %s", symbol, e)
        return capped

    def place_limit_order(self, symbol: str, side: str, price: float,
                           quantity: float, post_only: bool = True) -> OrderResult:
        params = {
            "symbol": symbol,
            "side": side.upper(),
            "type": "LIMIT",
            "timeInForce": "GTX" if post_only else "GTC",  # GTX = post-only (maker)
            "price": str(price),
            "quantity": str(quantity),
        }
        data = self._signed_post("/fapi/v1/order", params=params)
        return OrderResult(
            exchange="binance",
            order_id=str(data.get("orderId", "")),
            symbol=symbol,
            side=side.lower(),
            price=price,
            quantity=quantity,
            status=data.get("status", "NEW").lower(),
            filled_quantity=float(data.get("executedQty", 0)),
            avg_fill_price=float(data.get("avgPrice", 0)),
        )

    def cancel_order(self, symbol: str, order_id: str) -> bool:
        try:
            self._signed_delete("/fapi/v1/order",
                                params={"symbol": symbol, "orderId": order_id})
            return True
        except Exception as e:
            log.warning("Binance cancel_order failed: %s", e)
            return False

    def get_order_status(self, symbol: str, order_id: str) -> OrderResult:
        data = self._signed_get("/fapi/v1/order",
                                params={"symbol": symbol, "orderId": order_id})
        status_map = {
            "NEW": "open", "PARTIALLY_FILLED": "partially_filled",
            "FILLED": "filled", "CANCELED": "cancelled",
            "REJECTED": "rejected", "EXPIRED": "cancelled",
        }
        return OrderResult(
            exchange="binance",
            order_id=str(data.get("orderId", order_id)),
            symbol=symbol,
            side=data.get("side", "").lower(),
            price=float(data.get("price", 0)),
            quantity=float(data.get("origQty", 0)),
            status=status_map.get(data.get("status", ""), "unknown"),
            filled_quantity=float(data.get("executedQty", 0)),
            avg_fill_price=float(data.get("avgPrice", 0)),
        )

    def get_position(self, symbol: str) -> Optional[Position]:
        data = self._signed_get("/fapi/v2/positionRisk", params={"symbol": symbol})
        for p in data:
            if p.get("symbol") == symbol:
                qty = float(p.get("positionAmt", 0))
                if qty == 0:
                    continue
                return Position(
                    symbol=symbol,
                    side="long" if qty > 0 else "short",
                    quantity=abs(qty),
                    entry_price=float(p.get("entryPrice", 0)),
                    leverage=int(p.get("leverage", 0)),
                )
        return None

    def close_position_market(self, symbol: str) -> OrderResult:
        pos = self.get_position(symbol)
        if pos is None:
            return OrderResult(exchange="binance", order_id="", symbol=symbol, side="",
                                price=0, quantity=0, status="no_position")
        side = "SELL" if pos.side == "long" else "BUY"
        params = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": str(pos.quantity),
            "reduceOnly": "true",
        }
        data = self._signed_post("/fapi/v1/order", params=params)
        return OrderResult(
            exchange="binance",
            order_id=str(data.get("orderId", "")),
            symbol=symbol,
            side=side.lower(),
            price=0,
            quantity=pos.quantity,
            status=data.get("status", "FILLED").lower(),
        )

    def reprice_order(self, symbol: str, order_id: str, new_price: float,
                       side: str = "", quantity: float = 0) -> OrderResult:
        """Binance doesn't support in-place order edit on futures — cancel + replace."""
        # Get current order details first
        current = self.get_order_status(symbol, order_id)
        self.cancel_order(symbol, order_id)
        return self.place_limit_order(
            symbol,
            side or current.side,
            new_price,
            quantity or current.quantity,
            post_only=True,
        )
