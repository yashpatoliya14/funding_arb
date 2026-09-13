"""
Requirement #11: forward-test with fake money but REAL execution logic.

SimulatedClient wraps a real client (Delta/CoinSwitch/Shark) for MARKET DATA
ONLY — real tickers, real spreads, real funding rates — but never sends a
real order. Instead it runs a tiny in-memory matching engine: a limit order
"fills" the moment the real market's best bid/ask crosses your quoted price,
using the REAL price feed to decide fills and P&L.

This means:
  - Your order_manager.py, funding_window.py, spread_calc.py, and engine.py
    code paths are IDENTICAL between simulation and live — the only thing
    that changes when you go live is which client class engine.py
    instantiates.
  - Slippage/latency assumptions are still optimistic (no real order-book
    depth, no partial fills, no exchange downtime) — treat sim P&L as a
    sanity check on your LOGIC, not a slippage-accurate backtest.
"""

import uuid
import time
from typing import Optional, Dict

from exchanges.base import ExchangeClient, Ticker, OrderResult, Position, InstrumentInfo
from typing import List


class SimulatedClient(ExchangeClient):
    def __init__(self, real_client_for_data: ExchangeClient, starting_balance_inr: float = 100000):
        self._data_source = real_client_for_data
        self.name = f"sim-{real_client_for_data.name}"
        self.balance = starting_balance_inr
        self.open_orders: Dict[str, dict] = {}
        self.positions: Dict[str, Position] = {}
        self.fills_log = []

    # ---------------- market data passthrough (real) ----------------

    def get_ticker(self, symbol: str) -> Ticker:
        return self._data_source.get_ticker(symbol)

    def list_instruments(self) -> List[InstrumentInfo]:
        """Pass through to real client — sim uses real instrument data."""
        return self._data_source.list_instruments()

    # ---------------- simulated trading ----------------

    def set_leverage(self, symbol: str, leverage: int) -> int:
        return leverage  # sim: no cap, but engine still applies leverage_sync's common cap

    def place_limit_order(self, symbol: str, side: str, price: float,
                           quantity: float, post_only: bool = True) -> OrderResult:
        order_id = str(uuid.uuid4())
        self.open_orders[order_id] = {
            "symbol": symbol, "side": side, "price": price,
            "quantity": quantity, "filled": 0.0, "status": "open",
            "placed_at": time.time(),
        }
        self._try_fill(order_id)
        order = self.open_orders[order_id]
        return OrderResult(
            exchange=self.name, order_id=order_id, symbol=symbol, side=side,
            price=price, quantity=quantity, status=order["status"],
            filled_quantity=order["filled"],
        )

    def _try_fill(self, order_id: str):
        """Fill instantly if the current real market would cross this limit price.
        This is the simplification noted in the module docstring — good enough to
        validate strategy logic, not a substitute for live slippage testing."""
        order = self.open_orders.get(order_id)
        if not order or order["status"] != "open":
            return
        ticker = self._data_source.get_ticker(order["symbol"])
        crosses = (
            (order["side"].lower() == "buy" and order["price"] >= ticker.best_ask) or
            (order["side"].lower() == "sell" and order["price"] <= ticker.best_bid)
        )
        if crosses:
            order["filled"] = order["quantity"]
            order["status"] = "filled"
            order["fill_price"] = order["price"]
            self._apply_fill(order)

    def _apply_fill(self, order: dict):
        symbol = order["symbol"]
        side = order["side"].lower()
        qty = order["filled"]
        price = order["fill_price"]
        existing = self.positions.get(symbol)
        signed_qty = qty if side == "buy" else -qty
        if existing is None:
            self.positions[symbol] = Position(
                symbol=symbol, side="long" if signed_qty > 0 else "short",
                quantity=abs(signed_qty), entry_price=price, leverage=1,
            )
        else:
            # naive net: closing/adding — good enough for hedge-pair sim
            prev_signed = existing.quantity if existing.side == "long" else -existing.quantity
            new_signed = prev_signed + signed_qty
            if new_signed == 0:
                del self.positions[symbol]
            else:
                self.positions[symbol] = Position(
                    symbol=symbol, side="long" if new_signed > 0 else "short",
                    quantity=abs(new_signed), entry_price=price, leverage=existing.leverage,
                )
        self.fills_log.append({"time": time.time(), "symbol": symbol, "side": side,
                                "qty": qty, "price": price})

    def reprice_order(self, symbol: str, order_id: str, new_price: float, **kwargs) -> OrderResult:
        order = self.open_orders.get(order_id)
        if not order or order["status"] != "open":
            return OrderResult(exchange=self.name, order_id=order_id, symbol=symbol,
                                side="", price=new_price, quantity=0, status="not_found")
        order["price"] = new_price
        self._try_fill(order_id)
        return OrderResult(
            exchange=self.name, order_id=order_id, symbol=symbol, side=order["side"],
            price=new_price, quantity=order["quantity"], status=order["status"],
            filled_quantity=order["filled"],
        )

    def cancel_order(self, symbol: str, order_id: str) -> bool:
        if order_id in self.open_orders and self.open_orders[order_id]["status"] == "open":
            self.open_orders[order_id]["status"] = "cancelled"
        return True

    def get_order_status(self, symbol: str, order_id: str) -> OrderResult:
        order = self.open_orders.get(order_id)
        if not order:
            return OrderResult(exchange=self.name, order_id=order_id, symbol=symbol,
                                side="", price=0, quantity=0, status="not_found")
        self._try_fill(order_id)
        return OrderResult(
            exchange=self.name, order_id=order_id, symbol=symbol, side=order["side"],
            price=order["price"], quantity=order["quantity"], status=order["status"],
            filled_quantity=order["filled"],
        )

    def get_position(self, symbol: str) -> Optional[Position]:
        return self.positions.get(symbol)

    def close_position_market(self, symbol: str) -> OrderResult:
        pos = self.positions.get(symbol)
        if pos is None:
            return OrderResult(exchange=self.name, order_id="", symbol=symbol, side="",
                                price=0, quantity=0, status="no_position")
        ticker = self._data_source.get_ticker(symbol)
        exit_price = ticker.best_bid if pos.side == "long" else ticker.best_ask
        del self.positions[symbol]
        return OrderResult(
            exchange=self.name, order_id=str(uuid.uuid4()), symbol=symbol,
            side="sell" if pos.side == "long" else "buy", price=exit_price,
            quantity=pos.quantity, status="filled",
        )
