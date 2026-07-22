#!/usr/bin/env python3
"""
TradeSight Cache Manager
========================
CLI tool for managing the historical data cache.

Commands
--------
  status   — Show cached symbols, file counts, disk usage, freshness
  warm     — Pre-fetch all cluster symbols and populate the cache
  purge    — Delete stale cache entries (run after market close)
  refresh  — Force-refresh a specific symbol (ignores TTL)

Examples
--------
  python3 scripts/cache_manager.py status
  python3 scripts/cache_manager.py warm --days 730 --timeframe 1Hour
  python3 scripts/cache_manager.py purge
  python3 scripts/cache_manager.py refresh NVDA --timeframe 1Hour --days 730
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# ── path setup ──────────────────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))

from data.data_cache import HistoricalDataCache

_CACHE_DIR   = _ROOT / "data" / "cache"
_CLUSTER_FILE = _ROOT / "data" / "symbol_clusters.json"


# ── helpers ─────────────────────────────────────────────────────────────────

def _load_cluster_symbols() -> list[str]:
    """Return all unique symbols from symbol_clusters.json."""
    if not _CLUSTER_FILE.exists():
        print(f"⚠️  {_CLUSTER_FILE} not found")
        return []
    with open(_CLUSTER_FILE) as f:
        clusters = json.load(f)
    seen: set[str] = set()
    symbols: list[str] = []
    for cluster in clusters.values():
        for sym in cluster.get("symbols", []):
            if sym not in seen:
                seen.add(sym)
                symbols.append(sym)
    return symbols


def _get_ibkr_client(client_id: int = 11):
    """Return a connected IBKRClient (raises on failure)."""
    from data.ibkr_client import IBKRClient
    client = IBKRClient(client_id=client_id)
    return client


# ── commands ────────────────────────────────────────────────────────────────

def cmd_status(_args):
    cache = HistoricalDataCache(_CACHE_DIR)
    entries = cache.list_entries()
    stats   = cache.stats()

    print(f"\n📦 TradeSight Data Cache — {stats['cache_dir']}")
    print(f"   Files: {stats['cached_files']}  |  Disk: {stats['disk_mb']} MB")
    print(f"   Market open: {cache.is_market_open()}\n")

    if not entries:
        print("   (cache is empty — run 'warm' to populate)")
        return

    # Group by symbol
    from collections import defaultdict
    by_sym: dict[str, list] = defaultdict(list)
    for e in entries:
        by_sym[e["symbol"]].append(e)

    print(f"  {'Symbol':<8} {'Timeframe':<10} {'File':<25} {'Size':>7} {'Age(min)':>10} {'Fresh':>6}")
    print("  " + "-" * 72)
    for sym in sorted(by_sym):
        for e in by_sym[sym]:
            fresh_icon = "✅" if e["fresh"] else "⚠️ "
            print(
                f"  {sym:<8} {e['timeframe']:<10} {e['file']:<25} "
                f"{e['size_kb']:>6.1f}K {e['age_min']:>9.1f}m {fresh_icon}"
            )
    print()


def cmd_warm(args):
    symbols   = _load_cluster_symbols()
    days      = args.days
    timeframe = args.timeframe

    if not symbols:
        print("No symbols found in symbol_clusters.json")
        sys.exit(1)

    print(f"\n🔥 Warming cache for {len(symbols)} symbols "
          f"({days}d @ {timeframe}) …\n")

    try:
        client = _get_ibkr_client()
    except Exception as e:
        print(f"❌ Could not connect to TWS: {e}")
        sys.exit(1)

    if client.demo_mode:
        print("❌ TWS is not running (demo mode). Cache warm requires a live TWS connection.")
        sys.exit(1)

    ok, failed = 0, []
    for sym in symbols:
        try:
            print(f"  Fetching {sym} …", end=" ", flush=True)
            df = client.get_historical_data(sym, days=days, timeframe=timeframe)
            src = df.attrs.get("data_source", "?")
            if src in ("cache", "cache_stale"):
                print(f"already cached ({len(df)} bars) — skipped TWS fetch")
            else:
                print(f"{len(df)} bars [{src}]")
            ok += 1
        except Exception as e:
            print(f"FAILED: {e}")
            failed.append(sym)

    print(f"\n✅ Warmed {ok}/{len(symbols)} symbols.")
    if failed:
        print(f"⚠️  Failed: {', '.join(failed)}")

    # Show final stats
    cache = HistoricalDataCache(_CACHE_DIR)
    s = cache.stats()
    print(f"   Cache: {s['cached_files']} files, {s['disk_mb']} MB\n")


def cmd_purge(_args):
    cache   = HistoricalDataCache(_CACHE_DIR)
    deleted = cache.purge_stale()
    s       = cache.stats()
    print(f"\n🗑️  Purged {deleted} stale file(s).")
    print(f"   Remaining: {s['cached_files']} files, {s['disk_mb']} MB\n")


def cmd_refresh(args):
    symbol    = args.symbol.upper()
    days      = args.days
    timeframe = args.timeframe

    cache = HistoricalDataCache(_CACHE_DIR)
    path  = cache._path(symbol, days, timeframe)

    if path.exists():
        path.unlink()
        print(f"🗑️  Removed stale cache entry: {path.name}")

    try:
        client = _get_ibkr_client()
    except Exception as e:
        print(f"❌ Could not connect to TWS: {e}")
        sys.exit(1)

    if client.demo_mode:
        print("❌ TWS is not running. Refresh requires a live connection.")
        sys.exit(1)

    print(f"⬇️  Fetching {symbol} ({days}d @ {timeframe}) from TWS …", end=" ", flush=True)
    df = client.get_historical_data(symbol, days=days, timeframe=timeframe)
    print(f"{len(df)} bars [{df.attrs.get('data_source', '?')}]")
    s = cache.stats()
    print(f"✅ Cache updated. Total: {s['cached_files']} files, {s['disk_mb']} MB\n")


def cmd_clear(args):
    """Delete cache files matching the given filters.

    Filters can be combined — all specified filters must match.
    With no filters, ALL cache files are deleted (requires confirmation).
    Use --dry-run to preview without deleting.
    """
    days_filter      = args.days       # e.g. 365 → only delete *_365d.parquet
    symbol_filter    = args.symbol.upper() if args.symbol else None
    timeframe_filter = args.timeframe  # e.g. "1Hour"
    dry_run          = args.dry_run

    all_files = list(_CACHE_DIR.rglob("*.parquet"))
    to_delete = []

    for f in all_files:
        sym   = f.parent.name                        # e.g. "AAPL"
        stem  = f.stem                               # e.g. "1Hour_365d"
        parts = stem.split("_")
        tf    = parts[0]                             # e.g. "1Hour"
        try:
            d = int(parts[-1].rstrip("d"))           # e.g. 365
        except ValueError:
            continue

        if symbol_filter    and sym != symbol_filter:  continue
        if timeframe_filter and tf  != timeframe_filter: continue
        if days_filter      and d   != days_filter:    continue

        to_delete.append(f)

    if not to_delete:
        print("\nNo matching cache files found.\n")
        return

    print(f"\n{'[DRY RUN] ' if dry_run else ''}Files to delete ({len(to_delete)}):")
    for f in sorted(to_delete):
        print(f"   {f.relative_to(_CACHE_DIR)}")

    if dry_run:
        print("\n(dry run — nothing deleted. Remove --dry-run to confirm.)\n")
        return

    # Confirm when deleting everything with no filter
    if not days_filter and not symbol_filter and not timeframe_filter:
        confirm = input("\n⚠️  Delete ALL cache files? [y/N] ").strip().lower()
        if confirm != "y":
            print("Aborted.\n")
            return

    deleted = 0
    for f in to_delete:
        try:
            f.unlink()
            deleted += 1
        except Exception as e:
            print(f"   ⚠️  Could not delete {f.name}: {e}")

    # Remove empty symbol directories
    for sym_dir in _CACHE_DIR.iterdir():
        if sym_dir.is_dir() and not any(sym_dir.iterdir()):
            sym_dir.rmdir()

    remaining = list(_CACHE_DIR.rglob("*.parquet"))
    print(f"\n🗑️  Deleted {deleted} file(s). Remaining: {len(remaining)}\n")


# ── main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="TradeSight cache manager",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Show cache contents and disk usage")

    p_warm = sub.add_parser("warm", help="Pre-fetch all cluster symbols into cache")
    p_warm.add_argument("--days",      type=int, default=730,    help="History depth (default 730)")
    p_warm.add_argument("--timeframe", type=str, default="1Hour", help="Bar size (default 1Hour)")

    sub.add_parser("purge", help="Delete stale cache entries")

    p_ref = sub.add_parser("refresh", help="Force-refresh a single symbol")
    p_ref.add_argument("symbol",      type=str,                  help="Ticker symbol")
    p_ref.add_argument("--days",      type=int, default=730,    help="History depth (default 730)")
    p_ref.add_argument("--timeframe", type=str, default="1Hour", help="Bar size (default 1Hour)")

    p_clr = sub.add_parser("clear", help="Delete cache files by days/symbol/timeframe")
    p_clr.add_argument("--days",      type=int, default=None,   help="Only delete files for this day-depth (e.g. 365)")
    p_clr.add_argument("--symbol",    type=str, default=None,   help="Only delete files for this symbol (e.g. AAPL)")
    p_clr.add_argument("--timeframe", type=str, default=None,   help="Only delete files for this timeframe (e.g. 1Hour)")
    p_clr.add_argument("--dry-run",   action="store_true",      help="Preview what would be deleted without deleting")

    args = parser.parse_args()
    dispatch = {
        "status":  cmd_status,
        "warm":    cmd_warm,
        "purge":   cmd_purge,
        "refresh": cmd_refresh,
        "clear":   cmd_clear,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
