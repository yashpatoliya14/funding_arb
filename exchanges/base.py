"""
Common interface every exchange client implements.

The engine only ever talks to this interface — it never imports
DeltaClient/BinanceClient/BybitClient directly. That's what lets the
same engine code run against real exchanges with zero changes to engine.py.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Ticker:
    symbol: str
    best_bid: float
    best_ask: float
    mark_price: float
    funding_rate: Optional[float]        # current period funding rate, as fraction
    next_funding_time_ms: Optional[int]  # epoch ms of next funding snapshot
    funding_interval_minutes: Optional[int] = None  # funding interval in minutes (e.g. 480 = 8h)


@dataclass
class InstrumentInfo:
    """Describes a single tradeable perpetual futures instrument on an exchange."""
    symbol: str              # exchange-native symbol, e.g. "BTCUSD", "ETHUSDT"
    base_asset: str          # normalized uppercase, e.g. "BTC", "ETH"
    quote_asset: str         # e.g. "USD", "USDT", "INR"
    contract_type: str       # "perpetual" for perps we care about
    min_quantity: float = 0.0
    tick_size: float = 0.0
    is_active: bool = True
    funding_interval_minutes: Optional[int] = None  # e.g. 480 = 8h, 240 = 4h


@dataclass
class OrderResult:
    exchange: str
    order_id: str
    symbol: str
    side: str            # "buy" / "sell"
    price: float
    quantity: float
    status: str           # "open" / "filled" / "partially_filled" / "cancelled" / "rejected"
    filled_quantity: float = 0.0
    avg_fill_price: float = 0.0


@dataclass
class Position:
    symbol: str
    side: str             # "long" / "short"
    quantity: float
    entry_price: float
    leverage: int


class ExchangeClient(ABC):
    """Every method below must be implemented by real clients."""

    name: str = "base"

    @abstractmethod
    def get_ticker(self, symbol: str) -> Ticker:
        ...

    @abstractmethod
    def list_instruments(self) -> List[InstrumentInfo]:
        """Return all perpetual futures instruments available on this exchange.
        Used by CoinScanner to discover tradeable coins and build the
        cross-exchange symbol map."""
        ...

    @abstractmethod
    def set_leverage(self, symbol: str, leverage: int) -> int:
        """Returns the leverage actually applied (exchange may cap it lower)."""
        ...

    @abstractmethod
    def place_limit_order(self, symbol: str, side: str, price: float,
                           quantity: float, post_only: bool = True) -> OrderResult:
        ...

    @abstractmethod
    def cancel_order(self, symbol: str, order_id: str) -> bool:
        ...

    @abstractmethod
    def get_order_status(self, symbol: str, order_id: str) -> OrderResult:
        ...

    @abstractmethod
    def get_position(self, symbol: str) -> Optional[Position]:
        ...

    @abstractmethod
    def close_position_market(self, symbol: str) -> OrderResult:
        """Emergency/clean exit — used by the leg-risk and basis-drift kill-switches."""
        ...
