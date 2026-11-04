"""ECU fault code wording, from PCLink's own strings ("ECU Fault Code 3: Ethanol Sensor Fault")."""
from __future__ import annotations

import re

from .database import Database

_PREFIX = re.compile(r"^ECU Fault Code (\d+):")


def fault_texts(database: Database) -> dict[int, str]:
    cache = getattr(database, "_fault_texts", None)
    if cache is None:
        cache = {}
        for text in database._strings.values():
            match = _PREFIX.match(text)
            if match:
                cache.setdefault(int(match.group(1)), text)
        database._fault_texts = cache
    return cache


def fault_text(database: Database, code: int) -> str:
    """The full banner text for a fault code, as PCLink words it."""
    if code <= 0:
        return ""
    return fault_texts(database).get(code, f"ECU Fault Code {code}")
