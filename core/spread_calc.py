"""
Requirement #2 & #6: calculate spread + fee/tax-adjusted net edge before entering.

Nothing here is exchange-specific — it takes tickers already fetched by
price_feed.py and returns a plain-English verdict: is the expected funding
payout, net of round-trip fees + GST on both exchanges, worth the trade.
"""

from dataclasses import dataclass
from config.constants import FEES, GST_ON_FEES, MIN_NET_EDGE_PCT


@dataclass
class EdgeReport:
    exchange_a: str
    exchange_b: str
    funding_rate_a: float
    funding_rate_b: float
    net_funding_edge_pct: float     # expected payout for holding through snapshot, %
    round_trip_cost_pct: float      # both legs, both directions, incl. GST, %
    inter_exchange_spread_pct: float  # cross-exchange price gap right now, %
    net_after_cost_pct: float
    tradeable: bool
    reason: str


def _fee_pct_round_trip(exchange: str, maker: bool = True) -> float:
    """One leg = one entry + one exit. Assume maker (post-only) on both."""
    schedule = FEES[exchange]
    per_side = schedule.maker if maker else schedule.taker
    per_side_with_gst = per_side * (1 + GST_ON_FEES)
    return per_side_with_gst * 2  # entry + exit


def evaluate_funding_trade(exchange_a: str, exchange_b: str,
                            funding_rate_a: float, funding_rate_b: float,
                            price_a: float, price_b: float) -> EdgeReport:
    """
    Convention: you go the direction that COLLECTS funding on the exchange
    with the more extreme rate, and hedge on the other. If funding_rate_a is
    positive (longs pay shorts) you'd go short on A; if funding_rate_b is
    negative you'd go long on B to also collect there if the position happens
    to line up as a hedge. In practice for a pure funding-capture hedge you
    usually pick ONE exchange for the funding leg and the OTHER purely as the
    hedge (see engine.py) — this function just tells you if it's worth it.
    """
    # Expected payout: funding you receive minus funding you might owe on the
    # hedge leg (if the hedge leg also happens to have funding — e.g. two
    # perp legs on two different exchanges, both funding-bearing).
    net_funding_edge_pct = abs(funding_rate_a) * 100  # primary funding capture leg

    round_trip_cost_pct = (
        _fee_pct_round_trip(exchange_a) + _fee_pct_round_trip(exchange_b)
    ) * 100

    inter_exchange_spread_pct = abs(price_a - price_b) / min(price_a, price_b) * 100

    net_after_cost_pct = net_funding_edge_pct - round_trip_cost_pct - inter_exchange_spread_pct

    tradeable = net_after_cost_pct >= MIN_NET_EDGE_PCT
    reason = (
        f"Edge {net_after_cost_pct:.4f}% "
        f"(funding {net_funding_edge_pct:.4f}% - fees {round_trip_cost_pct:.4f}% "
        f"- spread {inter_exchange_spread_pct:.4f}%) "
        f"{'clears' if tradeable else 'does NOT clear'} threshold {MIN_NET_EDGE_PCT}%"
    )

    return EdgeReport(
        exchange_a=exchange_a, exchange_b=exchange_b,
        funding_rate_a=funding_rate_a, funding_rate_b=funding_rate_b,
        net_funding_edge_pct=net_funding_edge_pct,
        round_trip_cost_pct=round_trip_cost_pct,
        inter_exchange_spread_pct=inter_exchange_spread_pct,
        net_after_cost_pct=net_after_cost_pct,
        tradeable=tradeable, reason=reason,
    )
