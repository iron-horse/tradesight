#!/usr/bin/env python3
"""
TradeSight Unified Dashboard
Multi-market intelligence platform showing Polymarket, Stocks, and Strategy Lab
"""

import os
import sys

# Auto-redirect to local venv python if running with system python
_project_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_venv_python = os.path.abspath(os.path.join(_project_dir, ".venv", "bin", "python"))
if os.path.exists(_venv_python) and ".venv" not in sys.executable:
    os.execv(_venv_python, [_venv_python] + sys.argv)

from flask import Flask, render_template, jsonify, request
import sqlite3
import json
from datetime import datetime, timedelta
import os
import sys
import pandas as pd
import threading

def sanitize_for_json(obj):
    """Recursively convert numpy types to native Python types."""
    import numpy as np
    if isinstance(obj, dict):
        return {k: sanitize_for_json(v) for k, v in obj.items()}
    elif isinstance(obj, (list, tuple)):
        return [sanitize_for_json(v) for v in obj]
    elif isinstance(obj, (np.integer,)):
        return int(obj)
    elif isinstance(obj, (np.floating,)):
        return float(obj)
    elif isinstance(obj, np.ndarray):
        return obj.tolist()
    elif isinstance(obj, np.bool_):
        return bool(obj)
    return obj


import numpy as np

class NumpySafeEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        return super().default(obj)


# Set IBKR_CLIENT_ID to 2 for the web dashboard to prevent conflicts with run_paper_trader.py (which uses client_id 1)
os.environ["IBKR_CLIENT_ID"] = os.environ.get("IBKR_CLIENT_ID", "2")

# Add project root to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from scanners.stock_scanner import StockScanner
from strategy_lab.tournament import StrategyTournament
from strategy_lab.ai_engine import create_test_data

app = Flask(__name__, static_folder='static', static_url_path='/static')
# AlertManager — for dashboard alerts tab
import sys as _sys
_sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
try:
    from alerts.alert_manager import AlertManager as _AlertManager
    from alerts.alert_types import AlertType as _AlertType
    from config import ALERTS_CONFIG as _ALERTS_CONFIG, save_alerts_config as _save_alerts_config, reload_alerts_config as _reload_alerts_config, DATA_DIR as _DATA_DIR
    _dashboard_alert_manager = _AlertManager(config=_ALERTS_CONFIG, data_dir=str(_DATA_DIR))
    _DASHBOARD_ALERTS_AVAILABLE = True
except Exception as _e:
    _dashboard_alert_manager = None
    _DASHBOARD_ALERTS_AVAILABLE = False

app.json_encoder = NumpySafeEncoder

def safe_jsonify(data):
    """Convert numpy types before jsonifying."""
    return json.loads(json.dumps(data, cls=NumpySafeEncoder))


# ---------------------------------------------------------------------------
# Singleton IBKRClient — one shared connection for the entire dashboard.
# clientId=2 is reserved for the dashboard (paper trader uses 1).
# Call get_ibkr_client() from any route instead of constructing a new instance.
# ---------------------------------------------------------------------------
_ibkr_client = None
_ibkr_client_lock = threading.Lock()

def get_ibkr_client():
    """Return the shared IBKRClient, creating or reconnecting it if needed."""
    global _ibkr_client
    with _ibkr_client_lock:
        if _ibkr_client is None:
            try:
                from data.ibkr_client import IBKRClient
                _ibkr_client = IBKRClient(client_id=2)
            except Exception as e:
                import logging
                logging.getLogger("dashboard").warning("IBKRClient init failed: %s", e)
                return None
        elif not _ibkr_client.demo_mode and not _ibkr_client._wrapper.is_connected:
            # Connection dropped — attempt a reconnect in place
            try:
                _ibkr_client._ensure_connected()
            except Exception:
                pass
    return _ibkr_client


def get_db_connection():
    """Get database connection"""
    db_path = os.path.join(os.path.dirname(__file__), '..', 'data', 'tradesight.db')
    return sqlite3.connect(db_path)

def get_polymarket_stats():
    """Get Polymarket statistics"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Total markets and recent scans
        cursor.execute('SELECT COUNT(*) FROM markets')
        total_markets = cursor.fetchone()[0]
        
        cursor.execute('SELECT MAX(last_updated) FROM markets')
        last_scan = cursor.fetchone()[0]
        
        # High volume markets (using volume instead of volume_24h)
        cursor.execute('SELECT COUNT(*) FROM markets WHERE volume > 10000')
        high_volume_markets = cursor.fetchone()[0]
        
        # Active markets
        cursor.execute('SELECT COUNT(*) FROM markets WHERE active = 1')
        active_markets = cursor.fetchone()[0]
        
        conn.close()
        
        return {
            'total_markets': total_markets,
            'last_scan': last_scan,
            'active_markets': active_markets,
            'high_volume_markets': high_volume_markets
        }
    except Exception as e:
        return {
            'total_markets': 0,
            'last_scan': None,
            'active_markets': 0,
            'high_volume_markets': 0,
            'error': str(e)
        }

def get_cluster_symbols() -> list:
    """Load all unique symbols from data/symbol_clusters.json.

    This is the single source of truth for which symbols the scanner
    examines. Adding a ticker to symbol_clusters.json automatically
    includes it in every dashboard scan — no code change needed.
    """
    import json as _json
    cluster_file = os.path.join(os.path.dirname(__file__), '..', 'data', 'symbol_clusters.json')
    try:
        with open(cluster_file) as f:
            clusters = _json.load(f)
        seen = set()
        symbols = []
        for cluster in clusters.values():
            for sym in cluster.get('symbols', []):
                if sym not in seen:
                    seen.add(sym)
                    symbols.append(sym)
        return symbols
    except Exception as e:
        import logging
        logging.getLogger('dashboard').warning('Could not load symbol_clusters.json: %s', e)
        # Fallback: scan the default SP500 subset
        return []

def get_stock_stats():
    """Get stock market statistics"""
    try:
        # Create scanner using the shared IBKR client singleton (avoids new connections)
        scanner = StockScanner(ibkr_client=get_ibkr_client())

        # Scan every symbol defined in symbol_clusters.json
        cluster_symbols = get_cluster_symbols()
        if cluster_symbols:
            scan_result = scanner.custom_scan(symbols=cluster_symbols, min_score=30.0)
        else:
            scan_result = scanner.quick_scan(limit=5)  # fallback
        
        return {
            'total_scanned': scan_result.total_scanned,
            'opportunities_found': scan_result.opportunities_found,
            'scan_duration': scan_result.scan_duration_seconds,
            'last_scan': scan_result.scan_time.isoformat(),
            'top_opportunity': scan_result.top_opportunities[0].symbol if scan_result.top_opportunities else None,
            'top_score': scan_result.top_opportunities[0].overall_score if scan_result.top_opportunities else 0,
            'expected_horizon': '1–3 Weeks (Swing Trading)'
        }
    except Exception as e:
        return {
            'total_scanned': 0,
            'opportunities_found': 0,
            'scan_duration': 0,
            'last_scan': None,
            'top_opportunity': None,
            'top_score': 0,
            'error': str(e)
        }

def get_strategy_lab_stats():
    """Get Strategy Lab statistics"""
    try:
        from strategy_lab.tournament import get_builtin_strategies
        
        # Create tournament
        tournament = StrategyTournament(
            initial_balance=10000.0,
            elimination_rate=0.3,
            min_survivors=2
        )
        
        # Register built-in strategies
        builtin_strategies = get_builtin_strategies()
        for name, strategy_func in builtin_strategies.items():
            tournament.register_strategy(name, strategy_func)
        
        # Create test data for tournament
        test_data = create_test_data(days=100)
        round_datasets = [
            ('Test Data', test_data)
        ]
        
        # Run tournament with test data
        results = tournament.run_tournament(round_datasets)
        
        return {
            'strategies_tested': results.total_strategies_entered,
            'winner': results.winner if results.winner != 'None' else 'None',
            'winner_score': results.winner_avg_score,
            'rounds_completed': results.total_rounds,
            'last_run': datetime.now().isoformat()
        }
    except Exception as e:
        return {
            'strategies_tested': 0,
            'winner': 'None',
            'winner_score': 0,
            'rounds_completed': 0,
            'last_run': None,
            'error': str(e)
        }

@app.route('/')
def dashboard():
    """Main dashboard with all market types"""
    return render_template('unified_dashboard.html')

@app.route('/api/polymarket/stats')
def polymarket_stats():
    """API endpoint for Polymarket statistics."""
    try:
        from config import POLYMARKET_ENABLED
    except ImportError:
        POLYMARKET_ENABLED = False
    if not POLYMARKET_ENABLED:
        return jsonify({'disabled': True, 'message': 'Prediction market module is currently disabled.'})
    return jsonify(sanitize_for_json(get_polymarket_stats()))

@app.route('/api/polymarket/opportunities')
def polymarket_opportunities():
    """API endpoint for Polymarket opportunities."""
    try:
        from config import POLYMARKET_ENABLED
    except ImportError:
        POLYMARKET_ENABLED = False
    if not POLYMARKET_ENABLED:
        return jsonify({'disabled': True, 'message': 'Prediction market module is currently disabled.'})
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT question, category, volume, price_yes, price_no, last_updated
            FROM markets
            WHERE volume > 1000
            ORDER BY volume DESC
            LIMIT 20
        ''')
        opportunities = []
        for row in cursor.fetchall():
            opportunities.append({
                'question': row[0],
                'category': row[1] or 'Unknown',
                'volume': row[2],
                'yes_price': row[3],
                'no_price': row[4],
                'last_updated': row[5]
            })
        conn.close()
        return jsonify(sanitize_for_json(opportunities))
    except Exception as e:
        return jsonify({'error': str(e)})

@app.route('/api/stocks/stats')
def stocks_stats():
    """API endpoint for stock statistics"""
    return jsonify(sanitize_for_json(get_stock_stats()))

@app.route('/api/stocks/opportunities')
def stocks_opportunities():
    """API endpoint for stock opportunities"""
    try:
        scanner = StockScanner(ibkr_client=get_ibkr_client())
        cluster_symbols = get_cluster_symbols()
        if cluster_symbols:
            scan_result = scanner.custom_scan(symbols=cluster_symbols, min_score=30.0)
        else:
            scan_result = scanner.quick_scan(limit=7)  # fallback
        
        opportunities = []
        for opp in scan_result.top_opportunities:
            opportunities.append({
                'symbol': opp.symbol,
                'overall_score': opp.overall_score,
                'volume_score': opp.volume_score,
                'volatility_score': opp.volatility_score,
                'technical_score': opp.technical_score,
                'momentum_score': opp.momentum_score,
                'trend_score': opp.trend_score,
                'confidence': opp.confidence,
                'direction': opp.direction,
                'current_price': getattr(opp, 'current_price', 0),
                'target_price': getattr(opp, 'target_price', 0),
                'target_pct': getattr(opp, 'target_pct', 0),
                'volume': getattr(opp, 'volume', 0),
                'market_cap': getattr(opp, 'market_cap', 0)
            })
        
        query = (request.args.get('q') or request.args.get('symbol') or '').strip().upper()
        if query:
            opportunities = [o for o in opportunities if query in o['symbol'].upper()]
        
        return jsonify(sanitize_for_json(opportunities))
        
    except Exception as e:
        return jsonify({'error': str(e)})

@app.route('/api/stocks/portfolio')
def stocks_portfolio():
    """API endpoint for current portfolio state and open/closed positions"""
    try:
        from trading.position_manager import PositionManager
        pm = PositionManager()
        
        # Get TWS Connection status
        client = get_ibkr_client()
        tws_connected = client is not None and not client.demo_mode
        
        # If connected to TWS, fetch the live account values and persist them to DB
        if tws_connected:
            try:
                # 1. Update cash balance
                account_data = client.get_account()
                if account_data:
                    cash = float(account_data.get("cash", 0))
                    pm.persist_balance_sync(cash)
                
                # 2. Update open positions' price and unrealized P&L from TWS portfolio data
                remote_positions = client.get_remote_positions()
                if remote_positions:
                    db_path = pm.data_dir / 'positions.db'
                    with sqlite3.connect(db_path) as conn:
                        for r_pos in remote_positions:
                            symbol = r_pos.get("symbol")
                            c_price = float(r_pos.get("current_price", 0))
                            u_pnl = float(r_pos.get("unrealized_pnl", 0))
                            if symbol and c_price > 0:
                                conn.execute('''
                                    UPDATE positions 
                                    SET current_price = ?, unrealized_pnl = ?, updated_at = CURRENT_TIMESTAMP 
                                    WHERE symbol = ? AND status = 'open'
                                ''', (c_price, u_pnl, symbol))
            except Exception as se:
                import logging
                logging.getLogger("dashboard").warning("Failed to sync broker details live in route: %s", se)


        # 1. Get Portfolio State (will use the newly persisted balance if successful)
        portfolio = pm.get_portfolio_state()
        portfolio_dict = {
            'total_value': portfolio.total_value,
            'available_cash': portfolio.available_cash,
            'total_positions_value': portfolio.total_positions_value,
            'unrealized_pnl': portfolio.unrealized_pnl,
            'realized_pnl': portfolio.realized_pnl,
            'total_pnl': portfolio.total_pnl,
            'position_count': portfolio.position_count,
            'strategies_active': portfolio.strategies_active,
            'balance_synced_at': portfolio.balance_synced_at
        }

        
        # 2. Get Open Positions & Trade History from DB
        db_path = pm.data_dir / 'positions.db'
        open_positions = []
        closed_history = []
        
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            # Fetch active positions
            rows = conn.execute('''
                SELECT symbol, strategy, side, quantity, entry_price, current_price, unrealized_pnl, entry_time 
                FROM positions 
                WHERE status = 'open'
                ORDER BY entry_time DESC
            ''').fetchall()
            for r in rows:
                open_positions.append(dict(r))
                
            # Fetch recent trade history (last 10 closed trades)
            history_rows = conn.execute('''
                SELECT entry_time, exit_time, symbol, side, entry_price, exit_price, realized_pnl, strategy 
                FROM positions 
                WHERE status = 'closed'
                ORDER BY exit_time DESC
                LIMIT 10
            ''').fetchall()
            for r in history_rows:
                closed_history.append(dict(r))
                
        # TWS status check is already handled at route entry

        
        # Calculate win rate if we have trades
        win_rate = 0.0
        with sqlite3.connect(db_path) as conn:
            total_closed = conn.execute("SELECT COUNT(*) FROM positions WHERE status='closed'").fetchone()[0]
            if total_closed > 0:
                wins = conn.execute("SELECT COUNT(*) FROM positions WHERE status='closed' AND realized_pnl > 0").fetchone()[0]
                win_rate = (wins / total_closed) * 100

        return jsonify(sanitize_for_json({
            'portfolio': portfolio_dict,
            'open_positions': open_positions,
            'history': closed_history,
            'tws_connected': tws_connected,
            'win_rate': win_rate
        }))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/strategy-lab/stats')
def strategy_lab_stats():
    """API endpoint for Strategy Lab statistics"""
    return jsonify(sanitize_for_json(get_strategy_lab_stats()))

@app.route('/api/strategy-lab/tournament')
def strategy_lab_tournament():
    """API endpoint for tournament results - runs a quick tournament"""
    try:
        from strategy_lab.tournament import get_builtin_strategies
        
        tournament = StrategyTournament(
            initial_balance=10000.0,
            elimination_rate=0.3,
            min_survivors=2
        )
        
        # Register built-in strategies
        builtin_strategies = get_builtin_strategies()
        for name, strategy_func in builtin_strategies.items():
            tournament.register_strategy(name, strategy_func)
        
        # Create test data for tournament
        test_data = create_test_data(days=100)
        round_datasets = [
            ('Test Data', test_data)
        ]
        
        results = tournament.run_tournament(round_datasets)
        
        # Convert results to JSON-serializable format
        participants = []
        for p in tournament.entries:
            participants.append({
                'name': p.name,
                'wins': p.wins,
                'losses': p.losses,
                'total_score': p.total_score,
                'avg_score': p.avg_score,
                'eliminated': p.eliminated,
                'rounds_survived': p.rounds_survived
            })
        
        winner_data = None
        if results.winner and results.winner != 'None':
            winner_entry = next((p for p in tournament.entries if p.name == results.winner), None)
            if winner_entry:
                winner_data = {
                    'name': winner_entry.name,
                    'avg_score': winner_entry.avg_score,
                    'wins': winner_entry.wins,
                    'total_score': winner_entry.total_score
                }
        
        return jsonify(sanitize_for_json({
            'participants': participants,
            'winner': winner_data,
            'rounds_completed': results.total_rounds,
            'eliminations': results.elimination_log
        }))
        
    except Exception as e:
        return jsonify({'error': str(e)})


# Strategy Lab Management - Thread-safe tournament state
current_tournament = None
tournament_in_progress = False
tournament_results_history = []
MAX_TOURNAMENT_HISTORY = 20
tournament_lock = threading.Lock()

@app.route('/strategy-lab')
def strategy_lab():
    """Strategy Lab interface for interactive tournament management"""
    return render_template('strategy_lab.html')

@app.route('/api/strategy-lab/start-tournament', methods=['POST'])
def start_tournament():
    """Start a new tournament with custom parameters"""
    global current_tournament, tournament_in_progress
    
    try:
        data = request.get_json() or {}
        
        # Tournament parameters
        initial_balance = data.get('initial_balance', 10000.0)
        elimination_rate = data.get('elimination_rate', 0.3)
        min_survivors = data.get('min_survivors', 2)
        max_rounds = data.get('max_rounds', 3)
        data_days = max(60, data.get('data_days', 100))
        
        with tournament_lock:
            if tournament_in_progress:
                return jsonify({'error': 'Tournament already in progress'}), 400
            tournament_in_progress = True
        
        # Create tournament
        tournament = StrategyTournament(
            initial_balance=initial_balance,
            elimination_rate=elimination_rate,
            min_survivors=min_survivors
        )
        
        # Register built-in strategies
        from strategy_lab.tournament import get_builtin_strategies
        builtin_strategies = get_builtin_strategies()
        for name, strategy_func in builtin_strategies.items():
            tournament.register_strategy(name, strategy_func)
        
        # Create test data
        test_data = create_test_data(days=data_days)
        round_datasets = [('Test Data', test_data)]
        
        # Run tournament (blocking but protected by lock)
        results = tournament.run_tournament(round_datasets)
        tournament_in_progress = False
        
        # Store results (TournamentResults dataclass)
        current_tournament = results
        # Trim history to prevent unbounded memory growth
        if len(tournament_results_history) >= MAX_TOURNAMENT_HISTORY:
            tournament_results_history.pop(0)
        tournament_results_history.append({
            'timestamp': datetime.now().isoformat(),
            'results': results,
            'tournament_ref': tournament,
            'parameters': {
                'initial_balance': initial_balance,
                'elimination_rate': elimination_rate,
                'min_survivors': min_survivors,
                'max_rounds': max_rounds,
                'data_days': data_days
            }
        })
        # Cap history to prevent unbounded memory growth
        while len(tournament_results_history) > 20:
            tournament_results_history.pop(0)
        
        # Convert results for JSON (results is TournamentResults dataclass)
        participants = []
        for p in tournament.entries:  # Get participants from tournament entries
            participants.append({
                'name': p.name,
                'wins': p.wins,
                'losses': p.losses,
                'total_score': p.total_score,
                'avg_score': p.avg_score,
                'eliminated': p.eliminated,
                'rounds_survived': p.rounds_survived
            })
        
        winner_data = None
        if results.winner != 'None':
            winner_entry = next((p for p in tournament.entries if p.name == results.winner), None)
            if winner_entry:
                winner_data = {
                    'name': winner_entry.name,
                    'avg_score': winner_entry.avg_score,
                    'wins': winner_entry.wins,
                    'total_score': winner_entry.total_score
                }
        
        return jsonify({
            'status': 'completed',
            'participants': participants,
            'winner': winner_data,
            'rounds_completed': results.total_rounds,
            'eliminations': results.elimination_log
        })
        
    except Exception as e:
        tournament_in_progress = False
        return jsonify({'error': str(e)}), 500

@app.route('/api/strategy-lab/status')
def tournament_status():
    """Get current tournament status"""
    global tournament_in_progress, current_tournament
    
    return jsonify({
        'in_progress': tournament_in_progress,
        'has_results': current_tournament is not None,
        'history_count': len(tournament_results_history)
    })

@app.route('/api/strategy-lab/results')
def tournament_results():
    """Get latest tournament results"""
    global current_tournament, tournament_results_history
    
    if not current_tournament:
        return jsonify({'error': 'No tournament results available'}), 404
    
    # current_tournament is a TournamentResults dataclass from last start-tournament call
    try:
        # Get participant data from the latest history entry
        participants = []
        if tournament_results_history:
            latest = tournament_results_history[-1]
            # Re-extract from stored results
            results = latest['results']
            if hasattr(results, 'top_3'):
                for entry in results.top_3:
                    participants.append(entry)
        
        winner_data = None
        if hasattr(current_tournament, 'winner') and current_tournament.winner != 'None':
            winner_data = {
                'name': current_tournament.winner,
                'avg_score': current_tournament.winner_avg_score,
                'wins': 0,
                'total_score': current_tournament.winner_avg_score
            }
        
        eliminations = []
        if hasattr(current_tournament, 'elimination_log'):
            eliminations = current_tournament.elimination_log
        
        return jsonify(sanitize_for_json({
            'participants': participants,
            'winner': winner_data,
            'rounds_completed': current_tournament.total_rounds if hasattr(current_tournament, 'total_rounds') else 0,
            'eliminations': eliminations
        }))
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/strategy-lab/export-winner')
def export_winner():
    """Export winning strategy details"""
    global current_tournament, tournament_results_history
    
    if not current_tournament or (hasattr(current_tournament, 'winner') and current_tournament.winner == 'None'):
        return jsonify({'error': 'No winning strategy available'}), 404
    
    winner_name = current_tournament.winner if hasattr(current_tournament, 'winner') else 'Unknown'
    winner_score = current_tournament.winner_avg_score if hasattr(current_tournament, 'winner_avg_score') else 0
    
    export_data = {
        'strategy_name': winner_name,
        'performance': {
            'avg_score': winner_score,
        },
        'export_timestamp': datetime.now().isoformat(),
        'tournament_info': {
            'rounds_completed': current_tournament.total_rounds if hasattr(current_tournament, 'total_rounds') else 0,
            'total_participants': current_tournament.total_strategies_entered if hasattr(current_tournament, 'total_strategies_entered') else 0
        }
    }
    
    return jsonify(sanitize_for_json(export_data))

@app.route('/api/strategy-lab/history')
def tournament_history():
    """Get tournament history"""
    global tournament_results_history
    
    # Return simplified history (last 10 tournaments)
    history = []
    for entry in tournament_results_history[-10:]:
        results = entry['results']
        winner_name = None
        if hasattr(results, 'winner') and results.winner != 'None':
            winner_name = results.winner
        
        history.append({
            'timestamp': entry['timestamp'],
            'winner': winner_name,
            'rounds_completed': results.total_rounds if hasattr(results, 'total_rounds') else 0,
            'participants_count': results.total_strategies_entered if hasattr(results, 'total_strategies_entered') else 0,
            'parameters': entry['parameters']
        })
    
    return jsonify(sanitize_for_json(history))

# ===========================================================================
# Alerts API routes (Phase 5.1)
# ===========================================================================

@app.route('/api/alerts/recent')
def alerts_recent():
    """Return recent alert history."""
    if not _DASHBOARD_ALERTS_AVAILABLE or not _dashboard_alert_manager:
        return jsonify({'alerts': [], 'error': 'Alerts module not available'})
    limit = min(int(request.args.get('limit', 50)), 200)
    alerts = _dashboard_alert_manager.get_recent_alerts(limit=limit)
    return jsonify({'alerts': alerts})


@app.route('/api/alerts/stats')
def alerts_stats():
    """Return alert summary statistics."""
    if not _DASHBOARD_ALERTS_AVAILABLE or not _dashboard_alert_manager:
        return jsonify({'total': 0, 'alerts_enabled': False, 'error': 'Alerts module not available'})
    return jsonify(_dashboard_alert_manager.get_alert_stats())


@app.route('/api/alerts/config', methods=['GET'])
def alerts_config_get():
    """Return current alerts configuration (no credentials)."""
    if not _DASHBOARD_ALERTS_AVAILABLE:
        return jsonify({'error': 'Alerts module not available'}), 500
    safe_config = {k: v for k, v in _ALERTS_CONFIG.items()
                   if k not in ('smtp_password', 'smtp_username')}
    safe_config['smtp_password'] = '***' if _ALERTS_CONFIG.get('smtp_password') else ''
    safe_config['smtp_username'] = _ALERTS_CONFIG.get('smtp_username', '')
    return jsonify(safe_config)


@app.route('/api/alerts/config', methods=['POST'])
def alerts_config_save():
    """Save alerts configuration."""
    if not _DASHBOARD_ALERTS_AVAILABLE:
        return jsonify({'error': 'Alerts module not available'}), 500
    data = request.get_json() or {}
    # Protect — don't overwrite password if placeholder sent
    if data.get('smtp_password') == '***':
        data.pop('smtp_password', None)
    ok = _save_alerts_config(data)
    if ok:
        _reload_alerts_config()
        # Refresh the in-process alert manager config
        if _dashboard_alert_manager:
            _dashboard_alert_manager.config.update(_ALERTS_CONFIG)
        return jsonify({'status': 'saved'})
    return jsonify({'error': 'Failed to save config'}), 500


@app.route('/api/alerts/test', methods=['POST'])
def alerts_test():
    """Send a test alert through all configured channels."""
    if not _DASHBOARD_ALERTS_AVAILABLE or not _dashboard_alert_manager:
        return jsonify({'error': 'Alerts module not available'}), 500
    try:
        fired = _dashboard_alert_manager.fire(
            _AlertType.SIGNAL_FIRED,
            symbol='TEST',
            action='buy',
            score=99.9,
            reason='Dashboard test alert',
        )
        return jsonify({'sent': fired})
    except Exception as e:
        return jsonify({'error': str(e)}), 500



@app.route('/api/emergency/close-all-positions', methods=['POST'])
def emergency_close_all_positions():
    """Close all open IBKR positions and sync local DB. Emergency use only."""
    try:
        from trading.position_manager import PositionManager
        from datetime import datetime
        import sqlite3

        client = get_ibkr_client()
        if client is None or client.demo_mode:
            return jsonify({'error': 'IBKR not connected (TWS not running or not logged in)'}), 500

        ibkr_positions = client.get_remote_positions()
        closed = []
        errors = []

        for pos in ibkr_positions:
            symbol = pos.get('symbol')
            qty = float(pos.get('qty', 0))
            avg_price = float(pos.get('avg_entry_price', 0))
            current_price = float(pos.get('current_price', 0) or avg_price)

            result = client.close_full_position(symbol)
            if 'error' not in result:
                fill_price = result.get('fill_price') or current_price
                closed.append({'symbol': symbol, 'qty': qty, 'fill_price': fill_price})
            else:
                errors.append({'symbol': symbol, 'error': result.get('error')})

        # Clear all open local DB positions
        pm = PositionManager()
        db_path = pm.data_dir / 'positions.db'
        with sqlite3.connect(db_path) as conn:
            open_rows = conn.execute(
                "SELECT COUNT(*) FROM positions WHERE status='open'"
            ).fetchone()[0]
            conn.execute(
                "UPDATE positions SET status='closed', exit_time=?, exit_price=0, realized_pnl=0, "
                "updated_at=CURRENT_TIMESTAMP WHERE status='open'",
                (datetime.now().isoformat(),)
            )
            conn.commit()

        return jsonify({
            'closed_alpaca': closed,
            'errors': errors,
            'db_positions_cleared': open_rows
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500



@app.route('/api/emergency/restore-positions', methods=['POST'])
def emergency_restore_positions():
    """Fetch open IBKR positions and restore them to local DB for SL/TP tracking."""
    try:
        from trading.position_manager import PositionManager
        from datetime import datetime
        import sqlite3

        client = get_ibkr_client()
        if client is None or client.demo_mode:
            return jsonify({'error': 'IBKR Client is running in demo mode or TWS is disconnected'}), 500

        ibkr_positions = client.get_remote_positions()
        pm = PositionManager()
        db_path = pm.data_dir / 'positions.db'
        restored = []

        with sqlite3.connect(db_path) as conn:
            for pos in ibkr_positions:
                symbol = pos.get('symbol')
                qty = float(pos.get('qty', 0))
                entry_price = float(pos.get('avg_entry_price', 0))
                side = pos.get('side', 'long')

                # Check if already in DB
                existing = conn.execute(
                    "SELECT id FROM positions WHERE symbol=? AND strategy=? AND status=?",
                    (symbol, 'RSI Mean Reversion', 'open')
                ).fetchone()

                if not existing:
                    conn.execute(
                        "INSERT INTO positions (symbol, strategy, side, quantity, entry_price, current_price, status, entry_time, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, 'open', ?, CURRENT_TIMESTAMP)",
                        (symbol, 'RSI Mean Reversion', side, qty, entry_price, entry_price, datetime.now().isoformat())
                    )
                    restored.append({'symbol': symbol, 'qty': qty, 'entry_price': entry_price})
                    conn.commit()

        return jsonify({'restored': restored, 'alpaca_positions': len(ibkr_positions)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=False, host="127.0.0.1", port=5001)
