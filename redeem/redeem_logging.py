"""
Rolling file log + console for redeem scripts.

Env (optional, read after dotenv load):
  POLY_REDEEM_LOG_FILE      Path (absolute or relative to repo root). Default: logs/polymarket-redeem.log
  POLY_REDEEM_LOG_MAX_BYTES Max size before rotate (default 5242880 = 5 MiB)
  POLY_REDEEM_LOG_BACKUPS   Rotated files to keep (default 5)
  POLY_REDEEM_LOG_LEVEL     DEBUG|INFO|WARNING|ERROR (default INFO)
  POLY_REDEEM_LOG_DISABLE   1/true to skip file handler (console only)
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


def _parse_level(raw: str) -> int:
    m = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "WARN": logging.WARNING,
        "ERROR": logging.ERROR,
    }
    return m.get(raw.strip().upper(), logging.INFO)


def setup_rolling_logging(*, script_tag: str, repo_root: Path) -> logging.Logger:
    """
    Configure a logger named ``polymarket_redeem.<script_tag>`` with console + optional rolling file.
    """
    name = f"polymarket_redeem.{script_tag}"
    log = logging.getLogger(name)
    log.handlers.clear()
    log.setLevel(_parse_level(os.getenv("POLY_REDEEM_LOG_LEVEL") or "INFO"))
    log.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    out = logging.StreamHandler(sys.stdout)
    out.setLevel(logging.DEBUG)
    out.setFormatter(fmt)
    log.addHandler(out)

    if (os.getenv("POLY_REDEEM_LOG_DISABLE") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    ):
        return log

    raw_path = (os.getenv("POLY_REDEEM_LOG_FILE") or "").strip()
    if raw_path:
        p = Path(raw_path)
        log_path = p if p.is_absolute() else (repo_root / p)
    else:
        log_path = repo_root / "logs" / "polymarket-redeem.log"

    log_path.parent.mkdir(parents=True, exist_ok=True)
    max_bytes = int(os.getenv("POLY_REDEEM_LOG_MAX_BYTES") or str(5 * 1024 * 1024))
    backups = int(os.getenv("POLY_REDEEM_LOG_BACKUPS") or "5")

    fh = RotatingFileHandler(
        log_path,
        maxBytes=max_bytes,
        backupCount=backups,
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    log.addHandler(fh)
    log.debug("Logging to %s (max_bytes=%s backups=%s)", log_path, max_bytes, backups)
    return log
