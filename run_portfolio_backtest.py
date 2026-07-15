"""
TradeSight Portfolio Multi-Asset Backtest Simulator

Simulates exactly how the Paper Trader would behave in real-life:
  - Starting Capital: $100,000
  - Watchlist: 20 symbols (SPY, QQQ, AAPL, MSFT, etc.)
  - Grid: Walk through historical data hour-by-hour (bar-by-bar)
  - Rules:
    * Tailored parameters per symbol loaded from data/symbol_clusters.json
    * Maximum concurrent positions: 4
    * Correlation guard: Maximum 2 positions in the same sector
    * Position Sizing: 10% of portfolio ($10,000 per trade)
"""

import sys
import os
import json
from pathlib import Path
import pandas as pd
import numpy as np

# Add src to python path
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from data.ibkr_client import IBKRClient

# ==============================================================================
# SIMULATION CONFIGURATION
# ==============================================================================
# To test the last 3 years: set TIMEFRAME = "1Day" and DAYS = 1095
# To test the last 1 year (intraday): set TIMEFRAME = "1Hour" and DAYS = 365
# ==============================================================================
TIMEFRAME = "1Hour"  # "1Hour" or "1Day"
DAYS = 1095         # Number of calendar days (365 = 1 year, 1095 = 3 years)
# ==============================================================================

def run_portfolio_simulation():
    print("Connecting to TWS...")
    client = IBKRClient(client_id=12)
    if client.demo_mode:
        print("Error: TWS is not running. Cannot fetch real historical data.")
        return

    # Load symbol clusters
    cluster_file = Path(__file__).resolve().parent / 'data' / 'symbol_clusters.json'
    if not cluster_file.exists():
        print("Error: data/symbol_clusters.json not found.")
        return
    with open(cluster_file) as f:
        clusters = json.load(f)

    # Build maps
    symbol_to_params = {}
    symbol_to_sector = {}
    all_symbols = []
    for sector, data in clusters.items():
        params = data.get('default_params', {})
        # Force a safe 10% position size for the portfolio backtest (otherwise 25% sizing with 4 positions will over-leverage)
        params = dict(params)
        params['position_size'] = 0.10  # 10% allocation per trade
        for sym in data.get('symbols', []):
            symbol_to_params[sym] = params
            symbol_to_sector[sym] = sector
            all_symbols.append(sym)

    print(f"Fetching {DAYS} days of {TIMEFRAME} bars for {len(all_symbols)} symbols from TWS...")
    datasets = {}
    
    # Calculate chunks for intraday data
    if TIMEFRAME == "1Hour" and DAYS > 365:
        # Fetching in yearly chunks to bypass TWS duration constraints
        import datetime as dt
        from datetime import timedelta
        chunks = int(np.ceil(DAYS / 365))
        
        for sym in all_symbols:
            dfs = []
            failed = False
            for i in range(chunks):
                # Calculate end DateTime for this chunk (shift back by 1 year each loop)
                # First chunk is current time, second is 1 year ago, etc.
                end_dt = dt.datetime.now() - timedelta(days=365 * i)
                end_str = end_dt.strftime('%Y%m%d 16:00:00 US/Eastern') if i > 0 else ""
                
                try:
                    df = client.get_historical_data(sym, days=365, timeframe=TIMEFRAME, end_date=end_str)
                    if df is not None and len(df) > 0 and df.attrs.get('data_source') == 'ibkr':
                        dfs.append(df)
                    else:
                        failed = True
                        break
                except Exception as e:
                    failed = True
                    break
                    
            if not failed and dfs:
                # Merge chunks and sort chronologically
                full_df = pd.concat(dfs).drop_duplicates().sort_index()
                # Limit to total requested days (rough count of bars)
                max_bars = DAYS * 7  # 7 bars per day for 1 hour timeframe
                full_df = full_df.tail(max_bars)
                
                # Indicators
                delta = full_df['close'].diff()
                gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
                loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
                rs = gain / loss
                full_df['rsi'] = 100 - (100 / (1 + rs))
                full_df['sma_50'] = full_df['close'].rolling(window=50).mean()
                full_df.index = pd.to_datetime(full_df.index)
                
                datasets[sym] = full_df
                print(f"  {sym}: {len(full_df)} 1H bars loaded (merged across {chunks} chunks)")
            else:
                # Fallback to demo or standard fetch if chunks fail
                print(f"  ⚠️ {sym}: chunk-fetching failed, falling back to standard fetch")
                df = client.get_historical_data(sym, days=DAYS, timeframe=TIMEFRAME)
                if df is not None and len(df) >= 100:
                    delta = df['close'].diff()
                    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
                    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
                    rs = gain / loss
                    df['rsi'] = 100 - (100 / (1 + rs))
                    df['sma_50'] = df['close'].rolling(window=50).mean()
                    df.index = pd.to_datetime(df.index)
                    datasets[sym] = df
                    print(f"  {sym}: {len(df)} bars loaded")
                else:
                    print(f"  ⚠️ {sym}: failed to load data")
    else:
        # Standard single fetch for daily or short intraday
        for sym in all_symbols:
            df = client.get_historical_data(sym, days=DAYS, timeframe=TIMEFRAME)
            if df is not None and len(df) >= 100:
                delta = df['close'].diff()
                gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
                loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
                rs = gain / loss
                df['rsi'] = 100 - (100 / (1 + rs))
                df['sma_50'] = df['close'].rolling(window=50).mean()
                df.index = pd.to_datetime(df.index)
                datasets[sym] = df
                print(f"  {sym}: {len(df)} bars loaded")
            else:
                print(f"  ⚠️ {sym}: failed to load data")

    if not datasets:
        print("Error: No data loaded.")
        return

    # Align datasets by time index
    print("\nAligning timestamps...")
    # Find all unique timestamps across all datasets
    all_timestamps = sorted(list(set().union(*(df.index for df in datasets.values()))))
    print(f"Total trading hours in simulation: {len(all_timestamps)}")

    # Simulation variables
    initial_balance = 100000.0
    balance = initial_balance
    max_positions = 4
    max_per_sector = 2
    positions = []  # active positions list: [{symbol, entry_price, entry_time, qty, stop_loss, take_profit}]
    closed_trades = []
    equity_curve = []

    # Step through time hour-by-hour
    for current_time in all_timestamps:
        # 1. Update active positions (Exit Checks)
        active_positions = []
        for pos in positions:
            sym = pos['symbol']
            df = datasets[sym]
            
            # Check if this timestamp exists for this stock
            if current_time not in df.index:
                active_positions.append(pos)  # Keep active
                continue
                
            current_bar = df.loc[current_time]
            price = current_bar['close']
            rsi = current_bar['rsi']
            
            # Exit check 1: Stop Loss
            if price <= pos['stop_loss']:
                pnl = (price - pos['entry_price']) * pos['qty']
                pnl_pct = (price / pos['entry_price'] - 1.0) * 100.0
                balance += (pos['qty'] * price)
                closed_trades.append({
                    'symbol': sym,
                    'entry_time': pos['entry_time'],
                    'exit_time': current_time,
                    'entry_price': pos['entry_price'],
                    'exit_price': price,
                    'pnl': pnl,
                    'pnl_pct': pnl_pct,
                    'reason': 'Stop Loss'
                })
                continue
                
            # Exit check 2: Take Profit
            if price >= pos['take_profit']:
                pnl = (price - pos['entry_price']) * pos['qty']
                pnl_pct = (price / pos['entry_price'] - 1.0) * 100.0
                balance += (pos['qty'] * price)
                closed_trades.append({
                    'symbol': sym,
                    'entry_time': pos['entry_time'],
                    'exit_time': current_time,
                    'entry_price': pos['entry_price'],
                    'exit_price': price,
                    'pnl': pnl,
                    'pnl_pct': pnl_pct,
                    'reason': 'Take Profit'
                })
                continue
                
            # Exit check 3: RSI Overbought
            params = symbol_to_params[sym]
            if rsi > params['overbought']:
                pnl = (price - pos['entry_price']) * pos['qty']
                pnl_pct = (price / pos['entry_price'] - 1.0) * 100.0
                balance += (pos['qty'] * price)
                closed_trades.append({
                    'symbol': sym,
                    'entry_time': pos['entry_time'],
                    'exit_time': current_time,
                    'entry_price': pos['entry_price'],
                    'exit_price': price,
                    'pnl': pnl,
                    'pnl_pct': pnl_pct,
                    'reason': 'RSI Overbought'
                })
                continue
                
            active_positions.append(pos)
        positions = active_positions

        # Calculate current total equity (cash + position values)
        current_equity = balance
        for pos in positions:
            sym = pos['symbol']
            if current_time in datasets[sym].index:
                price = datasets[sym].loc[current_time]['close']
                current_equity += (pos['qty'] * price)
            else:
                current_equity += (pos['qty'] * pos['entry_price'])
        equity_curve.append(current_equity)

        # 2. Check for new Entries
        if len(positions) < max_positions:
            # Gather candidates that aren't already held
            active_symbols = {p['symbol'] for p in positions}
            
            # Check sector counts
            sector_counts = {}
            for p in positions:
                sec = symbol_to_sector[p['symbol']]
                sector_counts[sec] = sector_counts.get(sec, 0) + 1

            for sym in datasets.keys():
                if sym in active_symbols:
                    continue
                if current_time not in datasets[sym].index:
                    continue
                
                # Check sector limit
                sec = symbol_to_sector[sym]
                if sector_counts.get(sec, 0) >= max_per_sector:
                    continue
                
                current_bar = datasets[sym].loc[current_time]
                price = current_bar['close']
                rsi = current_bar['rsi']
                sma50 = current_bar['sma_50']
                
                # Skip invalid rows
                if pd.isna(rsi) or pd.isna(sma50):
                    continue
                
                params = symbol_to_params[sym]
                
                # Entry Signal Check
                if rsi < params['oversold'] and price >= sma50 * 0.97:
                    # Trigger entry!
                    trade_allocation = current_equity * params['position_size']
                    if balance < trade_allocation:
                        continue  # Not enough cash balance
                        
                    qty = trade_allocation / price
                    balance -= trade_allocation
                    
                    sl_price = price * (1.0 - params['stop_loss_pct'])
                    tp_price = price * (1.0 + params['take_profit_pct'])
                    
                    positions.append({
                        'symbol': sym,
                        'entry_time': current_time,
                        'entry_price': price,
                        'qty': qty,
                        'stop_loss': sl_price,
                        'take_profit': tp_price
                    })
                    
                    sector_counts[sec] = sector_counts.get(sec, 0) + 1
                    if len(positions) >= max_positions:
                        break

    # Calculate metrics
    final_equity = equity_curve[-1]
    total_pnl = final_equity - initial_balance
    total_pnl_pct = (final_equity / initial_balance - 1.0) * 100.0
    
    # Calculate drawdown
    peaks = pd.Series(equity_curve).cummax()
    drawdowns = (pd.Series(equity_curve) - peaks) / peaks * 100.0
    max_dd = drawdowns.min()

    winning_trades = [t for t in closed_trades if t['pnl'] > 0]
    win_rate = (len(winning_trades) / len(closed_trades) * 100.0) if closed_trades else 0.0

    print("\n==============================================")
    print(f"📈 PORTFOLIO SIMULATION REPORT ({DAYS} Days, {TIMEFRAME})")
    print("==============================================")
    print(f"Initial Value:       $100,000.00")
    print(f"Final Portfolio Px:  ${final_equity:,.2f}")
    print(f"Total Return:        {total_pnl_pct:+.2f}% (${total_pnl:,.2f})")
    print(f"Win Rate:            {win_rate:.2f}% ({len(winning_trades)} wins / {len(closed_trades)} trades)")
    print(f"Max Drawdown:        {max_dd:.2f}%")
    print("==============================================\n")

    print("📜 PORTFOLIO TRADES LOG (Recent 30):")
    if not closed_trades:
        print("  No trades executed.")
        return
        
    print(f"{'Symbol':<6} | {'Entry Date':<16} | {'Exit Date':<16} | {'Entry Px':<9} | {'Exit Px':<9} | {'PnL %':<8} | {'Reason':<15}")
    print("-" * 90)
    for t in closed_trades[-30:]:
        print(f"{t['symbol']:<6} | {t['entry_time'].strftime('%m-%d %H:%M'):<16} | {t['exit_time'].strftime('%m-%d %H:%M'):<16} | {t['entry_price']:<9.2f} | {t['exit_price']:<9.2f} | {t['pnl_pct']:<+8.2f} | {t['reason']:<15}")

    # Export all trades to reports/ folder
    os.makedirs("reports", exist_ok=True)
    report_path = f"reports/portfolio_backtest_all_trades.json"
    report_data = {
        "timeframe": TIMEFRAME,
        "days": DAYS,
        "metrics": {
            "initial_value": initial_balance,
            "final_equity": final_equity,
            "total_return_pct": total_pnl_pct,
            "total_pnl": total_pnl,
            "win_rate": win_rate,
            "total_trades": len(closed_trades),
            "winning_trades": len(winning_trades),
            "max_drawdown_pct": max_dd
        },
        "trades": [
            {
                "symbol": t["symbol"],
                "entry_time": t["entry_time"].strftime('%Y-%m-%d %H:%M'),
                "exit_time": t["exit_time"].strftime('%Y-%m-%d %H:%M'),
                "entry_price": round(float(t["entry_price"]), 2),
                "exit_price": round(float(t["exit_price"]), 2),
                "pnl_dollars": round(float(t["pnl"]), 2),
                "pnl_pct": round(float(t["pnl_pct"]), 2),
                "reason": t["reason"]
            }
            for t in closed_trades
        ]
    }
    with open(report_path, "w") as rf:
        json.dump(report_data, rf, indent=2)
    print(f"\n💾 Full list of all {len(closed_trades)} trades saved to: {report_path}")

if __name__ == "__main__":
    run_portfolio_simulation()
