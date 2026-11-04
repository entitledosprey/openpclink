"""Firmware packages and programming. A wrong step here can stop an ECU working.

Everything is checked before the ECU is touched, more strictly than PCLink does: a
file failing any check is refused. See docs/PROTOCOL.md for how this was recovered and
for what has only been run against the simulator.
"""
from __future__ import annotations

import datetime
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import protocol as p

MIN_HEADER = 36
CHUNK = 1024
FIRST_CHUNK_TIMEOUT = 30.0
CHUNK_TIMEOUT = 1.5
ERROR_RETRY_CHUNK = 2
ERROR_ENGINE_RUNNING = 4
CHUNK_ATTEMPTS = 4
ENTER_BOOT_ATTEMPTS = 30
STATE_RUNNING, STATE_SAFE, STATE_BOOT, STATE_TRAP = 1, 2, 3, 4
COMMS_DEVICES = (150, 155)
MAIN_DEVICE_MINIMUM = 1000
COMMS_ADDRESS_MINIMUM = 0x08000000
BASE_TIMING_CHANGE = (6, 20, 0)
OLDEST_ECU = (5, 0, 0)
COMMS_PRODUCT = 1007
FIRST_COMMS_PRODUCT = 1012
NO_PRODUCT_ID = 0
LEAVE_BOOT_COMMS_WAIT_S = 2.0
LEAVE_BOOT_POLL_S = 0.3
ENTER_BOOT_REOPEN_WAIT_S = 0.2


class FirmwareError(Exception):
    pass


class EngineRunning(FirmwareError):
    pass


class VersionMismatch(FirmwareError):
    """The ECU started, but reports a different version from the image it was given."""


class UpdateCancelled(FirmwareError):
    """The user chose to stop before anything was programmed."""


@dataclass(frozen=True)
class FirmwareImage:
    image_id: int
    device: int
    version: tuple[int, int, int]
    address: int
    header: bytes
    body: bytes

    @property
    def is_comms(self) -> bool:
        return self.device in COMMS_DEVICES or self.address >= COMMS_ADDRESS_MINIMUM


@dataclass
class FirmwarePackage:
    version: tuple[int, int, int]
    entries: dict[int, list[int]] = field(default_factory=dict)
    images: dict[int, FirmwareImage] = field(default_factory=dict)
    path: Path | None = None

    def images_for(self, product_id: int) -> list[FirmwareImage]:
        return [self.images[i] for i in self.entries.get(product_id, [])]


def _check_header(header: bytes) -> tuple[tuple[int, int, int], int, int, int]:
    if len(header) < MIN_HEADER:
        raise FirmwareError("the firmware image header is too short")
    if p.crc16(header[:2], p.crc16(header[2:])) != 0:
        raise FirmwareError("the firmware image header failed its CRC check")
    if struct.unpack_from(">H", header, 6)[0] != 1:
        raise FirmwareError("this block is not a firmware image")
    version = (header[8], *struct.unpack_from(">HH", header, 9))
    address, words, checksum = struct.unpack_from(">III", header, 20)
    return version, address, words, checksum


def _check_body(body: bytes, checksum: int, key: bytes) -> None:
    if len(key) != 8:
        raise FirmwareError("this install has no firmware key. Import PCLink again to update firmware.")
    if len(body) % 4:
        raise FirmwareError("the firmware image is not a whole number of words")
    repeated = (key * (len(body) // 8 + 1))[: len(body)]
    plain = (int.from_bytes(body, "big") ^ int.from_bytes(repeated, "big")).to_bytes(len(body), "big")
    total = sum(struct.unpack(f">{len(plain) // 4}I", plain)) & 0xFFFFFFFF
    if (-total) & 0xFFFFFFFF != checksum:
        raise FirmwareError("the firmware image failed its checksum")


def _read(data: bytes, at: int, count: int) -> bytes:
    if at + count > len(data):
        raise FirmwareError("the firmware file ends early")
    return data[at : at + count]


def _read_image_block(data: bytes, at: int, key: bytes) -> tuple[bytes, bytes, tuple, int, int]:
    size_field = _read(data, at, 6)
    header_size = struct.unpack_from(">H", size_field, 4)[0]
    header = _read(data, at, header_size)
    version, address, words, checksum = _check_header(header)
    body = _read(data, at + header_size, words * 4)
    _check_body(body, checksum, key)
    return header, body, version, address, at + header_size + words * 4


def parse_package(data: bytes, key: bytes) -> FirmwarePackage:
    if len(data) < 24:
        raise FirmwareError("this is not a firmware package")
    format_, major, minor, patch, _reserved, entry_count, image_count = struct.unpack_from("<7I", data, 0)
    if format_ != 1:
        raise FirmwareError(f"unknown firmware package format {format_}")
    package = FirmwarePackage((major, minor, patch))
    at = 28
    for _ in range(entry_count):
        entry_format, product, count = struct.unpack("<3I", _read(data, at, 12))
        if entry_format != 1:
            raise FirmwareError("unknown firmware package entry format")
        at += 12
        package.entries[product] = list(struct.unpack(f"<{count}I", _read(data, at, count * 4)))
        at += count * 4
    for _ in range(image_count):
        image_format, image_id, device, i_major, i_minor, i_patch, _reserved, total = struct.unpack("<8I", _read(data, at, 32))
        if image_format != 1:
            raise FirmwareError("unknown firmware image format")
        at += 32
        header, body, version, address, end = _read_image_block(data, at, key)
        if end - at != total:
            raise FirmwareError("a firmware image's size does not match its record")
        if version != (i_major, i_minor, i_patch):
            raise FirmwareError("a firmware image's version does not match its record")
        package.images[image_id] = FirmwareImage(image_id, device, version, address, header, body)
        at = end
    if at != len(data):
        raise FirmwareError("the firmware file has unexpected data at its end")
    for ids in package.entries.values():
        if any(i not in package.images for i in ids):
            raise FirmwareError("the firmware package names an image it does not contain")
    return package


def parse_image_file(data: bytes, key: bytes) -> FirmwareImage:
    header, body, version, address, end = _read_image_block(data, 0, key)
    if end != len(data):
        raise FirmwareError("the firmware file has unexpected data at its end")
    return FirmwareImage(0, 0, version, address, header, body)


def load(path: Path, key: bytes) -> FirmwarePackage:
    """A package, or a bare image wrapped as a package with no product table."""
    data = Path(path).read_bytes()
    if Path(path).suffix.lower() == ".img":
        image = parse_image_file(data, key)
        package = FirmwarePackage(image.version, images={0: image})
    else:
        package = parse_package(data, key)
    package.path = Path(path)
    return package


Version = tuple[int, int, int]


def version_text(version: Version | None) -> str:
    return ".".join(map(str, version)) if version else "0.0.0"


def build_time_text(header: bytes) -> str:
    """The build time in a header, as PCLink's log shows it."""
    when = datetime.datetime.fromtimestamp(struct.unpack_from(">I", header, 16)[0], datetime.timezone.utc)
    return f"{when.month}/{when.day}/{when.year} {when.hour % 12 or 12}:{when:%M:%S} {'AM' if when.hour < 12 else 'PM'}"


def verdict(new: Version, current: Version | None) -> str:
    if current is None or current == (0, 0, 0):
        return "Upgrade"
    if new[0] != current[0]:
        return "Incompatible"
    return "Upgrade" if new > current else "Downgrade" if new < current else "Reinstall"


PRIORITY = {"Incompatible": 3, "Upgrade": 2, "Downgrade": 1, "Reinstall": 0}


def needs_base_timing_check(old: Version, new: Version) -> bool:
    return (old < BASE_TIMING_CHANGE) != (new < BASE_TIMING_CHANGE)


def _read_state(link: p.Link) -> int:
    try:
        return struct.unpack(">H", link.request(p.CMD_STATE)[:2])[0]
    except (p.ProtocolError, struct.error):
        return 0


def _state(link: p.Link, sleep=time.sleep) -> int:
    """The ECU's state, 0 if unreadable. PCLink reads a boot state twice, 50 ms apart, and uses the second."""
    state = _read_state(link)
    if state == STATE_BOOT:
        sleep(0.05)
        state = _read_state(link)
    return state


# ---- what is connected -------------------------------------------------------

@dataclass
class EcuTarget:
    """What the updater knows about the ECU, read without the normal connect, so it works in boot mode."""

    product: int = NO_PRODUCT_ID
    main: Version | None = None
    comms: Version | None = None
    main_state: int = 0
    comms_in_boot: bool = False
    log: list[str] = field(default_factory=list)

    @property
    def in_boot(self) -> bool:
        return self.main_state == STATE_BOOT or self.comms_in_boot

    @property
    def has_comms(self) -> bool:
        return self.in_boot or self.product == COMMS_PRODUCT or self.product >= FIRST_COMMS_PRODUCT

    def version_for(self, image: FirmwareImage) -> Version | None:
        return self.comms if image.is_comms else self.main


def _info_block(link: p.Link, cmd: int, block_type: int) -> bytes | None:
    """0x16 or 0x18, checked as PCLink checks them: own length, header CRC, block type."""
    try:
        body = link.request(cmd)
    except p.ProtocolError:
        return None
    if len(body) < MIN_HEADER or struct.unpack_from(">H", body, 4)[0] != len(body):
        return None
    if p.crc16(body[:2], p.crc16(body[2:])) != 0 or struct.unpack_from(">H", body, 6)[0] != block_type:
        return None
    return body


def _version_of(body: bytes | None) -> Version | None:
    if body is None:
        return None
    return (body[8], *struct.unpack_from(">HH", body, 9))


def read_target(link: p.Link, state: int = 0, comms_in_boot: bool = False) -> EcuTarget:
    """0x16 then 0x18 at the main address, then the comms address. A failed read is version 0, as in PCLink."""
    target = EcuTarget(main_state=state, comms_in_boot=comms_in_boot)
    products = {}
    try:
        for name, addr in (("ECU", p.ADDR_MAIN), ("Comms", p.ADDR_COMMS)):
            link.addr = addr
            firmware = _info_block(link, p.CMD_FIRMWARE_INFO, 1)
            boot = _info_block(link, p.CMD_BOOT_INFO, 0)
            version = _version_of(firmware)
            if name == "ECU":
                target.main = version
            else:
                target.comms = version
            if firmware is None and boot is None:
                target.log.append(f"{name} Firmware information not available")
                continue
            products[name] = struct.unpack_from(">H", boot, 72)[0] if boot is not None and len(boot) >= 74 else NO_PRODUCT_ID
            target.log.append(f"{name} Firmware V{version_text(version)} found")
            if boot is not None:
                target.log.append(f"{name} Bootloader V{version_text(_version_of(boot))} found")
                hardware = struct.unpack_from(">HH", boot, 78) if len(boot) >= 82 else (0, 0)
                target.log.append(f"{name} Hardware V{hardware[0]}.{hardware[1]} Serial Number {struct.unpack_from('>I', boot, 68)[0]} found")
    finally:
        link.addr = p.ADDR_MAIN
    target.product = products.get("ECU") or products.get("Comms") or NO_PRODUCT_ID
    return target


def check_state(link: p.Link, log: Callable[[str], None], confirm_trap: Callable[[], bool], retry: Callable[[], bool], sleep=time.sleep) -> tuple[int, bool]:
    """PCLink's check before an update: the main state, else the comms state. Returns (state, comms in boot)."""
    while True:
        link.addr = p.ADDR_MAIN
        state = _state(link, sleep)
        if state == STATE_RUNNING:
            log("ECU is running in........NORMAL MODE")
        elif state == STATE_SAFE:
            log("ECU is running in........SAFE MODE\n\nLast update was unsuccesful or memory is corrupted.\n"
                "Please contact your ECU dealer for further advice.")
        elif state == STATE_BOOT:
            log("ECU is running in........BOOT MODE\n\nWarning: Last update was not complete. "
                "Engine will not start in this mode. Continue with update to boot main code.")
        elif state == STATE_TRAP:
            if not confirm_trap():
                raise UpdateCancelled("ECU is running in.....TRAP MODE")
        else:
            link.addr = p.ADDR_COMMS
            try:
                comms_state = _state(link, sleep)
            finally:
                link.addr = p.ADDR_MAIN
            if comms_state == STATE_BOOT:
                return state, True
            log(NO_CONNECTION_TEXT)
            if not retry():
                raise UpdateCancelled("Failed to connect to hardware")
            continue
        return state, False


# ---- what an update would do --------------------------------------------------

@dataclass
class Selection:
    button: str
    message: str
    colour: str
    enabled: bool
    base_timing: bool = False
    verdicts: list[tuple[FirmwareImage, str]] = field(default_factory=list)

    @property
    def text(self) -> str:
        return self.message + (f"\n{BASE_TIMING_TEXT}" if self.base_timing else "")


NO_CONNECTION_TEXT = (
    "Error:  Could not connect to ECU.\n\nSolutions:\n1. Check correct serial port (or auto) is selected in Options Connections menu.\n"
    "2. Cycle power to ECU and try again.\n3. Make sure ECU is powered up i.e. ignition key on.\n"
    "4. Serial/USB connector is plugged into both PC and ECU.\n"
    "5. Make sure no other programs are currently using the same serial port to communicate to the ECU.\n"
    "6. If using USB connection then make sure drivers are installed and are working correctly."
)
BASE_TIMING_TEXT = "The base timing must be verified after this firmware change!"
NO_IMAGES_TEXT = "No Firmware images in this package for the connected device."
TOO_OLD_TEXT = "ECU hardware is too old for this software"
G5_ECU_G4X_FILE = "You have a G5 ECU and have selected a G4X Firmware Package.\n Please Select a G5 Firmware (V7...)"
G4X_ECU_G5_FILE = "You have a G4X ECU and have selected a G5 Firmware Package.\n Please Select a G4X Firmware (V6...)"
NOT_MAIN_IMAGE_TEXT = "PCLink only offers an image file for the main processor. Use a firmware package for anything else."
NO_COMMS_TEXT = "You have selected a Communications firmware image but your ECU does not have a separate Communications device."
IMAGE_TEXT = {
    ("Upgrade", False): "Upgrading\nfrom: %s to %s", ("Downgrade", False): "You are downgrading your\n ECU firmware from:\n %s to %s",
    ("Reinstall", False): "ECU firmware matches update version\nThere is no need to upgrade from:\n %s to %s",
    ("Upgrade", True): "Upgrading Communications Firmware from:\n %s to %s",
    ("Downgrade", True): "You are downgrading your Communications firmware from:\n %s to %s",
    ("Reinstall", True): "Communications firmware matches update version\nThere is no need to upgrade from:\n %s to %s",
}
COLOURS = {"Upgrade": "green", "Downgrade": "red", "Reinstall": "grey"}


def describe(package: FirmwarePackage, images: list[FirmwareImage], target: EcuTarget, bare_image: bool) -> Selection:
    """The button and message PCLink's updater shows for a selected file."""
    if target.main and target.main < OLDEST_ECU:
        return Selection("Upgrade", TOO_OLD_TEXT, "red", False)
    if not images:
        return Selection("Upgrade", NO_IMAGES_TEXT, "grey", False)
    verdicts = []
    for image in images:
        if image.is_comms and not target.has_comms:
            return Selection("Upgrade", NO_COMMS_TEXT, "red", False)
        verdicts.append((image, verdict(image.version, target.version_for(image))))
    old_main = target.main
    if bare_image:
        image, word = verdicts[0]
        major = image.version[0]
        if major == 6 and old_main and old_main[0] == 7:
            return Selection("Upgrade", G5_ECU_G4X_FILE, "red", False)
        if major == 7 and old_main and old_main[0] == 6 and not (target.product in (1007, 1011) or target.product >= FIRST_COMMS_PRODUCT):
            return Selection("Upgrade", G4X_ECU_G5_FILE, "red", False)
        if major <= 5 or image.address >= COMMS_ADDRESS_MINIMUM:
            return Selection("Upgrade", NOT_MAIN_IMAGE_TEXT, "red", False)
        if word == "Incompatible":
            return Selection("Upgrade", "This firmware does not fit this ECU.", "red", False, verdicts=verdicts)
        old = target.version_for(image)
        timing = bool(old and needs_base_timing_check(old, image.version))
        message = IMAGE_TEXT[(word, image.is_comms)] % (version_text(old), version_text(image.version))
        return Selection(word, message, COLOURS[word], True, timing, verdicts)
    overall = max((word for _, word in verdicts), key=PRIORITY.__getitem__)
    notes = [f"Firmware Package Version: {version_text(package.version)}."]
    for image, _ in verdicts:
        notes.append(f"Package {'Comms' if image.is_comms else 'ECU'} Firmware Version: {version_text(image.version)}.")
    timing = bool(old_main and old_main < BASE_TIMING_CHANGE)
    if overall == "Incompatible":
        return Selection("Upgrade", "\n".join(notes), "grey", False, verdicts=verdicts)
    return Selection(overall, "\n".join(notes), COLOURS[overall], True, timing, verdicts)


# ---- programming ---------------------------------------------------------------

Reopen = Callable[[float], None]


def _reopen(link: p.Link, reopen: Reopen | None, wait: float, sleep) -> None:
    """Close and reopen the port, as PCLink does around every comms reset, then read the state."""
    if reopen:
        reopen(wait)
    else:
        sleep(wait)
    _state(link, sleep)


def _enter_boot(link: p.Link, reopen: Reopen | None, sleep) -> None:
    for _ in range(ENTER_BOOT_ATTEMPTS):
        link.send_only(p.CMD_STATE)
        link.send_only(p.CMD_STATE)
        if _state(link, sleep) == STATE_BOOT:
            return
        try:
            reset = False
            try:
                link.request_dangerous(p.CMD_FW_RESET)
                reset = True
            finally:
                if link.addr == p.ADDR_COMMS:
                    _reopen(link, reopen, ENTER_BOOT_REOPEN_WAIT_S, sleep)
            if reset:
                link.request_dangerous(p.CMD_FW_REQUEST_BOOT)
        except p.EcuError as exc:
            if exc.code == ERROR_ENGINE_RUNNING:
                raise EngineRunning("ECU Update failed with error:\nThe engine is running") from exc
        except p.ProtocolError:
            pass
        sleep(0.05)
    raise FirmwareError("The ECU did not enter boot mode.")


def _send_body(link: p.Link, image: FirmwareImage, progress: Callable[[int, int], None]) -> None:
    sent = 0
    timeout = FIRST_CHUNK_TIMEOUT
    while sent < len(image.body):
        chunk = image.body[sent : sent + CHUNK]
        payload = struct.pack(">II", image.address + sent, len(chunk) >> 2) + chunk
        for attempt in range(1, CHUNK_ATTEMPTS + 1):
            try:
                link.request_dangerous(p.CMD_FW_DATA, payload, timeout)
                break
            except p.EcuError as exc:
                if exc.code != ERROR_RETRY_CHUNK or attempt == CHUNK_ATTEMPTS:
                    raise
        sent += len(chunk)
        timeout = CHUNK_TIMEOUT
        progress(sent, len(image.body))


MODE_NAMES = {STATE_BOOT: "Boot mode", STATE_TRAP: "Trap mode"}


def program_image(
    link: p.Link, image: FirmwareImage, reopen: Reopen | None = None,
    progress: Callable[[int, int], None] = lambda _sent, _total: None, sleep=time.sleep,
    log: Callable[[str], None] = lambda _text: None, verb: str = "Upgrading",
) -> int:
    """Program one image and return the state the ECU then reports.

    Every timeout is passed with its command, so nothing is left set when this fails, and
    the link goes back to the main address however it ends."""
    link.addr = p.ADDR_COMMS if image.is_comms else p.ADDR_MAIN
    try:
        log(f"{verb} {'Comms' if image.is_comms else 'ECU'} Firmware to : {version_text(image.version)}. ({build_time_text(image.header)})")
        _enter_boot(link, reopen, sleep)
        log("PROGRAMMING HAS STARTED......please wait.")
        link.request_dangerous(p.CMD_FW_START)
        sleep(0.2)
        link.request_dangerous(p.CMD_FW_HEADER, image.header, 1.0)
        _send_body(link, image, progress)
        if image.is_comms:
            link.request_dangerous(p.CMD_FW_RESET, expect_reply=False)
            link.request_dangerous(p.CMD_FW_RESET)
            _reopen(link, reopen, LEAVE_BOOT_COMMS_WAIT_S, sleep)
        else:
            link.request_dangerous(p.CMD_FW_REQUEST_MAIN, expect_reply=False)
        state = 0
        for _ in range(3):
            sleep(LEAVE_BOOT_POLL_S)
            state = _state(link, sleep)
            if STATE_RUNNING <= state <= STATE_TRAP:
                break
        if state not in (STATE_RUNNING, STATE_SAFE):
            raise FirmwareError(f"Request maincode failed, ECU reported Mode: {MODE_NAMES.get(state, 'Unknown mode')}")
        return state
    finally:
        link.addr = p.ADDR_MAIN


def verify_versions(link: p.Link, images: list[FirmwareImage], log: Callable[[str], None] = lambda _text: None) -> EcuTarget:
    """Read the ECU again and compare what it reports with what was programmed. PCLink only logs it."""
    target = read_target(link)
    for line in target.log:
        log(line)
    wrong = []
    for image in images:
        found = target.version_for(image)
        if found != image.version:
            wrong.append(f"{'Comms' if image.is_comms else 'ECU'} firmware reports {version_text(found) if found else 'nothing'} but {version_text(image.version)} was programmed")
    if wrong:
        raise VersionMismatch("Programming finished, but " + "; ".join(wrong) + ". Do not rely on this ECU: run the update again.")
    return target


FAILED_TEXT = (
    "ECU Update failed !!\nSomehow the update had been interrupted.\nThis ECU will be disabled until the software update is complete.\n"
    "This is to ensure that ECU firmware has no errors.\nPlease cycle power to ECU and re-run this update software"
)
