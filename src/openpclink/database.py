"""Parameter names, units, limits and menu structure for each DataId."""
from __future__ import annotations

import csv
import gzip
import json
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from .importer import bundled_db_path, default_db_path

_MAX_INHERIT_DEPTH = 32


class DatabaseMissing(Exception):
    pass


def _number(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


def _int(value: str | None) -> int | None:
    return int(value) if value and value.isdigit() else None


def _csv_values(text: str | None) -> list[str]:
    if not text:
        return []
    return next(csv.reader([text]), [])


@dataclass(frozen=True)
class Parameter:
    data_id: int
    key: str
    caption: str
    path: tuple[str, ...]
    units: str
    minimum: float | None
    maximum: float | None
    decimals: int | None
    options: tuple[str, ...]
    rows: int
    columns: int
    row_labels: tuple[str, ...]
    column_labels: tuple[str, ...]
    visible: bool
    help_url: str
    column_axis_id: int | None = None
    row_axis_id: int | None = None

    @property
    def is_table(self) -> bool:
        return self.rows * self.columns > 1


class Database:
    def __init__(self, raw: dict) -> None:
        self._params: dict[int, dict] = {int(k): v for k, v in raw["parameters"].items()}
        self._strings: dict[int, str] = {int(k): v for k, v in raw["strings"].items()}
        self.map_key: tuple[int, ...] = tuple(raw["map_key"])
        self._cache: dict[int, Parameter] = {}

    @classmethod
    def load(cls, path: Path | None = None) -> "Database":
        candidates = [path] if path else [default_db_path(), bundled_db_path()]
        for candidate in candidates:
            if candidate and candidate.exists():
                with gzip.open(candidate, "rt", encoding="utf-8") as stream:
                    return cls(json.load(stream))
        raise DatabaseMissing(
            "No PCLink data found. Import it from your own PCLink installer: "
            "openpclink import /path/to/PCLink-EN-x.y.z.exe"
        )

    def _string(self, ref: str | None) -> str:
        if ref and ref.strip().isdigit():
            return self._strings.get(int(ref), "")
        return ref or ""

    def _prop(self, data_id: int, name: str, depth: int = 0) -> str | None:
        """A parameter property, following CopyFromID inheritance."""
        node = self._params.get(data_id)
        if node is None or depth > _MAX_INHERIT_DEPTH:
            return None
        props = node["props"]
        if name in props:
            return props[name]
        parent = props.get("CopyFromID")
        return self._prop(int(parent), name, depth + 1) if parent and parent.isdigit() else None

    def _menu_path(self, data_id: int) -> tuple[str, ...]:
        """The menu location. `Parent` is string IDs joined by a backslash."""
        raw = self._params[data_id]["props"].get("Parent") or ""
        names = (self._string(part.strip("_ ")) for part in raw.split("\\"))
        return tuple(name for name in names if name)

    def parameter(self, data_id: int) -> Parameter:
        cached = self._cache.get(data_id)
        if cached is not None:
            return cached
        node = self._params.get(data_id)
        if node is None:
            raise KeyError(f"parameter {data_id} is not in the database")
        prop = lambda name: self._prop(data_id, name)  # noqa: E731
        options = tuple(self._string(item.strip()) for item in _csv_values(prop("Options")))
        decimals = _number(prop("DecPlaces"))
        parameter = Parameter(
            data_id=data_id,
            key=node["name"],
            caption=self._string(prop("Caption")) or node["name"],
            path=self._menu_path(data_id),
            units=prop("Units") or "",
            minimum=_number(prop("Min")),
            maximum=_number(prop("Max")),
            decimals=int(decimals) if decimals is not None else None,
            options=options,
            rows=int(_number(prop("RowCount")) or 1),
            columns=int(_number(prop("ColCount")) or 1),
            row_labels=tuple(_csv_values(prop("RowLabels"))),
            column_labels=tuple(_csv_values(prop("ColLabels"))),
            visible=(prop("Visible") or "true").lower() == "true",
            help_url=prop("HelpURL") or "",
            column_axis_id=_int(prop("xLabels")),
            row_axis_id=_int(prop("yLabels")),
        )
        self._cache[data_id] = parameter
        return parameter

    def known(self, data_id: int) -> bool:
        return data_id in self._params

    def search(self, text: str) -> list[Parameter]:
        needle = text.lower()
        return [
            parameter
            for parameter in (self.parameter(i) for i in sorted(self._params))
            if needle in parameter.caption.lower() or needle in parameter.key.lower()
        ]

    @cached_property
    def ids(self) -> list[int]:
        return sorted(self._params)

    def check_range(self, data_id: int, value: float) -> None:
        """Raise ValueError if the value is outside the parameter's limits."""
        if not self.known(data_id):
            return
        parameter = self.parameter(data_id)
        if parameter.minimum is not None and value < parameter.minimum:
            raise ValueError(f"{parameter.caption}: {value} is below the minimum {parameter.minimum}")
        if parameter.maximum is not None and value > parameter.maximum:
            raise ValueError(f"{parameter.caption}: {value} is above the maximum {parameter.maximum}")
