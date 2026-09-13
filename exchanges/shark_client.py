"""
Shark Exchange REST client.

Auth scheme (verified from docs.sharkexchange.in):
    GET requests  -> sign the query string (includes timestamp)
    POST/PUT/PATCH/DELETE -> sign the JSON body (includes timestamp), using
                             json.dumps(params, separators=(',', ':'))  <- exact
                             separators matter, they must match what the server
                             re-serializes to when checking your signature.
    signature = HMAC-SHA256(api_secret, data_to_sign).hexdigest()
    headers = {api-key, signature, Content-Type}

Endpoints used here (from docs.sharkexchange.in):
    POST   /v1/order/place-order
    PATCH  /v1/order/edit-order          -> real in-place repricing (nice — unlike CoinSwitch)
    DELETE /v1/order/delete-order
    GET    /v1/order/{clientOrderId}
    GET    /v1/positions/{positionStatus}   (OPEN / CLOSED / LIQUIDATED)
    POST   /v1/exchange/update/leverage
    GET    /v1/market/ticker24Hr            (public, no auth)

⚠️ CAUTION — Shark Exchange is a small, newer Indian platform (launched
~2025). It is FIU-registered for AML purposes, but that is not the same as
SEBI/RBI oversight, and independent reviews flag it as higher-risk than
established venues (thin track record, mixed execution-quality reports).
Before routing real capital, start with small size and confirm withdrawal
reliability yourself.

⚠️ WebSocket: Shark's docs reference "Authenticated Web Sockets" and a
listen-key pattern (create/get/update/delete), similar to Binance, but the
full connection handshake wasn't available in the excerpt used to build this
file. This client uses REST polling every 10s (see core/price_feed.py) —
swap in a WS subscription later once you've confirmed the handshake against
https://docs.sharkexchange.in/#web-sockets yourself.
"""

import time
import json
import hmac
import hashlib
import requests

from config.constants import BASE_URLS, MAX_LEVERAGE
from exchanges.base import ExchangeClient, Ticker, OrderResult, Position, InstrumentInfo
from typing import List


class SharkClient(ExchangeClient):
    name = "shark"

    def __init__(self, api_key: str, api_secret: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = BASE_URLS["shark"]["rest"]
        self.session = requests.Session()

    # ---------------- auth plumbing ----------------

    def _sign(self, data_to_sign: str) -> str:
        return hmac.new(self.api_secret.encode("utf-8"), data_to_sign.encode("utf-8"),
                         hashlib.sha256).hexdigest()

    def _get(self, path: str, params: dict = None, authed: bool = True):
        params = dict(params or {})
        if authed:
            params["timestamp"] = str(int(time.time() * 1000))
            query_string = "&".join(f"{k}={v}" for k, v in params.items())
            signature = self._sign(query_string)
            headers = {"api-key": self.api_key, "signature": signature}
        else:
            headers = {}
        resp = self.session.get(self.base_url + path, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def _mutate(self, method: str, path: str, params: dict = None):
        params = dict(params or {})
        params["timestamp"] = str(int(time.time() * 1000))
        data_to_sign = json.dumps(params, separators=(",", ":"))
        signature = self._sign(data_to_sign)
        headers = {"api-key": self.api_key, "Content-Type": "application/json",
                   "signature": signature}
        resp = self.session.request(method, self.base_url + path, json=params,
                                     headers=headers, timeout=10)
        resp.raise_for_status()
        return resp.json()

    # ---------------- public/market data ----------------

    def get_ticker(self, symbol: str) -> Ticker:
        data = self._get(f"/v1/market/ticker24Hr/{symbol}", authed=False)
        result = data.get("data", data)
        last_price = float(result.get("c", result.get("lastPrice", 0)))
        
        # Depth endpoint gives real bid/ask for spread & post-only order simulation
        best_bid = last_price
        best_ask = last_price
        try:
            depth = self._get(f"/v1/market/depth/{symbol}", authed=False).get("data", {})
            bids = depth.get("b", [])
            asks = depth.get("a", [])
            if bids and len(bids) > 0:
                best_bid = float(bids[0][0])
            if asks and len(asks) > 0:
                best_ask = float(asks[0][0])
        except Exception:
            pass

        return Ticker(
            symbol=symbol,
            best_bid=best_bid,
            best_ask=best_ask,
            mark_price=float(result.get("w", last_price)) or last_price,
            funding_rate=float(result.get("fundingRate", 0)) if result.get("fundingRate") else None,
            next_funding_time_ms=result.get("nextFundingTime"),
        )

    def list_instruments(self) -> List[InstrumentInfo]:
        """Fetch all instruments from Shark's exchangeInfo and filter for perpetuals.

        NOTE: This is a PUBLIC endpoint — no auth headers needed. Sending auth
        headers causes a 400 Bad Request.
        """
        import logging
        log = logging.getLogger("shark_client")
        try:
            data = self._get("/v1/exchange/exchangeInfo", authed=False)
            # Response shape: {"markets":["INR","USDT"], "contracts":[{...}, ...]}
            items = data.get("contracts", data.get("data", data))
            if not isinstance(items, list):
                items = [items] if isinstance(items, dict) else []
            instruments = []
            for item in items:
                # Shark uses "name" as the symbol (e.g. "BTCUSDT", "ETHINR")
                symbol = str(item.get("name", item.get("symbol", item.get("contractPair", item.get("pair", "")))))
                if not symbol:
                    continue
                ctype = str(item.get("contractType", item.get("type", ""))).upper()
                if ctype and ctype not in ("PERPETUAL", "PERPETUAL_FUTURES", "PERP", ""):
                    continue
                base = str(item.get("baseAsset", item.get("base", ""))).upper()
                quote = str(item.get("quoteAsset", item.get("quote", ""))).upper()
                if not base:
                    for suffix in ("USDT", "USD", "INR", "BUSD"):
                        if symbol.upper().endswith(suffix):
                            base = symbol.upper()[:-len(suffix)]
                            quote = suffix
                            break
                    else:
                        base = symbol.upper()
                        quote = "INR"
                instruments.append(InstrumentInfo(
                    symbol=symbol,
                    base_asset=base,
                    quote_asset=quote or "INR",
                    contract_type="perpetual",
                    min_quantity=float(item.get("minQty", item.get("minQuantity", 0)) or 0),
                    tick_size=float(item.get("tickSize", item.get("pricePrecision", 0)) or 0),
                    is_active=True,
                ))
            log.info("Shark: discovered %d perpetual instruments", len(instruments))
            return instruments
        except Exception as e:
            log.warning("Shark list_instruments failed: %s", e)
            return []

    # ---------------- trading ----------------

    def set_leverage(self, symbol: str, leverage: int) -> int:
        capped = min(leverage, MAX_LEVERAGE["shark"])
        self._mutate("POST", "/v1/exchange/update/leverage",
                     {"leverage": capped, "contractName": symbol})
        return capped

    def place_limit_order(self, symbol: str, side: str, price: float,
                           quantity: float, post_only: bool = True) -> OrderResult:
        params = {
            "placeType": "ORDER_FORM",
            "quantity": quantity,
            "side": side.upper(),
            "symbol": symbol,
            "reduceOnly": False,
            "marginAsset": "INR",
            "type": "LIMIT",
            "price": price,
            # Shark's docs don't show a dedicated post_only flag on place-order.
            # If your account/pair doesn't support one, enforce "maker-only"
            # behaviour at the strategy layer (see core/order_manager.py) by
            # only ever quoting inside the spread, never crossing it.
        }
        data = self._mutate("POST", "/v1/order/place-order", params)
        return OrderResult(
            exchange="shark", order_id=data["clientOrderId"], symbol=symbol, side=side,
            price=price, quantity=quantity, status="open",
            filled_quantity=float(data.get("filledAmount", 0) or 0),
        )

    def reprice_order(self, symbol: str, client_order_id: str, new_price: float,
                       quantity: float = None) -> OrderResult:
        """Shark supports real in-place edits — requirement #8, no cancel/replace needed."""
        params = {"clientOrderId": client_order_id, "price": new_price}
        if quantity is not None:
            params["quantity"] = quantity
        data = self._mutate("PATCH", "/v1/order/edit-order", params)
        return OrderResult(
            exchange="shark", order_id=client_order_id, symbol=symbol, side="",
            price=new_price, quantity=quantity or 0, status=data.get("status", "open"),
        )

    def cancel_order(self, symbol: str, order_id: str) -> bool:
        self._mutate("DELETE", "/v1/order/delete-order", {"clientOrderId": order_id})
        return True

    def get_order_status(self, symbol: str, order_id: str) -> OrderResult:
        data = self._get(f"/v1/order/{order_id}")
        return OrderResult(
            exchange="shark", order_id=data.get("clientOrderId", order_id), symbol=symbol,
            side=data.get("side", "").lower(), price=float(data.get("price", 0)),
            quantity=float(data.get("quantity", 0)), status=data.get("status", "unknown").lower(),
            filled_quantity=float(data.get("filledQty", 0) or 0),
            avg_fill_price=float(data.get("avgPrice", 0) or 0),
        )

    def get_position(self, symbol: str):
        data = self._get("/v1/positions/OPEN", params={"symbol": symbol})
        positions = data if isinstance(data, list) else data.get("data", [])
        for p in positions:
            if p.get("contractPair") == symbol:
                qty = float(p.get("positionAmount", p.get("quantity", 0)))
                if qty == 0:
                    continue
                side = p.get("positionType", "").lower() or ("long" if qty > 0 else "short")
                return Position(
                    symbol=symbol, side=side, quantity=abs(qty),
                    entry_price=float(p.get("entryPrice", 0)),
                    leverage=int(p.get("leverage", 0) or 0),
                )
        return None

    def close_position_market(self, symbol: str) -> OrderResult:
        pos = self.get_position(symbol)
        if pos is None:
            return OrderResult(exchange="shark", order_id="", symbol=symbol, side="",
                                price=0, quantity=0, status="no_position")
        side = "SELL" if pos.side == "long" else "BUY"
        params = {
            "placeType": "ORDER_FORM", "quantity": pos.quantity, "side": side,
            "symbol": symbol, "reduceOnly": True, "marginAsset": "INR", "type": "MARKET",
        }
        data = self._mutate("POST", "/v1/order/place-order", params)
        return OrderResult(
            exchange="shark", order_id=data["clientOrderId"], symbol=symbol,
            side=side.lower(), price=0, quantity=pos.quantity, status="filled",
        )
