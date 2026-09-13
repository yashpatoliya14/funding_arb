"""
Multi-coin scanner: discovers all perpetual futures instruments across all
exchanges, matches them by base asset, fetches funding rates, and ranks
arbitrage opportunities by expected net P&L.

This is the brain of the multi-coin upgrade. The engine calls:
    scanner.refresh_instruments(clients)   # once at startup + periodically
    scanner.find_best_opportunity(...)      # each funding cycle

The scanner is exchange-agnostic — it talks through the ExchangeClient
interface only, so it works identically with real and simulated clients.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from config.constants import (
    COIN_WHITELIST, SCAN_ALL_COINS, MIN_NET_EDGE_PCT,
    FIXED_NOTIONAL_INR, SLIPPAGE_BPS_PER_LEG,
)
from config import settings
from exchanges.base import ExchangeClient, InstrumentInfo, Ticker

log = logging.getLogger("coin_scanner")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class SymbolMap:
    """Maps base_asset -> {exchange_name: exchange-native symbol}"""
    mapping: Dict[str, Dict[str, str]] = field(default_factory=dict)
    instruments: Dict[str, Dict[str, InstrumentInfo]] = field(default_factory=dict)

    def common_coins(self, exchange_a: str, exchange_b: str) -> List[str]:
        """Return base assets tradeable on BOTH exchanges."""
        result = []
        for base, exmap in self.mapping.items():
            if exchange_a in exmap and exchange_b in exmap:
                result.append(base)
        return sorted(result)

    def get_symbol(self, base_asset: str, exchange: str) -> Optional[str]:
        return self.mapping.get(base_asset, {}).get(exchange)


@dataclass
class FundingSnapshot:
    """Ticker + funding data for one coin on one exchange, at a point in time."""
    base_asset: str
    exchange: str
    symbol: str
    funding_rate: float       # as fraction (e.g. 0.001 = 0.1%)
    mark_price: float
    best_bid: float
    best_ask: float
    timestamp: float = 0.0    # epoch seconds


@dataclass
class ArbOpportunity:
    """A single arbitrage opportunity: one coin, two exchanges, with full cost breakdown."""
    base_asset: str
    # The exchange where we collect funding (short if funding > 0)
    funding_exchange: str
    funding_symbol: str
    funding_rate: float
    funding_side: str         # "sell" (short to collect positive funding) or "buy"
    # The hedge exchange (opposite side)
    hedge_exchange: str
    hedge_symbol: str
    hedge_rate: float
    hedge_side: str
    # Prices
    funding_bid: float
    funding_ask: float
    funding_mark: float
    hedge_bid: float
    hedge_ask: float
    hedge_mark: float
    # Cost breakdown (all in %)
    funding_received_pct: float
    funding_paid_pct: float
    net_funding_pct: float
    entry_fees_pct: float
    exit_fees_pct: float
    spread_cost_pct: float
    slippage_cost_pct: float
    total_cost_pct: float
    net_pnl_pct: float
    # Sizing
    quantity: float = 0.0
    notional_inr: float = 0.0
    tradeable: bool = False
    reason: str = ""


# ---------------------------------------------------------------------------
# CoinScanner
# ---------------------------------------------------------------------------

class CoinScanner:
    def __init__(self):
        self._symbol_map = SymbolMap()
        self._last_refresh: float = 0
        self._refresh_interval_sec: float = 300  # re-scan instruments every 5 min

    @property
    def symbol_map(self) -> SymbolMap:
        return self._symbol_map

    def refresh_instruments(self, clients: Dict[str, ExchangeClient],
                             force: bool = False) -> SymbolMap:
        """Fetch instrument lists from all exchanges, build the cross-exchange
        symbol map. Caches results and only re-fetches after refresh interval."""
        now = time.time()
        if not force and (now - self._last_refresh) < self._refresh_interval_sec:
            return self._symbol_map

        log.info("Refreshing instrument lists from %d exchanges...", len(clients))
        mapping: Dict[str, Dict[str, str]] = {}
        instruments: Dict[str, Dict[str, InstrumentInfo]] = {}

        # Determine which coins to include
        whitelist = _effective_whitelist()

        for ex_name, client in clients.items():
            try:
                inst_list = client.list_instruments()
            except Exception as e:
                log.warning("Failed to list instruments for %s: %s", ex_name, e)
                inst_list = []

            if not inst_list:
                # Fallback: create a synthetic instrument from legacy symbol config
                log.warning("%s returned no instruments — using legacy fallback symbol", ex_name)
                fallback_sym = _legacy_symbol(ex_name)
                fallback_base = _parse_base_from_symbol(fallback_sym)
                inst_list = [InstrumentInfo(
                    symbol=fallback_sym, base_asset=fallback_base,
                    quote_asset="USDT", contract_type="perpetual",
                )]

            for inst in inst_list:
                base = inst.base_asset.upper()
                if whitelist and base not in whitelist:
                    continue
                if not inst.is_active:
                    continue
                # Prefer USDT-margined perps when multiple exist for the same base
                if base in mapping and ex_name in mapping[base]:
                    existing = instruments[base][ex_name]
                    if "USDT" in inst.symbol.upper() and "USDT" not in existing.symbol.upper():
                        pass  # replace with USDT version
                    else:
                        continue
                mapping.setdefault(base, {})[ex_name] = inst.symbol
                instruments.setdefault(base, {})[ex_name] = inst

        self._symbol_map = SymbolMap(mapping=mapping, instruments=instruments)
        self._last_refresh = now

        coin_count = len(mapping)
        log.info("Symbol map built: %d coins across %d exchanges", coin_count, len(clients))
        for base, exmap in sorted(mapping.items()):
            log.debug("  %s: %s", base, exmap)

        return self._symbol_map

    def scan_funding_rates(self, clients: Dict[str, ExchangeClient],
                            exchange_a: str, exchange_b: str) -> List[FundingSnapshot]:
        """Fetch tickers for all common coins between two exchanges.
        Returns a flat list of FundingSnapshot objects."""
        common = self._symbol_map.common_coins(exchange_a, exchange_b)
        if not common:
            log.warning("No common coins between %s and %s", exchange_a, exchange_b)
            return []

        snapshots = []
        now = time.time()

        for base in common:
            for ex_name in (exchange_a, exchange_b):
                symbol = self._symbol_map.get_symbol(base, ex_name)
                if not symbol:
                    continue
                client = clients[ex_name]
                try:
                    ticker = client.get_ticker(symbol)
                    snapshots.append(FundingSnapshot(
                        base_asset=base,
                        exchange=ex_name,
                        symbol=symbol,
                        funding_rate=ticker.funding_rate or 0.0,
                        mark_price=ticker.mark_price,
                        best_bid=ticker.best_bid,
                        best_ask=ticker.best_ask,
                        timestamp=now,
                    ))
                except Exception as e:
                    log.warning("Failed to get ticker for %s on %s: %s", symbol, ex_name, e)

        return snapshots

    def find_best_opportunity(
        self,
        clients: Dict[str, ExchangeClient],
        exchange_a: str,
        exchange_b: str,
    ) -> Optional[ArbOpportunity]:
        """Scan all common coins, compute full cost for each, return the one
        with the highest net P&L (or None if nothing clears the threshold)."""
        from core.spread_calc import evaluate_funding_trade_full

        snapshots = self.scan_funding_rates(clients, exchange_a, exchange_b)

        # Group snapshots by base asset
        by_coin: Dict[str, Dict[str, FundingSnapshot]] = {}
        for snap in snapshots:
            by_coin.setdefault(snap.base_asset, {})[snap.exchange] = snap

        best: Optional[ArbOpportunity] = None

        for base, exsnaps in by_coin.items():
            snap_a = exsnaps.get(exchange_a)
            snap_b = exsnaps.get(exchange_b)
            if not snap_a or not snap_b:
                continue
            if snap_a.mark_price <= 0 or snap_b.mark_price <= 0:
                continue

            opp = evaluate_funding_trade_full(
                exchange_a=exchange_a,
                exchange_b=exchange_b,
                base_asset=base,
                funding_rate_a=snap_a.funding_rate,
                funding_rate_b=snap_b.funding_rate,
                bid_a=snap_a.best_bid,
                ask_a=snap_a.best_ask,
                mark_a=snap_a.mark_price,
                bid_b=snap_b.best_bid,
                ask_b=snap_b.best_ask,
                mark_b=snap_b.mark_price,
                symbol_a=snap_a.symbol,
                symbol_b=snap_b.symbol,
            )

            if opp.tradeable and (best is None or opp.net_pnl_pct > best.net_pnl_pct):
                best = opp

        if best:
            log.info("Best opportunity: %s on %s<->%s — net %.4f%% (%s)",
                     best.base_asset, best.funding_exchange, best.hedge_exchange,
                     best.net_pnl_pct, best.reason)
        else:
            log.info("No tradeable opportunity found across %d coins on %s<->%s",
                     len(by_coin), exchange_a, exchange_b)

        return best


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _effective_whitelist() -> set:
    """Determine the active coin whitelist from settings + constants."""
    if settings.SCAN_ALL_COINS or SCAN_ALL_COINS:
        return set()   # empty = no filter

    override = getattr(settings, "COIN_WHITELIST_OVERRIDE", None)
    if override:
        return {c.upper() for c in override}

    if COIN_WHITELIST:
        return {c.upper() for c in COIN_WHITELIST}

    # Ultimate fallback: BTC only
    return {"BTC"}


def _legacy_symbol(exchange_name: str) -> str:
    """Return the legacy single-symbol from settings for backward compat."""
    return {
        "delta": settings.DELTA_SYMBOL,
        "coinswitch": settings.COINSWITCH_SYMBOL,
        "shark": settings.SHARK_SYMBOL,
    }.get(exchange_name, "BTCUSDT")


def _parse_base_from_symbol(symbol: str) -> str:
    """Best-effort extraction of base asset from a symbol string."""
    import re
    s = symbol.upper()
    # Remove common separators
    s = re.sub(r'[-_]', '', s)
    for suffix in ("USDT", "USD", "INR", "BUSD", "PERP"):
        if s.endswith(suffix):
            return s[:-len(suffix)]
    return s
