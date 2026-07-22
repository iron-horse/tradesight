"""
TradeSight Historical Data Cache
=================================
Disk-based Parquet cache for IBKRClient.get_historical_data().

Design goals
------------
* Zero new dependencies — pandas, pathlib, datetime only.
* Transparent to all callers; interception happens inside IBKRClient.
* Market-hours aware TTL: no point refreshing on weekends/after-hours.
* Stale-cache fallback: prefer yesterday's real data over random-walk demo data.

Cache layout
------------
  data/cache/
    AAPL/
      1Hour_365d.parquet
      1Day_200d.parquet
    NVDA/
      1Hour_730d.parquet
    ...

Cache key:  symbol + timeframe + days  (e.g. "AAPL_1Hour_365d")
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

logger = logging.getLogger("DataCache")

# ---------------------------------------------------------------------------
# TTL constants
# ---------------------------------------------------------------------------
_TTL_INTRADAY = 6 * 3600        # 6 hours  — for 1H / 30min bars
_TTL_DAILY    = 24 * 3600       # 24 hours — for 1Day bars

# NYSE market hours (Eastern) expressed as UTC offsets.
# EDT = UTC-4, EST = UTC-5. Conservative window that covers both.
_MARKET_OPEN_UTC_HOUR  = 13   # 09:00 ET ~= 13:00 UTC (EDT)
_MARKET_CLOSE_UTC_HOUR = 21   # 16:30 ET ~= 20:30 UTC (EDT)


class HistoricalDataCache:
    """Parquet-based cache for historical OHLCV DataFrames.

    Parameters
    ----------
    cache_dir : Path
        Root directory for cache files, e.g. ``data/cache/``.
        Created automatically if it does not exist.
    """

    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._hits   = 0
        self._misses = 0
        logger.debug("DataCache initialised at %s", self.cache_dir)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(
        self,
        symbol: str,
        days: int,
        timeframe: str,
        allow_stale: bool = False,
    ) -> Optional[pd.DataFrame]:
        """Return cached DataFrame if available and fresh.

        Parameters
        ----------
        symbol, days, timeframe : cache key components.
        allow_stale : if True, return the cached df even if the TTL has expired.
                      Used as a fallback when TWS is unreachable.

        Returns
        -------
        DataFrame with ``data_source`` attr set to ``"cache"`` or
        ``"cache_stale"``, or ``None`` if no usable entry exists.

        Note
        ----
        Superset matching: if the exact ``days`` file is absent, any cached
        file for the same symbol+timeframe with *more* days is read and
        sliced to the requested depth.  This means warming with 730d
        satisfies requests for 365d, 200d, 100d, etc. without re-fetching.
        """
        path = self._path(symbol, days, timeframe)

        # --- exact match ---
        if path.exists():
            fresh = self.is_fresh(path, timeframe)
            if fresh or allow_stale:
                df = self._read_parquet(path)
                if df is not None:
                    df.attrs["data_source"] = "cache" if fresh else "cache_stale"
                    self._hits += 1
                    logger.debug(
                        "Cache %s  %s/%s/%dd  (%d bars)",
                        "HIT" if fresh else "STALE-HIT",
                        symbol, timeframe, days, len(df),
                    )
                    return df
            else:
                self._misses += 1
                logger.debug("Cache STALE  %s/%s/%dd", symbol, timeframe, days)
                return None

        # --- superset match: look for a larger cached file for same timeframe ---
        sym_dir = self.cache_dir / symbol.upper()
        if sym_dir.exists():
            best_path: Optional[Path] = None
            best_cached_days = 0
            for candidate in sym_dir.glob(f"{timeframe}_*.parquet"):
                stem = candidate.stem          # e.g. "1Hour_730d"
                try:
                    cached_days = int(stem.split("_")[-1].rstrip("d"))
                except ValueError:
                    continue
                if cached_days > days and cached_days > best_cached_days:
                    fresh = self.is_fresh(candidate, timeframe)
                    if fresh or allow_stale:
                        best_path = candidate
                        best_cached_days = cached_days

            if best_path is not None:
                df = self._read_parquet(best_path)
                if df is not None:
                    # Slice to requested trading bars (tail = most recent)
                    df = df.tail(len(df))  # ensure sorted
                    # Approximate bars per day for slicing
                    bars_per_day = {"1Hour": 7, "30 mins": 13, "1 day": 1}.get(timeframe, 7)
                    max_bars = days * bars_per_day
                    df = df.tail(max_bars)
                    fresh = self.is_fresh(best_path, timeframe)
                    df.attrs["data_source"] = "cache" if fresh else "cache_stale"
                    df.attrs["cache_superset"] = str(best_path.name)
                    self._hits += 1
                    logger.debug(
                        "Cache SUPERSET-HIT  %s/%s  (req %dd, served from %s, sliced to %d bars)",
                        symbol, timeframe, days, best_path.name, len(df),
                    )
                    return df

        self._misses += 1
        return None

    def put(self, symbol: str, days: int, timeframe: str, df: pd.DataFrame) -> None:
        """Persist *df* to the cache.  Silently ignores write errors."""
        if df is None or df.empty:
            return
        path = self._path(symbol, days, timeframe)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            df.to_parquet(path, index=True)
            logger.debug("Cache WRITE %s/%s/%dd  (%d bars)", symbol, timeframe, days, len(df))
        except Exception as exc:
            logger.warning("Cache write failed for %s: %s", path, exc)

    def is_fresh(self, path: Path, timeframe: str) -> bool:
        """Return True if *path* is within its TTL or market is closed."""
        if not path.exists():
            return False
        # When the market is closed (evenings/weekends) cached data cannot
        # have changed — treat it as always fresh to avoid pointless fetches.
        if not self.is_market_open():
            return True
        age = time.time() - path.stat().st_mtime
        ttl = _TTL_INTRADAY if timeframe != "1Day" else _TTL_DAILY
        return age < ttl

    def is_market_open(self) -> bool:
        """Rough check: Mon-Fri between 13:00 and 21:00 UTC."""
        now = datetime.now(tz=timezone.utc)
        if now.weekday() >= 5:   # Saturday=5, Sunday=6
            return False
        return _MARKET_OPEN_UTC_HOUR <= now.hour < _MARKET_CLOSE_UTC_HOUR

    def purge_stale(self) -> int:
        """Delete cache files whose TTL has expired.

        Returns
        -------
        int : Number of files deleted.
        """
        deleted = 0
        for parquet in self.cache_dir.rglob("*.parquet"):
            # Derive timeframe from filename  e.g. "1Hour_365d.parquet"
            timeframe = parquet.stem.split("_")[0]
            if not self.is_fresh(parquet, timeframe):
                try:
                    parquet.unlink()
                    deleted += 1
                    logger.debug("Cache PURGED %s", parquet)
                except Exception as exc:
                    logger.warning("Could not delete %s: %s", parquet, exc)
        logger.info("Cache purge complete: %d file(s) deleted", deleted)
        return deleted

    def stats(self) -> dict:
        """Return hit/miss counters and disk usage summary."""
        files = list(self.cache_dir.rglob("*.parquet"))
        total = sum(f.stat().st_size for f in files)
        return {
            "hits":         self._hits,
            "misses":       self._misses,
            "hit_rate":     round(self._hits / max(1, self._hits + self._misses), 3),
            "cached_files": len(files),
            "disk_bytes":   total,
            "disk_mb":      round(total / 1_048_576, 2),
            "cache_dir":    str(self.cache_dir),
        }

    def list_entries(self) -> list:
        """Return a list of all cache entries with metadata."""
        entries = []
        for parquet in sorted(self.cache_dir.rglob("*.parquet")):
            stat = parquet.stat()
            timeframe = parquet.stem.split("_")[0]
            entries.append({
                "symbol":    parquet.parent.name,
                "timeframe": timeframe,
                "file":      parquet.name,
                "size_kb":   round(stat.st_size / 1024, 1),
                "age_min":   round((time.time() - stat.st_mtime) / 60, 1),
                "fresh":     self.is_fresh(parquet, timeframe),
            })
        return entries

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _path(self, symbol: str, days: int, timeframe: str) -> Path:
        """Return the Parquet file path for the given cache key."""
        filename = f"{timeframe}_{days}d.parquet"
        return self.cache_dir / symbol.upper() / filename

    def _read_parquet(self, path: Path) -> Optional[pd.DataFrame]:
        """Read a Parquet file, returning None on any error."""
        try:
            df = pd.read_parquet(path)
            return df if not df.empty else None
        except Exception as exc:
            logger.warning("Cache read failed for %s: %s", path, exc)
            return None
