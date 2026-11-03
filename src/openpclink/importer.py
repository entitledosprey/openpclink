"""Builds the local parameter database from a user's copy of PCLink.

Link's parameter database, strings and file keys are not part of this project.
They are read once from the user's own PCLink installer or install folder.
"""
from __future__ import annotations

import gzip
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from . import crypto

# PCLink keeps this string directly before its three file keys.
_KEY_MARKER = b"gfkkdfgdbiub3492jsdohdfs"
_KEYS_OFFSET = 0x24


class ImportFailed(Exception):
    pass


def default_db_path() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "openpclink" / "pclink-data.json.gz"


def bundled_db_path() -> Path | None:
    """A database baked into a local build, if there is one."""
    candidates = [Path(__file__).parent / "data" / "pclink-data.json.gz"]
    if getattr(sys, "_MEIPASS", None):
        candidates.insert(0, Path(sys._MEIPASS) / "openpclink" / "data" / "pclink-data.json.gz")
    return next((path for path in candidates if path.exists()), None)


def find_keys(exe: bytes) -> list[tuple[int, ...]]:
    at = exe.find(_KEY_MARKER)
    if at < 0:
        raise ImportFailed("this PCLink.exe has an unrecognised format")
    start = at + _KEYS_OFFSET
    return [struct.unpack_from("<4I", exe, start + i * 16) for i in range(3)]


def _decode_with_any(data: bytes, keys: list[tuple[int, ...]]) -> tuple[bytes, tuple[int, ...]]:
    for key in keys:
        try:
            return crypto.decode(data, key), key
        except crypto.BadContainer:
            continue
    raise ImportFailed("could not decode a PCLink data file with this version's keys")


def _parse_strings(text: str) -> dict[int, str]:
    strings: dict[int, str] = {}
    for line in text.split("\r\n")[3:]:
        number, _, rest = line.partition(",")
        _, _, value = rest.partition(",")
        if number.isdigit():
            strings[int(number)] = value
    return strings


def _parse_parameters(xml_text: bytes) -> dict[str, dict]:
    root = ET.fromstring(xml_text)
    params: dict[str, dict] = {}
    for node in root.find("Parameters"):
        props = {q.get("Name"): q.get("Value") for q in node.iter("Property")}
        params[node.get("DataId")] = {"name": node.get("Name"), "props": props}
    return params


def _locate(source: Path) -> tuple[Path, Path | None]:
    """Return the PCLink program folder, and a temp dir to clean up if any."""
    if source.is_dir():
        for folder in (source, source / "app" / "PCLink G5", source / "PCLink G5"):
            if (folder / "ECU.cfgr").exists():
                return folder, None
        raise ImportFailed(f"no ECU.cfgr found under {source}")
    if shutil.which("innoextract") is None:
        raise ImportFailed(
            "Reading the installer needs innoextract (macOS: brew install innoextract; "
            "Linux: apt install innoextract). You can also point at an installed PCLink folder."
        )
    temp = Path(tempfile.mkdtemp(prefix="openpclink-import-"))
    done = subprocess.run(
        ["innoextract", "-q", "-e", "-d", str(temp), str(source)], capture_output=True, text=True
    )
    folder = temp / "app" / "PCLink G5"
    if done.returncode != 0 or not (folder / "ECU.cfgr").exists():
        shutil.rmtree(temp, ignore_errors=True)
        raise ImportFailed(f"{source} does not look like a PCLink installer")
    return folder, temp


def import_pclink(source: Path, destination: Path | None = None) -> Path:
    destination = destination or default_db_path()
    folder, temp = _locate(source)
    try:
        keys = find_keys((folder / "PCLink.exe").read_bytes())
        ecu_xml, map_key = _decode_with_any((folder / "ECU.cfgr").read_bytes(), keys)
        strings_raw, _ = _decode_with_any((folder / "Languages.cfgr").read_bytes(), keys)
        database = {
            "version": 1,
            "parameters": _parse_parameters(ecu_xml),
            "strings": _parse_strings(strings_raw.decode("utf-16")),
            "map_key": list(map_key),
        }
        destination.parent.mkdir(parents=True, exist_ok=True)
        _extract_help(folder, destination.with_name("pclink-help.json.gz"))
    finally:
        if temp:
            shutil.rmtree(temp, ignore_errors=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(destination, "wt", encoding="utf-8") as out:
        json.dump(database, out)
    return destination


def default_help_path() -> Path:
    return default_db_path().with_name("pclink-help.json.gz")


def bundled_help_path() -> Path | None:
    bundled = bundled_db_path()
    if bundled is None:
        return None
    path = bundled.with_name("pclink-help.json.gz")
    return path if path.exists() else None


def _extract_help(folder: Path, destination: Path) -> bool:
    """Convert the CHM manual's pages to a small searchable file (text only, no images)."""
    chm = folder / "Help_File_ENG.chm"
    tool = shutil.which("7z") or shutil.which("7zz") or shutil.which("7za")
    if tool is None or not chm.exists():
        return False
    with tempfile.TemporaryDirectory(prefix="openpclink-help-") as temp:
        done = subprocess.run([tool, "x", "-y", f"-o{temp}", str(chm)], capture_output=True)
        if done.returncode != 0:
            return False
        pages = {}
        for page in Path(temp).rglob("*.htm*"):
            html = page.read_bytes().decode("utf-8", "replace")
            pages[page.stem.lower()] = _clean_help_page(html)
    if not pages:
        return False
    with gzip.open(destination, "wt", encoding="utf-8") as out:
        json.dump(pages, out)
    return True


def _clean_help_page(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", "", html)
    html = re.sub(r"(?is)<img\b[^>]*>", "", html)
    html = re.sub(r'(?is)<div id="header".*?</div>\s*</div>', "", html, count=1)
    return html.strip()


def rules_table_path() -> Path | None:
    """The generated rules table, if this build or install has one."""
    bundled = bundled_db_path()
    for path in (default_db_path().with_name("pclink-rules.json.gz"), bundled and bundled.with_name("pclink-rules.json.gz")):
        if path and path.exists():
            return path
    return None
