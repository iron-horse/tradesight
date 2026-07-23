"""
TradeSight Alpaca Integration — DEPRECATED

This module has been superseded by ibkr_client.py (IBKR TWS via ib_async).
It is kept for backward compatibility only (test fixtures, legacy scripts).

Do NOT use AlpacaClient in new code. Use IBKRClient instead:
    from data.ibkr_client import IBKRClient
"""

import os
import sys
import requests
import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import time

# Ensure src/ is on path regardless of working directory
_src_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

import config
from indicators.technical_indicators import TechnicalIndicators


@dataclass
class StockQuote:
    """Real-time stock quote"""
    symbol: str
    timestamp: datetime
    bid: float
    ask: float
    last: float
    volume: int
    change: float
    change_pct: float


@dataclass
class PaperPosition:
    """Paper trading position"""
    symbol: str
    quantity: int
    side: str  # 'long' or 'short'
    avg_entry_price: float
    current_price: float
    unrealized_pnl: float
    market_value: float


class AlpacaClient:
    """
    Alpaca Markets API client for stock data and paper trading.
    
    Features:
    - Historical OHLCV data for any stock
    - Real-time quotes and market data
    - Paper trading (simulated trades with real prices)
    - S&P 500 universe scanning
    - Integration with TechnicalIndicators
    """
    
    # S&P 500 symbols (subset for demo - in production would load from file/API)
    SP500_SYMBOLS = [
        'AAPL', 'MSFT', 'AMZN', 'GOOGL', 'GOOG', 'TSLA', 'BRK.B', 'UNH', 'JNJ', 'XOM',
        'JPM', 'V', 'PG', 'CVX', 'HD', 'MA', 'BAC', 'ABBV', 'PFE', 'KO',
        'PEP', 'AVGO', 'COST', 'DIS', 'WMT', 'TMO', 'VZ', 'ADBE', 'MRK', 'NFLX',
        'ABT', 'CRM', 'ACN', 'NKE', 'TXN', 'LIN', 'MDT', 'UPS', 'AMD', 'PM',
        'BMY', 'QCOM', 'HON', 'RTX', 'LLY', 'ORCL', 'IBM', 'BA', 'GE', 'MMM'
    ]
    
    def __init__(self, api_key: str = None, secret_key: str = None, paper: bool = True):
        """
        Initialize Alpaca client.
        
        Args:
            api_key: Alpaca API key (if None, uses demo mode)
            secret_key: Alpaca secret key
            paper: If True, uses paper trading endpoints
        """
        self.api_key = api_key
        self.secret_key = secret_key
        self.paper = paper
        
        # API endpoints
        if self.paper:
            self.base_url = "https://paper-api.alpaca.markets"
            self.data_url = "https://data.alpaca.markets"
        else:
            self.base_url = "https://api.alpaca.markets"
            self.data_url = "https://data.alpaca.markets"
        
        self.headers = {}
        if self.api_key and self.secret_key:
            self.headers = {
                "APCA-API-KEY-ID": self.api_key,
                "APCA-API-SECRET-KEY": self.secret_key,
                "Content-Type": "application/json"
            }
        
        self.indicators = TechnicalIndicators()
        
        if self.headers:
            import socket
            for host in ['data.alpaca.markets', 'paper-api.alpaca.markets']:
                try:
                    socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
                except socket.gaierror as e:
                    import logging
                    logging.getLogger('AlpacaClient').warning(
                        f"DNS pre-warm failed for {host}: {e} — will retry on each request"
                    )
    
    def get_historical_data(self, 
                          symbol: str,
                          days: int = 100,
                          timeframe: str = '1Day') -> pd.DataFrame:
        """
        Get historical OHLCV data for a symbol.
        """
        if not self.headers:
            # Fall back to Yahoo Finance
            import yfinance as yf
            import warnings
            interval_map = {"1Day": "1d", "1Hour": "1h", "30Min": "30m", "15Min": "15m", "5Min": "5m", "1Min": "1m"}
            interval = interval_map.get(timeframe, "1d")
            period = '2y' if days >= 365 else ('1y' if days >= 30 else '30d')
            with warnings.catch_warnings():
                warnings.simplefilter('ignore')
                df = yf.download(symbol, period=period, interval=interval, progress=False, auto_adjust=True)
            if df is not None and len(df) >= 10:
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = [c[0].lower() for c in df.columns]
                else:
                    df.columns = [c.lower() for c in df.columns]
                req_cols = [c for c in ['open', 'high', 'low', 'close', 'volume'] if c in df.columns]
                if len(req_cols) == 5:
                    df = df[['open', 'high', 'low', 'close', 'volume']].dropna()
                    df.index = pd.to_datetime(df.index)
                    cutoff = df.index[-1] - pd.Timedelta(days=days)
                    df = df[df.index >= cutoff]
                    if len(df) >= 5:
                        df.attrs["data_source"] = "yfinance"
                        return df
            raise RuntimeError(f"Alpaca API keys not set and Yahoo Finance fetch failed for {symbol}")
        
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days)
        
        url = f"{self.data_url}/v2/stocks/{symbol}/bars"
        params = {
            'start': start_date.strftime('%Y-%m-%dT%H:%M:%SZ'),
            'end': end_date.strftime('%Y-%m-%dT%H:%M:%SZ'),
            'timeframe': timeframe,
            # For intraday bars, limit must account for bars-per-day (not days)
            'limit': min({
                '1Min': (days + 10) * 390,
                '5Min': (days + 10) * 78,
                '15Min': (days + 10) * 26,
                '30Min': (days + 10) * 13,
                '1Hour': (days + 10) * 7,
                '1Day': days + 50,
            }.get(timeframe, days + 50), 10000),  # Cap at Alpaca max
            'feed': 'iex'  # Explicit IEX feed; swap to 'sip' if on paid tier
        }
        
        # Retry with backoff for transient DNS/network failures
        max_retries = 3
        last_error = None
        for attempt in range(max_retries):
            try:
                response = requests.get(url, headers=self.headers, params=params, timeout=15)
                break  # Success
            except requests.exceptions.ConnectionError as e:
                last_error = e
                if attempt < max_retries - 1:
                    wait = (attempt + 1) * 5  # 5s, 10s backoff
                    import logging as _log
                    _log.getLogger('AlpacaClient').warning(
                        f"Connection error for {symbol} (attempt {attempt+1}/{max_retries}), retrying in {wait}s: {e}"
                    )
                    import time as _time
                    _time.sleep(wait)
                    continue
                else:
                    raise
        try:
            response  # Check we have a response
            
            if response.status_code == 200:
                data = response.json()
                bars = data.get('bars', [])
                
                if not bars:
                    raise ValueError(f"No data returned for {symbol}")
                
                df_data = []
                for bar in bars:
                    df_data.append({
                        'timestamp': pd.to_datetime(bar['t']),
                        'open': float(bar['o']),
                        'high': float(bar['h']),
                        'low': float(bar['l']),
                        'close': float(bar['c']),
                        'volume': int(bar['v'])
                    })
                
                df = pd.DataFrame(df_data)
                df.set_index('timestamp', inplace=True)
                df.columns = ['open', 'high', 'low', 'close', 'volume']
                
                # Return timeframe-appropriate number of rows (not just calendar days)
                bars_per_day = {'1Min':390,'5Min':78,'15Min':26,'30Min':13,'1Hour':7,'1Day':1}
                max_rows = days * bars_per_day.get(timeframe, 1)
                return df.tail(max_rows)
                
            else:
                raise RuntimeError(f"Alpaca API Error {response.status_code} for {symbol}")
                
        except Exception as e:
            # Attempt Yahoo Finance fallback before raising error
            try:
                import yfinance as yf
                import warnings
                interval_map = {"1Day": "1d", "1Hour": "1h", "30Min": "30m", "15Min": "15m", "5Min": "5m", "1Min": "1m"}
                interval = interval_map.get(timeframe, "1d")
                period = '2y' if days >= 365 else ('1y' if days >= 30 else '30d')
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore')
                    df = yf.download(symbol, period=period, interval=interval, progress=False, auto_adjust=True)
                if df is not None and len(df) >= 10:
                    if isinstance(df.columns, pd.MultiIndex):
                        df.columns = [c[0].lower() for c in df.columns]
                    else:
                        df.columns = [c.lower() for c in df.columns]
                    req_cols = [c for c in ['open', 'high', 'low', 'close', 'volume'] if c in df.columns]
                    if len(req_cols) == 5:
                        df = df[['open', 'high', 'low', 'close', 'volume']].dropna()
                        df.index = pd.to_datetime(df.index)
                        cutoff = df.index[-1] - pd.Timedelta(days=days)
                        df = df[df.index >= cutoff]
                        if len(df) >= 5:
                            df.attrs["data_source"] = "yfinance"
                            return df
            except Exception:
                pass
            raise RuntimeError(f"Error fetching historical data for {symbol}: {e}")
    
    def get_quote(self, symbol: str) -> Optional[StockQuote]:
        """Get real-time quote for a symbol"""
        if not self.headers:
            return self._get_yahoo_quote_fallback(symbol)
        
        url = f"{self.data_url}/v2/stocks/{symbol}/quotes/latest"
        try:
            response = requests.get(url, headers=self.headers, timeout=5)
            if response.status_code == 200:
                data = response.json()
                quote_data = data.get('quote', {})
                return StockQuote(
                    symbol=symbol,
                    timestamp=pd.to_datetime(quote_data.get('t')),
                    bid=float(quote_data.get('bp', 0)),
                    ask=float(quote_data.get('ap', 0)),
                    last=float(quote_data.get('ap', 0)),
                    volume=int(quote_data.get('as', 0)),
                    change=0.0,
                    change_pct=0.0
                )
            else:
                return self._get_yahoo_quote_fallback(symbol)
        except Exception:
            return self._get_yahoo_quote_fallback(symbol)

    def _get_yahoo_quote_fallback(self, symbol: str) -> Optional[StockQuote]:
        import urllib.request
        import json
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=5) as response:
                data = json.loads(response.read().decode())
                if not data.get('chart', {}).get('result'):
                    return None
                meta = data['chart']['result'][0]['meta']
                last_price = meta.get('regularMarketPrice')
                if not last_price or last_price <= 0:
                    return None
                prev_close = meta.get('chartPreviousClose', last_price)
                change = last_price - prev_close
                change_pct = (change / prev_close * 100) if prev_close else 0.0
                volume = meta.get('regularMarketVolume', 0)
                return StockQuote(
                    symbol=symbol,
                    timestamp=datetime.now(),
                    bid=last_price,
                    ask=last_price,
                    last=last_price,
                    volume=int(volume or 0),
                    change=round(change, 4),
                    change_pct=round(change_pct, 4)
                )
        except Exception:
            return None
    
    def scan_sp500(self, min_volume: int = 1000000) -> List[Dict]:
        opportunities = []
        print(f"📊 Scanning S&P 500 stocks...")
        for i, symbol in enumerate(self.SP500_SYMBOLS):
            try:
                print(f"  Analyzing {symbol} ({i+1}/{len(self.SP500_SYMBOLS)})...")
                data = self.get_historical_data(symbol, days=100)
                if len(data) < 50:
                    continue
                avg_volume = data['volume'].tail(20).mean()
                if avg_volume < min_volume:
                    continue
                indicators = self.indicators.calculate_all(data)
                confluence = indicators.get('confluence_score', 0)
                rsi = indicators['indicators'].get('rsi', 50)
                macd = indicators['indicators'].get('macd', 0)
                score = confluence * 100
                if rsi < 30:
                    score += 20
                elif rsi > 70:
                    score += 15
                if abs(macd) > 1:
                    score += 10
                opportunities.append({
                    'symbol': symbol,
                    'score': min(100, score),
                    'current_price': float(data['close'].iloc[-1]),
                    'volume': int(avg_volume),
                    'rsi': float(rsi),
                    'confluence': float(confluence),
                    'signals': self._extract_signals(indicators)
                })
            except Exception as e:
                print(f"  Error analyzing {symbol}: {e}")
                continue
        opportunities.sort(key=lambda x: x['score'], reverse=True)
        print(f"✅ Found {len(opportunities)} opportunities")
        return opportunities
    
    def place_paper_trade(self, 
                          symbol: str, 
                          quantity: int, 
                          side: str,
                          order_type: str = 'market') -> Dict:
        if not self.headers:
            return {'error': 'Alpaca API keys not configured.'}
        
        url = f"{self.base_url}/v2/orders"
        order_data = {
            'symbol': symbol,
            'qty': str(round(float(quantity), 6)),
            'side': side,
            'type': order_type,
            'time_in_force': 'day'
        }
        try:
            response = requests.post(url, headers=self.headers, json=order_data, timeout=10)
            if response.status_code in (200, 201):
                order = response.json()
                if 'status' not in order or order['status'] not in ('filled',):
                    order['status'] = 'accepted'
                order['fill_price'] = float(order.get('filled_avg_price') or order.get('limit_price') or 0) or None
                return order
            else:
                return {'error': response.text, 'status_code': response.status_code}
        except Exception as e:
            return {'error': str(e)}

    def close_full_position(self, symbol: str) -> dict:
        if not self.headers:
            return {'error': 'Alpaca API keys not configured.'}
        url = f"{self.base_url}/v2/positions/{symbol}"
        try:
            response = requests.delete(url, headers=self.headers, timeout=10)
            if response.status_code in (200, 204):
                data = response.json() if response.text else {}
                fill_price = float(data.get('filled_avg_price') or data.get('avg_fill_price') or 0) or None
                return {'status': 'closed', 'fill_price': fill_price, 'symbol': symbol}
            else:
                return {'error': response.text, 'status_code': response.status_code}
        except Exception as e:
            return {'error': str(e)}

    def get_account(self) -> dict:
        if not self.headers:
            return {"cash": "0.00", "buying_power": "0.00", "equity": "0.00",
                    "portfolio_value": "0.00", "long_market_value": "0", "status": "UNCONFIGURED"}
        url = f"{self.base_url}/v2/account"
        try:
            response = requests.get(url, headers=self.headers, timeout=10)
            if response.status_code == 200:
                return response.json()
            else:
                return {}
        except Exception as e:
            return {}

    def get_remote_positions(self) -> list:
        if not self.headers:
            return []
        url = f"{self.base_url}/v2/positions"
        try:
            response = requests.get(url, headers=self.headers, timeout=10)
            if response.status_code == 200:
                return response.json()
            else:
                return []
        except Exception as e:
            return []

    def get_paper_positions(self) -> List[PaperPosition]:
        if not self.headers:
            return []
        url = f"{self.base_url}/v2/positions"
        try:
            response = requests.get(url, headers=self.headers, timeout=10)
            if response.status_code == 200:
                positions_data = response.json()
                positions = []
                for pos in positions_data:
                    positions.append(PaperPosition(
                        symbol=pos['symbol'],
                        quantity=int(pos['qty']),
                        side='long' if float(pos['qty']) > 0 else 'short',
                        avg_entry_price=float(pos['avg_entry_price']),
                        current_price=float(pos['market_value']) / abs(float(pos['qty'])),
                        unrealized_pnl=float(pos['unrealized_pnl']),
                        market_value=float(pos['market_value'])
                    ))
                return positions
        except Exception:
            return []
        return []

    def _extract_signals(self, indicators: Dict) -> List[str]:
        signals = []
        ind_data = indicators.get('indicators', {})
        rsi = ind_data.get('rsi', 50)
        if rsi < 30:
            signals.append('RSI_OVERSOLD')
        elif rsi > 70:
            signals.append('RSI_OVERBOUGHT')
        confluence = indicators.get('confluence_score', 0)
        if confluence > 0.7:
            signals.append('HIGH_CONFLUENCE')
        return signals
