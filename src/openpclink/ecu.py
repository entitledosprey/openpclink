"""A live session with an ECU: shadow memory plus checked reads and writes."""
from __future__ import annotations

import json
import logging
import struct
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import protocol as p
from .database import Database
from .eculog import EcuLogs
from .layout import Location, parse_descriptor
from .transports import Transport

log = logging.getLogger("openpclink.ecu")
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
FAULT_CLEAR_ID = 1171
TPS_RAW_ID = 1077
TPS_OPEN_ID = 1704
TPS_CLOSED_ID = 1703
ETHROTTLE_MODE_ID = 1692
ETHROTTLE_INACTIVE = (0, 3)  # OFF and Disabled.
CALIBRATION_TIMEOUT = 1.5
FACTORY_TIMEOUT = 1.0
FACTORY_SETTLE_S = 0.5
PASSWORD_LENGTH = 20
PASSWORD_MINIMUM = 3
COMMS_FIRST_ID = 10000
MEMO_FILE = "Memo"
CAN_FILE = "CANFile"
ERROR_ENGINE_RUNNING = 4
ERROR_TEXT = {
    2: "Protocol Error", 3: "Function Failed", 4: "The engine is running", 5: "Data Flash write failed",
    6: "Data Flash verify failed", 0x1A: "Function Unknown", 0x1D: "Validation Failed", 0x25: "Invalid Data Size",
    0x26: "Program Failed", 0x27: "Invalid Flash Block",
}


def has_comms_processor(family: int) -> bool:
    return family == 0x3EF or family > 0x3F3


class EcuProtected(Exception):
    """The ECU is locked with a tuner password."""


class WriteRefused(Exception):
    """A write failed validation; nothing was sent to the ECU."""


class WriteMismatch(Exception):
    """The ECU did not hold the value it was sent."""


class StoreUnconfirmed(Exception):
    """The ECU's reply to Store was lost, so it may or may not have saved."""


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


@dataclass
class CommsValue:
    """A setting held by the G5 comms processor."""

    data_id: int
    kind: int
    count: int
    minimum: float
    maximum: float
    read_only: bool
    values: list = field(default_factory=list)
    text: str = ""

    KIND_FLOATS = (1, 5)
    KIND_TEXT = (4, 7)
    KIND_ARRAYS = (5, 8, 9)

    @property
    def is_float(self) -> bool:
        return self.kind in self.KIND_FLOATS

    @property
    def is_text(self) -> bool:
        return self.kind in self.KIND_TEXT

    @property
    def is_array(self) -> bool:
        return self.kind in self.KIND_ARRAYS

    @classmethod
    def parse(cls, data_id: int, body: bytes) -> "CommsValue | None":
        if len(body) < 0x16:
            return None
        kind = body[3]
        if kind not in (1, 2, 3, 4, 5, 7, 8, 9):
            return None
        count = struct.unpack_from(">H", body, 4)[0]
        minimum, maximum = struct.unpack_from(">ff", body, 0xA)
        item = cls(data_id, kind, count, minimum, maximum, bool(body[0x15] & 0x80))
        item.values = [0.0] * max(count, 1) if item.is_array else [0.0]
        item._decode(body[0x16:], 0)
        return item

    def _decode(self, raw: bytes, start: int) -> None:
        if self.is_text:
            self.text = raw[: self.count * 2].decode("utf-16-be", "replace").split("\0", 1)[0]
            return
        code = ">f" if self.is_float else ">i" if self.kind == 3 else ">I"
        for at in range(len(raw) // 4):
            if start + at < len(self.values):
                self.values[start + at] = float(struct.unpack_from(code, raw, at * 4)[0])

    def apply(self, body: bytes) -> None:
        """A reply to a read or write: type, id, offset, count, then values."""
        if len(body) < 10:
            return
        offset = struct.unpack_from(">h", body, 6)[0]
        self._decode(body[10:], 0 if offset < 0 else offset)

    def location(self, offset: int) -> Location:
        if self.is_text:
            return Location(self.data_id, 8, offset, width=1, count=self.count, scale=0.0, read_only=self.read_only)
        kind = 1 if self.is_array else 0
        return Location(self.data_id, kind, offset, width=4, count=len(self.values), is_float=self.is_float, read_only=self.read_only)


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
        self._unlocked = False
        self.memo: str | None = None
        self._memo_changed = False
        self.logs = EcuLogs(self.link)
        self.comms: dict[int, "CommsValue"] = {}
        self.baseline = b""

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
        if has_comms_processor(self.info.hardware_family):
            try:
                self._load_comms()
            except p.ProtocolError as exc:
                log.warning("comms processor settings unavailable: %s", exc)
        self.baseline = bytes(self.memory)
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
        if self._unlocked:
            return False
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
        if data_id in self.comms:
            values = self.comms[data_id].values
            return float(values[index]) if index < len(values) else 0.0
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
        if data_id in self.comms:
            return self.comms[data_id].text
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
        if data_id in self.comms:
            return self._write_comms_number(data_id, value, index)
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
        if data_id in self.comms:
            return self._write_comms_text(data_id, text)
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
            at = absolute + start
            self._dirty = True
            try:
                body = self.link.request(cmd, struct.pack(">HH", offset, len(chunk)) + chunk)
            except p.EcuError:
                raise
            except p.ProtocolError:
                body = b""  # The reply was lost; the read-back below decides.
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
        try:
            if self._memo_changed:
                self.logs.write_named(p.CMD_WRITE_MEMO, (self.memo or "").encode("utf-16-le"))
            self.link.request(p.CMD_STORE, timeout=STORE_TIMEOUT)
            if self.comms:
                self._on_comms(lambda: self.link.request(p.CMD_STORE, timeout=STORE_TIMEOUT))
        except p.EcuError:
            raise
        except p.ProtocolError as exc:
            raise StoreUnconfirmed(
                "The ECU did not confirm the Store, so it may not have saved. Changes are still "
                f"live. Store again before powering off. ({exc})"
            ) from exc
        self.reload()
        self.baseline = bytes(self.memory)
        self._dirty = False
        self._memo_changed = False

    def is_changed(self, data_id: int, index: int = 0) -> bool:
        """Whether this differs from what was on the ECU at connecting or the last Store."""
        loc = self.table.get(data_id)
        if loc is None or data_id in self.comms or not self.baseline or self._is_runtime(loc):
            return False
        at = loc.offset + loc.width * index
        size = max(loc.width, 1)
        return self.memory[at : at + size] != self.baseline[at : at + size]

    def undo_short_term_changes(self) -> int:
        """Put back everything changed since connecting or the last Store. Returns how many."""
        info = self._need_info()
        start, end = info.runtime_size, info.runtime_size + info.config_size + info.section2_size
        restored = 0
        at = start
        while at < end:
            if self.memory[at] == self.baseline[at]:
                at += 1
                continue
            stop = at
            while stop < end and self.memory[stop] != self.baseline[stop]:
                stop += 1
            self._write_memory(at, self.baseline[at:stop])
            restored += stop - at
            at = stop
        self._dirty = False
        return restored

    def clear_faults(self) -> None:
        """Ask the ECU to clear its fault codes, as PCLink does: by setting the CE Clear flag."""
        if FAULT_CLEAR_ID not in self.table:
            raise WriteRefused("this ECU has no fault clear flag")
        loc = self.table[FAULT_CLEAR_ID]
        dirty, before = self._dirty, self.memory[loc.offset]
        self._write_memory(loc.offset, bytes([(before & ~loc.mask & 0xFF) | loc.mask]))
        # The flag is a request, not a setting: it must not be resent with its neighbours or count as a tune change.
        self.memory[loc.offset] = before & ~loc.mask & 0xFF
        self._dirty = dirty

    def clear_statistics(self) -> None:
        self.link.request(p.CMD_CLEAR_STATISTICS)

    def calibrate_map(self) -> None:
        """Calibrate the MAP sensor to the barometric sensor, which needs the engine stopped."""
        self._require_engine_stopped(p.CMD_CALIBRATE_MAP)
        self.link.request(p.CMD_CALIBRATE_MAP, p.CALIBRATE_MAP_PAYLOAD, CALIBRATION_TIMEOUT)
        self._dirty = True
        self.reload()

    def set_tps_span(self, data_id: int) -> float:
        """Save the throttle's current voltage as its open or closed point, as PCLink's TPS Setup does."""
        if data_id not in (TPS_OPEN_ID, TPS_CLOSED_ID) or TPS_RAW_ID not in self.table:
            raise WriteRefused("this ECU does not support TPS setup")
        self._require_engine_stopped(data_id)
        if ETHROTTLE_MODE_ID in self.table and self.get(ETHROTTLE_MODE_ID) not in ETHROTTLE_INACTIVE:
            raise WriteRefused("this ECU uses an electronic throttle, which needs its own calibration")
        return self.set(data_id, round(self.get(TPS_RAW_ID), 2))

    def calibrate_baro(self, kpa: float) -> None:
        """Tell the ECU the true barometric pressure."""
        self.link.request(p.CMD_CALIBRATE_BARO, struct.pack(">I", round(kpa * 10)), CALIBRATION_TIMEOUT)
        self._dirty = True
        self.reload()

    def update_statistics(self) -> None:
        info = self._need_info()
        if info.section2_size:
            body = self.link.request(p.CMD_READ_SECTION2, timeout=2.0)
            start = info.runtime_size + info.config_size
            self.memory[start : start + len(body)] = body[: info.section2_size]

    def read_memo(self) -> str:
        if self.memo is None:
            raw = self.logs.read_named(MEMO_FILE)
            self.memo = raw[: len(raw) & ~1].decode("utf-16-le", "replace").split("\0", 1)[0]
        return self.memo

    def set_memo(self, text: str) -> None:
        """Held here until a Store, as PCLink does."""
        self.memo = text
        self._memo_changed = True
        self._dirty = True

    def exception_dump(self) -> bytes:
        return self.link.request(p.CMD_EXCEPTION_DUMP, timeout=2.0)

    def set_clock(self, now: time.struct_time | None = None) -> None:
        """Set the G5 comms processor's clock from the computer's."""
        now = now or time.localtime()
        payload = bytes(4) + struct.pack(">HBBBBBB", now.tm_year, now.tm_mon, now.tm_mday, now.tm_hour, now.tm_min, now.tm_sec, (now.tm_wday + 1) % 7 + 1)
        self._on_comms(lambda: self.link.request(p.CMD_SET_CLOCK, payload))

    def feature_unlock(self, code: str) -> None:
        """Send a dealer's 16-digit unlock code. The ECU decides whether it is right."""
        code = code.strip()
        if len(code) < 16:
            raise WriteRefused("The unlock code is too short")
        if len(code) > 16:
            raise WriteRefused("The unlock code is too long")
        try:
            words = [int(code[at : at + 4], 16) for at in (0, 4, 8, 12)]
        except ValueError:
            raise WriteRefused("The unlock code is not the correct format") from None
        self.link.request(p.CMD_FEATURE_UNLOCK, struct.pack(">HHHH", words[1], words[0], words[3], words[2]))

    # ---- passwords --------------------------------------------------------

    @staticmethod
    def _password_values(text: str) -> list[int]:
        raw = text.encode("ascii")
        return list(raw[:PASSWORD_LENGTH]) + [0] * (PASSWORD_LENGTH - len(raw[:PASSWORD_LENGTH]))

    def _write_password(self, data_id: int, text: str) -> None:
        loc = self.location(data_id)
        for index, value in enumerate(self._password_values(text)[: loc.count]):
            self._write_memory(loc.offset + loc.width * index, self._encode(loc, value))

    def _password_matches(self, text: str) -> bool:
        master = self.table.get(MASTER_PASSWORD_ID)
        if master is None:
            return False
        wanted = self._password_values(text)[: master.count]
        return [int(self._read_raw_number(master, i)) for i in range(master.count)] == wanted

    def set_password(self, text: str) -> None:
        """Lock the ECU with a new password, which PCLink stores straight away."""
        if self.is_protected:
            raise EcuProtected("Before performing this operation, password protection must be deactivated.")
        if not PASSWORD_MINIMUM <= len(text) <= PASSWORD_LENGTH or not text.isascii() or not text.isprintable():
            raise WriteRefused(f"The password must be {PASSWORD_MINIMUM} to {PASSWORD_LENGTH} plain characters.")
        if MASTER_PASSWORD_ID not in self.table:
            raise WriteRefused("This ECU has no password setting.")
        self._write_password(USER_PASSWORD_ID, "")
        self._write_password(MASTER_PASSWORD_ID, text)
        self._unlocked = False
        self.store()

    def unlock(self, text: str, temporary: bool = False) -> bool:
        """Unlock with the password. A temporary unlock changes nothing on the ECU and ends on disconnecting."""
        if not self._password_matches(text):
            return False
        if not temporary:
            self._write_password(USER_PASSWORD_ID, text)
            self.store()
        self._unlocked = True
        return True

    # ---- loading a map ----------------------------------------------------

    def load_map(self, entries, progress=None) -> int:
        """Write a map's differing settings, as PCLink does on opening a file while connected.

        Two passes, since setting one value can change another's scale. Returns cells written.
        """
        self._require_engine_stopped(p.CMD_BULK_DOWNLOAD)
        self.link.request(p.CMD_BULK_DOWNLOAD, bytes([1]), 2.0)
        written = 0
        try:
            for _ in range(2):
                for entry in sorted(entries, key=lambda item: item.data_id):
                    written += self._load_entry(entry)
                if progress:
                    progress(written)
        finally:
            self.link.request(p.CMD_BULK_DOWNLOAD, bytes([0]), 2.0)
        self._dirty = True
        self.reload()
        return written

    def _load_entry(self, entry) -> int:
        data_id = entry.data_id
        if data_id in SECRET_IDS or data_id == 0 or data_id in self.comms:
            return 0
        loc = self.table.get(data_id)
        if loc is None or loc.read_only or self._is_runtime(loc):
            return 0
        if loc.is_string:
            if loc.width == 1 and entry.text != self.get_string(data_id):
                self.set_string(data_id, entry.text)
                return 1
            return 0
        written = 0
        for index, value in enumerate(entry.values[: 1 if loc.is_bitfield else loc.count]):
            current = self._read_raw_number(loc, index)
            if loc.is_bitfield:
                if int(value) != int(current):
                    data = (self.memory[loc.offset] & ~loc.mask & 0xFF) | ((int(value) << loc.shift) & loc.mask)
                    self._write_memory(loc.offset, bytes([data]))
                    written += 1
                continue
            encoded = self._encode(loc, value)
            at = loc.offset + loc.width * index
            if encoded != bytes(self.memory[at : at + loc.width]):
                self._write_memory(at, encoded)
                written += 1
        return written

    # ---- factory settings -------------------------------------------------

    def restore_factory(self, sleep=time.sleep) -> None:
        """Erase every setting on the ECU. It works on a locked ECU, as PCLink's does."""
        self._require_engine_stopped(p.CMD_FACTORY_RESET)
        self.link.request_dangerous(p.CMD_FACTORY_RESET, timeout=FACTORY_TIMEOUT)
        sleep(FACTORY_SETTLE_S)
        if self.comms:
            self._on_comms(lambda: self.link.request_dangerous(p.CMD_FACTORY_RESET, timeout=FACTORY_TIMEOUT))
            sleep(FACTORY_SETTLE_S)
        self._dirty = True
        self._unlocked = False
        self.reload()

    # ---- G5 comms processor -----------------------------------------------

    def _on_comms(self, action):
        self.link.addr = p.ADDR_COMMS
        try:
            return action()
        finally:
            self.link.addr = p.ADDR_MAIN

    def _load_comms(self) -> None:
        """G5 network and logging settings live on a second processor, from id 10000."""

        def load() -> None:
            memory = self.link.request(p.CMD_MEMORY_INFO)
            top = struct.unpack_from(">H", memory, 0x12)[0] if len(memory) >= 0x14 else COMMS_FIRST_ID
            for data_id in range(COMMS_FIRST_ID, top):
                try:
                    record = self.link.request(p.CMD_COMMS_READ_RECORD, struct.pack(">H", data_id) + bytes(54))
                except p.EcuError:
                    continue
                value = CommsValue.parse(data_id, record)
                if value is None:
                    continue
                if value.is_array:
                    value.apply(self.link.request(p.CMD_COMMS_READ, struct.pack(">HH", data_id, 0xFFFF) + bytes(52)))
                self.comms[data_id] = value

        self._on_comms(load)
        beyond = self._need_info().runtime_size + self._need_info().config_size + self._need_info().section2_size
        for data_id, value in self.comms.items():
            self.table[data_id] = value.location(beyond)

    def _write_comms_number(self, data_id: int, value: float, index: int) -> float:
        item = self.comms[data_id]
        if item.minimum < item.maximum:
            value = min(max(value, item.minimum), item.maximum)
        raw = struct.pack(">f", value) if item.is_float else struct.pack(">I" if item.kind != 3 else ">i", int(value))
        where = 0xFFFF if not item.is_array else index
        reply = self._on_comms(lambda: self.link.request(p.CMD_COMMS_WRITE, struct.pack(">HH", data_id, where) + raw + bytes(48)))
        item.apply(reply)
        self._dirty = True
        return self.get(data_id, index)

    def _write_comms_text(self, data_id: int, text: str) -> str:
        item = self.comms[data_id]
        if len(text) > item.count:
            raise WriteRefused(f"text is longer than the {item.count} characters allowed")
        raw = text.ljust(item.count + 1, "\0").encode("utf-16-be")
        reply = self._on_comms(lambda: self.link.request(p.CMD_COMMS_WRITE, struct.pack(">HH", data_id, 0xFFFF) + raw))
        item.apply(reply)
        self._dirty = True
        return item.text

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
