"""A small log of communication faults. It records sizes and codes, not tune data."""
from __future__ import annotations

import logging
import logging.handlers

from .importer import default_db_path


def log_path():
    return default_db_path().with_name("openpclink.log")


def enable_log_file() -> None:
    path = log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(path, maxBytes=500_000, backupCount=1)
    except OSError:
        return  # Logging must never stop someone connecting.
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root = logging.getLogger("openpclink")
    root.addHandler(handler)
    root.setLevel(logging.INFO)
