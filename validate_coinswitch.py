import os
import sys
import logging
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO)

load_dotenv()

api_key = os.getenv("COINSWITCH_API_KEY")
api_secret = os.getenv("COINSWITCH_API_SECRET")

if not api_key or not api_secret:
    print("Error: COINSWITCH_API_KEY or COINSWITCH_API_SECRET not found in .env")
    sys.exit(1)

print(f"Loaded COINSWITCH_API_KEY: {api_key[:4]}...{api_key[-4:]}")
print(f"Loaded COINSWITCH_API_SECRET: ***HIDDEN***")

from exchanges.coinswitch_client import CoinswitchClient

try:
    print("Initializing CoinswitchClient...")
    client = CoinswitchClient(api_key, api_secret)
    
    print("Testing authenticated endpoint (GET /trade/api/v2/futures/positions for BTCUSDT)...")
    position = client.get_position("BTCUSDT")
    
    print("=========================================")
    print("SUCCESS! Authentication works.")
    print(f"Position response: {position}")
    print("=========================================")
    
except Exception as e:
    import traceback
    print("=========================================")
    print("FAILED! Authentication or network error.")
    print(f"Error: {e}")
    traceback.print_exc()
    print("=========================================")
