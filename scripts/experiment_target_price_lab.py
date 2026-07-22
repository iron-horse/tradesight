#!/usr/bin/env python3
"""
TradeSight Target Price Experimentation Lab

Compares 3 different Target Price calculation models against historical price action:
1. Baseline Model: Current Bollinger Band + 20D SMA model
2. ATR Volatility Model: 2.0x Average True Range projection
3. Multi-Factor Confluence Model: Blends ATR, Sector Cluster Benchmarks, & Technical Levels

Evaluates performance over a 15-day (1-3 week) forward holding horizon:
- Target Hit Rate (%)
- Average Days to Target
- Average Forward Return (%)
"""

import sys
import os
import json
import pandas as pd
import numpy as np
from datetime import datetime

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from data.ibkr_client import IBKRClient


def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Calculate Average True Range (ATR)"""
    high = df['high']
    low = df['low']
    close = df['close']
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(period).mean()


# Target Price Models
def model_1_baseline(df: pd.DataFrame, idx: int, direction: str) -> float:
    """Current Model: Bollinger Band + 20D SMA"""
    close = df['close'].iloc[:idx+1]
    current_price = close.iloc[-1]
    
    if len(close) < 20:
        return current_price
    
    sma_20 = float(close.rolling(20).mean().iloc[-1])
    std_20 = float(close.rolling(20).std().iloc[-1])
    upper_bb = sma_20 + 2 * std_20 if std_20 > 0 else current_price * 1.08
    lower_bb = max(0.01, sma_20 - 2 * std_20) if std_20 > 0 else current_price * 0.92

    if direction == 'bullish':
        return round(max(current_price * 1.03, upper_bb if upper_bb > current_price else current_price * 1.08), 2)
    elif direction == 'bearish':
        return round(min(current_price * 0.97, sma_20 if sma_20 < current_price else lower_bb), 2)
    else:
        return round(current_price, 2)


def model_2_atr(df: pd.DataFrame, idx: int, direction: str, atr_mult: float = 2.0) -> float:
    """Model 2: Dynamic ATR-based Price Target"""
    close = df['close'].iloc[:idx+1]
    current_price = close.iloc[-1]
    
    if 'atr' not in df.columns:
        atr_series = calculate_atr(df)
        df['atr'] = atr_series
    
    atr_val = df['atr'].iloc[idx]
    if pd.isna(atr_val) or atr_val == 0:
        atr_val = current_price * 0.02
    
    if direction == 'bullish':
        return round(current_price + (atr_mult * atr_val), 2)
    elif direction == 'bearish':
        return round(max(0.01, current_price - (atr_mult * atr_val)), 2)
    else:
        return round(current_price, 2)


def model_4_high_conviction_atr(df: pd.DataFrame, idx: int, direction: str, atr_mult: float = 1.25) -> float:
    """Model 4: Realistic Target Scaling (1.25x ATR / First Resistance) for High Conviction Signals"""
    close = df['close'].iloc[:idx+1]
    current_price = close.iloc[-1]
    
    if 'atr' not in df.columns:
        df['atr'] = calculate_atr(df)
    
    atr_val = df['atr'].iloc[idx]
    if pd.isna(atr_val) or atr_val == 0:
        atr_val = current_price * 0.015
    
    if direction == 'bullish':
        return round(current_price + (atr_mult * atr_val), 2)
    elif direction == 'bearish':
        return round(max(0.01, current_price - (atr_mult * atr_val)), 2)
    else:
        return round(current_price, 2)


def run_experiment():
    print("🧪 TradeSight Target Price Experimentation Lab")
    print("=" * 70)
    
    # Load symbol clusters for sector benchmarks
    cluster_path = os.path.join(os.path.dirname(__file__), "..", "data", "symbol_clusters.json")
    symbol_tp_map = {}
    if os.path.exists(cluster_path):
        with open(cluster_path) as f:
            clusters = json.load(f)
            for c_name, c_data in clusters.items():
                tp_pct = c_data.get('take_profit_pct', 0.08)
                for sym in c_data.get('symbols', []):
                    symbol_tp_map[sym] = tp_pct
    
    symbols = ['AAPL', 'NVDA', 'MSFT', 'AMZN', 'GOOGL', 'META', 'XOM', 'JPM', 'BAC', 'ALAB', 'WMT', 'COST']
    client = IBKRClient(client_id=17)
    
    horizon_days = 15  # 1-3 week holding horizon
    
    results = {
        'Baseline (BB + 20D SMA)': {'hits': 0, 'misses': 0, 'total': 0, 'days_sum': 0, 'progress_ratio_sum': 0.0, 'near_misses_80': 0, 'near_misses_90': 0},
        'ATR Volatility Model (2x ATR)': {'hits': 0, 'misses': 0, 'total': 0, 'days_sum': 0, 'progress_ratio_sum': 0.0, 'near_misses_80': 0, 'near_misses_90': 0},
        'Realistic 1.25x ATR Target': {'hits': 0, 'misses': 0, 'total': 0, 'days_sum': 0, 'progress_ratio_sum': 0.0, 'near_misses_80': 0, 'near_misses_90': 0}
    }
    
    print(f"Analyzing {len(symbols)} symbols across historical daily bars (15-day forward horizon)...")
    
    for sym in symbols:
        try:
            df = client.get_historical_data(sym, days=300, timeframe='1Day')
            if df is None or len(df) < 100:
                continue
            
            df['atr'] = calculate_atr(df)
            sector_tp = symbol_tp_map.get(sym, 0.08)
            
            # Sample every 5th day to generate simulated signals
            for i in range(50, len(df) - horizon_days, 5):
                close = df['close'].iloc[:i+1]
                sma_20 = float(close.rolling(20).mean().iloc[-1])
                curr_price = close.iloc[-1]
                
                direction = 'bullish' if curr_price >= sma_20 else 'bearish'
                
                # Compute targets for models
                t1 = model_1_baseline(df, i, direction)
                t2 = model_2_atr(df, i, direction, atr_mult=2.0)
                t4 = model_4_high_conviction_atr(df, i, direction, atr_mult=1.25)
                
                # Check forward 15 bars
                fwd_bars = df.iloc[i+1 : i+1+horizon_days]
                
                models = [
                    ('Baseline (BB + 20D SMA)', t1),
                    ('ATR Volatility Model (2x ATR)', t2),
                    ('Realistic 1.25x ATR Target', t4)
                ]
                
                for name, target_px in models:
                    hit = False
                    hit_day = 0
                    best_fwd_price = curr_price
                    
                    if direction == 'bullish':
                        best_fwd_price = fwd_bars['high'].max()
                        target_dist = target_px - curr_price
                        max_achieved_dist = best_fwd_price - curr_price
                    else:
                        best_fwd_price = fwd_bars['low'].min()
                        target_dist = curr_price - target_px
                        max_achieved_dist = curr_price - best_fwd_price
                    
                    progress_ratio = max_achieved_dist / target_dist if target_dist > 0 else 0
                    
                    for day_idx, (_, row) in enumerate(fwd_bars.iterrows(), 1):
                        if direction == 'bullish' and row['high'] >= target_px:
                            hit = True
                            hit_day = day_idx
                            break
                        elif direction == 'bearish' and row['low'] <= target_px:
                            hit = True
                            hit_day = day_idx
                            break
                    
                    results[name]['total'] += 1
                    if hit:
                        results[name]['hits'] += 1
                        results[name]['days_sum'] += hit_day
                    else:
                        results[name]['misses'] += 1
                        results[name]['progress_ratio_sum'] += max(0, progress_ratio)
                        if progress_ratio >= 0.80:
                            results[name]['near_misses_80'] += 1
                        if progress_ratio >= 0.90:
                            results[name]['near_misses_90'] += 1
                        
        except Exception as e:
            print(f"Error processing {sym}: {e}")
            continue
            
    print("\n" + "=" * 70)
    print("📊 TARGET PRICE EXPERIMENTATION RESULTS (15-Day Forward Horizon)")
    print("=" * 70)
    print(f"{'Model Name':<32} | {'Hit Rate (%)':<12} | {'Avg Days':<10} | {'Avg Miss Progress %':<20} | {'Reached >=80% Target':<20}")
    print("-" * 105)
    
    for name, stats in results.items():
        total = stats['total']
        if total == 0:
            continue
        hit_rate = (stats['hits'] / total) * 100
        avg_days = (stats['days_sum'] / stats['hits']) if stats['hits'] > 0 else 0
        miss_count = stats['misses']
        avg_progress = (stats['progress_ratio_sum'] / miss_count * 100) if miss_count > 0 else 0
        near_80_pct = ((stats['hits'] + stats['near_misses_80']) / total) * 100
        
        print(f"{name:<32} | {hit_rate:>10.1f}% | {avg_days:>8.1f} d | {avg_progress:>18.1f}% | {near_80_pct:>18.1f}% ({stats['hits'] + stats['near_misses_80']}/{total})")
        
    print("=" * 105)
    print("💡 NEAR-MISS & TARGET ACCURACY INSIGHTS:")
    print("   - Hit Rate (%): Trades that hit 100% of the target price within 15 days.")
    print("   - Avg Miss Progress (%): Average % of target distance covered by non-hitting trades before reversing.")
    print("   - Reached >=80% Target: Combine 100% Hits + Near-Misses (trades that got within 80%-99% of target).")


if __name__ == "__main__":
    run_experiment()
