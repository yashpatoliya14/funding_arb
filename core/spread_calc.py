"""
Requirement #2 & #6: calculate spread + fee/tax-adjusted net edge before entering.

Full cost model (multi-coin upgrade):
    Net PnL = funding_received − funding_paid
            − entry_fees (both legs, incl. GST)
            − exit_fees (both legs, incl. GST)
            − bid_ask_crossing_cost (both legs)
            − slippage_buffer (both legs)

Nothing here is exchange-specific — it takes tickers already fetched by
the coin scanner and returns a detailed cost breakdown verdict.
"""

from dataclasses import dataclass
from config.constants import (
    FEES, GST_ON_FEES, MIN_NET_EDGE_PCT, SLIPPAGE_BPS_PER_LEG,
    FIXED_NOTIONAL_INR,
)
from config import settings


# ---------------------------------------------------------------------------
# Legacy EdgeReport (kept for backward compatibility with any callers)
# ---------------------------------------------------------------------------

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
    schedule = FEES.get(exchange)
    if not schedule:
        return 0.001  # conservative fallback: 0.1% round-trip
    per_side = schedule.maker if maker else schedule.taker
    per_side_with_gst = per_side * (1 + GST_ON_FEES)
    return per_side_with_gst * 2  # entry + exit


def _fee_pct_one_side(exchange: str, maker: bool = True) -> float:
    """Fee for a single entry OR exit on one exchange, incl. GST."""
    schedule = FEES.get(exchange)
    if not schedule:
        return 0.0005  # conservative fallback
    per_side = schedule.maker if maker else schedule.taker
    return per_side * (1 + GST_ON_FEES)


def evaluate_funding_trade(exchange_a: str, exchange_b: str,
                            funding_rate_a: float, funding_rate_b: float,
                            price_a: float, price_b: float) -> EdgeReport:
    """Legacy simplified edge calculation — kept for backward compatibility.
    For the full cost model, use evaluate_funding_trade_full() instead."""

    net_funding_edge_pct = abs(funding_rate_a - funding_rate_b) * 100

    round_trip_cost_pct = (
        _fee_pct_round_trip(exchange_a) + _fee_pct_round_trip(exchange_b)
    ) * 100

    inter_exchange_spread_pct = abs(price_a - price_b) / min(price_a, price_b) * 100 if min(price_a, price_b) > 0 else 0

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


# ---------------------------------------------------------------------------
# Full cost model for multi-coin scanning
# ---------------------------------------------------------------------------

def evaluate_funding_trade_full(
    exchange_a: str,
    exchange_b: str,
    base_asset: str,
    funding_rate_a: float,
    funding_rate_b: float,
    bid_a: float,
    ask_a: float,
    mark_a: float,
    bid_b: float,
    ask_b: float,
    mark_b: float,
    symbol_a: str = "",
    symbol_b: str = "",
):
    """
    Comprehensive cost analysis for a funding arbitrage trade.

    Determines direction automatically:
    - The exchange with the higher funding rate is the "funding leg" —
      you go SHORT there to collect positive funding (or LONG if funding
      is negative, to collect from shorts).
    - The other exchange is the "hedge leg" — opposite direction.

    Returns an ArbOpportunity with full cost breakdown.
    """
    from core.coin_scanner import ArbOpportunity

    # ---- Step 1: Determine direction ----
    # Funding spread = rate_a - rate_b. If positive, you want to SHORT A
    # (collect from longs) and LONG B (hedge). If negative, reverse.
    spread = funding_rate_a - funding_rate_b

    if spread >= 0:
        # A has higher rate -> short A (collect funding), long B (hedge)
        funding_ex, hedge_ex = exchange_a, exchange_b
        funding_sym, hedge_sym = symbol_a, symbol_b
        funding_rate, hedge_rate = funding_rate_a, funding_rate_b
        f_bid, f_ask, f_mark = bid_a, ask_a, mark_a
        h_bid, h_ask, h_mark = bid_b, ask_b, mark_b
        funding_side = "sell"   # short the funding leg
        hedge_side = "buy"      # long the hedge leg
    else:
        # B has higher rate -> short B (collect funding), long A (hedge)
        funding_ex, hedge_ex = exchange_b, exchange_a
        funding_sym, hedge_sym = symbol_b, symbol_a
        funding_rate, hedge_rate = funding_rate_b, funding_rate_a
        f_bid, f_ask, f_mark = bid_b, ask_b, mark_b
        h_bid, h_ask, h_mark = bid_a, ask_a, mark_a
        funding_side = "sell"
        hedge_side = "buy"

    # ---- Step 2: Calculate funding income/expense ----
    # Funding leg: you're SHORT there.
    #   If funding_rate > 0: shorts RECEIVE from longs (positive funding)
    #   If funding_rate < 0: shorts PAY to longs (negative funding)
    # Hedge leg: you're LONG there.
    #   If hedge_rate > 0: longs PAY to shorts (positive funding)
    #   If hedge_rate < 0: longs RECEIVE from shorts (negative funding)
    if funding_rate >= 0:
        funding_received_pct = abs(funding_rate) * 100   # shorts receive ✓
    else:
        funding_received_pct = -abs(funding_rate) * 100  # shorts PAY when rate negative ✓

    if hedge_rate >= 0:
        funding_paid_pct = abs(hedge_rate) * 100         # long pays when rate positive ✓
    else:
        funding_paid_pct = -abs(hedge_rate) * 100        # long receives when rate negative ✓

    net_funding_pct = funding_received_pct - funding_paid_pct

    # ---- Step 3: Calculate fees (entry + exit, both legs, incl. GST) ----
    entry_fee_funding = _fee_pct_one_side(funding_ex, maker=True) * 100
    exit_fee_funding = _fee_pct_one_side(funding_ex, maker=True) * 100
    entry_fee_hedge = _fee_pct_one_side(hedge_ex, maker=True) * 100
    exit_fee_hedge = _fee_pct_one_side(hedge_ex, maker=True) * 100

    entry_fees_pct = entry_fee_funding + entry_fee_hedge
    exit_fees_pct = exit_fee_funding + exit_fee_hedge

    # ---- Step 4: Bid/ask spread cost ----
    # Funding leg: selling (going short) → you sell at the bid
    # Cost of crossing: (mid - bid) / mid ≈ half_spread / mid
    f_mid = (f_bid + f_ask) / 2 if (f_bid > 0 and f_ask > 0) else f_mark
    h_mid = (h_bid + h_ask) / 2 if (h_bid > 0 and h_ask > 0) else h_mark

    if f_mid > 0 and f_bid > 0 and f_ask > 0:
        spread_cost_funding_pct = (f_ask - f_bid) / f_mid * 100 * 0.5  # half spread
    else:
        spread_cost_funding_pct = 0.0

    if h_mid > 0 and h_bid > 0 and h_ask > 0:
        spread_cost_hedge_pct = (h_ask - h_bid) / h_mid * 100 * 0.5
    else:
        spread_cost_hedge_pct = 0.0

    spread_cost_pct = spread_cost_funding_pct + spread_cost_hedge_pct

    # ---- Step 5: Slippage buffer ----
    slippage_cost_pct = SLIPPAGE_BPS_PER_LEG * 100 * 2  # both legs, entry only
    # (exit slippage is already budgeted in exit_fees above; the slippage buffer
    # here covers market impact beyond the quoted top-of-book)

    # ---- Step 6: Total cost and net P&L ----
    total_cost_pct = entry_fees_pct + exit_fees_pct + spread_cost_pct + slippage_cost_pct
    net_pnl_pct = net_funding_pct - total_cost_pct

    tradeable = net_pnl_pct >= MIN_NET_EDGE_PCT

    # ---- Step 7: Compute quantity from notional ----
    avg_mark = (f_mark + h_mark) / 2 if (f_mark > 0 and h_mark > 0) else max(f_mark, h_mark)
    notional = settings.FIXED_NOTIONAL_INR if settings.FIXED_NOTIONAL_INR > 0 else 0
    quantity = notional / avg_mark if (notional > 0 and avg_mark > 0) else settings.TRADE_QUANTITY

    reason = (
        f"{base_asset}: net_pnl={net_pnl_pct:+.4f}% "
        f"[funding={net_funding_pct:.4f}% (recv={funding_received_pct:.4f}% pay={funding_paid_pct:.4f}%) "
        f"- fees={entry_fees_pct + exit_fees_pct:.4f}% "
        f"- spread={spread_cost_pct:.4f}% "
        f"- slip={slippage_cost_pct:.4f}%] "
        f"{'✓ TRADEABLE' if tradeable else '✗ below threshold'} (min={MIN_NET_EDGE_PCT}%)"
    )

    return ArbOpportunity(
        base_asset=base_asset,
        funding_exchange=funding_ex,
        funding_symbol=funding_sym,
        funding_rate=funding_rate,
        funding_side=funding_side,
        hedge_exchange=hedge_ex,
        hedge_symbol=hedge_sym,
        hedge_rate=hedge_rate,
        hedge_side=hedge_side,
        funding_bid=f_bid,
        funding_ask=f_ask,
        funding_mark=f_mark,
        hedge_bid=h_bid,
        hedge_ask=h_ask,
        hedge_mark=h_mark,
        funding_received_pct=funding_received_pct,
        funding_paid_pct=funding_paid_pct,
        net_funding_pct=net_funding_pct,
        entry_fees_pct=entry_fees_pct,
        exit_fees_pct=exit_fees_pct,
        spread_cost_pct=spread_cost_pct,
        slippage_cost_pct=slippage_cost_pct,
        total_cost_pct=total_cost_pct,
        net_pnl_pct=net_pnl_pct,
        quantity=quantity,
        notional_inr=notional,
        tradeable=tradeable,
        reason=reason,
    )
