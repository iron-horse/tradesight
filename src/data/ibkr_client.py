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
        from data_cache import HistoricalDataCache as _HistoricalDataCache
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
        return self._connected and self._ib is not None and self._ib.isConnected()


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

        # Check if demo mode is forced via config (skips TWS connection entirely)
        _force_demo = False
        try:
            import src.config as _cfg
            _force_demo = getattr(_cfg, "IBKR_DEMO_MODE", False)
        except ImportError:
            try:
                import config as _cfg
                _force_demo = getattr(_cfg, "IBKR_DEMO_MODE", False)
            except ImportError:
                pass

        if _force_demo:
            logger.info("[IBKRClient] IBKR_DEMO_MODE=True — skipping TWS connection, using Yahoo Finance / demo data.")
            self._connected = False
        else:
            self._connected = self._wrapper.connect()
        self.demo_mode = not self._connected

        try:
            if _INDICATORS_AVAILABLE:
                self.indicators = TechnicalIndicators()
            else:
                self.indicators = None
        except Exception:
            self.indicators = None
        self._demo_fallback_count = 0
        self._last_request_time = 0.0

        # Disk cache — stores DataFrames as Parquet files in data/cache/
        # Falls back gracefully if the module is unavailable.
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

        if self.demo_mode:
            logger.warning(
                "[IBKRClient] TWS unreachable — running in DEMO MODE. "
                "Start TWS, log into a Paper Trading account, then restart TradeSight."
            )
        else:
            logger.info("[IBKRClient] Live connection to TWS established.")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_connected(self) -> bool:
        if self._wrapper.is_connected:
            return True
        logger.info("[IBKRClient] Attempting reconnect to TWS...")
        self._connected = self._wrapper.connect()
        self.demo_mode = not self._connected
        return self._connected

    def _pace(self):
        elapsed = time.time() - self._last_request_time
        if elapsed < self._PACING_DELAY:
            time.sleep(self._PACING_DELAY - elapsed)
        self._last_request_time = time.time()

    def _make_contract(self, symbol: str):
        from ib_async import Stock
        return Stock(symbol, "SMART", "USD")

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
        # ------------------------------------------------------------------
        # Cache lookup — skipped for end_date requests (precise backfills
        # must always fetch exact bars from TWS, not a cached snapshot).
        # ------------------------------------------------------------------
        use_cache = self._cache is not None and not end_date
        if use_cache:
            cached = self._cache.get(symbol, days, timeframe)
            if cached is not None:
                logger.debug(
                    "[IBKRClient] Cache HIT %s/%s/%dd (%d bars)",
                    symbol, timeframe, days, len(cached),
                )
                return cached

        # ------------------------------------------------------------------
        # Demo / disconnected path
        # ------------------------------------------------------------------
        if self.demo_mode or not self._ensure_connected():
            # Prefer stale cache over synthetic random-walk data
            if use_cache:
                stale = self._cache.get(symbol, days, timeframe, allow_stale=True)
                if stale is not None:
                    logger.info(
                        "[IBKRClient] TWS unavailable — serving stale cache for %s (%d bars)",
                        symbol, len(stale),
                    )
                    return stale
            df = self._generate_demo_data(symbol, days)
            df.attrs["data_source"] = "demo_mode"
            return df

        # ------------------------------------------------------------------
        # Live TWS fetch
        # ------------------------------------------------------------------
        bar_size = _TIMEFRAME_MAP.get(timeframe, "1 day")
        duration = _duration_str(days, bar_size)

        try:
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
            if not bars:
                raise ValueError(f"No historical bars returned for {symbol}")

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

            # Persist to cache so next run is instant
            if use_cache:
                self._cache.put(symbol, days, timeframe, df)

            return df

        except Exception as e:
            self._demo_fallback_count += 1
            logger.warning(
                "[IBKRClient] get_historical_data(%s) failed: %s — "
                "trying stale cache, then DEMO data (fallback #%d)",
                symbol, e, self._demo_fallback_count,
            )
            # Prefer stale cache over synthetic random-walk data
            if use_cache:
                stale = self._cache.get(symbol, days, timeframe, allow_stale=True)
                if stale is not None:
                    logger.info(
                        "[IBKRClient] Serving stale cache for %s (%d bars) after TWS error",
                        symbol, len(stale),
                    )
                    return stale
            df = self._generate_demo_data(symbol, days)
            df.attrs["data_source"] = "demo_fallback"
            df.attrs["fallback_reason"] = str(e)
            return df

    # ------------------------------------------------------------------
    # Real-time quote
    # ------------------------------------------------------------------

    def get_quote(self, symbol: str) -> Optional[StockQuote]:
        try:
            import src.config as tradesight_config
            force_yahoo = getattr(tradesight_config, "FORCE_YAHOO_QUOTES", True)
        except ImportError:
            force_yahoo = True

        if force_yahoo:
            logger.info("[IBKRClient] get_quote(%s): FORCE_YAHOO_QUOTES is enabled — prioritizing Yahoo Finance query...", symbol)
            yf_quote = self._get_yahoo_quote_fallback(symbol)
            if yf_quote:
                return yf_quote
            logger.warning("[IBKRClient] get_quote(%s): Forced Yahoo Finance query failed — falling back to TWS query...", symbol)

        if self.demo_mode or not self._ensure_connected():
            return self._generate_demo_quote(symbol)

        try:
            contract = self._make_contract(symbol)
            # Qualify contract to populate conId — required by reqTickersAsync
            async def _qualify_and_tick(c):
                qualified = await self._wrapper.ib.qualifyContractsAsync(c)
                if not qualified or qualified[0] is None:
                    return None
                # Request delayed market data (type 3 = delayed, free, no subscription needed)
                self._wrapper.ib.reqMarketDataType(3)
                tickers = await self._wrapper.ib.reqTickersAsync(qualified[0])
                if not tickers:
                    return None
                ticker = tickers[0]
                # Wait up to 2 seconds for price data to arrive from TWS
                import asyncio
                for _ in range(10):
                    last_price = getattr(ticker, 'last', 0.0)
                    bid = getattr(ticker, 'bid', 0.0)
                    ask = getattr(ticker, 'ask', 0.0)
                    # Check if we got a valid price (non-NaN and > 0)
                    if (last_price == last_price and last_price > 0) or \
                       (bid == bid and bid > 0) or \
                       (ask == ask and ask > 0):
                        break
                    await asyncio.sleep(0.2)
                return ticker

            t = self._wrapper.run_async(_qualify_and_tick(contract), timeout=15)
            if not t:
                return self._generate_demo_quote(symbol)
            # Guard against NaN values returned when market data subscription is missing
            def _safe_float(val):
                try:
                    f = float(val)
                    return f if f == f else 0.0  # NaN check
                except (TypeError, ValueError):
                    return 0.0

            bid   = _safe_float(t.bid)   if _safe_float(t.bid)   > 0 else 0.0
            ask   = _safe_float(t.ask)   if _safe_float(t.ask)   > 0 else 0.0
            last  = _safe_float(t.last)  if _safe_float(t.last)  > 0 else (ask or bid or 0.0)
            vol   = int(_safe_float(t.volume)) if t.volume else 0
            close = _safe_float(t.close) if _safe_float(t.close) > 0 else last
            change     = round(last - close, 4) if close else 0.0
            change_pct = round(change / close * 100, 4) if close else 0.0

            if last <= 0:
                logger.info("[IBKRClient] get_quote(%s): got zero/NaN price from TWS — attempting Yahoo Finance fallback...", symbol)
                yf_quote = self._get_yahoo_quote_fallback(symbol)
                if yf_quote:
                    return yf_quote
                logger.warning("[IBKRClient] get_quote(%s): Yahoo Finance fallback failed, using demo fallback", symbol)
                return self._generate_demo_quote(symbol)

            return StockQuote(
                symbol=symbol,
                timestamp=datetime.now(),
                bid=bid, ask=ask, last=last, volume=vol,
                change=change, change_pct=change_pct,
            )
        except Exception as e:
            logger.warning("[IBKRClient] get_quote(%s) failed: %s", symbol, e)
            logger.info("[IBKRClient] get_quote(%s): attempting Yahoo Finance fallback after error...", symbol)
            yf_quote = self._get_yahoo_quote_fallback(symbol)
            if yf_quote:
                return yf_quote
            return self._generate_demo_quote(symbol)

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
        # IBKR requires integer shares for most US equities
        qty_int = max(1, math.floor(float(quantity)))

        if self.demo_mode or not self._ensure_connected():
            quote = self.get_quote(symbol)
            fill_price = quote.last if quote else 100.0
            return {
                "order_id": f"demo_{int(time.time())}",
                "symbol": symbol, "quantity": qty_int, "side": side,
                "status": "filled", "fill_price": fill_price,
                "fill_time": datetime.now().isoformat(), "demo_mode": True,
            }

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
        if self.demo_mode or not self._ensure_connected():
            quote = self.get_quote(symbol)
            fill_price = quote.last if quote else 100.0
            return {"status": "closed", "fill_price": fill_price, "symbol": symbol, "demo_mode": True}

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
        if self.demo_mode or not self._ensure_connected():
            return {
                "cash": "500.00", "buying_power": "500.00",
                "equity": "500.00", "portfolio_value": "500.00",
                "long_market_value": "0", "status": "ACTIVE",
            }
        try:
            # PRIMARY: $LEDGER tags with currency=USD give exact USD balances.
            # These are the correct source for USD position sizing.
            # $LEDGER-CashBalance        = USD cash available
            # $LEDGER-NetLiquidationByCurrency = total USD equity
            # $LEDGER-StockMarketValue   = USD value of stock positions
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

            # FALLBACK: sum portfolio() market values (always in USD for US stocks)
            if stock_market_value == 0:
                stock_market_value = sum(
                    float(p.marketValue) for p in self._wrapper.ib.portfolio()
                    if p.marketValue and str(p.marketValue) not in ("nan", "0", "")
                    and getattr(p.contract, "currency", "USD") == "USD"
                )

            # Equity = net liquidation if available, else cash + positions
            equity = net_liq if net_liq > 0 else (cash + stock_market_value)
            # Buying power ≈ 4x cash for margin accounts; use cash as conservative estimate
            buying_power = cash

            logger.debug(
                "[IBKRClient] get_account (USD): cash=%.2f net_liq=%.2f stock_mv=%.2f",
                cash, net_liq, stock_market_value
            )

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
        if self.demo_mode or not self._ensure_connected():
            return []
        try:
            # Use TWS portfolio() updates to query live market value, current price, and unrealized P&L
            portfolio_items = self._wrapper.ib.portfolio()
            result = []
            for p in (portfolio_items or []):
                # Only process US equity (Stock) positions — skip Forex, Futures, Options etc.
                contract_type = getattr(p.contract, 'secType', '') or type(p.contract).__name__
                if contract_type not in ('STK', 'Stock', ''):
                    logger.debug("[IBKRClient] Skipping non-equity position: %s (%s)", p.contract.symbol, contract_type)
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

    def get_paper_positions(self) -> List[PaperPosition]:
        raw = self.get_remote_positions()
        result = []
        for pos in raw:
            try:
                qty = float(pos["qty"])
                result.append(PaperPosition(
                    symbol=pos["symbol"], quantity=int(abs(qty)),
                    side=pos.get("side", "long"),
                    avg_entry_price=float(pos["avg_entry_price"]),
                    current_price=float(pos["current_price"]),
                    unrealized_pnl=float(pos.get("unrealized_pnl", 0.0)),
                    market_value=float(pos["market_value"]),
                ))
            except Exception:
                continue
        return result

    def scan_sp500(self, min_volume: int = 1_000_000) -> List[Dict]:
        opportunities = []
        print("Scanning stocks via IBKR...")
        for i, symbol in enumerate(self.SP500_SYMBOLS):
            try:
                print(f"  Analyzing {symbol} ({i+1}/{len(self.SP500_SYMBOLS)})...")
                data = self.get_historical_data(symbol, days=100)
                if len(data) < 50:
                    continue
                avg_volume = data["volume"].tail(20).mean()
                if avg_volume < min_volume:
                    continue

                indicators = {}
                confluence = 0
                rsi = 50
                macd = 0
                if self.indicators is not None:
                    indicators = self.indicators.calculate_all(data)
                    confluence = indicators.get("confluence_score", 0)
                    rsi = indicators.get("indicators", {}).get("rsi", 50)
                    macd = indicators.get("indicators", {}).get("macd", 0)

                score = confluence * 100
                if rsi < 30:
                    score += 20
                elif rsi > 70:
                    score += 15
                if abs(macd) > 1:
                    score += 10
                opportunities.append({
                    "symbol": symbol, "score": min(100, score),
                    "current_price": float(data["close"].iloc[-1]),
                    "volume": int(avg_volume), "rsi": float(rsi),
                    "confluence": float(confluence),
                    "signals": self._extract_signals(indicators),
                })
            except Exception as e:
                print(f"  Error analyzing {symbol}: {e}")
        opportunities.sort(key=lambda x: x["score"], reverse=True)
        print(f"Found {len(opportunities)} opportunities")
        return opportunities

    # ------------------------------------------------------------------
    # Demo / fallback data  (identical to AlpacaClient)
    # ------------------------------------------------------------------

    def _generate_demo_data(self, symbol: str, days: int) -> pd.DataFrame:
        import zlib
        symbol_seed = zlib.adler32(symbol.encode('utf-8'))
        np.random.seed((symbol_seed + int(datetime.now().strftime("%Y%m%d"))) % 2147483647)
        dates = pd.date_range(end=datetime.now(), periods=days, freq="D")
        base_prices = {
            'AAPL': 222, 'MSFT': 380, 'AMZN': 210, 'GOOGL': 175, 'GOOG': 175,
            'TSLA': 260, 'AMD': 105, 'META': 520, 'PYPL': 68,  'NVDA': 900,
            'QCOM': 165, 'INTC': 25,  'IBM':  230, 'ORCL': 130, 'CSCO': 52,
            'MU':   98,  'TXN': 185,  'ADBE': 450, 'HON':  210, 'GE':   175,
            'JPM':  230, 'BAC': 46,   'V':    300, 'MA':   510, 'KO':   72,
            'PEP':  165, 'WMT': 90,   'COST': 935, 'HD':   385, 'NKE':  75,
            'DIS':  112, 'XOM': 115,  'CVX':  155, 'BA':   170, 'PFE':  28,
            'BMY':  58,  'JNJ': 158,  'MRK':  125, 'ABT':  124, 'VZ':   42, 'T': 22,
        }
        base_price = base_prices.get(symbol, 100)
        returns = np.random.normal(0.001, 0.02, days)
        prices = [base_price]
        for ret in returns[1:]:
            prices.append(prices[-1] * (1 + ret))
        data = []
        for date, close in zip(dates, prices):
            o = close + np.random.normal(0, close * 0.005)
            h = max(o, close) + np.random.uniform(0, close * 0.01)
            l = min(o, close) - np.random.uniform(0, close * 0.01)
            data.append({
                "open": round(o, 2), "high": round(h, 2),
                "low": round(l, 2), "close": round(close, 2),
                "volume": int(np.random.uniform(1_000_000, 10_000_000)),
            })
        return pd.DataFrame(data, index=dates)

    def _generate_demo_quote(self, symbol: str) -> StockQuote:
        data = self._generate_demo_data(symbol, 1)
        last_price = float(data["close"].iloc[-1])
        return StockQuote(
            symbol=symbol, timestamp=datetime.now(),
            bid=round(last_price - 0.01, 2), ask=round(last_price + 0.01, 2),
            last=last_price,
            volume=int(np.random.uniform(100_000, 1_000_000)),
            change=round(np.random.uniform(-5, 5), 2),
            change_pct=round(np.random.uniform(-3, 3), 2),
        )

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
