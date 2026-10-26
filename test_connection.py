import os
from datetime import date, timedelta

from dotenv import load_dotenv
from rich import print
from thetadata import ThetaClient

load_dotenv()

print("Connecting to ThetaData...")

try:
    client = ThetaClient(api_key=os.getenv("THETADATA_API_KEY"))
    print("Connected.")

    end = date.today()
    start = end - timedelta(days=10)
    df = client.stock_history_eod(symbol="SPY", start_date=start, end_date=end)
    print("\nSPY EOD tail:")
    print(df.tail())

    live = client.stock_snapshot_trade(symbol="SPY")
    print("\nSPY live snapshot:")
    print(live.tail() if live is not None else "no snapshot")

except Exception as e:
    print(f"Error: {e}")
