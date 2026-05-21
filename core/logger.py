"""
Centralized logging for Atlas.

Writes to console and rotating log files under logs/.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from core.config import AtlasConfig

_CONFIGURED = False


def setup_logging(config: AtlasConfig) -> None:
    """Configure root and Atlas loggers once per process."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    config.log_dir.mkdir(parents=True, exist_ok=True)
    level = getattr(logging, config.log_level.upper(), logging.INFO)
    fmt = config._yaml.get("logging", {}).get(
        "format",
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )
    formatter = logging.Formatter(fmt)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        config.log_dir / "atlas.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    logging.getLogger("slack_sdk").setLevel(logging.WARNING)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced logger (e.g. 'atlas.orchestrator')."""
    if not name.startswith("atlas."):
        name = f"atlas.{name}"
    return logging.getLogger(name)
