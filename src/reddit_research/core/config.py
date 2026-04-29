"""Configuration loading.

Reads optional ``.env`` from the working directory or a caller-supplied path,
plus environment variables. All keys have sensible defaults; only
``REDDIT_USER_AGENT`` is worth setting in MVP.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


DEFAULT_USER_AGENT = "reddit-research:0.1 (by /u/joeblowfromidaho)"
DEFAULT_CACHE_DIRNAME = "reddit-research"


@dataclass(frozen=True)
class Config:
    """Runtime configuration. Immutable; load once, pass through operations."""

    user_agent: str
    cache_dir: Path
    # Future OAuth fields, populated only if the upgrade path activates.
    # Kept as Optional fields rather than separate Config subclass so callers
    # don't have to branch on transport.
    oauth_client_id: str | None = None
    oauth_client_secret: str | None = None
    oauth_username: str | None = None
    oauth_password: str | None = None
    write_enabled: bool = False

    @property
    def cache_db_path(self) -> Path:
        return self.cache_dir / "cache.db"


def _default_cache_dir() -> Path:
    """Default cache dir.

    Uses the XDG cache home if set, otherwise ``~/.cache/reddit-research/``.
    The deploy user's data dir on warehouse-vm overrides this via
    ``REDDIT_CACHE_DIR``.
    """
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / DEFAULT_CACHE_DIRNAME


def _truthy(s: str | None) -> bool:
    return (s or "").strip().lower() in {"1", "true", "yes", "on"}


def load_config(env_file: Path | str | None = None) -> Config:
    """Load configuration from environment + optional ``.env`` file.

    Args:
        env_file: path to a ``.env`` file. If ``None``, looks for ``./.env``
            relative to the working directory; if no file is found, only
            actual environment variables are used.
    """
    if env_file is not None:
        load_dotenv(env_file, override=False)
    else:
        load_dotenv(override=False)  # auto-discovers ./.env

    user_agent = os.environ.get("REDDIT_USER_AGENT", DEFAULT_USER_AGENT).strip()
    if not user_agent:
        user_agent = DEFAULT_USER_AGENT

    cache_dir_raw = os.environ.get("REDDIT_CACHE_DIR", "").strip()
    cache_dir = Path(cache_dir_raw).expanduser() if cache_dir_raw else _default_cache_dir()

    return Config(
        user_agent=user_agent,
        cache_dir=cache_dir,
        oauth_client_id=os.environ.get("REDDIT_CLIENT_ID") or None,
        oauth_client_secret=os.environ.get("REDDIT_CLIENT_SECRET") or None,
        oauth_username=os.environ.get("REDDIT_USERNAME") or None,
        oauth_password=os.environ.get("REDDIT_PASSWORD") or None,
        write_enabled=_truthy(os.environ.get("REDDIT_WRITE_ENABLED")),
    )
