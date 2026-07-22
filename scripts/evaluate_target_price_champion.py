#!/usr/bin/env python3
"""
TradeSight Target Price Champion Evaluator & Tournament Runner

Runs an automated evaluation across candidate target price formulas.
Whichever candidate achieves the highest combined score (Hit Rate % + Speed)
is saved as the reigning champion in `data/target_price_champion.json`.

The stock scanner (`stock_opportunities.py`) automatically consumes
the reigning champion config to display target prices on the dashboard.

Usage:
    python3 scripts/evaluate_target_price_champion.py
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


# Candidate Target Models
def calc_target_price(df: pd.DataFrame, idx: int, direction: str, model_type: str, mult: float = 1.25) -> float:
    close = df['close'].iloc[:idx+1]
    curr_price = close.iloc[-1]
    
    if direction == 'neutral':
        return curr_price

    if 'atr' not in df.columns:
        df['atr'] = calculate_atr(df)
        
    atr_val = df['atr'].iloc[idx]
    if pd.isna(atr_val) or atr_val <= 0:
        atr_val = curr_price * 0.02

    if model_type == 'atr_scaled':
        offset = mult * atr_val
        return round(curr_price + offset, 2) if direction == 'bullish' else round(max(0.01, curr_price - offset), 2)
    
    elif model_type == 'bollinger_sma':
        sma_20 = float(close.rolling(20).mean().iloc[-1]) if len(close) >= 20 else curr_price
        std_20 = float(close.rolling(20).std().iloc[-1]) if len(close) >= 20 else 0
        upper_bb = sma_20 + 2 * std_20 if std_20 > 0 else curr_price * 1.08
        lower_bb = max(0.01, sma_20 - 2 * std_20) if std_20 > 0 else curr_price * 0.92
        return round(max(curr_price * 1.03, upper_bb if upper_bb > curr_price else curr_price * 1.08), 2) if direction == 'bullish' else round(min(curr_price * 0.97, sma_20 if sma_20 < curr_price else lower_bb), 2)
    
    elif model_type == 'confluence_blend':
        m1 = calc_target_price(df, idx, direction, 'bollinger_sma')
        m2 = calc_target_price(df, idx, direction, 'atr_scaled', mult=1.25)
        return round(m1 * 0.5 + m2 * 0.5, 2)
        
    return curr_price


from scanners.stock_opportunities import StockOpportunityScorer

def run_tournament():
    print("🏆 TradeSight Target Price Champion Tournament")
    print("=" * 70)
    
    symbols = ['AAPL', 'NVDA', 'MSFT', 'AMZN', 'GOOGL', 'META', 'XOM', 'JPM', 'BAC', 'ALAB', 'WMT', 'COST']
    client = IBKRClient(client_id=18)
    scorer = StockOpportunityScorer()
    horizon_days = 15
    
    candidates = [
        {'name': 'atr_1.0x', 'type': 'atr_scaled', 'mult': 1.0},
        {'name': 'atr_1.25x', 'type': 'atr_scaled', 'mult': 1.25},
        {'name': 'atr_1.5x', 'type': 'atr_scaled', 'mult': 1.50},
        {'name': 'atr_2.0x', 'type': 'atr_scaled', 'mult': 2.00},
        {'name': 'bollinger_sma', 'type': 'bollinger_sma', 'mult': 1.0},
        {'name': 'confluence_blend', 'type': 'confluence_blend', 'mult': 1.25}
    ]
    
    results = {}
    high_conf_results = {}
    for c in candidates:
        results[c['name']] = {'hits': 0, 'hits_80': 0, 'hits_90': 0, 'total': 0, 'days_sum': 0, 'days_list': [], 'config': c}
        high_conf_results[c['name']] = {'hits': 0, 'hits_80': 0, 'hits_90': 0, 'total': 0, 'days_sum': 0, 'days_list': [], 'config': c}
        
    print(f"Evaluating {len(candidates)} candidate models across {len(symbols)} symbols with StockOpportunityScorer...")
    
    for sym in symbols:
        try:
            df = client.get_historical_data(sym, days=450, timeframe='1Day')
            if df is None or len(df) < 220:
                continue
            
            df['atr'] = calculate_atr(df)
            
            for i in range(200, len(df) - horizon_days, 5):
                window_df = df.iloc[:i+1]
                opp_score = scorer.score_opportunity(window_df, sym)
                
                curr_price = window_df['close'].iloc[-1]
                direction = opp_score.direction if opp_score else ('bullish' if curr_price >= float(window_df['close'].rolling(20).mean().iloc[-1]) else 'bearish')
                is_high_conf = (opp_score.overall_score >= 50.0 and opp_score.confidence in ['high', 'medium']) if opp_score else False
                
                fwd_bars = df.iloc[i+1 : i+1+horizon_days]
                
                for c in candidates:
                    name = c['name']
                    target_px = calc_target_price(df, i, direction, c['type'], c['mult'])
                    
                    hit = False
                    hit_day = 0
                    
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
                        results[name]['days_list'].append(hit_day)
                        results[name]['hits_80'] += 1
                        results[name]['hits_90'] += 1
                    else:
                        if progress_ratio >= 0.80:
                            results[name]['hits_80'] += 1
                        if progress_ratio >= 0.90:
                            results[name]['hits_90'] += 1

                    if is_high_conf:
                        high_conf_results[name]['total'] += 1
                        if hit:
                            high_conf_results[name]['hits'] += 1
                            high_conf_results[name]['days_sum'] += hit_day
                            high_conf_results[name]['days_list'].append(hit_day)
                            high_conf_results[name]['hits_80'] += 1
                            high_conf_results[name]['hits_90'] += 1
                        else:
                            if progress_ratio >= 0.80:
                                high_conf_results[name]['hits_80'] += 1
                            if progress_ratio >= 0.90:
                                high_conf_results[name]['hits_90'] += 1
                        
        except Exception as e:
            print(f"Error evaluating {sym}: {e}")
            continue
            
    print("\n" + "=" * 105)
    print("📊 ALL SIGNALS (RAW BARS) (100%, 90%, 80% Thresholds & Speed)")
    print("=" * 105)
    print(f"{'Candidate Model':<18} | {'100% Target':<12} | {'>= 90% Target':<14} | {'>= 80% Target':<14} | {'Mean Days':<10} | {'Median Days':<12} | {'Score':<10}")
    print("-" * 105)
    
    for name, stats in results.items():
        total = stats['total']
        if total == 0:
            continue
        hit_100_pct = (stats['hits'] / total) * 100
        hit_90_pct  = (stats['hits_90'] / total) * 100
        hit_80_pct  = (stats['hits_80'] / total) * 100
        mean_days   = (stats['days_sum'] / stats['hits']) if stats['hits'] > 0 else 15.0
        median_days = float(np.median(stats['days_list'])) if stats['days_list'] else 15.0
        t_score = round(hit_100_pct + max(0, (15.0 - median_days)) * 1.5, 2)
        print(f"{name:<18} | {hit_100_pct:>9.1f}%   | {hit_90_pct:>10.1f}%   | {hit_80_pct:>10.1f}%   | {mean_days:>8.1f} d | {median_days:>10.1f} d | {t_score:>8.2f}")

    print("\n" + "=" * 105)
    print("🔥 HIGH CONFIDENCE OPPORTUNITIES ONLY (Score >= 50.0 & Medium/High Conf)")
    print("=" * 105)
    print(f"{'Candidate Model':<18} | {'100% Target':<12} | {'>= 90% Target':<14} | {'>= 80% Target':<14} | {'Mean Days':<10} | {'Median Days':<12} | {'Score':<10}")
    print("-" * 105)

    champion_name = None
    best_tournament_score = -1.0
    champion_stats = {}

    for name, stats in high_conf_results.items():
        total = stats['total']
        if total == 0:
            continue
        hit_100_pct = (stats['hits'] / total) * 100
        hit_90_pct  = (stats['hits_90'] / total) * 100
        hit_80_pct  = (stats['hits_80'] / total) * 100
        mean_days   = (stats['days_sum'] / stats['hits']) if stats['hits'] > 0 else 15.0
        median_days = float(np.median(stats['days_list'])) if stats['days_list'] else 15.0
        
        # Tournament score combines high 100% hit rate % and median speed
        speed_bonus = max(0, (15.0 - median_days)) * 1.5
        t_score = round(hit_100_pct + speed_bonus, 2)
        
        print(f"{name:<18} | {hit_100_pct:>9.1f}%   | {hit_90_pct:>10.1f}%   | {hit_80_pct:>10.1f}%   | {mean_days:>8.1f} d | {median_days:>10.1f} d | {t_score:>8.2f}")
        
        if t_score > best_tournament_score:
            best_tournament_score = t_score
            champion_name = name
            champion_stats = {
                'champion_model': name,
                'model_type': stats['config']['type'],
                'atr_multiplier': stats['config']['mult'],
                'high_conf_trades_evaluated': total,
                'hit_rate_100_pct': round(hit_100_pct, 1),
                'hit_rate_90_pct': round(hit_90_pct, 1),
                'hit_rate_80_pct': round(hit_80_pct, 1),
                'mean_days_to_hit': round(mean_days, 1),
                'median_days_to_hit': round(median_days, 1),
                'tournament_score': t_score,
                'last_evaluated': datetime.now().isoformat()
            }
            
    print("=" * 70)
    if champion_name:
        print(f"🏆 REIGNING CHAMPION: {champion_name} (Score: {best_tournament_score})")
        champion_file = os.path.join(os.path.dirname(__file__), "..", "data", "target_price_champion.json")
        with open(champion_file, 'w') as f:
            json.dump(champion_stats, f, indent=2)
        print(f"✅ Saved champion configuration to {champion_file}")
        print("💡 The dashboard stock scanner will automatically use this winning target price formula!")


def run_hybrid_backtest_experiment():
    print("\n" + "=" * 105)
    print("🧪 EXPERIMENTAL SIMULATION: Flat vs. Strict vs. Tiered Hybrid Strategy")
    print("=" * 105)
    
    symbols = ['AAPL', 'NVDA', 'MSFT', 'AMZN', 'GOOGL', 'META', 'XOM', 'JPM', 'BAC', 'ALAB', 'WMT', 'COST']
    client = IBKRClient(client_id=19)
    scorer = StockOpportunityScorer()
    horizon_days = 15

    strategies = {
        'Flat (All Signals)': {'total_pnl': 0.0, 'wins': 0, 'losses': 0, 'trades': 0, 'days_list': []},
        'Strict (High Conf Only)': {'total_pnl': 0.0, 'wins': 0, 'losses': 0, 'trades': 0, 'days_list': []},
        'Tiered Hybrid (Recommended)': {'total_pnl': 0.0, 'wins': 0, 'losses': 0, 'trades': 0, 'days_list': []}
    }

    print("Running portfolio simulation across 12 tickers...")

    for sym in symbols:
        try:
            df = client.get_historical_data(sym, days=450, timeframe='1Day')
            if df is None or len(df) < 220:
                continue
            
            df['atr'] = calculate_atr(df)
            
            for i in range(200, len(df) - horizon_days, 5):
                window_df = df.iloc[:i+1]
                opp_score = scorer.score_opportunity(window_df, sym)
                if not opp_score:
                    continue

                score = opp_score.overall_score
                curr_price = window_df['close'].iloc[-1]
                direction = opp_score.direction
                atr_val = df['atr'].iloc[i] if not pd.isna(df['atr'].iloc[i]) else curr_price * 0.02

                fwd_bars = df.iloc[i+1 : i+1+horizon_days]

                # 1. Flat Strategy (All setups, 1.0x ATR target, 2.0x ATR stop loss, 100% position size)
                flat_target = curr_price + atr_val if direction == 'bullish' else max(0.01, curr_price - atr_val)
                flat_stop = curr_price - (2.0 * atr_val) if direction == 'bullish' else curr_price + (2.0 * atr_val)
                
                flat_hit = False
                flat_stopped = False
                flat_day = 0
                for day_idx, (_, row) in enumerate(fwd_bars.iterrows(), 1):
                    if direction == 'bullish':
                        if row['high'] >= flat_target: flat_hit = True; flat_day = day_idx; break
                        if row['low'] <= flat_stop: flat_stopped = True; break
                    else:
                        if row['low'] <= flat_target: flat_hit = True; flat_day = day_idx; break
                        if row['high'] >= flat_stop: flat_stopped = True; break

                strategies['Flat (All Signals)']['trades'] += 1
                if flat_hit:
                    strategies['Flat (All Signals)']['wins'] += 1
                    strategies['Flat (All Signals)']['days_list'].append(flat_day)
                    strategies['Flat (All Signals)']['total_pnl'] += (atr_val / curr_price) * 100.0
                elif flat_stopped:
                    strategies['Flat (All Signals)']['losses'] += 1
                    strategies['Flat (All Signals)']['total_pnl'] -= (2.0 * atr_val / curr_price) * 100.0

                # 2. Strict Strategy (Score >= 50 only, 1.0x ATR target, 2.0x ATR stop loss, 100% position size)
                if score >= 50.0:
                    strategies['Strict (High Conf Only)']['trades'] += 1
                    if flat_hit:
                        strategies['Strict (High Conf Only)']['wins'] += 1
                        strategies['Strict (High Conf Only)']['days_list'].append(flat_day)
                        strategies['Strict (High Conf Only)']['total_pnl'] += (atr_val / curr_price) * 100.0
                    elif flat_stopped:
                        strategies['Strict (High Conf Only)']['losses'] += 1
                        strategies['Strict (High Conf Only)']['total_pnl'] -= (2.0 * atr_val / curr_price) * 100.0

                # 3. Tiered Hybrid Strategy
                # High Conf (Score >= 50): 100% pos size, 1.0x ATR target, 2.0x ATR stop
                # Med Conf (Score 40 - 49): 50% pos size, 0.75x ATR target, 2.0x ATR stop
                # Low Conf (Score < 40): 0% pos size (Skip)
                if score >= 50.0:
                    pos_size = 1.0
                    target_mult = 1.0
                elif 40.0 <= score < 50.0:
                    pos_size = 0.5
                    target_mult = 0.75
                else:
                    pos_size = 0.0
                    target_mult = 0.0

                if pos_size > 0:
                    hybrid_target = curr_price + (target_mult * atr_val) if direction == 'bullish' else max(0.01, curr_price - (target_mult * atr_val))
                    hybrid_stop = curr_price - (2.0 * atr_val) if direction == 'bullish' else curr_price + (2.0 * atr_val)
                    
                    h_hit = False
                    h_stopped = False
                    h_day = 0
                    for day_idx, (_, row) in enumerate(fwd_bars.iterrows(), 1):
                        if direction == 'bullish':
                            if row['high'] >= hybrid_target: h_hit = True; h_day = day_idx; break
                            if row['low'] <= hybrid_stop: h_stopped = True; break
                        else:
                            if row['low'] <= hybrid_target: h_hit = True; h_day = day_idx; break
                            if row['high'] >= hybrid_stop: h_stopped = True; break

                    strategies['Tiered Hybrid (Recommended)']['trades'] += 1
                    pnl_win = (target_mult * atr_val / curr_price) * 100.0 * pos_size
                    pnl_loss = (2.0 * atr_val / curr_price) * 100.0 * pos_size
                    if h_hit:
                        strategies['Tiered Hybrid (Recommended)']['wins'] += 1
                        strategies['Tiered Hybrid (Recommended)']['days_list'].append(h_day)
                        strategies['Tiered Hybrid (Recommended)']['total_pnl'] += pnl_win
                    elif h_stopped:
                        strategies['Tiered Hybrid (Recommended)']['losses'] += 1
                        strategies['Tiered Hybrid (Recommended)']['total_pnl'] -= pnl_loss

        except Exception as e:
            print(f"Error in hybrid experiment for {sym}: {e}")
            continue

    print("\n" + "=" * 125)
    print(f"{'Strategy Model':<28} | {'Target Hits / Setups':<22} | {'Hit %':<8} | {'Mean Days':<10} | {'Median Days':<12} | {'PnL %':<12} | {'Profit Factor':<12}")
    print("-" * 125)
    
    for s_name, res in strategies.items():
        tr = res['trades']
        if tr == 0: continue
        wins_cnt = res['wins']
        win_rate = (wins_cnt / tr) * 100.0
        pnl = res['total_pnl']
        pf = round((wins_cnt / max(1, res['losses'])), 2)
        mean_d = np.mean(res['days_list']) if res['days_list'] else 15.0
        median_d = np.median(res['days_list']) if res['days_list'] else 15.0
        hits_str = f"{wins_cnt} / {tr}"
        print(f"{s_name:<28} | {hits_str:<22} | {win_rate:>6.1f}% | {mean_d:>8.1f} d | {median_d:>10.1f} d | {pnl:>10.1f}% | {pf:>12.2f}")
    print("=" * 125)


if __name__ == "__main__":
    run_tournament()
    run_hybrid_backtest_experiment()

