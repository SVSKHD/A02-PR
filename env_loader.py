"""
AUREON v2 — .env file loader.

Loads environment variables from a .env file in the current working directory
(or the AUREON_ENV_FILE override path). Existing OS env vars take precedence
over .env values, which is the safe default — if you set AUREON_TELEGRAM_TOKEN
in your shell, it won't be silently overridden by a stale .env.

This module is imported by bot.py, watchdog.py, auto_analyze.py, fetch_data.py,
fetch_lab.py, and telemetry.py (when run standalone) so any of those scripts
work whether you use .env, OS env vars, or both.

Usage
-----
    from env_loader import load_env
    load_env()   # call once at the top of your script's __main__
"""

import logging
import os
from typing import Optional

log = logging.getLogger("env_loader")


def load_env(path: Optional[str] = None, verbose: bool = True) -> bool:
    """
    Load .env from `path` (default: ./.env or $AUREON_ENV_FILE).
    Returns True if a file was loaded, False if none found.

    OS env vars always take precedence over .env values.
    """
    target = path or os.environ.get("AUREON_ENV_FILE") or ".env"

    try:
        from dotenv import load_dotenv
    except ImportError:
        if verbose:
            log.warning(
                "python-dotenv not installed; skipping .env load. "
                "Run: pip install python-dotenv"
            )
        return False

    if not os.path.isfile(target):
        if verbose:
            log.debug(f"No .env file found at {os.path.abspath(target)} — "
                      "using OS env vars only")
        return False

    # override=False (default) → existing OS env vars win
    loaded = load_dotenv(target, override=False)
    if loaded and verbose:
        log.info(f"Loaded environment from {os.path.abspath(target)}")
    return loaded