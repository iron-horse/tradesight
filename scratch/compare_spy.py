import sys
from pathlib import Path
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from data.ibkr_client import IBKRClient

def calculate_spy_bh():
    client = IBKRClient(client_id=15)
    if client.demo_mode:
        print("TWS not connected")
        return
    df = client.get_historical_data("SPY", days=1095, timeframe="1Day")
    if df is not None and len(df) > 0:
        start_px = float(df.iloc[0]['close'])
        end_px = float(df.iloc[-1]['close'])
        pnl_pct = (end_px / start_px - 1.0) * 100.0
        final_val = 100000.0 * (end_px / start_px)
        print(f"SPY Start Price: ${start_px:.2f}")
        print(f"SPY End Price:   ${end_px:.2f}")
        print(f"Buy & Hold Return: {pnl_pct:+.2f}%")
        print(f"Ending Value of $100K: ${final_val:,.2f}")

if __name__ == "__main__":
    calculate_spy_bh()
