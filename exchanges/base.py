"""
Common interface every exchange client (real or simulated) implements.

The engine only ever talks to this interface — it never imports
DeltaClient/CoinswitchClient/SharkClient directly. That's what lets the
same engine code run against the dummy simulator (requirement #11) and
against real exchanges with zero changes to engine.py.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class Ticker:
    symbol: str
    best_bid: float
    best_ask: float
    mark_price: float
    funding_rate: Optional[float]        # current period funding rate, as fraction
    next_funding_time_ms: Optional[int]  # epoch ms of next funding snapshot


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
    """Every method below must be implemented by real clients AND the simulator."""

    name: str = "base"

    @abstractmethod
    def get_ticker(self, symbol: str) -> Ticker:
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
