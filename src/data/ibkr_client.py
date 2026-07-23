"""
TradeSight IBKR TWS Integration

Drop-in replacement for AlpacaClient using Interactive Brokers TWS
via the ib_async library (maintained fork of ib_insync).

Public interface is identical to AlpacaClient — all existing callers
work without modification.

Prerequisites:
  - TWS (Trader Workstation) running and logged into a Paper Trading account
  - TWS API enabled: Global Configuration → API → Settings
      Enable ActiveX and Socket Clients
      Socket port: 7497
      Allow connections from localhost only

Connection defaults:
  Host: 127.0.0.1  (override via IBKR_HOST env var)
  Port: 7497        (override via IBKR_PORT env var — use 4002 for IB Gateway)
  ClientID: 1       (override via IBKR_CLIENT_ID env var)
"""

import os
import sys
import math
import time
import logging
import threading
from typing import Dict, List, Optional
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

_src_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

try:
    from indicators.technical_indicators import TechnicalIndicators
    _INDICATORS_AVAILABLE = True
except ImportError:
    _INDICATORS_AVAILABLE = False
    TechnicalIndicators = None

try:
    from data.data_cache import HistoricalDataCache as _HistoricalDataCache
    _CACHE_AVAILABLE = True
except ImportError:
    try:
        from .data_cache import HistoricalDataCache as _HistoricalDataCache
        _CACHE_AVAILABLE = True
    except ImportError:
        _CACHE_AVAILABLE = False
        _HistoricalDataCache = None

logger = logging.getLogger("IBKRClient")

# ---------------------------------------------------------------------------
# Shared dataclasses (identical to alpaca_client.py so imports still work)
# ---------------------------------------------------------------------------

@dataclass
class StockQuote:
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
    symbol: str
    quantity: int
    side: str
    avg_entry_price: float
    current_price: float
    unrealized_pnl: float
    market_value: float


# ---------------------------------------------------------------------------
# Timeframe mapping: Alpaca notation -> IBKR bar size string
# ---------------------------------------------------------------------------
_TIMEFRAME_MAP = {
    "1Min":  "1 min",
    "5Min":  "5 mins",
    "15Min": "15 mins",
    "30Min": "30 mins",
    "1Hour": "1 hour",
    "1Day":  "1 day",
}

_BARS_PER_DAY = {
    "1 min": 390, "5 mins": 78, "15 mins": 26,
    "30 mins": 13, "1 hour": 7, "1 day": 1,
}


def _duration_str(days: int, bar_size: str) -> str:
    if bar_size == "1 day":
        if days >= 365:
            years = math.ceil(days / 365)
            return f"{years} Y"
        return f"{max(days + 10, 10)} D"
    
    calendar_days = math.ceil(days * 7 / 5) + 5
    if calendar_days >= 365:
        return "1 Y"  # Cap intraday at 1 year, formatted in years to pass validation
    return f"{calendar_days} D"


# ---------------------------------------------------------------------------
# Sync wrapper around ib_async
# ---------------------------------------------------------------------------

class _IBKRSyncWrapper:
    """
    Thread-safe synchronous wrapper around ib_async's IB object.
    Runs the asyncio event loop in a dedicated daemon thread so all
    callers can stay purely synchronous.
    """

    def __init__(self, host: str, port: int, client_id: int):
        self.host = host
        self.port = port
        self.client_id = client_id
        self._ib = None
        self._loop = None
        self._thread = None
        self._connected = False

    def _start_loop(self):
        import asyncio
        import nest_asyncio
        nest_asyncio.apply()
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def connect(self) -> bool:
        if self._connected:
            return True
        try:
            from ib_async import IB
            import asyncio

            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._start_loop, daemon=True, name="ibkr-event-loop"
                )
                self._thread.start()
                time.sleep(0.3)

            self._ib = IB()
            future = asyncio.run_coroutine_threadsafe(
                self._ib.connectAsync(self.host, self.port, clientId=self.client_id),
                self._loop,
            )
            future.result(timeout=10)
            self._connected = True
            logger.info(
                "[IBKRClient] Connected to TWS at %s:%d clientId=%d",
                self.host, self.port, self.client_id,
            )

            # Wait for TWS handshake to complete and managedAccounts to populate
            # (managedAccounts() is empty immediately after connectAsync returns)
            async def _subscribe_account():
                # Poll until managedAccounts is populated (up to 3 seconds)
                for _ in range(15):
                    accounts = self._ib.managedAccounts()
                    if accounts:
                        break
                    await asyncio.sleep(0.2)
                else:
                    logger.warning("[IBKRClient] managedAccounts() still empty after 3s — account sync may fail")
                    accounts = []

                account = accounts[0] if accounts else ""
                logger.info("[IBKRClient] Subscribing to account updates for: %s", account or "<empty>")

                # Fire-and-forget subscriptions — these start streaming account data into the cache
                self._ib.reqAccountUpdates(account)   # populates accountValues()
                self._ib.reqAccountSummary()          # populates accountSummary()

                # Wait for the first batch of data to arrive from TWS
                await asyncio.sleep(2.0)
                logger.info(
                    "[IBKRClient] Account subscription ready — %d values cached",
                    len(self._ib.accountValues())
                )

            try:
                asyncio.run_coroutine_threadsafe(
                    _subscribe_account(), self._loop
                ).result(timeout=10)
            except Exception as sub_e:
                logger.warning("[IBKRClient] Account subscription failed (non-fatal): %s", sub_e)

            return True
        except Exception as e:
            err_type = type(e).__name__
            if "Timeout" in err_type or "timeout" in str(e).lower():
                logger.warning(
                    "[IBKRClient] Connection to TWS timed out (clientId=%d). "
                    "Most likely causes:\n"
                    "  1. clientId=%d is already in use by another process "
                    "(dashboard uses clientId=2, paper trader uses clientId=1 — "
                    "never run two instances with the same clientId).\n"
                    "  2. TWS API is not enabled: in TWS go to "
                    "Edit → Global Configuration → API → Settings → "
                    "check 'Enable ActiveX and Socket Clients', port=7497.\n"
                    "  3. TWS is still starting up — wait 30s and retry.\n"
                    "  Falling back to DEMO MODE.",
                    self.client_id, self.client_id,
                )
            else:
                logger.warning("[IBKRClient] Could not connect to TWS: %s", e)
            self._connected = False
            return False

    def disconnect(self):
        if self._ib and self._connected:
            try:
                import asyncio
                future = asyncio.run_coroutine_threadsafe(
                    self._ib.disconnectAsync(), self._loop
                )
                future.result(timeout=5)
            except Exception:
                pass
        self._connected = False

    def run_async(self, coro, timeout: float = 30):
        import asyncio
        if not self._loop or not self._loop.is_running():
            raise RuntimeError("IBKRClient background loop is not running")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    @property
    def ib(self):
        return self._ib

    @property
    def is_connected(self) -> bool:
        if self._ib is None:
            return self._connected
        return self._connected and self._ib.isConnected()


# ---------------------------------------------------------------------------
# IBKRClient — drop-in replacement for AlpacaClient
# ---------------------------------------------------------------------------

class IBKRClient:
    """
    Interactive Brokers TWS client for stock data and paper trading.

    Drop-in replacement for AlpacaClient. Public interface is identical:
      get_historical_data(), get_quote(), place_paper_trade(),
      close_full_position(), get_account(), get_remote_positions(),
      demo_mode, SP500_SYMBOLS.

    Note: IBKR does not support fractional shares for most US equities.
    All quantities are floor()'d to integers automatically.
    """

    SP500_SYMBOLS = [
        'AAPL', 'MSFT', 'AMZN', 'GOOGL', 'GOOG', 'TSLA', 'BRK.B', 'UNH', 'JNJ', 'XOM',
        'JPM', 'V', 'PG', 'CVX', 'HD', 'MA', 'BAC', 'ABBV', 'PFE', 'KO',
        'PEP', 'AVGO', 'COST', 'DIS', 'WMT', 'TMO', 'VZ', 'ADBE', 'MRK', 'NFLX',
        'ABT', 'CRM', 'ACN', 'NKE', 'TXN', 'LIN', 'MDT', 'UPS', 'AMD', 'PM',
        'BMY', 'QCOM', 'HON', 'RTX', 'LLY', 'ORCL', 'IBM', 'BA', 'GE', 'MMM',
    ]

    # Pacing: ~60 historical requests per 10 min -> ~10s minimum spacing
    _PACING_DELAY = 12.0

    def __init__(
        self,
        host: str = None,
        port: int = None,
        client_id: int = None,
        # Legacy Alpaca params: accepted but ignored for backward compat
        api_key: str = None,
        secret_key: str = None,
        paper: bool = True,
    ):
        self.host = host or os.environ.get("IBKR_HOST", "127.0.0.1")
        self.port = port or int(os.environ.get("IBKR_PORT", "7497"))
        self.client_id = client_id or int(os.environ.get("IBKR_CLIENT_ID", "1"))

        self._wrapper = _IBKRSyncWrapper(self.host, self.port, self.client_id)

        self._connected = self._wrapper.connect()

        try:
            if _INDICATORS_AVAILABLE:
                self.indicators = TechnicalIndicators()
            else:
                self.indicators = None
        except Exception:
            self.indicators = None
        self._last_request_time = 0.0

        # Disk cache — stores DataFrames as Parquet files in data/cache/
        if _CACHE_AVAILABLE:
            try:
                _base = _src_dir.replace('/src', '') if '/src' in _src_dir else _src_dir
                _cache_dir = os.path.join(os.path.dirname(_src_dir), 'data', 'cache')
                self._cache = _HistoricalDataCache(_cache_dir)
            except Exception as _ce:
                logger.warning("[IBKRClient] Cache init failed: %s — running without cache", _ce)
                self._cache = None
        else:
            self._cache = None

        if self._connected:
            logger.info("[IBKRClient] Live connection to TWS established.")
        else:
            logger.info("[IBKRClient] TWS not connected — queries will use Yahoo Finance fallback for market data.")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_connected(self) -> bool:
        if self._wrapper.is_connected:
            return True
        logger.info("[IBKRClient] Attempting reconnect to TWS...")
        self._connected = self._wrapper.connect()
        return self._connected

    def _pace(self):
        elapsed = time.time() - self._last_request_time
        if elapsed < self._PACING_DELAY:
            time.sleep(self._PACING_DELAY - elapsed)
        self._last_request_time = time.time()

    def _make_contract(self, symbol: str):
        from ib_async import Stock
        return Stock(symbol, "SMART", "USD")

    def _fetch_yfinance_historical(self, symbol: str, days: int, timeframe: str = "1Day") -> Optional[pd.DataFrame]:
        """Fetch real historical bars from Yahoo Finance as a TWS fallback."""
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
                        logger.info("[IBKRClient] Fetched %d real bars for %s via Yahoo Finance", len(df), symbol)
                        return df
        except Exception as e:
            logger.warning("[IBKRClient] Yahoo Finance historical fetch failed for %s: %s", symbol, e)
        return None

    # ------------------------------------------------------------------
    # Historical data
    # ------------------------------------------------------------------

    def get_historical_data(
        self,
        symbol: str,
        days: int = 100,
        timeframe: str = "1Day",
        end_date: str = "",
    ) -> pd.DataFrame:
        use_cache = self._cache is not None and not end_date
        if use_cache:
            cached = self._cache.get(symbol, days, timeframe)
            if cached is not None and cached.attrs.get("data_source") not in ("demo_mode", "demo_fallback", "synthetic"):
                logger.debug(
                    "[IBKRClient] Cache HIT %s/%s/%dd (%d bars)",
                    symbol, timeframe, days, len(cached),
                )
                return cached

        # 1) Try TWS fetch if connected
        if self._ensure_connected():
            try:
                bar_size = _TIMEFRAME_MAP.get(timeframe, "1 day")
                duration = _duration_str(days, bar_size)
                self._pace()
                contract = self._make_contract(symbol)
                bars = self._wrapper.run_async(
                    self._wrapper.ib.reqHistoricalDataAsync(
                        contract,
                        endDateTime=end_date,
                        durationStr=duration,
                        barSizeSetting=bar_size,
                        whatToShow="TRADES",
                        useRTH=True,
                        formatDate=1,
                    ),
                    timeout=45,
                )
                if bars:
                    rows = []
                    for b in bars:
                        rows.append({
                            "timestamp": pd.to_datetime(b.date),
                            "open":   float(b.open),
                            "high":   float(b.high),
                            "low":    float(b.low),
                            "close":  float(b.close),
                            "volume": int(b.volume),
                        })
                    df = pd.DataFrame(rows).set_index("timestamp")
                    df.sort_index(inplace=True)
                    max_rows = days * _BARS_PER_DAY.get(bar_size, 1)
                    df = df.tail(max_rows)
                    df.attrs["data_source"] = "ibkr"
                    if use_cache:
                        self._cache.put(symbol, days, timeframe, df)
                    return df
            except Exception as e:
                logger.warning("[IBKRClient] TWS get_historical_data(%s) failed: %s — falling back to Yahoo Finance", symbol, e)

        # 2) Fallback to Yahoo Finance real data
        yf_df = self._fetch_yfinance_historical(symbol, days, timeframe)
        if yf_df is not None:
            if use_cache:
                self._cache.put(symbol, days, timeframe, yf_df)
            return yf_df

        # 3) Fallback to stale cache if real data
        if use_cache:
            stale = self._cache.get(symbol, days, timeframe, allow_stale=True)
            if stale is not None and stale.attrs.get("data_source") not in ("demo_mode", "demo_fallback", "synthetic"):
                logger.info("[IBKRClient] Serving stale real cache for %s (%d bars)", symbol, len(stale))
                return stale

        raise RuntimeError(f"Could not fetch real historical market data for '{symbol}' from TWS or Yahoo Finance")

    # ------------------------------------------------------------------
    # Real-time quote
    # ------------------------------------------------------------------

    def get_quote(self, symbol: str) -> Optional[StockQuote]:
        try:
            import config as tradesight_config
        except ImportError:
            tradesight_config = None
        force_yahoo = getattr(tradesight_config, "FORCE_YAHOO_QUOTES", True) if tradesight_config else True

        if force_yahoo:
            logger.info("[IBKRClient] get_quote(%s): FORCE_YAHOO_QUOTES is enabled — prioritizing Yahoo Finance query...", symbol)
            yf_quote = self._get_yahoo_quote_fallback(symbol)
            if yf_quote:
                return yf_quote
            logger.warning("[IBKRClient] get_quote(%s): Forced Yahoo Finance query failed — falling back to TWS query...", symbol)

        if self._ensure_connected():
            try:
                contract = self._make_contract(symbol)
                async def _qualify_and_tick(c):
                    qualified = await self._wrapper.ib.qualifyContractsAsync(c)
                    if not qualified or qualified[0] is None:
                        return None
                    self._wrapper.ib.reqMarketDataType(3)
                    tickers = await self._wrapper.ib.reqTickersAsync(qualified[0])
                    if not tickers:
                        return None
                    ticker = tickers[0]
                    import asyncio
                    for _ in range(10):
                        last_price = getattr(ticker, 'last', 0.0)
                        bid = getattr(ticker, 'bid', 0.0)
                        ask = getattr(ticker, 'ask', 0.0)
                        if (last_price == last_price and last_price > 0) or \
                           (bid == bid and bid > 0) or \
                           (ask == ask and ask > 0):
                            break
                        await asyncio.sleep(0.2)
                    return ticker

                t = self._wrapper.run_async(_qualify_and_tick(contract), timeout=15)
                if t:
                    def _safe_float(val):
                        try:
                            f = float(val)
                            return f if f == f else 0.0
                        except (TypeError, ValueError):
                            return 0.0

                    bid   = _safe_float(t.bid)   if _safe_float(t.bid)   > 0 else 0.0
                    ask   = _safe_float(t.ask)   if _safe_float(t.ask)   > 0 else 0.0
                    last  = _safe_float(t.last)  if _safe_float(t.last)  > 0 else (ask or bid or 0.0)
                    vol   = int(_safe_float(t.volume)) if t.volume else 0
                    close = _safe_float(t.close) if _safe_float(t.close) > 0 else last
                    change     = round(last - close, 4) if close else 0.0
                    change_pct = round(change / close * 100, 4) if close else 0.0

                    if last > 0:
                        return StockQuote(
                            symbol=symbol, timestamp=datetime.now(),
                            bid=bid, ask=ask, last=last, volume=vol,
                            change=change, change_pct=change_pct,
                        )
            except Exception as e:
                logger.warning("[IBKRClient] TWS get_quote(%s) failed: %s", symbol, e)

        # Fall back to Yahoo Finance live quote query
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
                
                logger.info("[IBKRClient] Yahoo Finance fallback successful for %s: $%.2f", symbol, last_price)
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
        except Exception as e:
            logger.warning("[IBKRClient] Yahoo Finance fallback query failed for %s: %s", symbol, e)
            return None

    # ------------------------------------------------------------------
    # Order execution
    # ------------------------------------------------------------------

    def place_paper_trade(
        self,
        symbol: str,
        quantity: float,
        side: str,
        order_type: str = "market",
    ) -> Dict:
        if not self._ensure_connected():
            return {"error": "TWS is disconnected. Live/paper orders require TWS connection."}

        qty_int = max(1, math.floor(float(quantity)))
        try:
            from ib_async import MarketOrder
            contract = self._make_contract(symbol)
            action = "BUY" if side.lower() == "buy" else "SELL"
            order = MarketOrder(action, qty_int)
            async def _place():
                return self._wrapper.ib.placeOrder(contract, order)
            trade = self._wrapper.run_async(_place(), timeout=30)
            time.sleep(1.5)  # brief wait for fill

            fill_price = None
            status = "accepted"
            order_id = str(trade.order.orderId) if trade and trade.order else f"ibkr_{int(time.time())}"
            if trade and trade.fills:
                fill_price = float(trade.fills[-1].execution.price)
                status = "filled"
            elif trade and trade.orderStatus:
                status = trade.orderStatus.status.lower()

            return {
                "order_id": order_id, "symbol": symbol, "quantity": qty_int,
                "side": side, "status": status, "fill_price": fill_price,
                "fill_time": datetime.now().isoformat(),
            }
        except Exception as e:
            logger.error("[IBKRClient] place_paper_trade(%s) failed: %s", symbol, e)
            return {"error": str(e)}

    def close_full_position(self, symbol: str) -> Dict:
        if not self._ensure_connected():
            return {"error": "TWS is disconnected. Closing positions requires TWS connection."}

        try:
            from ib_async import MarketOrder
            async def _req_positions():
                return await self._wrapper.ib.reqPositionsAsync()
            positions = self._wrapper.run_async(_req_positions(), timeout=15)
            pos_qty = 0.0
            for p in (positions or []):
                if p.contract.symbol == symbol:
                    pos_qty = float(p.position)
                    break

            if pos_qty == 0:
                return {"status": "closed", "fill_price": None, "symbol": symbol}

            action = "SELL" if pos_qty > 0 else "BUY"
            qty_int = max(1, math.floor(abs(pos_qty)))
            contract = self._make_contract(symbol)
            order = MarketOrder(action, qty_int)
            async def _place():
                return self._wrapper.ib.placeOrder(contract, order)
            trade = self._wrapper.run_async(_place(), timeout=30)
            time.sleep(1.5)

            fill_price = None
            if trade and trade.fills:
                fill_price = float(trade.fills[-1].execution.price)

            logger.info("[IBKRClient] Closed position: %s @ fill_price=%s", symbol, fill_price)
            return {"status": "closed", "fill_price": fill_price, "symbol": symbol}
        except Exception as e:
            logger.error("[IBKRClient] close_full_position(%s) failed: %s", symbol, e)
            return {"error": str(e)}

    # ------------------------------------------------------------------
    # Account & positions
    # ------------------------------------------------------------------

    def get_account(self) -> Dict:
        if not self._ensure_connected():
            return {
                "cash": "0.00", "buying_power": "0.00",
                "equity": "0.00", "portfolio_value": "0.00",
                "long_market_value": "0", "status": "DISCONNECTED",
            }
        try:
            cash = 0.0
            net_liq = 0.0
            stock_market_value = 0.0

            for av in self._wrapper.ib.accountValues():
                if av.currency != "USD":
                    continue
                if av.tag == "$LEDGER-CashBalance" or av.tag == "$LEDGER-TotalCashBalance":
                    try:
                        v = float(av.value)
                        if v != 0:
                            cash = v
                    except (ValueError, TypeError):
                        pass
                elif av.tag == "$LEDGER-NetLiquidationByCurrency":
                    try:
                        v = float(av.value)
                        if v != 0:
                            net_liq = v
                    except (ValueError, TypeError):
                        pass
                elif av.tag == "$LEDGER-StockMarketValue":
                    try:
                        v = float(av.value)
                        if v != 0:
                            stock_market_value = v
                    except (ValueError, TypeError):
                        pass

            if stock_market_value == 0:
                stock_market_value = sum(
                    float(p.marketValue) for p in self._wrapper.ib.portfolio()
                    if p.marketValue and str(p.marketValue) not in ("nan", "0", "")
                    and getattr(p.contract, "currency", "USD") == "USD"
                )

            equity = net_liq if net_liq > 0 else (cash + stock_market_value)
            buying_power = cash

            return {
                "cash": str(cash),
                "buying_power": str(buying_power),
                "equity": str(equity),
                "portfolio_value": str(equity),
                "long_market_value": str(stock_market_value),
                "status": "ACTIVE",
            }
        except Exception as e:
            logger.error("[IBKRClient] get_account() failed: %s", e)
            return {}

    def get_remote_positions(self) -> List[Dict]:
        if not self._ensure_connected():
            return []
        try:
            portfolio_items = self._wrapper.ib.portfolio()
            result = []
            for p in (portfolio_items or []):
                contract_type = getattr(p.contract, 'secType', '') or type(p.contract).__name__
                if contract_type not in ('STK', 'Stock', ''):
                    continue
                sym = p.contract.symbol
                qty = float(p.position)
                avg_cost = float(p.averageCost)
                if qty == 0:
                    continue
                
                current_price = float(p.marketPrice) if p.marketPrice else avg_cost
                market_value = float(p.marketValue) if p.marketValue else (current_price * abs(qty))
                unrealized_pnl = float(p.unrealizedPNL) if p.unrealizedPNL and str(p.unrealizedPNL) != 'nan' else 0.0
                side = "long" if qty > 0 else "short"
                
                result.append({
                    "symbol": sym, 
                    "qty": str(qty),
                    "avg_entry_price": str(avg_cost),
                    "market_value": str(market_value),
                    "current_price": str(current_price),
                    "unrealized_pnl": str(unrealized_pnl),
                    "side": side,
                })
            return result
        except Exception as e:
            logger.error("[IBKRClient] get_remote_positions() failed: %s", e)
            return []

    def _extract_signals(self, indicators: Dict) -> List[str]:
        signals = []
        ind_data = indicators.get("indicators", {})
        rsi = ind_data.get("rsi", 50)
        if rsi < 30:
            signals.append("RSI_OVERSOLD")
        elif rsi > 70:
            signals.append("RSI_OVERBOUGHT")
        if indicators.get("confluence_score", 0) > 0.7:
            signals.append("HIGH_CONFLUENCE")
        return signals
