"""PCLink's dependent-setting rules, evaluated natively from a data table.

The table lists, for each setting and value, the conditions under which other
settings show or hide. It is generated at build time (see reverse-engineering/
extract_rules.py). No Windows code runs here.
"""
from __future__ import annotations

import gzip
import json
import marshal
from pathlib import Path
from typing import Callable

_PROP_VALUE = 2
_PROP_VISIBLE = 10
_PINNED_MENU = "Table Allocation"  # Extraction fixed these, so they decide nothing.


class RulesTable:
    def __init__(self, raw: dict, is_pinned: Callable[[int], bool]) -> None:
        # setting -> probed value -> (rules, most specific first; values probed per read)
        self.rules: dict[int, dict[int, tuple[list, dict[int, list[float]]]]] = {}
        for data_id, by_value in raw["rules"].items():
            self.rules[int(data_id)] = {
                int(value): (
                    [
                        (
                            tuple((c[0], c[1], c[2], c[3]) for c in rule["when"] if not is_pinned(c[0])),
                            tuple((v[0], v[1]) for v in rule["visible"]),
                            tuple((v[0], v[1]) for v in rule.get("captions", ())),
                            tuple(rule.get("messages", ())),
                            tuple((v[0], v[1]) for v in rule.get("parents", ())),
                        )
                        for rule in sorted(group["rules"], key=lambda rule: -len(rule["when"]))
                    ],
                    {int(k): v for k, v in group["domains"].items()},
                )
                for value, group in by_value.items()
            }

    @classmethod
    def load(cls, path: Path, is_pinned: Callable[[int], bool]) -> "RulesTable":
        with gzip.open(path, "rb") as stream:
            data = stream.read()
        table = cls.__new__(cls)
        if data[:1] == b"{":
            table.__init__(json.loads(data), is_pinned)
        else:
            table.rules = marshal.loads(data)  # Precompiled by compile_table() in this build.
        return table

    def save_compiled(self, path: Path) -> None:
        with gzip.open(path, "wb", compresslevel=1) as stream:
            stream.write(marshal.dumps(self.rules))


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
        self.units: dict[int, str] = {}
        self.parents: dict[int, str] = {}
        self.messages: list[str] = []
        self.visibility_changed = False

    def visible(self, data_id: int) -> bool:
        stored = self.shown.get(data_id)
        return bool(stored) if stored is not None else self.default_visible(data_id)

    def _holds(self, domains: dict[int, list[float]], data_id: int, prop: int, index: int, expected: float) -> bool:
        if prop == _PROP_VISIBLE:
            return int(self.visible(data_id)) == expected
        if prop != _PROP_VALUE:
            return expected == 0
        actual = self.read_value(data_id, index) if self.exists(data_id) else 0.0
        probed = domains.get(data_id)
        if probed:
            # Probes bracket every compared constant, so the nearest behaves the same.
            actual = min(probed, key=lambda value: (abs(value - actual), value))
        return actual == expected

    def value_changed(self, data_id: int, new: float) -> None:
        by_value = self.table.rules.get(data_id)
        if not by_value:
            return
        group = by_value.get(1 if new > 0 else 0) or next(iter(by_value.values()))
        rules, domains = group
        for conditions, shown, units, messages, parents in rules:
            if all(self._holds(domains, *condition) for condition in conditions):
                for target, value in shown:
                    if self.shown.get(target) != value:
                        self.visibility_changed = True
                    self.shown[target] = value
                self.units.update(units)
                for target, parent in parents:
                    if self.parents.get(target) != parent:
                        self.visibility_changed = True
                    self.parents[target] = parent
                self.messages.extend(messages)
                return

    def take_messages(self) -> list[str]:
        messages, self.messages = self.messages, []
        return messages


def compile_table(source: Path, target: Path, is_pinned: Callable[[int], bool]) -> None:
    """Precompile for this Python version, so connecting does not wait on parsing."""
    RulesTable.load(source, is_pinned).save_compiled(target)
