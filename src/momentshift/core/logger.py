"""Application-wide logging.

Writes timestamped logs to a ``logs/`` directory next to the executable (or the
repo root in dev) and keeps only the last 7 days of files. Every log file is
named ``momentshift-YYYY-MM-DD.log``.

This module is intentionally stdlib-only so it can be imported from anywhere
(core engine, GUI, entry point) without pulling in Qt.
"""

from __future__ import annotations

import glob
import logging
import os
import sys
import time
from pathlib import Path

_LOG = logging.getLogger("momentshift")
_configured = False


def app_root() -> Path:
    """Directory that counts as the app "root" (where ``logs/`` lives)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    # src/momentshift/core/logger.py -> parents[3] is the repo root.
    return Path(__file__).resolve().parents[3]


def log_dir() -> Path:
    return app_root() / "logs"


def _cleanup_old_logs(days: int = 7) -> None:
    """Delete log files older than ``days`` days (best effort)."""
    try:
        cutoff = time.time() - days * 86400
        for f in glob.glob(str(log_dir() / "*.log")):
            try:
                if os.path.getmtime(f) < cutoff:
                    os.remove(f)
            except OSError:
                pass
    except Exception:
        pass


def init_logging(level: int = logging.DEBUG) -> None:
    """Configure the root logger once. Safe to call multiple times."""
    global _configured
    if _configured:
        return
    _configured = True

    d = log_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    _cleanup_old_logs(7)

    date = time.strftime("%Y-%m-%d")
    logfile = d / f"momentshift-{date}.log"
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )

    try:
        fh = logging.FileHandler(logfile, encoding="utf-8", errors="replace")
        fh.setLevel(level)
        fh.setFormatter(fmt)
        _LOG.addHandler(fh)
    except OSError:
        pass

    ch = logging.StreamHandler(sys.stderr)
    ch.setLevel(logging.WARNING)
    ch.setFormatter(fmt)
    _LOG.addHandler(ch)

    _LOG.setLevel(level)
    _LOG.propagate = False
    _LOG.info("Logging initialized; log file: %s", logfile)


def get_logger(name: str = "momentshift") -> logging.Logger:
    """Return a child logger. Initializes logging on first use."""
    if not _configured:
        init_logging()
    if name == "momentshift":
        return _LOG
    return _LOG.getChild(name)
