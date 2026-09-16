"""
Paper Trading Exchange Client — with SQLite persistence.

Wraps a REAL exchange client to get live market data (prices, funding
rates, instruments), but simulates all order placement and position
management locally. Virtual state is persisted to SQLite so the bot
survives restarts without losing capital or trade history.

Persistence: data/paper_trading.db (auto-created)
"""

import time
import uuid
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from exchanges.base import (
    ExchangeClient, Ticker, InstrumentInfo, OrderResult, Position,
)
from core.paper_db import paper_db

log = logging.getLogger("paper_client")


@dataclass
class PaperTrade:
    """Record of a simulated trade for P&L reporting."""
    trade_id: str
    exchange: str
    symbol: str
    side: str             # "buy" / "sell"
    quantity: float
    entry_price: float
    exit_price: float = 0.0
    pnl: float = 0.0
    funding_earned: float = 0.0
    opened_at: float = 0.0   # epoch
    closed_at: float = 0.0


class PaperExchangeClient(ExchangeClient):
    """Simulated exchange client that uses REAL market data but fake execution.

    - get_ticker()        → delegates to the real client (live prices)
    - list_instruments()  → delegates to the real client
    - place_limit_order() → simulates an instant fill at quoted price + slippage
    - get_order_status()  → returns simulated fill status
    - close_position_market() → simulates market close at live price
    - Tracks virtual balance, open positions, and P&L history
    - ALL state persisted to SQLite — survives restarts
    """

    def __init__(self, real_client: ExchangeClient, exchange_name: str,
                 initial_balance: float = 100_000.0,
                 slippage_pct: float = 0.01):
        """
        Args:
            real_client:     A real exchange client for live market data.
            exchange_name:   e.g. "binance", "bybit", "delta"
            initial_balance: Starting virtual balance (in quote currency).
                             Only used on FIRST run; subsequent runs load from DB.
            slippage_pct:    Simulated slippage as percentage (0.01 = 0.01%).
        """
        self._real = real_client
        self.name = f"{exchange_name}[PAPER]"
        self._exchange_name = exchange_name
        self._slippage_pct = slippage_pct

        # In-memory state (loaded from DB or initialized fresh)
        self._orders: Dict[str, OrderResult] = {}
        self._positions: Dict[str, Position] = {}     # symbol → Position
        self._leverage: Dict[str, int] = {}            # symbol → leverage
        self._trade_history: List[PaperTrade] = []

        # Load persisted state or initialize fresh
        self._initial_balance = initial_balance
        self._balance = initial_balance
        self._total_fees_paid = 0.0

        self._load_from_db(initial_balance)

    def _load_from_db(self, default_balance: float):
        """Load persisted state from SQLite. If no prior state exists,
        initialize with the default balance."""
        # --- Balance ---
        saved = paper_db.load_balance(self._exchange_name)
        if saved:
            self._balance = saved["balance"]
            self._initial_balance = saved["initial_balance"]
            self._total_fees_paid = saved["total_fees_paid"]
            log.info("[%s] Restored balance from DB: ₹%.2f (initial ₹%.2f, fees ₹%.2f)",
                     self.name, self._balance, self._initial_balance, self._total_fees_paid)
        else:
            self._balance = default_balance
            self._initial_balance = default_balance
            self._total_fees_paid = 0.0
            self._persist_balance()
            log.info("[%s] No prior state — initialized fresh. Balance: ₹%.2f",
                     self.name, default_balance)

        # --- Positions ---
        saved_positions = paper_db.load_positions(self._exchange_name)
        for p in saved_positions:
            self._positions[p["symbol"]] = Position(
                symbol=p["symbol"],
                side=p["side"],
                quantity=p["quantity"],
                entry_price=p["entry_price"],
                leverage=p["leverage"],
            )
        if saved_positions:
            log.info("[%s] Restored %d open positions from DB", self.name, len(saved_positions))

        # --- Trade history ---
        saved_trades = paper_db.load_trades(self._exchange_name)
        for t in saved_trades:
            self._trade_history.append(PaperTrade(
                trade_id=t["trade_id"],
                exchange=self._exchange_name,
                symbol=t["symbol"],
                side=t["side"],
                quantity=t["quantity"],
                entry_price=t["entry_price"],
                exit_price=t["exit_price"],
                pnl=t["pnl"],
                funding_earned=t["funding_earned"],
                opened_at=t["opened_at"],
                closed_at=t["closed_at"],
            ))
        if saved_trades:
            log.info("[%s] Loaded %d trade records from DB", self.name, len(saved_trades))

    def _persist_balance(self):
        """Save current balance to DB."""
        paper_db.save_balance(
            self._exchange_name, self._balance,
            self._initial_balance, self._total_fees_paid,
        )

    def _persist_position(self, symbol: str):
        """Save or remove a position in DB."""
        pos = self._positions.get(symbol)
        if pos:
            paper_db.save_position(
                self._exchange_name, pos.symbol, pos.side,
                pos.quantity, pos.entry_price, pos.leverage,
            )
        else:
            paper_db.remove_position(self._exchange_name, symbol)

    def _persist_trade(self, trade: PaperTrade):
        """Save trade to DB."""
        paper_db.save_trade(
            trade_id=trade.trade_id,
            exchange=self._exchange_name,
            symbol=trade.symbol,
            side=trade.side,
            quantity=trade.quantity,
            entry_price=trade.entry_price,
            exit_price=trade.exit_price,
            pnl=trade.pnl,
            funding_earned=trade.funding_earned,
            opened_at=trade.opened_at,
            closed_at=trade.closed_at,
        )

    # ------------------------------------------------------------------
    # Market data — delegated to the REAL client (live prices)
    # ------------------------------------------------------------------

    def get_ticker(self, symbol: str) -> Ticker:
        """Returns REAL live market data from the actual exchange."""
        return self._real.get_ticker(symbol)

    def list_instruments(self) -> List[InstrumentInfo]:
        """Returns instrument list — from DB cache if fresh, else live fetch.
        After a live fetch, caches the result in SQLite for next restart."""
        # Try cache first (valid for 24 hours)
        cached = paper_db.load_instruments(self._exchange_name, max_age_sec=86400)
        if cached:
            age = paper_db.get_instruments_cache_age(self._exchange_name)
            age_str = f"{age/3600:.1f}h" if age else "unknown"
            log.info("[%s] Using cached instruments (%d symbols, age %s)",
                     self.name, len(cached), age_str)
            return cached

        # Cache miss or stale — fetch live
        log.info("[%s] Instrument cache miss — fetching live...", self.name)
        instruments = self._real.list_instruments()

        # Cache in DB for next time
        if instruments:
            paper_db.save_instruments(self._exchange_name, instruments)

        return instruments

    # ------------------------------------------------------------------
    # Simulated leverage
    # ------------------------------------------------------------------

    def set_leverage(self, symbol: str, leverage: int) -> int:
        """Simulate setting leverage — just stores it locally."""
        self._leverage[symbol] = leverage
        log.info("[%s] Leverage set to %dx for %s (simulated)",
                 self.name, leverage, symbol)
        return leverage

    # ------------------------------------------------------------------
    # Simulated order placement
    # ------------------------------------------------------------------

    def place_limit_order(self, symbol: str, side: str, price: float,
                           quantity: float, post_only: bool = True) -> OrderResult:
        """Simulate a limit order fill.

        For paper trading, maker (post_only) orders fill at the quoted price.
        Taker orders (post_only=False) fill with simulated slippage.
        """
        order_id = f"paper-{uuid.uuid4().hex[:12]}"

        # Apply slippage for taker orders
        if not post_only:
            if side.lower() == "buy":
                fill_price = price * (1 + self._slippage_pct / 100)
            else:
                fill_price = price * (1 - self._slippage_pct / 100)
        else:
            fill_price = price

        # Simulate fee (using a rough taker/maker fee)
        fee_rate = 0.0002 if post_only else 0.0005
        fee = fill_price * quantity * fee_rate
        self._total_fees_paid += fee
        self._balance -= fee

        order = OrderResult(
            exchange=self.name,
            order_id=order_id,
            symbol=symbol,
            side=side.lower(),
            price=fill_price,
            quantity=quantity,
            status="filled",          # paper orders fill instantly
            filled_quantity=quantity,
            avg_fill_price=fill_price,
        )

        self._orders[order_id] = order

        # Update virtual position
        self._update_position_on_fill(symbol, side.lower(), quantity, fill_price)

        # Persist balance after every order
        self._persist_balance()

        log.info("[%s] PAPER %s %s: %.6f @ ₹%.2f (fee: ₹%.4f) [%s]",
                 self.name,
                 "MAKER" if post_only else "TAKER",
                 side.upper(), quantity, fill_price, fee,
                 order_id)

        return order

    def _update_position_on_fill(self, symbol: str, side: str, quantity: float,
                                  fill_price: float):
        """Update the virtual position tracker after a fill."""
        pos_side = "long" if side == "buy" else "short"
        leverage = self._leverage.get(symbol, 10)

        existing = self._positions.get(symbol)

        if existing is None:
            # New position
            self._positions[symbol] = Position(
                symbol=symbol,
                side=pos_side,
                quantity=quantity,
                entry_price=fill_price,
                leverage=leverage,
            )
            self._persist_position(symbol)

            # Record trade open
            trade = PaperTrade(
                trade_id=f"pt-{uuid.uuid4().hex[:8]}",
                exchange=self._exchange_name,
                symbol=symbol,
                side=side,
                quantity=quantity,
                entry_price=fill_price,
                opened_at=time.time(),
            )
            self._trade_history.append(trade)
            self._persist_trade(trade)

        elif existing.side == pos_side:
            # Adding to existing position — average the entry price
            total_qty = existing.quantity + quantity
            avg_price = (existing.entry_price * existing.quantity +
                         fill_price * quantity) / total_qty
            existing.quantity = total_qty
            existing.entry_price = avg_price
            self._persist_position(symbol)

        else:
            # Closing / reducing position (opposite side)
            close_qty = min(existing.quantity, quantity)
            pnl = self._calculate_pnl(existing.side, existing.entry_price,
                                       fill_price, close_qty)
            self._balance += pnl

            remaining = existing.quantity - close_qty
            if remaining <= 0.000001:
                del self._positions[symbol]
            else:
                existing.quantity = remaining

            self._persist_position(symbol)

            # Record trade close
            for trade in reversed(self._trade_history):
                if trade.symbol == symbol and trade.exit_price == 0:
                    trade.exit_price = fill_price
                    trade.pnl = pnl
                    trade.closed_at = time.time()
                    self._persist_trade(trade)
                    break

            log.info("[%s] Position closed: %s PnL=₹%.4f", self.name, symbol, pnl)

    @staticmethod
    def _calculate_pnl(side: str, entry: float, exit_price: float,
                        quantity: float) -> float:
        """Compute raw P&L for a position close."""
        if side == "long":
            return (exit_price - entry) * quantity
        else:  # short
            return (entry - exit_price) * quantity

    # ------------------------------------------------------------------
    # Simulated order management
    # ------------------------------------------------------------------

    def cancel_order(self, symbol: str, order_id: str) -> bool:
        """Simulate order cancellation."""
        if order_id in self._orders:
            self._orders[order_id].status = "cancelled"
            log.info("[%s] Order %s cancelled (simulated)", self.name, order_id)
            return True
        return False

    def get_order_status(self, symbol: str, order_id: str) -> OrderResult:
        """Return the simulated order status."""
        order = self._orders.get(order_id)
        if order is None:
            return OrderResult(
                exchange=self.name, order_id=order_id, symbol=symbol,
                side="", price=0, quantity=0, status="unknown",
            )
        return order

    def reprice_order(self, symbol: str, order_id: str, new_price: float,
                       new_quantity: float = None) -> OrderResult:
        """Simulate repricing a maker order."""
        order = self._orders.get(order_id)
        if order:
            order.price = new_price
            order.avg_fill_price = new_price
            log.debug("[%s] Order %s repriced to ₹%.2f (simulated)",
                      self.name, order_id, new_price)
        return order

    # ------------------------------------------------------------------
    # Simulated position queries
    # ------------------------------------------------------------------

    def get_position(self, symbol: str) -> Optional[Position]:
        return self._positions.get(symbol)

    def close_position_market(self, symbol: str) -> OrderResult:
        """Simulate closing a position at the current market price."""
        pos = self._positions.get(symbol)
        if pos is None:
            log.warning("[%s] No position to close for %s", self.name, symbol)
            return OrderResult(
                exchange=self.name, order_id="none", symbol=symbol,
                side="", price=0, quantity=0, status="no_position",
            )

        # Get live price
        ticker = self.get_ticker(symbol)
        if pos.side == "long":
            exit_price = ticker.best_bid  # sell at bid
            close_side = "sell"
        else:
            exit_price = ticker.best_ask  # buy at ask
            close_side = "buy"

        # Apply slippage
        if close_side == "buy":
            exit_price *= (1 + self._slippage_pct / 100)
        else:
            exit_price *= (1 - self._slippage_pct / 100)

        return self.place_limit_order(
            symbol, close_side, exit_price, pos.quantity, post_only=False,
        )

    # ------------------------------------------------------------------
    # Paper trading stats & reporting
    # ------------------------------------------------------------------

    @property
    def balance(self) -> float:
        return self._balance

    @property
    def total_pnl(self) -> float:
        return self._balance - self._initial_balance

    @property
    def open_positions(self) -> Dict[str, Position]:
        return dict(self._positions)

    @property
    def trade_count(self) -> int:
        return len(self._trade_history)

    @property
    def closed_trades(self) -> List[PaperTrade]:
        return [t for t in self._trade_history if t.exit_price > 0]

    def get_db_stats(self) -> Dict:
        """Get aggregate stats from DB (survives restarts)."""
        return paper_db.get_trade_stats(self._exchange_name)

    def get_summary(self) -> str:
        """Human-readable summary of paper trading performance."""
        stats = self.get_db_stats()
        closed_count = stats.get("closed_trades", 0)
        wins = stats.get("wins", 0)
        losses = stats.get("losses", 0)
        total_pnl_db = stats.get("total_pnl", 0)

        open_pos_str = ""
        for sym, pos in self._positions.items():
            try:
                ticker = self.get_ticker(sym)
                if pos.side == "long":
                    unrealized = (ticker.mark_price - pos.entry_price) * pos.quantity
                else:
                    unrealized = (pos.entry_price - ticker.mark_price) * pos.quantity
                open_pos_str += (
                    f"\n  {sym}: {pos.side.upper()} {pos.quantity:.6f} "
                    f"@ ₹{pos.entry_price:,.2f} (UPnL: ₹{unrealized:,.2f})"
                )
            except Exception:
                open_pos_str += (
                    f"\n  {sym}: {pos.side.upper()} {pos.quantity:.6f} "
                    f"@ ₹{pos.entry_price:,.2f}"
                )

        lines = [
            f"📊 <b>{self.name} — Paper Trading Summary</b>",
            f"━━━━━━━━━━━━━━━━",
            f"",
            f"<b>Balance:</b> ₹{self._balance:,.2f} (started ₹{self._initial_balance:,.2f})",
            f"<b>Net P&L:</b> ₹{self.total_pnl:+,.2f}",
            f"<b>Fees Paid:</b> ₹{self._total_fees_paid:,.2f}",
            f"",
            f"<b>Trades:</b> {closed_count} closed, {len(self._positions)} open",
            f"<b>Win/Loss:</b> {wins}W / {losses}L",
            f"<b>Realized P&L:</b> ₹{total_pnl_db:+,.2f}",
        ]
        if open_pos_str:
            lines.extend([
                f"",
                f"<b>Open Positions:</b>{open_pos_str}",
            ])
        return "\n".join(lines)
