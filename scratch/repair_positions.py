import sqlite3
import os
import sys
from pathlib import Path
from datetime import datetime

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from data.ibkr_client import IBKRClient
from trading.position_manager import PositionManager

def main():
    print("🛠️ TradeSight Position Database Repair Script")
    print("=" * 60)
    
    # Initialize Client with client_id=99 to avoid conflicts
    print("🔌 Connecting to TWS (clientId=99)...")
    client = IBKRClient(client_id=99)
    if client.demo_mode:
        print("❌ Error: Could not connect to TWS. Make sure Trader Workstation is running.")
        sys.exit(1)
        
    print("✅ Connected to TWS!")
    
    # Fetch real positions from IBKR
    print("📋 Fetching real positions from IBKR...")
    ibkr_positions = client.get_remote_positions()
    print(f"   Found {len(ibkr_positions)} positions on broker:")
    for pos in ibkr_positions:
        print(f"     - {pos['symbol']}: qty={pos['qty']}, avg_price={pos['avg_entry_price']}, side={pos['side']}")
        
    # Connect to local positions DB
    pm = PositionManager()
    db_path = pm.data_dir / 'positions.db'
    
    print(f"\n📂 Opening database at {db_path}...")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # 1. Fetch current open positions from DB
    cursor.execute("SELECT id, symbol, quantity, entry_price, status FROM positions WHERE status='open';")
    local_open = {row[1]: {'id': row[0], 'qty': row[2], 'price': row[3]} for row in cursor.fetchall()}
    print(f"   Local DB has {len(local_open)} open positions.")
    
    # Track symbols processed
    processed_symbols = set()
    
    # 2. Re-sync/Update positions from TWS to local DB
    for pos in ibkr_positions:
        symbol = pos['symbol']
        qty = float(pos['qty'])
        avg_price = float(pos['avg_entry_price'])
        side = pos['side']
        processed_symbols.add(symbol)
        
        if symbol in local_open:
            db_pos = local_open[symbol]
            if float(db_pos['qty']) != qty:
                print(f"   ⚠️ Quantity mismatch for {symbol}: DB={db_pos['qty']}, Broker={qty}. Updating DB...")
                cursor.execute(
                    "UPDATE positions SET quantity=?, updated_at=CURRENT_TIMESTAMP WHERE id=?;",
                    (qty, db_pos['id'])
                )
            else:
                print(f"   ✅ {symbol} quantity matches: {qty}")
        else:
            print(f"   ➕ Restoring missing position for {symbol} (qty={qty}, price={avg_price})...")
            cursor.execute(
                "INSERT INTO positions (symbol, strategy, side, quantity, entry_price, current_price, status, entry_time, updated_at) "
                "VALUES (?, 'RSI Mean Reversion', ?, ?, ?, ?, 'open', ?, CURRENT_TIMESTAMP);",
                (symbol, side, qty, avg_price, avg_price, datetime.now().isoformat())
            )
            
    # 3. Close local positions that are not in TWS
    for symbol, db_pos in local_open.items():
        if symbol not in processed_symbols:
            print(f"   ➖ Closing stale local position {symbol} (not on broker)...")
            cursor.execute(
                "UPDATE positions SET status='closed', exit_time=?, exit_price=?, realized_pnl=0.0, updated_at=CURRENT_TIMESTAMP WHERE id=?;",
                (datetime.now().isoformat(), db_pos['price'], db_pos['id'])
            )
            
    # 4. Sync buying power and cash balance to resolve the negative cash balance issue
    print("\n💰 Syncing cash balance and buying power...")
    account = client.get_account()
    if account:
        real_equity = float(account.get("equity", 0))
        real_cash = float(account.get("buying_power", 0))
        print(f"   Broker Equity: ${real_equity:,.2f}, Cash: ${real_cash:,.2f}")
        
        # Persist balance to balance_cache
        cursor.execute("INSERT OR REPLACE INTO balance_cache (id, buying_power, synced_at) VALUES (1, ?, ?);", (real_cash, datetime.now().isoformat()))
        
        # Set portfolio history to match real metrics
        cursor.execute(
            "INSERT INTO portfolio_history (timestamp, total_value, available_cash, total_positions_value, unrealized_pnl, realized_pnl, total_pnl, position_count, strategies_active, buying_power, balance_synced_at) "
            "VALUES (?, ?, ?, ?, 0.0, 0.0, 0.0, ?, '[]', ?, ?);",
            (datetime.now().isoformat(), real_equity, real_cash, real_equity - real_cash, len(ibkr_positions), real_cash, datetime.now().isoformat())
        )
        
    conn.commit()
    conn.close()
    
    print("\n🎉 Repair complete!")

if __name__ == "__main__":
    main()
