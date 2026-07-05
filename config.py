"""
Central configuration helpers.

Every module loads its settings through this file so there is exactly one place
that:
  * finds and loads the .env file (relative to the project, not the CWD), and
  * enforces that required variables are actually set.

Secrets are never hardcoded anywhere — they only ever come from .env.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# Resolve paths relative to THIS file so cron and systemd (which run with a bare
# environment and an arbitrary working directory) still find the .env file.
SCRIPT_DIR = Path(__file__).resolve().parent

# Load variables from the .env file sitting next to the project files. This is a
# no-op if the file is missing, so required-var checks below still fire cleanly.
load_dotenv(SCRIPT_DIR / ".env")


class ConfigError(RuntimeError):
    """Raised when a required environment variable is missing or empty."""


def get_env(name, default=None):
    """Return an optional environment variable, or `default` if unset."""
    return os.environ.get(name, default)


def get_bool(name, default=False):
    """Return a boolean env var. Accepts true/1/yes/on (case-insensitive)."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"true", "1", "yes", "on"}


def get_int(name, default):
    """Return an integer env var, falling back to `default` if unset/blank."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return int(raw)


def require_env(name):
    """
    Return a required environment variable.

    Raises ConfigError with an actionable message if the variable is missing or
    empty — this is the "fail loud" behaviour we want so misconfiguration is
    obvious instead of silently producing broken backups.
    """
    value = os.environ.get(name)
    if value is None or value == "":
        raise ConfigError(
            f"Required environment variable {name!r} is not set. "
            f"Add it to your .env file (see .env.example)."
        )
    return value
