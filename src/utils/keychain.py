"""macOS Keychain integration for TradeSight.

IBKR TWS uses a local socket connection — no API keys are required.
This module is kept for potential future secret storage (e.g. webhook tokens).
The Alpaca-specific helpers have been removed.
"""
import subprocess
import logging
import os

logger = logging.getLogger(__name__)


class KeychainManager:
    """Manages secrets in macOS Keychain with environment variable fallback."""

    def __init__(self, service_prefix="TradeSight"):
        self.service_prefix = service_prefix

    def _run_security_command(self, args):
        """Run macOS security command with error handling."""
        try:
            result = subprocess.run(
                ['security'] + args,
                capture_output=True, text=True, check=True
            )
            return result.stdout.strip()
        except subprocess.CalledProcessError as e:
            logger.debug(f"Security command failed: {e}")
            return None
        except FileNotFoundError:
            logger.warning("macOS security command not found")
            return None

    def get_secret(self, key_name, account="api-key", fallback_env=None):
        """Get a secret from Keychain with environment variable fallback."""
        service_name = f"{self.service_prefix}-{key_name}"
        secret = self._run_security_command([
            'find-generic-password', '-s', service_name, '-a', account, '-w'
        ])
        if secret:
            return secret
        if fallback_env:
            env_val = os.environ.get(fallback_env, "")
            if env_val:
                return env_val
        return ""

    def set_secret(self, key_name, value, account="api-key"):
        """Store a secret in Keychain."""
        service_name = f"{self.service_prefix}-{key_name}"
        result = self._run_security_command([
            'add-generic-password', '-s', service_name,
            '-a', account, '-w', value, '-U'
        ])
        return result is not None


# Global instance
keychain = KeychainManager()


# ---------------------------------------------------------------------------
# IBKR connection helpers
# ---------------------------------------------------------------------------

def get_ibkr_host() -> str:
    """Get IBKR TWS host (default: 127.0.0.1)."""
    return os.environ.get("IBKR_HOST", "127.0.0.1")


def get_ibkr_port() -> int:
    """Get IBKR TWS port (7497=TWS paper, 4002=IB Gateway paper)."""
    return int(os.environ.get("IBKR_PORT", "7497"))


def get_ibkr_client_id() -> int:
    """Get IBKR client ID for this connection (default: 1)."""
    return int(os.environ.get("IBKR_CLIENT_ID", "1"))
