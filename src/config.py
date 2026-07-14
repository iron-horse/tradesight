"""TradeSight configuration — IBKR TWS edition."""
import os
import logging
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass

KEYCHAIN_AVAILABLE = False  # No API secrets needed for local IBKR connection

# Set up logging
logger = logging.getLogger(__name__)

# Base directories
BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
CONFIG_DIR = BASE_DIR / "config" 
LOGS_DIR = BASE_DIR / "logs"

# Ensure directories exist
for directory in [DATA_DIR, CONFIG_DIR, LOGS_DIR]:
    directory.mkdir(exist_ok=True, parents=True)

# ---------------------------------------------------------------------------
# IBKR TWS connection settings
# No API secrets needed — IBKR connects to the locally running TWS app.
# ---------------------------------------------------------------------------
IBKR_HOST      = os.environ.get("IBKR_HOST",      "127.0.0.1")
IBKR_PORT      = int(os.environ.get("IBKR_PORT",  "7497"))   # 7497=TWS paper, 4002=IB Gateway paper
IBKR_CLIENT_ID = int(os.environ.get("IBKR_CLIENT_ID", "1"))

# Trading Configuration
USE_PAPER_TRADING = True  # Always start with paper trading for safety
MAX_POSITION_SIZE = 0.10  # 10% of portfolio per position
MAX_DAILY_TRADES = 10
STOP_LOSS_PERCENTAGE = 0.05  # 5% stop loss
TAKE_PROFIT_PERCENTAGE = 0.10  # 10% take profit

# Scanner Configuration
SCAN_INTERVAL_SECONDS = 300  # 5 minutes
MAX_CONCURRENT_SCANS = 3

# Database Configuration  
DATABASE_URL = f"sqlite:///{DATA_DIR / 'tradesight.db'}"

def get_api_key_status():
    """Return IBKR connection settings (replaces former Alpaca key status)."""
    return {"IBKR-Host": bool(IBKR_HOST), "IBKR-Port": bool(IBKR_PORT)}


def refresh_api_keys():
    """No-op: IBKR uses local socket — no keys to refresh."""
    logger.info("refresh_api_keys: IBKR uses local TWS connection, no keys required.")
    return get_api_key_status()


# Log startup status
logger.info("TradeSight config loaded — IBKR TWS: %s:%d clientId=%d", IBKR_HOST, IBKR_PORT, IBKR_CLIENT_ID)


# ---------------------------------------------------------------------------
# Alerts Configuration (Phase 5.1)
# ---------------------------------------------------------------------------
# All alert channels are disabled by default.  Enable and configure via
# config/config.json (settings persist across restarts).
# ---------------------------------------------------------------------------
import json as _json

_ALERTS_CONFIG_PATH = CONFIG_DIR / 'config.json'

_ALERTS_DEFAULTS: dict = {
    # Master switch — no alerts fire until this is True
    'alerts_enabled': False,

    # --- Email (SMTP) ---
    'email_enabled': False,
    'smtp_host': '',
    'smtp_port': 587,
    'smtp_use_tls': True,
    'smtp_username': '',   # keep empty if no auth needed
    'smtp_password': '',   # never hardcode — set via config.json
    'email_from': '',
    'email_to': [],        # list of recipient addresses

    # --- Webhook ---
    'webhook_enabled': False,
    'webhook_url': '',
    'webhook_timeout': 10,
    'webhook_headers': {},  # extra HTTP headers if required by target
}


def _load_alerts_config() -> dict:
    """Load alerts config from config.json, falling back to defaults."""
    cfg = dict(_ALERTS_DEFAULTS)
    if _ALERTS_CONFIG_PATH.exists():
        try:
            with open(_ALERTS_CONFIG_PATH) as f:
                stored = _json.load(f)
            alerts_section = stored.get('alerts', {})
            cfg.update(alerts_section)
        except Exception as e:
            logger.warning(f"Could not read alerts config from {_ALERTS_CONFIG_PATH}: {e}")
    return cfg


def save_alerts_config(updates: dict) -> bool:
    """
    Persist alert config changes to config.json.
    Merges *updates* into the existing file (creates if absent).
    Returns True on success.
    """
    try:
        existing: dict = {}
        if _ALERTS_CONFIG_PATH.exists():
            with open(_ALERTS_CONFIG_PATH) as f:
                existing = _json.load(f)
        alerts_section = existing.get('alerts', {})
        alerts_section.update(updates)
        existing['alerts'] = alerts_section
        _ALERTS_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_ALERTS_CONFIG_PATH, 'w') as f:
            _json.dump(existing, f, indent=2)
        return True
    except Exception as e:
        logger.error(f"Failed to save alerts config: {e}")
        return False


# Active alerts config (module-level singleton — reload with load_alerts_config())
ALERTS_CONFIG: dict = _load_alerts_config()


def reload_alerts_config():
    """Reload ALERTS_CONFIG from disk (useful after saving new settings)."""
    global ALERTS_CONFIG
    ALERTS_CONFIG = _load_alerts_config()
    return ALERTS_CONFIG
