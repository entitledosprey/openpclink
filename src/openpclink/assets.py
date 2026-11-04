"""Files read from the user's own copy of PCLink: layouts, display limits, CAN setups and so on."""
from __future__ import annotations

import shutil
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from .importer import bundled_db_path, default_db_path

FOLDERS = ("Layouts", "CAN", "Calibrations", "Firmware", "Sample Maps")
FILES = ("DefaultParameterConfig.rtcr", "LoggingOptions.xml", "RuntimeViewLayout", "TSViewlayout", "TSViewlayoutM")


def assets_dir() -> Path:
    return default_db_path().with_name("pclink-assets")


_override: Path | None = None


def use_root(path: Path) -> None:
    """Read assets from beside a database loaded from an explicit path."""
    global _override
    _override = path


def _roots() -> list[Path]:
    if _override is not None:
        return [_override]
    roots = [assets_dir()]
    bundled = bundled_db_path()
    if bundled:
        roots.append(bundled.with_name("pclink-assets"))
    return roots


def asset(*parts: str) -> Path | None:
    for root in _roots():
        path = root.joinpath(*parts)
        if path.exists():
            return path
    return None


def layout_path(name: str) -> Path | None:
    return asset("Layouts", f"{name}.llf5")


def user_config_path() -> Path:
    """The owner's edits to parameter display settings, kept apart from what was imported."""
    return default_db_path().with_name("DefaultParameterConfig.rtcr")


def data_gaps(database) -> list[str]:
    """What an older import left out. Importing again from the PCLink installer fills these in."""
    gaps = []
    if not database.firmware_key:
        gaps.append("firmware key (Firmware Update cannot check images)")
    if not database.stream_key or not database.file_key:
        gaps.append("CAN file keys (CAN stream and custom CAN files cannot be opened)")
    if asset("Layouts") is None:
        gaps.append("PCLink's own screen layouts")
    if asset("Firmware") is None:
        gaps.append("the firmware package")
    return gaps


def copy_assets(folder: Path, destination: Path | None = None) -> None:
    """Keep these beside the database, so the installer or PCLink folder is not needed again."""
    destination = destination or assets_dir()
    destination.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        if (folder / name).exists():
            shutil.copy2(folder / name, destination / name)
    for name in FOLDERS:
        if (folder / name).is_dir():
            shutil.copytree(folder / name, destination / name, dirs_exist_ok=True)
    from .logviews import extract_plan_images

    extract_plan_images(folder / "PCLink.exe", destination / "VehiclePlan")


@dataclass(frozen=True)
class ParameterDisplay:
    colour: int = 0
    minimum: float | None = None
    maximum: float | None = None
    high_error: float | None = None
    high_warning: float | None = None
    low_warning: float | None = None
    low_error: float | None = None
    auto_scale: bool = True


class RuntimeConfig:
    """PCLink's display range, warning limits and colour for each live value."""

    def __init__(self, entries: dict[int, ParameterDisplay] | None = None) -> None:
        self.entries = entries or {}

    @classmethod
    def load(cls, path: Path | None = None) -> "RuntimeConfig":
        user = user_config_path()
        path = path or (user if user.exists() else asset("DefaultParameterConfig.rtcr"))
        if path is None:
            return cls()
        try:
            root = ET.parse(path).getroot()
        except (OSError, ET.ParseError):
            return cls()
        entries = {}
        for node in root.iter("Parameter"):
            props = {p.get("Name"): p.get("Value") for p in node.iter("Property")}

            def number(name: str) -> float | None:
                try:
                    return float(props[name])
                except (KeyError, TypeError, ValueError):
                    return None

            entries[int(node.get("DataId", "0"))] = ParameterDisplay(
                colour=int(number("Colour") or 0), minimum=number("DisplayMin"), maximum=number("DisplayMax"),
                high_error=number("HighError"), high_warning=number("HighWarning"),
                low_warning=number("LowWarning"), low_error=number("LowError"),
                auto_scale=props.get("LogAutoScaling") != "false",
            )
        return cls(entries)

    def get(self, data_id: int) -> ParameterDisplay:
        return self.entries.get(data_id, ParameterDisplay())

    def save(self, path: Path) -> None:
        root = ET.Element("LinkStorage", {"Version": "100", "Type": ".xml"})
        parameters = ET.SubElement(root, "Parameters")
        for data_id, entry in sorted(self.entries.items()):
            node = ET.SubElement(parameters, "Parameter", {"DataId": str(data_id), "Name": str(data_id)})
            properties = ET.SubElement(node, "Properties")
            ET.SubElement(properties, "Property", {"Name": "Colour", "Type": "1", "Value": str(entry.colour)})
            for name, value in (("DisplayMax", entry.maximum), ("DisplayMin", entry.minimum), ("HighError", entry.high_error), ("HighWarning", entry.high_warning), ("LowWarning", entry.low_warning), ("LowError", entry.low_error)):
                if value is not None:
                    ET.SubElement(properties, "Property", {"Name": name, "Type": "2", "Value": f"{value:g}"})
            ET.SubElement(properties, "Property", {"Name": "LogAutoScaling", "Type": "4", "Value": "true" if entry.auto_scale else "false"})
        ET.indent(root, "\t")
        Path(path).write_bytes(b'<?xml version="1.0"?>\r\n' + ET.tostring(root, encoding="utf-8"))
