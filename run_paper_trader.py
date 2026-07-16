#!/usr/bin/env python3
"""
TradeSight Paper Trading Runner

Reads Alpaca keys from Keychain, initializes paper trader with real API,
runs a trading session, outputs report.

Usage:
  python3 run_paper_trader.py              # Run one trading session
  python3 run_paper_trader.py --report     # Just generate report (no trades)
  python3 run_paper_trader.py --status     # Show portfolio status
"""
import sys
import os

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

# Legacy Alpaca keys are no longer imported from keychain since migrating to IBKR
from trading.paper_trader import PaperTrader
from datetime import datetime

# Weekday guard (defense-in-depth): exit immediately on weekends (markets closed)
if datetime.now().weekday() >= 5:
    print(f'Weekday guard: skipping run on {datetime.now().strftime("%A")} (market closed)')
    sys.exit(0)

def main():
    # Get Alpaca keys from environment if set, otherwise default to None (IBKR/Demo mode)
    api_key = os.environ.get("ALPACA_API_KEY")
    secret_key = os.environ.get("ALPACA_SECRET_KEY")
    
    if api_key and secret_key:
        print("Alpaca keys loaded from environment")
    
    base_dir = os.path.dirname(__file__)
    trader = PaperTrader(base_dir=base_dir, alpaca_api_key=api_key, alpaca_secret=secret_key)
    
    mode = sys.argv[1] if len(sys.argv) > 1 else "--trade"
    
    if mode == "--report":
        report = trader.generate_trading_report()
        print(report)
    elif mode == "--status":
        portfolio = trader.position_manager.get_portfolio_state()
        ts = datetime.now().strftime('%Y-%m-%d %H:%M')
        print(f"Portfolio Status ({ts})")
        print(f"   Total Value:  ${portfolio.total_value:,.2f}")
        print(f"   Cash:         ${portfolio.available_cash:,.2f}")
        print(f"   Positions:    ${portfolio.total_positions_value:,.2f}")
        print(f"   P&L:          ${portfolio.total_pnl:,.2f}")
        print(f"   Open:         {portfolio.position_count}")
        strats = ', '.join(portfolio.strategies_active) if portfolio.strategies_active else 'none'
        print(f"   Strategies:   {strats}")
    elif mode == "--loop":
        import time
        trade_mode = "ALPACA PAPER" if api_key else "IBKR PAPER"
        print("=" * 60)
        print("🚀 Starting Continuous Paper Trading Loop (5-minute intervals)...")
        print(f"   Mode: {trade_mode}")
        print("💡 Keep this terminal window open. Press Ctrl+C to exit.")
        print("=" * 60)
        
        try:
            while True:
                ts = datetime.now().strftime('%Y-%m-%d %H:%M')
                print(f"\n⏰ [{ts}] Executing trading session...")
                try:
                    report = trader.run_trading_session()
                    print(report)
                except Exception as run_e:
                    print(f"❌ Session Execution Error: {run_e}")
                print("⏳ Sleeping 5 minutes before next scan...")
                time.sleep(300)
        except KeyboardInterrupt:
            print("\n👋 Continuous trading loop stopped by user.")
    else:
        ts = datetime.now().strftime('%Y-%m-%d %H:%M')
        trade_mode = "ALPACA PAPER" if api_key else "IBKR PAPER"
        print(f"Starting paper trading session ({ts})")
        print(f"   Mode: {trade_mode}")
        print()
        report = trader.run_trading_session()
        print(report)

if __name__ == "__main__":
    main()
