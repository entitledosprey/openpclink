"""A live session with an ECU: shadow memory plus checked reads and writes."""
from __future__ import annotations

import json
import struct
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from . import protocol as p
from .database import Database
from .layout import Location, parse_descriptor
from .transports import Transport

DESCRIPTOR_CHUNK = 0x24CC
STATE_RUNNING = 1
STORE_TIMEOUT = 1.5
STATE_BOOTLOADER = 3

# Tuner password storage and protection state. PCLink's password check lives on
# the PC and the password is held in plain text on the ECU, so a client that
# ignored these would silently defeat a tuner's lock or expose their password.
MASTER_PASSWORD_ID = 2294
USER_PASSWORD_ID = 2295
PROTECTION_STATE_IDS = (2303, 2324)
SECRET_IDS = frozenset({MASTER_PASSWORD_ID, USER_PASSWORD_ID, *PROTECTION_STATE_IDS})
ENGINE_SPEED_ID = 1000
# Settings real PCLink was seen to padlock while the engine runs: Number of
# Cylinders, Engine Type, Custom TDC Points. The rule behind its padlock is not
# recovered, so other settings may be locked there too (docs/VERIFICATION.md).
ENGINE_LOCKED_IDS = frozenset({2419, 2428, 2377})


class EcuProtected(Exception):
    """The ECU is locked with a tuner password."""


class WriteRefused(Exception):
    """A write failed validation; nothing was sent to the ECU."""


class WriteMismatch(Exception):
    """The ECU did not hold the value it was sent."""


@dataclass(frozen=True)
class EcuInfo:
    firmware: str
    boot: str
    hardware_family: int
    serial: int
    link_id: int
    max_id: int
    runtime_size: int
    config_size: int
    section2_size: int


def _version(body: bytes) -> str:
    major = body[8]
    minor, patch = struct.unpack_from(">HH", body, 9)
    return f"{major}.{minor}.{patch}"


class Ecu:
    def __init__(self, transport: Transport, database: Database | None = None) -> None:
        self.link = p.Link(transport)
        self.transport = transport
        self.database = database
        self.info: EcuInfo | None = None
        self.table: dict[int, Location] = {}
        self.memory = bytearray()
        self._dirty = False

    # ---- connection -------------------------------------------------------

    def connect(self) -> EcuInfo:
        """Identify the ECU, then load its memory layout and current values."""
        self._require_running()
        boot = self._info_block(p.CMD_BOOT_INFO)
        firmware = self._info_block(p.CMD_FIRMWARE_INFO)
        identity = self.link.request(p.CMD_IDENTITY)
        memory = self.link.request(p.CMD_MEMORY_INFO)
        if len(memory) < 0x14 or len(boot) < 0x4A or len(firmware) < 16 or len(identity) < 2:
            raise p.ProtocolError("ECU information replies are too short")
        config_size, runtime_size, section2_size = struct.unpack_from(">HHH", memory, 4)
        max_id = struct.unpack_from(">H", memory, 0x12)[0]
        packet_bits = memory[0x0B]
        if packet_bits > 5:
            self.link.max_packet = (1 << packet_bits) - 8
        self.info = EcuInfo(
            firmware=_version(firmware),
            boot=_version(boot),
            hardware_family=struct.unpack_from(">H", boot, 0x48)[0],
            serial=struct.unpack_from(">I", boot, 0x44)[0],
            link_id=struct.unpack_from(">H", identity, 0)[0],
            max_id=max_id,
            runtime_size=runtime_size,
            config_size=config_size,
            section2_size=section2_size,
        )
        self.table = parse_descriptor(self._read_descriptor(), max_id)
        self.memory = bytearray(runtime_size + config_size + section2_size)
        self.reload()
        self.poll_runtime()
        return self.info

    def _require_running(self) -> None:
        state = 0
        for attempt in range(3):
            try:
                state = struct.unpack(">H", self.link.request(p.CMD_STATE)[:2])[0]
            except (p.ProtocolError, struct.error):
                if attempt == 2:
                    raise
                continue
            if state == STATE_BOOTLOADER:
                time.sleep(0.05)
                continue
            break
        if state != STATE_RUNNING:
            raise p.ProtocolError(
                f"ECU is not running its main firmware (state {state}). "
                "It may be in bootloader mode; use official PCLink to recover it."
            )

    def _info_block(self, cmd: int) -> bytes:
        body = self.link.request(cmd)
        if len(body) < 8:
            raise p.ProtocolError(f"info reply 0x{cmd:02X} too short")
        length = struct.unpack_from(">H", body, 4)[0]
        if length != len(body) or p.crc16(body[:2], p.crc16(body[2:])) != 0:
            raise p.ProtocolError(f"info reply 0x{cmd:02X} failed its check")
        return body

    def _read_descriptor(self) -> bytes:
        blob = bytearray()
        while True:
            try:
                chunk = self.link.request(
                    p.CMD_READ_DESCRIPTOR,
                    struct.pack(">HH", len(blob), DESCRIPTOR_CHUNK),
                    timeout=2.0,
                )
            except p.EcuError as exc:
                if exc.code == p.ERR_UNSUPPORTED:
                    return self.link.request(p.CMD_READ_DESCRIPTOR_LEGACY, timeout=2.0)
                raise
            blob += chunk
            if len(chunk) < DESCRIPTOR_CHUNK:
                return bytes(blob)

    def reload(self) -> None:
        """Replace the shadow copy of config and section 2 with the ECU's."""
        info = self._need_info()
        self._read_config(info)
        if info.section2_size:
            body = self.link.request(p.CMD_READ_SECTION2, timeout=2.0)
            start = info.runtime_size + info.config_size
            self.memory[start : start + len(body)] = body[: info.section2_size]

    def _read_config(self, info: EcuInfo) -> None:
        offset = 0
        while offset < info.config_size:
            size = min(info.config_size - offset, DESCRIPTOR_CHUNK)
            body = self.link.request(p.CMD_READ_CONFIG, struct.pack(">HH", offset, size), timeout=2.0)
            if not body:
                raise p.ProtocolError("ECU returned no config data")
            start = info.runtime_size + offset
            self.memory[start : start + len(body)] = body
            offset += len(body)

    def poll_runtime(self) -> None:
        info = self._need_info()
        body = self.link.request(p.CMD_POLL_RUNTIME)
        self.memory[0 : min(len(body), info.runtime_size)] = body[: info.runtime_size]

    def _need_info(self) -> EcuInfo:
        if self.info is None:
            raise RuntimeError("not connected")
        return self.info

    # ---- protection -------------------------------------------------------

    @property
    def is_protected(self) -> bool:
        for data_id in PROTECTION_STATE_IDS:
            loc = self.table.get(data_id)
            if loc is not None and self._read_raw_number(loc, 0) != 0:
                return True
        master = self.table.get(MASTER_PASSWORD_ID)
        user = self.table.get(USER_PASSWORD_ID)
        if master is None:
            return False
        for index in range(master.count):
            entered = self._read_raw_number(user, index) if user and index < user.count else 0
            stored = self._read_raw_number(master, index)
            if stored > 0 and stored != entered:
                return True
        return False

    def _check_access(self, data_id: int, loc: Location) -> None:
        if data_id in SECRET_IDS:
            raise EcuProtected(f"parameter {data_id} is reserved for password protection")
        if self.is_protected and not self._is_runtime(loc):
            raise EcuProtected(
                "This ECU is locked with a tuner password. Unlock it with that "
                "password in official PCLink."
            )

    def _is_runtime(self, loc: Location) -> bool:
        return loc.offset < self._need_info().runtime_size

    # ---- values -----------------------------------------------------------

    def location(self, data_id: int) -> Location:
        try:
            return self.table[data_id]
        except KeyError:
            raise KeyError(f"this ECU has no parameter {data_id}") from None

    def get(self, data_id: int, index: int = 0) -> float:
        loc = self.location(data_id)
        self._check_access(data_id, loc)
        if not 0 <= index < max(loc.count, 1) and not loc.is_bitfield:
            raise IndexError(f"parameter {data_id} has no element {index}")
        return self._read_raw_number(loc, index)

    def get_all(self, data_id: int) -> list[float]:
        loc = self.location(data_id)
        return [self.get(data_id, i) for i in range(1 if loc.is_bitfield else loc.count)]

    def get_string(self, data_id: int) -> str:
        loc = self.location(data_id)
        self._check_access(data_id, loc)
        if not loc.is_string or loc.width != 1:
            raise TypeError(f"parameter {data_id} is not text")
        raw = bytes(self.memory[loc.offset : loc.offset + loc.count])
        return raw.split(b"\0", 1)[0].decode("latin-1")

    def _read_raw_number(self, loc: Location, index: int) -> float:
        if loc.is_bitfield:
            return float((self.memory[loc.offset] & loc.mask) >> loc.shift)
        at = loc.offset + loc.width * index
        raw = bytes(self.memory[at : at + loc.width])
        if loc.is_float:
            return float(struct.unpack(">f", raw)[0])
        return int.from_bytes(raw, "big", signed=loc.signed) * loc.scale + loc.add

    def set(self, data_id: int, value: float, index: int = 0) -> float:
        """Write a value live to ECU RAM and return what the ECU then holds.

        The change is lost at power-off until store() is called.
        """
        loc = self._writable(data_id)
        if self.database is not None and not loc.is_bitfield:
            try:
                self.database.check_range(data_id, value)
            except ValueError as exc:
                raise WriteRefused(str(exc)) from exc
        if loc.is_bitfield:
            option = int(value)
            if option != value or not 0 <= option <= (loc.mask >> loc.shift):
                raise WriteRefused(f"{value} is not a valid option for parameter {data_id}")
            current = self.memory[loc.offset]
            data = bytes([(current & ~loc.mask & 0xFF) | ((option << loc.shift) & loc.mask)])
            self._write_memory(loc.offset, data)
            return self._read_raw_number(loc, 0)
        if loc.is_string:
            raise WriteRefused(f"parameter {data_id} is text; use set_string")
        if not 0 <= index < loc.count:
            raise WriteRefused(f"parameter {data_id} has no element {index}")
        self._write_memory(loc.offset + loc.width * index, self._encode(loc, value))
        return self._read_raw_number(loc, index)

    def set_string(self, data_id: int, text: str) -> str:
        loc = self._writable(data_id)
        if not loc.is_string or loc.width != 1:
            raise WriteRefused(f"parameter {data_id} is not text")
        raw = text.encode("latin-1")
        if len(raw) > loc.count:
            raise WriteRefused(f"text is longer than the {loc.count} characters allowed")
        self._write_memory(loc.offset, raw.ljust(loc.count, b"\0"))
        return self.get_string(data_id)

    def _writable(self, data_id: int) -> Location:
        loc = self.location(data_id)
        self._check_access(data_id, loc)
        if loc.read_only or self._is_runtime(loc):
            raise WriteRefused(f"parameter {data_id} is read-only")
        if data_id in ENGINE_LOCKED_IDS:
            self._require_engine_stopped(data_id)
        return loc

    def _require_engine_stopped(self, data_id: int) -> None:
        """PCLink padlocks these settings while the engine runs. Check live data."""
        speed = self.table.get(ENGINE_SPEED_ID)
        if speed is None:
            raise WriteRefused(f"parameter {data_id} needs the engine stopped; cannot read engine speed")
        self.poll_runtime()
        if self._read_raw_number(speed, 0) > 0:
            raise WriteRefused(f"parameter {data_id} cannot be changed while the engine is running")

    @staticmethod
    def _encode(loc: Location, value: float) -> bytes:
        if value != value or value in (float("inf"), float("-inf")):
            raise WriteRefused("value is not a number")
        if loc.is_float:
            return struct.pack(">f", value)
        if loc.scale == 0:
            raise WriteRefused(f"parameter {loc.data_id} has no scale")
        raw = round((value - loc.add) / loc.scale)
        bits = loc.width * 8
        low, high = (-(1 << (bits - 1)), (1 << (bits - 1)) - 1) if loc.signed else (0, (1 << bits) - 1)
        if not low <= raw <= high:
            raise WriteRefused(f"{value} does not fit parameter {loc.data_id}")
        return raw.to_bytes(loc.width, "big", signed=loc.signed)

    def _write_memory(self, absolute: int, data: bytes) -> None:
        info = self._need_info()
        config_start = info.runtime_size
        section2_start = config_start + info.config_size
        if config_start <= absolute < section2_start:
            cmd, base = p.CMD_WRITE_CONFIG, config_start
        elif section2_start <= absolute < section2_start + info.section2_size:
            cmd, base = p.CMD_WRITE_SECTION2, section2_start
        else:
            raise WriteRefused(f"address {absolute} is outside writable memory")
        limit = max(self.link.max_packet - 4, 1)
        for start in range(0, len(data), limit):
            chunk = data[start : start + limit]
            offset = absolute + start - base
            body = self.link.request(cmd, struct.pack(">HH", offset, len(chunk)) + chunk)
            at = absolute + start
            self._dirty = True
            if body[: len(chunk)] == chunk:
                self.memory[at : at + len(chunk)] = chunk
                continue
            # The reply is not an exact echo. Judge by what the ECU really holds.
            self.reload()
            if bytes(self.memory[at : at + len(chunk)]) != chunk:
                raise WriteMismatch(
                    "the ECU did not accept the value exactly as sent; "
                    "the value shown is what it now holds"
                )

    @property
    def has_unstored_changes(self) -> bool:
        return self._dirty

    def store(self) -> None:
        """Make live changes permanent in the ECU's flash."""
        self.link.request(p.CMD_STORE, timeout=STORE_TIMEOUT)
        self.reload()
        self._dirty = False

    def backup(self, path: Path) -> None:
        """Save a plain-text copy of the configuration, excluding secrets."""
        if self.is_protected:
            raise EcuProtected("cannot back up a password-protected ECU")
        info = self._need_info()
        values: dict[str, object] = {}
        for data_id, loc in sorted(self.table.items()):
            if data_id in SECRET_IDS or data_id == 0 or self._is_runtime(loc):
                continue
            values[str(data_id)] = self.get_string(data_id) if loc.is_string and loc.width == 1 else (
                self.get(data_id) if loc.is_bitfield else self.get_all(data_id)
            )
        path.write_text(json.dumps({"ecu": asdict(info), "values": values}, indent=1))

    def close(self) -> None:
        self.transport.close()
