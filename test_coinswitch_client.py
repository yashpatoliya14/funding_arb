import os
import sys
import logging
from dotenv import load_dotenv

# Set up logging so we can see the internal debug/info messages
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

# Load environment variables
load_dotenv()
api_key = os.getenv("COINSWITCH_API_KEY")
api_secret = os.getenv("COINSWITCH_API_SECRET")

if not api_key or not api_secret:
    print("Error: COINSWITCH_API_KEY or COINSWITCH_API_SECRET not found in .env")
    sys.exit(1)

from exchanges.coinswitch_client import CoinswitchClient

def main():
    print("==================================================")
    print("      CoinSwitch Client Interactive Test          ")
    print("==================================================")
    
    print("\n[1] Initializing CoinswitchClient...")
    client = CoinswitchClient(api_key, api_secret)
    
    print("\n[2] Fetching Coin List (list_instruments)...")
    try:
        instruments = client.list_instruments()
        print(f"[SUCCESS] Successfully loaded {len(instruments)} instruments.")
        
        # Display the first 10 coins as an example
        print("Sample of available coins:")
        for inst in instruments[:10]:
            print(f"  - {inst.symbol} (Base: {inst.base_asset}, Quote: {inst.quote_asset})")
        if len(instruments) > 10:
            print(f"  ... and {len(instruments) - 10} more.")
            
    except Exception as e:
        print(f"[ERROR] Failed to fetch instruments: {e}")

    print("\n[3] Testing Authenticated Endpoint (get_position for BTCUSDT)...")
    try:
        position = client.get_position("BTCUSDT")
        print("[SUCCESS] Authentication successful! Bypassed Cloudflare.")
        if position:
            print(f"  -> Open Position: {position}")
        else:
            print("  -> No open positions for BTCUSDT.")
    except Exception as e:
        print(f"[ERROR] Failed to fetch position: {e}")

    print("\n[4] Testing Ticker Fetch (get_ticker for BTCUSDT)...")
    try:
        # Note: This will try the WebSocket first, then fallback to REST if blocked
        ticker = client.get_ticker("BTCUSDT")
        print("[SUCCESS] Successfully fetched ticker.")
        print(f"  -> Mark Price: {ticker.mark_price}")
        print(f"  -> Best Bid: {ticker.best_bid}")
        print(f"  -> Best Ask: {ticker.best_ask}")
        print(f"  -> Funding Rate: {ticker.funding_rate}")
    except Exception as e:
        print(f"[ERROR] Failed to fetch ticker: {e}")
        
    print("\n==================================================")
    print("                 Test Complete                    ")
    print("==================================================")

if __name__ == "__main__":
    main()
