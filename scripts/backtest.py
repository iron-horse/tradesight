#!/usr/bin/env python3
"""
TradeSight Single-Symbol Backtester
====================================
Quick backtest of the RSI Mean Reversion strategy (+ variants) on any symbol.
Uses the same BacktestEngine, data fetch, and scoring logic as the overnight
optimizer — but runs in seconds rather than 15–30 minutes.

Usage:
    python3 scripts/backtest.py TSLA
    python3 scripts/backtest.py TSLA --days 365
    python3 scripts/backtest.py TSLA --days 365 --strategy rsi
    python3 scripts/backtest.py TSLA --cluster       # use cluster-specific params
    python3 scripts/backtest.py TSLA --walk-forward  # 5-fold walk-forward
    python3 scripts/backtest.py TSLA --monte-carlo   # 100-sim Monte Carlo

Strategies available: rsi, ma, bollinger, all (default: all)
"""

import os
import sys
import json
import argparse
import warnings
from pathlib import Path
from datetime import datetime

# ── path setup ────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
_venv_python = ROOT / ".venv" / "bin" / "python3"
if _venv_python.exists() and ".venv" not in sys.executable:
    os.execv(str(_venv_python), [str(_venv_python)] + sys.argv)

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / 'src') not in sys.path:
    sys.path.insert(0, str(ROOT / 'src'))

import pandas as pd
import numpy as np

try:
    import yfinance as yf
    _YFINANCE_AVAILABLE = True
except ImportError:
    _YFINANCE_AVAILABLE = False

from strategy_lab.backtest import (
    BacktestEngine,
    rsi_mean_reversion,
    simple_ma_crossover,
)
from strategy_lab.backtester import MultiAssetBacktester
from strategy_lab.tournament import get_builtin_strategies

# ── helpers ───────────────────────────────────────────────────────────────────

def _fetch_data(symbol: str, days: int) -> pd.DataFrame:
    """Fetch OHLCV data: yfinance primary (no TWS needed for backtesting).

    The IBKRClient silently returns synthetic demo data when TWS is offline,
    which is useless for real backtesting. yfinance gives 2 years of real
    1H bars with no connection required.
    """

    # PRIMARY: yfinance — free, no API key, real historical data
    if _YFINANCE_AVAILABLE:
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            period = '2y' if days >= 365 else '1y'
            df = yf.download(symbol, period=period, interval='1h',
                             progress=False, auto_adjust=True)
        if df is not None and len(df) >= 50:
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = [c[0].lower() for c in df.columns]
            else:
                df.columns = [c.lower() for c in df.columns]
            df = df[['open', 'high', 'low', 'close', 'volume']].dropna()
            df.index = pd.to_datetime(df.index)
            # Slice to requested days
            cutoff = df.index[-1] - pd.Timedelta(days=days)
            df = df[df.index >= cutoff]
            print(f"  Data source : yfinance  ({len(df)} 1H bars, real historical)")
            return df

    # FALLBACK: IBKR disk cache (only if yfinance not installed)
    # Note: only returns real data if TWS was previously running and cached bars.
    try:
        from data.ibkr_client import IBKRClient
        import contextlib, io
        with contextlib.redirect_stderr(io.StringIO()):  # suppress TWS noise
            client = IBKRClient(client_id=15)
            df = client.get_historical_data(symbol, days=days, timeframe='1Hour')
        src = df.attrs.get('data_source', '') if df is not None else ''
        if df is not None and len(df) >= 50 and src not in ('demo_mode', 'synthetic', 'demo_fallback'):
            print(f"  Data source : {src}  ({len(df)} 1H bars)")
            return df
    except Exception:
        pass

    raise RuntimeError(
        f"Could not fetch real data for {symbol}.\n"
        "  Install yfinance:  pip install yfinance\n"
        "  Or start TWS and run again to use the IBKR cache."
    )


def _load_cluster_params(symbol: str) -> dict:
    """Return cluster default_params for symbol, or empty dict."""
    cluster_file = ROOT / 'data' / 'symbol_clusters.json'
    if not cluster_file.exists():
        return {}
    with open(cluster_file) as f:
        clusters = json.load(f)
    for cluster_name, cluster_data in clusters.items():
        if symbol in cluster_data.get('symbols', []):
            p = cluster_data.get('default_params', {})
            print(f"  Cluster     : {cluster_name}  params={p}")
            return p
    print(f"  Cluster     : none found for {symbol} — using defaults")
    return {}


def _make_rsi_from_params(params: dict):
    """Build an RSI variant from cluster params dict."""
    from scripts.overnight_strategy_evolution import ParameterTuner
    tuner = ParameterTuner.__new__(ParameterTuner)
    tuner.training_data = None
    tuner.backtest_engine = None
    tuner.results = []
    return tuner.create_rsi_variant(
        oversold=params.get('oversold', 30),
        overbought=params.get('overbought', 70),
        size=params.get('position_size', 0.25),
        stop_loss_pct=params.get('stop_loss_pct', 0.05),
        take_profit_pct=params.get('take_profit_pct', 0.10),
        max_holding_bars=params.get('max_holding_bars', 0),
        use_atr=True,
    )


def _print_metrics(name: str, metrics: dict):
    pnl   = metrics.get('total_pnl_pct', 0)
    sharpe = metrics.get('sharpe_ratio', 0)
    wr    = metrics.get('win_rate', 0)
    trades = metrics.get('total_trades', 0)
    mdd   = metrics.get('max_drawdown_pct', 0)
    sign  = '+' if pnl >= 0 else ''
    print(f"\n  ── {name}")
    print(f"     PnL      : {sign}{pnl:.2f}%")
    print(f"     Sharpe   : {sharpe:.4f}")
    print(f"     Win Rate : {wr:.1f}%")
    print(f"     Trades   : {trades}")
    print(f"     Max DD   : {mdd:.2f}%")


def _print_walk_forward(results):
    print(f"\n  ── Walk-Forward ({len(results)} folds)")
    profitable = 0
    for r in results:
        flag = '✅' if r.test_score > 0 else '❌'
        print(f"     Fold {r.fold}: train={r.train_score:+.2f}%  "
              f"test={r.test_score:+.2f}%  "
              f"degradation={r.degradation_pct:.0f}%  {flag}")
        if r.test_score > 0:
            profitable += 1
    avg_deg = sum(r.degradation_pct for r in results) / len(results)
    print(f"     Profitable folds: {profitable}/{len(results)}  "
          f"avg degradation: {avg_deg:.0f}%")
    if avg_deg > 50:
        print("     ⚠️  High degradation — possible overfitting")
    elif profitable / len(results) >= 0.6:
        print("     ✅ Strategy is stable across folds")


def _print_monte_carlo(mc):
    print(f"\n  ── Monte Carlo ({mc.num_simulations} simulations)")
    print(f"     Mean PnL : {mc.mean_pnl:+.2f}%  (median {mc.median_pnl:+.2f}%)")
    print(f"     5th pct  : {mc.percentile_5:+.2f}%  (worst-case scenario)")
    print(f"     95th pct : {mc.percentile_95:+.2f}%  (best-case scenario)")
    print(f"     Profitable: {mc.probability_profitable:.0f}% of simulations")
    print(f"     Worst DD  : {mc.max_drawdown_worst:.1f}%")
    if mc.probability_profitable >= 60:
        print("     ✅ Robust to trade-order randomization")
    else:
        print("     ⚠️  Sensitive to trade ordering — be cautious")


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='TradeSight single-symbol backtest',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument('symbol', type=str, help='Stock symbol, e.g. TSLA')
    parser.add_argument('--days', type=int, default=365,
                        help='Days of 1H history to fetch (default: 365)')
    parser.add_argument('--strategy', choices=['rsi', 'ma', 'bollinger', 'all'],
                        default='all', help='Strategy to test (default: all)')
    parser.add_argument('--cluster', action='store_true',
                        help='Use cluster-tuned params for RSI (from symbol_clusters.json)')
    parser.add_argument('--walk-forward', action='store_true',
                        help='Run 5-fold walk-forward validation')
    parser.add_argument('--monte-carlo', action='store_true',
                        help='Run 100-simulation Monte Carlo')
    args = parser.parse_args()

    symbol = args.symbol.upper()

    print()
    print('=' * 60)
    print(f'  TradeSight Backtest — {symbol}')
    print(f'  {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    print('=' * 60)
    print(f'  Days      : {args.days}')
    print(f'  Strategy  : {args.strategy}')

    # ── fetch data ────────────────────────────────────────────────────────────
    try:
        data = _fetch_data(symbol, args.days)
    except RuntimeError as e:
        print(f'\n❌ {e}')
        sys.exit(1)

    if len(data) < 100:
        print(f'\n❌ Only {len(data)} bars — need at least 100. Try --days 730.')
        sys.exit(1)

    # Walk-forward split: optimizer trains on first 70%
    split = int(len(data) * 0.70)
    train_data = data.iloc[:split].copy()
    test_data  = data.iloc[split:].copy()

    print(f'  Bars      : {len(data)} total  '
          f'(train={len(train_data)}, OOS test={len(test_data)})')

    engine = BacktestEngine(initial_balance=500.0, slippage_pct=0.0005)

    # ── pick strategies ───────────────────────────────────────────────────────
    builtin = get_builtin_strategies()   # dict[name → func]
    strategies_to_run: dict = {}

    if args.strategy in ('rsi', 'all'):
        if args.cluster:
            params = _load_cluster_params(symbol)
            if params:
                strategies_to_run['RSI (cluster params)'] = _make_rsi_from_params(params)
            else:
                strategies_to_run['RSI Mean Reversion'] = rsi_mean_reversion
        else:
            strategies_to_run['RSI Mean Reversion'] = rsi_mean_reversion

    if args.strategy in ('ma', 'all'):
        strategies_to_run['MA Crossover'] = simple_ma_crossover

    if args.strategy in ('bollinger', 'all'):
        bb = builtin.get('Bollinger Bounce') or builtin.get('Bollinger_Bounce')
        if bb:
            strategies_to_run['Bollinger Bounce'] = bb

    # ── run backtests ─────────────────────────────────────────────────────────
    print('\n📊 Backtest Results (train period — 70% of data)')
    print('-' * 60)
    for name, func in strategies_to_run.items():
        try:
            res = engine.run_backtest(train_data, func, name)
            _print_metrics(name, res['metrics'])
        except Exception as e:
            print(f'\n  ── {name}  ❌ {e}')

    print('\n📊 Out-of-Sample Results (test period — last 30%, unseen)')
    print('-' * 60)
    best_strategy_name = None
    best_oos_pnl = float('-inf')
    for name, func in strategies_to_run.items():
        try:
            res = engine.run_backtest(test_data, func, f'OOS_{name}')
            _print_metrics(name + ' [OOS]', res['metrics'])
            pnl = res['metrics'].get('total_pnl_pct', float('-inf'))
            if pnl > best_oos_pnl:
                best_oos_pnl = pnl
                best_strategy_name = name
        except Exception as e:
            print(f'\n  ── {name} [OOS]  ❌ {e}')

    # ── optional: walk-forward ────────────────────────────────────────────────
    if args.walk_forward and strategies_to_run:
        print('\n📊 Walk-Forward Validation (primary strategy)')
        print('-' * 60)
        backtester = MultiAssetBacktester(initial_balance=500.0)
        primary_func = list(strategies_to_run.values())[0]
        try:
            wf = backtester.walk_forward_validation(primary_func, data, n_folds=5)
            _print_walk_forward(wf)
        except Exception as e:
            print(f'  ❌ Walk-forward failed: {e}')

    # ── optional: monte carlo ─────────────────────────────────────────────────
    if args.monte_carlo and strategies_to_run:
        print('\n📊 Monte Carlo Simulation (primary strategy, 100 sims)')
        print('-' * 60)
        backtester = MultiAssetBacktester(initial_balance=500.0)
        primary_func = list(strategies_to_run.values())[0]
        try:
            mc = backtester.monte_carlo_simulation(primary_func, data, n_simulations=100)
            _print_monte_carlo(mc)
        except Exception as e:
            print(f'  ❌ Monte Carlo failed: {e}')

    # ── summary ───────────────────────────────────────────────────────────────
    print()
    print('=' * 60)
    if best_strategy_name:
        sign = '+' if best_oos_pnl >= 0 else ''
        print(f'  Best OOS strategy : {best_strategy_name}')
        print(f'  Best OOS PnL      : {sign}{best_oos_pnl:.2f}%')
    print(f'  Symbol            : {symbol}')
    print(f'  Period            : {args.days} days of 1H bars')
    print('=' * 60)
    print()


if __name__ == '__main__':
    main()
