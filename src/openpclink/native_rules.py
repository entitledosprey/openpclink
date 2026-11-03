"""PCLink's dependent-setting rules, evaluated natively from a data table.

The table lists, for each setting and value, the conditions under which other
settings show or hide. It is generated at build time (see reverse-engineering/
extract_rules.py). No Windows code runs here.
"""
from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Callable

_PROP_VALUE = 2
_PROP_VISIBLE = 10
_PINNED_MENU = "Table Allocation"  # Extraction fixed these, so they decide nothing.


class RulesTable:
    def __init__(self, raw: dict, is_pinned: Callable[[int], bool]) -> None:
        self.rules: dict[int, dict[int, list]] = {}
        for data_id, by_value in raw["rules"].items():
            self.rules[int(data_id)] = {
                int(value): [
                    (
                        tuple((c[0], c[1], c[2], c[3]) for c in rule["when"] if not is_pinned(c[0])),
                        tuple((v[0], v[1]) for v in rule["visible"]),
                    )
                    for rule in sorted(rules, key=lambda rule: -len(rule["when"]))
                ]
                for value, rules in by_value.items()
            }

    @classmethod
    def load(cls, path: Path, is_pinned: Callable[[int], bool]) -> "RulesTable":
        with gzip.open(path, "rt") as stream:
            return cls(json.load(stream), is_pinned)


class NativeRules:
    def __init__(
        self,
        table: RulesTable,
        read_value: Callable[[int, int], float],
        exists: Callable[[int], bool],
        default_visible: Callable[[int], bool],
    ) -> None:
        self.table = table
        self.read_value, self.exists, self.default_visible = read_value, exists, default_visible
        self.shown: dict[int, int] = {}
        self.visibility_changed = False

    def visible(self, data_id: int) -> bool:
        stored = self.shown.get(data_id)
        return bool(stored) if stored is not None else self.default_visible(data_id)

    def _holds(self, data_id: int, prop: int, index: int, expected: float) -> bool:
        if prop == _PROP_VISIBLE:
            return int(self.visible(data_id)) == expected
        if prop != _PROP_VALUE:
            return expected == 0
        actual = self.read_value(data_id, index) if self.exists(data_id) else 0.0
        if actual == expected:
            return True
        # Plain numbers were probed at 0 and 1, the difference the rules test for.
        return expected == 1 and actual > 0 and not self._is_option(data_id)

    def _is_option(self, data_id: int) -> bool:
        by_value = self.table.rules.get(data_id)
        return bool(by_value) and len(by_value) > 2

    def value_changed(self, data_id: int, new: float) -> None:
        by_value = self.table.rules.get(data_id)
        if not by_value:
            return
        rules = by_value.get(int(new))
        if rules is None:
            rules = by_value.get(1 if new > 0 else 0)
        for conditions, shown in rules or ():
            if all(self._holds(*condition) for condition in conditions):
                for target, value in shown:
                    if self.shown.get(target) != value:
                        self.visibility_changed = True
                    self.shown[target] = value
                return
