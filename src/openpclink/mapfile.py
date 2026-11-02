"""PCLink map files (.pclx, .pcl5): read, write, compare, and edit offline."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from . import crypto
from .database import Database
from .ecu import MASTER_PASSWORD_ID, SECRET_IDS, USER_PASSWORD_ID, Ecu, EcuInfo, EcuProtected, WriteRefused
from .layout import Location, TYPE_ARRAY, TYPE_STRING, TYPES_BITFIELD

_RUNTIME_MARKER = 0
_CONFIG_MARKER = 1
_FORMAT_VERSION = "100"


class MapError(Exception):
    pass


def _number_text(value: float) -> str:
    return f"{int(value)}" if float(value).is_integer() else f"{value:.15g}"


@dataclass
class MapEntry:
    data_id: int
    kind: int
    count: int
    multi: float
    offset: float
    read_only: bool
    can_writeable: bool
    values: list[float] = field(default_factory=list)
    text: str = ""
    rows: int | None = None
    columns: int | None = None


@dataclass
class MapFile:
    memory: dict[str, tuple[str, str]]
    entries: dict[int, MapEntry]

    @classmethod
    def load(cls, path: Path, key: tuple[int, ...]) -> "MapFile":
        return cls.parse(crypto.decode(Path(path).read_bytes(), key))

    @classmethod
    def parse(cls, xml_data: bytes) -> "MapFile":
        root = ET.fromstring(xml_data)
        memory_node, map_node = root.find("Memory"), root.find("Map")
        if memory_node is None or map_node is None:
            raise MapError("this is not a PCLink map file")
        memory = {node.tag: (node.get("Type", "3"), node.get("Value", "")) for node in memory_node}
        entries: dict[int, MapEntry] = {}
        for node in map_node:
            kind = int(node.get("Type"))
            rows = [row.get("Value", "") for row in node]
            flat = ",".join(rows) if rows else node.get("Value", "")
            data_id = int(node.get("DataId"))
            entries[data_id] = MapEntry(
                data_id=data_id,
                kind=kind,
                count=int(node.get("Count", "1")),
                multi=float(node.get("Multi", "1")),
                offset=float(node.get("Offset", "0")),
                read_only=node.get("ReadOnly") == "true",
                can_writeable=node.get("CanWriteable") == "true",
                values=[] if kind == TYPE_STRING else [float(item) for item in flat.split(",") if item != ""],
                text=flat if kind == TYPE_STRING else "",
                rows=int(node.get("RowCount")) if node.get("RowCount") else None,
                columns=int(node.get("ColCount")) if node.get("ColCount") else None,
            )
        return cls(memory, entries)

    @classmethod
    def from_ecu(cls, ecu: Ecu, database: Database) -> "MapFile":
        """Capture an ECU's current tune. This only reads what is already loaded."""
        if ecu.is_protected:
            raise EcuProtected("a password-protected ECU cannot be saved to a map file")
        info = ecu.info
        major = info.firmware.split(".")
        memory = {
            "Firmware": ("3", info.firmware),
            "Bootcode": ("3", info.boot),
            "HWRevision": ("3", ""),
            "CommsFirmware": ("3", ""),
            "CommsBootcode": ("3", ""),
            "CommsHWRevision": ("3", ""),
            "ECUMinSpec": ("3", f"{major[0]}:{major[1]}"),
            "LinkID": ("1", str(info.link_id)),
            "SerialNumber": ("1", str(info.serial)),
            "MaxID": ("1", str(info.max_id)),
            "Memo": ("3", ""),
        }
        entries = {}
        for data_id, loc in sorted(ecu.table.items()):
            if data_id == 0:
                continue
            runtime = loc.offset < info.runtime_size
            if data_id in (MASTER_PASSWORD_ID, USER_PASSWORD_ID):
                values, text = [0.0] * loc.count, ""
            elif data_id in SECRET_IDS:
                values, text = [0.0], ""
            elif loc.is_string and loc.width == 1:
                values, text = [], ecu.get_string(data_id)
            elif loc.is_bitfield:
                values, text = [ecu.get(data_id)], ""
            else:
                values, text = ecu.get_all(data_id), ""
            rows = columns = None
            if loc.kind == TYPE_ARRAY:
                parameter = database.parameter(data_id) if database.known(data_id) else None
                columns = parameter.columns if parameter and parameter.columns > 1 else loc.count
                rows = max(loc.count // max(columns, 1), 1)
            entries[data_id] = MapEntry(
                data_id=data_id, kind=loc.kind, count=loc.count, multi=loc.scale, offset=loc.add,
                read_only=runtime or loc.read_only, can_writeable=runtime,
                values=values, text=text, rows=rows, columns=columns,
            )
        return cls(memory, entries)

    def to_xml(self) -> bytes:
        lines = ['<?xml version="1.0"?>', f'<LinkStorage Version="{_FORMAT_VERSION}" Type=".xml">', "\t<Memory>"]
        for tag, (kind, value) in self.memory.items():
            lines.append(f'\t\t<{tag} Type="{kind}" Value="{_escape(value)}"/>')
        lines += ["\t</Memory>", "\t<Map>"]
        for data_id, entry in sorted(self.entries.items()):
            flags = (
                f'Count="{entry.count}" Multi="{entry.multi:.15g}" Offset="{entry.offset:.15g}" '
                f'ReadOnly="{str(entry.read_only).lower()}" CanWriteable="{str(entry.can_writeable).lower()}"'
            )
            if entry.kind == TYPE_ARRAY or entry.rows is not None:
                columns = entry.columns or max(entry.count, 1)
                rows = entry.rows or 1
                lines.append(
                    f'\t\t<D{data_id} DataId="{data_id}" Type="{entry.kind}" RowCount="{rows}" '
                    f'ColCount="{columns}" {flags}>'
                )
                for row in range(rows):
                    cells = entry.values[row * columns : (row + 1) * columns]
                    lines.append(f'\t\t\t<Row{row} Value="{",".join(map(_number_text, cells))}" />')
                lines.append(f"\t\t</D{data_id}>")
            else:
                value = entry.text if entry.kind == TYPE_STRING else _number_text(entry.values[0] if entry.values else 0)
                lines.append(
                    f'\t\t<D{data_id} DataId="{data_id}" Type="{entry.kind}" Value="{_escape(value)}" {flags}/>'
                )
        lines += ["\t</Map>", "</LinkStorage>", ""]
        return "\n".join(lines).encode("utf-8")

    def save(self, path: Path, key: tuple[int, ...]) -> None:
        Path(path).write_bytes(crypto.encode(self.to_xml(), key))


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


@dataclass(frozen=True)
class Difference:
    data_id: int
    cells: int
    first_a: object
    first_b: object


def compare(a: MapFile, b: MapFile) -> list[Difference]:
    """Settings that differ between two tunes. Live values are ignored."""
    found: list[Difference] = []
    for data_id, left in sorted(a.entries.items()):
        right = b.entries.get(data_id)
        if right is None or left.read_only or data_id in SECRET_IDS:
            continue
        if left.kind == TYPE_STRING:
            if left.text.rstrip("\0") != right.text.rstrip("\0"):
                found.append(Difference(data_id, 1, left.text, right.text))
            continue
        tolerance = abs(left.multi) / 2 if left.multi else 1e-6
        pairs = list(zip(left.values, right.values))
        changed = [(x, y) for x, y in pairs if abs(x - y) > tolerance]
        if changed or len(left.values) != len(right.values):
            first = changed[0] if changed else (len(left.values), len(right.values))
            found.append(Difference(data_id, len(changed) or 1, first[0], first[1]))
    return found


class OfflineEcu:
    """Edits a map file through the same interface as a live session."""

    is_protected = False

    def __init__(self, map_file: MapFile, path: Path | None = None) -> None:
        self.map = map_file
        self.path = path
        self._dirty = False
        self.table: dict[int, Location] = {}
        for data_id, entry in map_file.entries.items():
            bitfield = entry.kind in TYPES_BITFIELD
            self.table[data_id] = Location(
                data_id, entry.kind, _RUNTIME_MARKER if entry.read_only else _CONFIG_MARKER,
                width=1 if entry.kind == TYPE_STRING else (0 if bitfield else 2),
                count=entry.count, scale=entry.multi, add=entry.offset,
                read_only=entry.read_only, mask=0xFF if bitfield else 0,
            )
        memory = map_file.memory
        self.info = EcuInfo(
            firmware=memory.get("Firmware", ("", "?"))[1], boot=memory.get("Bootcode", ("", "?"))[1],
            hardware_family=0, serial=int(memory.get("SerialNumber", ("", "0"))[1] or 0),
            link_id=int(memory.get("LinkID", ("", "0"))[1] or 0), max_id=int(memory.get("MaxID", ("", "0"))[1] or 0),
            runtime_size=1, config_size=1, section2_size=0,
        )

    @property
    def has_unstored_changes(self) -> bool:
        return self._dirty

    def location(self, data_id: int) -> Location:
        return self.table[data_id]

    def _entry(self, data_id: int) -> MapEntry:
        if data_id in SECRET_IDS:
            raise EcuProtected(f"parameter {data_id} is reserved for password protection")
        return self.map.entries[data_id]

    def get(self, data_id: int, index: int = 0) -> float:
        values = self._entry(data_id).values
        return values[index] if index < len(values) else 0.0

    def get_all(self, data_id: int) -> list[float]:
        return list(self._entry(data_id).values)

    def get_string(self, data_id: int) -> str:
        return self._entry(data_id).text

    def set(self, data_id: int, value: float, index: int = 0) -> float:
        entry = self._entry(data_id)
        if entry.read_only:
            raise WriteRefused(f"parameter {data_id} is read-only")
        if not 0 <= index < max(len(entry.values), 1):
            raise WriteRefused(f"parameter {data_id} has no element {index}")
        if not entry.values:
            entry.values = [0.0]
        entry.values[index] = float(value)
        self._dirty = True
        return entry.values[index]

    def set_string(self, data_id: int, text: str) -> str:
        entry = self._entry(data_id)
        if entry.read_only:
            raise WriteRefused(f"parameter {data_id} is read-only")
        if len(text) > entry.count:
            raise WriteRefused(f"text is longer than the {entry.count} characters allowed")
        entry.text = text
        self._dirty = True
        return text

    def poll_runtime(self) -> None:
        pass

    def reload(self) -> None:
        pass

    def save(self, path: Path, key: tuple[int, ...]) -> None:
        self.map.save(path, key)
        self.path = Path(path)
        self._dirty = False

    def close(self) -> None:
        pass



