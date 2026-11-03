"""Firmware packages and programming. A wrong step here can stop an ECU working.

Everything is checked before the ECU is touched, more strictly than PCLink does: a
file failing any check is refused. See docs/PROTOCOL.md for how this was recovered and
for what has only been run against the simulator.
"""
from __future__ import annotations

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


class FirmwareError(Exception):
    pass


class EngineRunning(FirmwareError):
    pass


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


def verdict(new: tuple[int, int, int], current: tuple[int, int, int] | None) -> str:
    if current is None or current == (0, 0, 0):
        return "Upgrade"
    if new[0] != current[0]:
        return "Incompatible"
    return "Upgrade" if new > current else "Downgrade" if new < current else "Reinstall"


def needs_base_timing_check(old: tuple[int, int, int], new: tuple[int, int, int]) -> bool:
    return (old < BASE_TIMING_CHANGE) != (new < BASE_TIMING_CHANGE)


def _state(link: p.Link) -> int:
    try:
        return struct.unpack(">H", link.request(p.CMD_STATE)[:2])[0]
    except (p.ProtocolError, struct.error):
        return 0


def _enter_boot(link: p.Link, reopen: Callable[[], None] | None, sleep) -> None:
    for _ in range(ENTER_BOOT_ATTEMPTS):
        link.send_only(p.CMD_STATE)
        link.send_only(p.CMD_STATE)
        if _state(link) == STATE_BOOT:
            sleep(0.05)
            if _state(link) == STATE_BOOT:
                return
        try:
            link.request_dangerous(p.CMD_FW_RESET)
            if reopen:
                reopen()
                sleep(0.2)
            link.request_dangerous(p.CMD_FW_REQUEST_BOOT)
        except p.EcuError as exc:
            if exc.code == ERROR_ENGINE_RUNNING:
                raise EngineRunning("The ECU refused because the engine is running.") from exc
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


def program_image(
    link: p.Link, image: FirmwareImage, reopen: Callable[[], None] | None = None,
    progress: Callable[[int, int], None] = lambda _sent, _total: None, sleep=time.sleep,
) -> int:
    """Program one image and return the state the ECU then reports."""
    link.addr = p.ADDR_COMMS if image.is_comms else p.ADDR_MAIN
    try:
        _enter_boot(link, reopen if image.is_comms else None, sleep)
        link.request_dangerous(p.CMD_FW_START)
        sleep(0.2)
        link.request_dangerous(p.CMD_FW_HEADER, image.header, 1.0)
        _send_body(link, image, progress)
        if image.is_comms:
            link.request_dangerous(p.CMD_FW_RESET, expect_reply=False)
            link.request_dangerous(p.CMD_FW_RESET)
            if reopen:
                reopen()
            sleep(2.0)
        else:
            link.request_dangerous(p.CMD_FW_REQUEST_MAIN, expect_reply=False)
        state = 0
        for _ in range(3):
            sleep(0.3)
            state = _state(link)
            if STATE_RUNNING <= state <= STATE_TRAP:
                break
        if state not in (STATE_RUNNING, STATE_SAFE):
            raise FirmwareError(f"The ECU did not start its main firmware (mode {state}).")
        return state
    finally:
        link.addr = p.ADDR_MAIN


FAILED_TEXT = (
    "ECU update failed.\n\nThe update was interrupted, and this ECU will be disabled until it completes. "
    "Please cycle power to the ECU and run this update again."
)
