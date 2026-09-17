"""App logging: rotating file + warnings-to-stdout.

Two sinks with distinct jobs:
- ``~/.conductor/logs/app.log`` (RotatingFileHandler, 5 MB × 5): everything at
  INFO+ — poller errors, source sync counts. /tmp is purged by macOS, so logs
  live under the home dir.
- stdout at WARNING+: launchd redirects stdout to backend.log, which stays a
  small crash/serious-problems file (uvicorn banner + tracebacks) instead of a
  firehose. uvicorn's per-request access log is disabled in the launchd plist.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_DIR = Path.home() / ".conductor" / "logs"


def setup_logging() -> None:
    root = logging.getLogger()
    if any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        return  # idempotent (uvicorn reload / tests importing main twice)
    root.setLevel(logging.INFO)

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fh = RotatingFileHandler(LOG_DIR / "app.log", maxBytes=5 * 1024 * 1024, backupCount=5)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    fh.setLevel(logging.INFO)
    root.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(logging.Formatter("%(levelname)-7s %(name)s: %(message)s"))
    sh.setLevel(logging.WARNING)
    root.addHandler(sh)
