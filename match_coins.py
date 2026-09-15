import os
import sys
import logging
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

load_dotenv()

from config import settings
from exchanges.delta_client import DeltaClient
from exchanges.binance_client import BinanceClient
from exchanges.bybit_client import BybitClient
from core.coin_scanner import CoinScanner
from core.spread_calc import evaluate_funding_trade_full

def main():
    print("==================================================")
    print("      High Funding Arbitrage Scanner              ")
    print("==================================================")
    
    delta_key = os.getenv("DELTA_API_KEY", "")
    delta_secret = os.getenv("DELTA_API_SECRET", "")
    binance_key = os.getenv("BINANCE_API_KEY", "")
    binance_secret = os.getenv("BINANCE_API_SECRET", "")
    bybit_key = os.getenv("BYBIT_API_KEY", "")
    bybit_secret = os.getenv("BYBIT_API_SECRET", "")
    
    print("\n[1] Initializing Clients...")
    clients = {}
    if delta_key and delta_secret:
        clients["delta"] = DeltaClient(delta_key, delta_secret)
        print("  ✓ Delta initialized")

    # Binance/Bybit work without API keys for market data
    clients["binance"] = BinanceClient(binance_key, binance_secret)
    print(f"  ✓ Binance initialized {'(scan-only, no API key)' if not binance_key else ''}")

    clients["bybit"] = BybitClient(bybit_key, bybit_secret)
    print(f"  ✓ Bybit initialized {'(scan-only, no API key)' if not bybit_key else ''}")
        
    if len(clients) < 2:
        print("Error: Need at least 2 configured exchanges to compare pairs.")
        sys.exit(1)

    print("\n[2] Scanning instruments via CoinScanner...")
    scanner = CoinScanner()
    scanner.refresh_instruments(clients, force=True)
    
    pairs = [("delta", "binance"), ("delta", "bybit"), ("binance", "bybit")]
    
    for ex_a, ex_b in pairs:
        if ex_a not in clients or ex_b not in clients:
            continue
            
        print(f"\n=========================================================================")
        print(f"      Pair: {ex_a.upper()} <-> {ex_b.upper()}                            ")
        print(f"=========================================================================")
        
        print(f"[3] Fetching real-time funding rates & prices for {ex_a} and {ex_b}...")
        snapshots = scanner.scan_funding_rates(clients, ex_a, ex_b)
        
        # Group snapshots by base asset
        by_coin = {}
        for snap in snapshots:
            by_coin.setdefault(snap.base_asset, {})[snap.exchange] = snap

        opportunities = []
        
        for base, exsnaps in by_coin.items():
            snap_a = exsnaps.get(ex_a)
            snap_b = exsnaps.get(ex_b)
            if not snap_a or not snap_b:
                continue
            if snap_a.mark_price <= 0 or snap_b.mark_price <= 0:
                continue

            opp = evaluate_funding_trade_full(
                exchange_a=ex_a,
                exchange_b=ex_b,
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
            
            # Filter by RAW funding difference (before fees) > 0.01%
            if opp.net_funding_pct > 0.01:
                opportunities.append(opp)

        # Sort descending by raw funding difference (net_funding_pct)
        opportunities.sort(key=lambda x: x.net_funding_pct, reverse=True)
        
        print(f"\n[SUCCESS] Found {len(opportunities)} coins with > 0.01% Raw Funding Difference!")
        print("-" * 130)
        print(f"{'COIN':<8} | {ex_a.upper()+' RATE':<12} | {ex_b.upper()+' RATE':<12} | {'FUNDING DIFF':<13} | {'SLIPPAGE %':<11} | {'FEES %':<10} | {'NET P&L %':<10}")
        print("-" * 130)
        
        for opp in opportunities[:30]:  # Print top 30
            if opp.funding_exchange == ex_a:
                rate_a = opp.funding_rate
                rate_b = opp.hedge_rate
            else:
                rate_a = opp.hedge_rate
                rate_b = opp.funding_rate
                
            fees_pct = opp.entry_fees_pct + opp.exit_fees_pct
            spread_and_slip_pct = opp.spread_cost_pct + opp.slippage_cost_pct
            
            print(f"{opp.base_asset:<8} | {rate_a*100:>11.4f}% | {rate_b*100:>11.4f}% | "
                  f"{opp.net_funding_pct:>12.4f}% | {spread_and_slip_pct:>10.4f}% | {fees_pct:>9.4f}% | {opp.net_pnl_pct:>8.4f}%")

        print("-" * 130)

    print("\n==================================================")
    print("                 Test Complete                    ")
    print("==================================================")

if __name__ == "__main__":
    main()
