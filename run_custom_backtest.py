"""
TradeSight Custom Backtest Script

Runs a historical backtest of QQQ (Tech Cluster) starting with $100,000
using the optimized sector parameters:
  - RSI Oversold: 30
  - RSI Overbought: 72
  - Sizing: 25% ($25,000 per trade)
  - Stop Loss: 5%
  - Take Profit: 8%
"""

import sys
import os
from pathlib import Path
import pandas as pd

# Add src to python path
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from data.ibkr_client import IBKRClient
from strategy_lab.backtest import BacktestEngine

def run_qqq_backtest():
    print("Connecting to TWS...")
    client = IBKRClient(client_id=11)
    if not client._wrapper.is_connected:
        print("Notice: TWS is not running — fetching historical data via Yahoo Finance fallback.")

    print("Fetching 1 year of QQQ 1H bars from TWS...")
    df = client.get_historical_data("QQQ", days=365, timeframe="1Hour")
    if df is None or len(df) < 50:
        print("Error: Failed to fetch QQQ historical data.")
        return

    # Calculate indicators needed by strategy
    # Simple rolling RSI(14)
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    df['rsi'] = 100 - (100 / (1 + rs))
    df['sma_50'] = df['close'].rolling(window=50).mean()

    # Optimized tech params
    oversold = 30
    overbought = 72
    size = 0.25  # 25% of portfolio size
    stop_loss_pct = 0.05
    take_profit_pct = 0.08

    def rsi_strategy(data, index, positions):
        if index < 50:
            return None
        current = data.iloc[index]
        
        # Exit Logic
        if positions:
            if current['rsi'] > overbought:
                return {'action': 'close'}
        
        # Entry Logic
        if not positions and current['rsi'] < oversold:
            price = current['close']
            sma50 = current['sma_50']
            if sma50 is not None and not pd.isna(sma50) and price < sma50 * 0.97:
                return None  # Trend filter
            
            sl_price = price * (1.0 - stop_loss_pct)
            tp_price = price * (1.0 + take_profit_pct)
            return {
                'action': 'buy',
                'size': size,
                'stop_loss': sl_price,
                'take_profit': tp_price
            }
        return None

    print(f"Running backtest on QQQ ({len(df)} bars) starting with $100,000...")
    engine = BacktestEngine(initial_balance=100000.0, slippage_pct=0.0005)
    results = engine.run_backtest(df, rsi_strategy, asset_name="QQQ")

    metrics = results['metrics']
    print("\n==============================================")
    print("📊 BACKTEST PERFORMANCE SUMMARY")
    print("==============================================")
    print(f"Initial Balance:     ${metrics['start_date'].strftime('%Y-%m-%d')}: $100,000.00")
    print(f"Final Value:         ${metrics['end_date'].strftime('%Y-%m-%d')}: ${engine.balance:,.2f}")
    print(f"Total Return:        {metrics['total_pnl_pct']:.2f}% (${metrics['total_pnl']:,.2f})")
    print(f"Win Rate:            {metrics['win_rate']:.2f}%")
    print(f"Total Trades:        {metrics['total_trades']}")
    print(f"Winning Trades:      {metrics['winning_trades']}")
    print(f"Losing Trades:       {metrics['losing_trades']}")
    print(f"Sharpe Ratio:        {metrics['sharpe_ratio']:.4f}")
    print(f"Max Drawdown:        {metrics['max_drawdown_pct']:.2f}%")
    print("==============================================\n")

    print("📜 DETAILED TRADES LOG:")
    trades = results['trades']
    if not trades:
        print("  No trades executed during this period.")
        return

    print(f"{'Entry Date':<20} | {'Exit Date':<20} | {'Entry Px':<10} | {'Exit Px':<10} | {'PnL %':<10} | {'PnL $':<12}")
    print("-" * 90)
    for t in trades:
        entry_t = t.get('entry_time')
        entry_time_str = entry_t.strftime('%Y-%m-%d %H:%M') if entry_t else "Unknown"
        exit_t = t.get('exit_time')
        exit_time_str = exit_t.strftime('%Y-%m-%d %H:%M') if exit_t else "Open"
        entry_px = t.get('entry_price', 0.0)
        exit_px = t.get('exit_price', 0.0)
        exit_px_str = f"{exit_px:.2f}" if exit_px else "N/A"
        pnl_pct = t.get('pnl_pct', 0.0)
        pnl_dollars = t.get('pnl', 0.0)
        print(f"{entry_time_str:<20} | {exit_time_str:<20} | {entry_px:<10.2f} | {exit_px_str:<10} | {pnl_pct:<10.2f} | ${pnl_dollars:<11.2f}")

if __name__ == "__main__":
    run_qqq_backtest()
