"""Parameter names, units, limits and menu structure for each DataId."""
from __future__ import annotations

import csv
import gzip
import json
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from .importer import bundled_db_path, bundled_help_path, default_db_path, default_help_path

_MAX_INHERIT_DEPTH = 32


# PCLink's metric defaults for parameters that give a value type, not a unit.
_VALUE_TYPE_UNITS = {
    "Pressure": "kPa",
    "BigPressure": "kPa",
    "Temperature": "°C",
    "Speed": "km/h",
    "Lambda": "λ",
    "Volume": "L",
}

# The order of the top of PCLink's settings menu, as shown by the real program.
MENU_ORDER = (
    "Configuration", "Fuel", "Ignition", "Engine Protection", "Auxiliary Outputs", "Digital Inputs",
    "Analog Inputs", "Triggers", "MotorSport", "Idle Speed Control", "Electronic Throttle",
    "Boost Control", "Knock Control", "VVT Control", "Chassis and Body", "CAN",
)


class DatabaseMissing(Exception):
    pass


def _number(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


def _int(value: str | None) -> int | None:
    return int(value) if value and value.isdigit() else None


def _int_list(value: str | None) -> list[int]:
    return [int(item) for item in (value or "").split(",") if item.strip().isdigit()]


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
    priority: int = 0
    hidden_on_ecus: frozenset[int] = frozenset()
    children_by_option: tuple[tuple[int, ...], ...] = ()
    hidden_options: tuple[frozenset[int], ...] = ()
    warnings: tuple[str, ...] = ()
    active_zone: int | None = None
    column_source_id: int | None = None
    row_source_id: int | None = None
    kind: int | None = None

    @property
    def is_table(self) -> bool:
        return self.rows * self.columns > 1

    @property
    def is_runtime_value(self) -> bool:
        return self.kind == 4


class Database:
    def __init__(self, raw: dict) -> None:
        self._params: dict[int, dict] = {int(k): v for k, v in raw["parameters"].items()}
        self._strings: dict[int, str] = {int(k): v for k, v in raw["strings"].items()}
        self.map_key: tuple[int, ...] = tuple(raw["map_key"])
        self._cache: dict[int, Parameter] = {}
        self._controllers: dict[int, list[int]] | None = None

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
            units=prop("Units") or _VALUE_TYPE_UNITS.get(prop("ValueType") or "", ""),
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
            priority=_int(prop("PriorityPos")) or 0,
            hidden_on_ecus=frozenset(_int_list(prop("InvisibleLICs"))),
            children_by_option=tuple(tuple(_int_list(item)) for item in _csv_values(prop("VisibleChildren"))),
            hidden_options=tuple(frozenset(_int_list(item)) for item in _csv_values(prop("InvisibleLICsSL"))),
            warnings=tuple(self._string(item.strip()) if item.strip() != "0" else "" for item in _csv_values(prop("Warnings"))),
            active_zone=_int(prop("ActiveZone")),
            column_source_id=_int(prop("XAxisParameterID")),
            row_source_id=_int(prop("YAxisParameterID")),
            kind=_int(prop("Type")),
        )
        self._cache[data_id] = parameter
        return parameter

    def controllers(self) -> dict[int, list[int]]:
        """For each parameter, the parents whose current option decides if it shows."""
        if self._controllers is None:
            self._controllers = {}
            for data_id in self.ids:
                for child in {c for option in self.parameter(data_id).children_by_option for c in option}:
                    self._controllers.setdefault(child, []).append(data_id)
        return self._controllers

    def is_visible(self, data_id: int, link_id: int, option_of) -> bool:
        """Should PCLink's data show this for an ECU model, given current options?

        `option_of(id)` returns a parameter's option index, or None if it is absent.
        """
        if not self.known(data_id):
            return False
        parameter = self.parameter(data_id)
        if not parameter.visible or link_id in parameter.hidden_on_ecus:
            return False
        parents = self.controllers().get(data_id)
        if not parents:
            return True
        for parent_id in parents:
            option = option_of(parent_id)
            children = self.parameter(parent_id).children_by_option
            if option is not None and 0 <= option < len(children) and data_id in children[option]:
                return True
        return False

    def visible_options(self, data_id: int, link_id: int) -> list[tuple[int, str]]:
        """The (value, text) choices this ECU model offers."""
        parameter = self.parameter(data_id)
        hidden = parameter.hidden_options
        return [
            (value, text)
            for value, text in enumerate(parameter.options)
            if not (value < len(hidden) and link_id in hidden[value])
        ]

    def warning_for(self, data_id: int, option: int) -> str:
        warnings = self.parameter(data_id).warnings
        return warnings[option] if 0 <= option < len(warnings) else ""

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


class HelpManual:
    """Pages of Link's manual, found by the HelpURL of a parameter."""

    def __init__(self) -> None:
        self.pages: dict[str, str] = {}
        for path in (default_help_path(), bundled_help_path()):
            if path and path.exists():
                with gzip.open(path, "rt", encoding="utf-8") as stream:
                    self.pages = json.load(stream)
                return

    def page_for(self, help_url: str) -> str | None:
        return self.pages.get(help_url.partition("#")[0].lower())
