"""
Requirement #5: if exchange A allows 20x and exchange B allows 30x on this
symbol, use 20x on BOTH so notional stays hedged 1:1. Using the higher
leverage on one leg only would leave you over- or under-hedged in notional
terms even though "quantity" matches, because margin/liquidation dynamics
differ — but more importantly here, mismatched leverage doesn't itself
unbalance a hedge (notional is what must match, not leverage), so this
module's real job is: find the leverage BOTH exchanges will actually accept
for this symbol right now, so your set_leverage() calls don't get silently
capped to different numbers by each exchange.
"""

from dataclasses import dataclass
from exchanges.base import ExchangeClient


@dataclass
class LeverageDecision:
    requested: int
    exchange_a_applied: int
    exchange_b_applied: int
    common_leverage: int


def sync_leverage(client_a: ExchangeClient, client_b: ExchangeClient,
                   symbol_a: str, symbol_b: str, requested_leverage: int) -> LeverageDecision:
    applied_a = client_a.set_leverage(symbol_a, requested_leverage)
    applied_b = client_b.set_leverage(symbol_b, requested_leverage)
    common = min(applied_a, applied_b)

    # If either exchange capped below what we asked, re-apply the common
    # (lower) leverage to BOTH so the notional-per-unit-margin ratio matches
    # on both legs — this is the actual point of requirement #5.
    if applied_a != common:
        client_a.set_leverage(symbol_a, common)
    if applied_b != common:
        client_b.set_leverage(symbol_b, common)

    return LeverageDecision(
        requested=requested_leverage,
        exchange_a_applied=applied_a,
        exchange_b_applied=applied_b,
        common_leverage=common,
    )
