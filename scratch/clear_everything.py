import sqlite3
import os
import sys
import asyncio
import time
from datetime import datetime

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from data.ibkr_client import IBKRClient
from trading.position_manager import PositionManager

def main():
    print("🚨 TradeSight Clear Everything Script")
    print("=" * 60)
    print("⚠️ WARNING: This will close ALL open positions on TWS and reset your local database.")
    print("=" * 60)
    
    # Initialize Client with client_id=99
    print("🔌 Connecting to TWS (clientId=99)...")
    client = IBKRClient(client_id=99)
    if client.demo_mode:
        print("❌ Error: Could not connect to TWS. Make sure Trader Workstation is running.")
        sys.exit(1)
        
    print("✅ Connected to TWS!")
    
    # Fetch real positions from IBKR
    print("📋 Fetching real positions from IBKR...")
    ibkr_positions = client.get_remote_positions()
    print(f"   Found {len(ibkr_positions)} positions to close.")
    
    # Close positions on TWS
    for pos in ibkr_positions:
        symbol = pos['symbol']
        qty = float(pos['qty'])
        print(f"   Selling {abs(qty)} shares of {symbol} to close position...")
        try:
            result = client.close_full_position(symbol)
            if 'error' in result:
                print(f"     ❌ Failed to close {symbol}: {result['error']}")
            else:
                print(f"     ✅ Successfully placed close order for {symbol}. Fill Price: {result.get('fill_price')}")
        except Exception as e:
            print(f"     ❌ Exception while closing {symbol}: {e}")
            
    # Wait a few seconds for order execution and settlement
    print("\n⏳ Waiting 5 seconds for order fills and balance updates to settle...")
    time.sleep(5)
    
    # Connect to local positions DB
    pm = PositionManager()
    db_path = pm.data_dir / 'positions.db'
    
    print(f"\n📂 Opening database at {db_path}...")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Clear local DB positions
    print("🧹 Clearing local database positions...")
    # Delete all positions to start 100% fresh, or mark them closed
    cursor.execute("DELETE FROM positions;")
    print("   Deleted all rows from 'positions' table.")
    
    # Sync new buying power and cash balance (since positions are closed, cash = total equity)
    print("\n💰 Syncing fresh cash balance from TWS...")
    account = client.get_account()
    if account:
        real_equity = float(account.get("equity", 0))
        real_cash = float(account.get("buying_power", 0))
        print(f"   Broker Equity after clearing: ${real_equity:,.2f}, Cash: ${real_cash:,.2f}")
        
        # Persist balance to balance_cache
        cursor.execute("INSERT OR REPLACE INTO balance_cache (id, buying_power, synced_at) VALUES (1, ?, ?);", (real_cash, datetime.now().isoformat()))
        
        # Reset portfolio history with 0 positions and full cash
        cursor.execute("DELETE FROM portfolio_history;")
        cursor.execute(
            "INSERT INTO portfolio_history (timestamp, total_value, available_cash, total_positions_value, unrealized_pnl, realized_pnl, total_pnl, position_count, strategies_active, buying_power, balance_synced_at) "
            "VALUES (?, ?, ?, 0.0, 0.0, 0.0, 0.0, 0, '[]', ?, ?);",
            (datetime.now().isoformat(), real_equity, real_cash, real_cash, datetime.now().isoformat())
        )
        
    conn.commit()
    conn.close()
    
    print("\n🎉 All positions closed and local database cleared successfully!")

if __name__ == "__main__":
    main()
