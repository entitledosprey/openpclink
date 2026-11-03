"""Framing and command layer of the Link ECU serial protocol.

Request:  5A addr cmd:u16 len:u16 payload crc:u16      (len = payload + 2)
Reply:    addr 5A|01 cmd:u16 len:u16 payload crc:u16

All integers are big-endian. The CRC is CRC-16/XMODEM over everything before
it. See docs/PROTOCOL.md for how each part was recovered.
"""
from __future__ import annotations

import logging
import os
import struct
import time
from dataclasses import dataclass

from .transports import Transport

ADDR_MAIN = 0x5B

ADDR_COMMS = 0x7D

CMD_READ_CONFIG = 0x00
CMD_READ_DESCRIPTOR_LEGACY = 0x01
CMD_WRITE_CONFIG = 0x02
CMD_STATE = 0x03
CMD_COMMS_READ_RECORD = 0x04
CMD_COMMS_POLL_A = 0x07
CMD_WRITE_SECTION2 = 0x08
CMD_POLL_RUNTIME = 0x09
CMD_COMMS_POLL_B = 0x0A
CMD_MEMORY_INFO = 0x0B
CMD_READ_DESCRIPTOR = 0x0D
CMD_FW_START = 0x10
CMD_FW_REQUEST_BOOT = 0x11
CMD_FW_REQUEST_MAIN = 0x12
CMD_FW_DATA = 0x14
CMD_FW_HEADER = 0x15
CMD_FIRMWARE_INFO = 0x16
CMD_BOOT_INFO = 0x18
CMD_ERROR = 0x19
CMD_IDENTITY = 0x1A
CMD_LOG_INFO = 0x1B
CMD_CALIBRATE_BARO = 0x1E
CMD_CALIBRATE_MAP = 0x1F
CMD_STORE = 0x20
CMD_FACTORY_RESET = 0x21
CMD_FW_RESET = 0x22
CMD_BULK_DOWNLOAD = 0x23
CMD_FEATURE_UNLOCK = 0x24
CMD_TRIGGER_SCOPE = 0x26
CMD_EXCEPTION_DUMP = 0x27
CMD_COMMS_WRITE = 0x2A
CMD_COMMS_READ = 0x2B
CMD_FILE = 0x2E
CMD_CLEAR_STATISTICS = 0x2F
CMD_READ_SECTION2 = 0x32
CMD_CAN_RECEIVE = 0x33
CMD_CAN_TRANSMIT = 0x34
CMD_CAN_CONTROL = 0x35
CMD_WRITE_MEMO = 0x36
CMD_WRITE_CAN_FILE = 0x38
CMD_SET_CLOCK = 0x39

# Only these commands can be sent through request(). The firmware updater's commands
# and the factory reset (0x21, right next to Store) can brick or wipe an ECU, so they
# are absent; they go through request_dangerous(), which only the updater and the
# factory reset call. Anything unidentified cannot be sent at all.
ALLOWED_COMMANDS = frozenset(
    {
        CMD_READ_CONFIG,
        CMD_READ_DESCRIPTOR_LEGACY,
        CMD_WRITE_CONFIG,
        CMD_STATE,
        CMD_COMMS_READ_RECORD,
        CMD_COMMS_POLL_A,
        CMD_WRITE_SECTION2,
        CMD_POLL_RUNTIME,
        CMD_COMMS_POLL_B,
        CMD_MEMORY_INFO,
        CMD_READ_DESCRIPTOR,
        CMD_FIRMWARE_INFO,
        CMD_BOOT_INFO,
        CMD_IDENTITY,
        CMD_LOG_INFO,
        CMD_CALIBRATE_BARO,
        CMD_CALIBRATE_MAP,
        CMD_STORE,
        CMD_BULK_DOWNLOAD,
        CMD_FEATURE_UNLOCK,
        CMD_TRIGGER_SCOPE,
        CMD_EXCEPTION_DUMP,
        CMD_COMMS_WRITE,
        CMD_COMMS_READ,
        CMD_FILE,
        CMD_CLEAR_STATISTICS,
        CMD_READ_SECTION2,
        CMD_CAN_RECEIVE,
        CMD_CAN_TRANSMIT,
        CMD_CAN_CONTROL,
        CMD_WRITE_MEMO,
        CMD_WRITE_CAN_FILE,
        CMD_SET_CLOCK,
    }
)
DANGEROUS_COMMANDS = frozenset(
    {CMD_FW_START, CMD_FW_REQUEST_BOOT, CMD_FW_REQUEST_MAIN, CMD_FW_DATA, CMD_FW_HEADER, CMD_FACTORY_RESET, CMD_FW_RESET}
)

# Safe to repeat after a lost or damaged reply. Writes and Store are not here:
# a write is settled by reading back, and a Store is never repeated unasked.
RETRYABLE_COMMANDS = frozenset(
    {
        CMD_READ_CONFIG,
        CMD_READ_DESCRIPTOR_LEGACY,
        CMD_STATE,
        CMD_COMMS_READ_RECORD,
        CMD_POLL_RUNTIME,
        CMD_MEMORY_INFO,
        CMD_READ_DESCRIPTOR,
        CMD_FIRMWARE_INFO,
        CMD_BOOT_INFO,
        CMD_IDENTITY,
        CMD_LOG_INFO,
        CMD_EXCEPTION_DUMP,
        CMD_COMMS_READ,
        CMD_READ_SECTION2,
    }
)
READ_ATTEMPTS = 3

# Sub-operations of CMD_FILE.
FILE_READ, FILE_DELETE, FILE_NEXT, FILE_STREAM, FILE_PAUSE_LOGGING, FILE_RESUME_LOGGING = 0, 1, 2, 3, 4, 5
_READ_ONLY_FILE_OPERATIONS = (FILE_READ, FILE_NEXT)
_FEATURE_LOCK_WORDS = bytes.fromhex("0EF1DEAD0EF1DEAD")

# Exactly what real PCLink sends. 0x1F has another form with no payload whose purpose is unknown.
CALIBRATE_MAP_PAYLOAD = bytes([0x00, CMD_CALIBRATE_MAP]) + bytes(8)

# With this set in the environment, nothing that changes an ECU can be sent.
READ_ONLY_ENV = "OPENPCLINK_READ_ONLY"

ERR_UNSUPPORTED = 0x1B
DEFAULT_TIMEOUT = 0.3
_RESYNC_QUIET_S = 0.02
_RESYNC_LIMIT_S = 0.5

log = logging.getLogger("openpclink.protocol")


class ProtocolError(Exception):
    """A malformed, missing or corrupt reply."""


class EcuError(ProtocolError):
    """The ECU answered with an error code."""

    def __init__(self, cmd: int, code: int) -> None:
        super().__init__(f"ECU rejected command 0x{cmd:02X} with error {code}")
        self.cmd = cmd
        self.code = code


class ForbiddenCommand(Exception):
    """A command outside ALLOWED_COMMANDS was requested."""


def crc16(data: bytes, crc: int = 0) -> int:
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = (crc << 1) ^ 0x1021 if crc & 0x8000 else crc << 1
            crc &= 0xFFFF
    return crc


def build_request(addr: int, cmd: int, payload: bytes = b"") -> bytes:
    frame = struct.pack(">BBHH", 0x5A, addr, cmd, len(payload) + 2) + payload
    return frame + struct.pack(">H", crc16(frame))


def build_reply(addr: int, cmd: int, payload: bytes = b"") -> bytes:
    frame = struct.pack(">BBHH", addr, 0x5A, cmd, len(payload) + 2) + payload
    return frame + struct.pack(">H", crc16(frame))


@dataclass
class Link:
    """One request/reply conversation with the ECU's main processor."""

    transport: Transport
    addr: int = ADDR_MAIN
    max_packet: int = 0x38

    def _refused(self, cmd: int, payload: bytes) -> str | None:
        if cmd == CMD_CALIBRATE_MAP and payload != CALIBRATE_MAP_PAYLOAD:
            return f"command 0x{cmd:02X} is not allowed"
        if cmd == CMD_FEATURE_UNLOCK and payload == _FEATURE_LOCK_WORDS:
            return "the code that disables an ECU's features is not allowed"
        if os.environ.get(READ_ONLY_ENV):
            is_read = cmd == CMD_FILE and len(payload) >= 4 and struct.unpack_from(">I", payload)[0] in _READ_ONLY_FILE_OPERATIONS
            if cmd not in RETRYABLE_COMMANDS and not is_read:
                return f"read-only mode: command 0x{cmd:02X} was not sent"
        return None

    def request_dangerous(self, cmd: int, payload: bytes = b"", timeout: float = DEFAULT_TIMEOUT, expect_reply: bool = True) -> bytes:
        """A firmware or factory reset command. Only firmware.py and Ecu.restore_factory call this."""
        if cmd not in DANGEROUS_COMMANDS:
            raise ForbiddenCommand(f"command 0x{cmd:02X} is not a firmware command")
        if os.environ.get(READ_ONLY_ENV):
            raise ForbiddenCommand(f"read-only mode: command 0x{cmd:02X} was not sent")
        if expect_reply:
            return self._exchange(cmd, payload, timeout)
        self.send_only(cmd, payload)
        return b""

    def send_only(self, cmd: int, payload: bytes = b"") -> None:
        self.transport.purge()
        request = build_request(self.addr, cmd, payload)
        if self.transport.write(request) != len(request):
            raise ProtocolError(f"short write sending command 0x{cmd:02X}")

    def request(self, cmd: int, payload: bytes = b"", timeout: float = DEFAULT_TIMEOUT) -> bytes:
        refused = "" if cmd in ALLOWED_COMMANDS else f"command 0x{cmd:02X} is not allowed"
        refused = refused or self._refused(cmd, payload)
        if refused:
            raise ForbiddenCommand(refused)
        attempts = READ_ATTEMPTS if cmd in RETRYABLE_COMMANDS else 1
        for attempt in range(1, attempts + 1):
            try:
                return self._exchange(cmd, payload, timeout)
            except EcuError:
                raise
            except ProtocolError as exc:
                log.warning("command 0x%02X attempt %d/%d: %s", cmd, attempt, attempts, exc)
                self._resync()
                if attempt == attempts:
                    raise
        raise AssertionError("unreachable")

    def _resync(self) -> None:
        """Wait for the line to go quiet, so a late reply is not read as the next one."""
        deadline = time.monotonic() + _RESYNC_LIMIT_S
        while time.monotonic() < deadline and self.transport.read(4096, _RESYNC_QUIET_S):
            pass

    def _exchange(self, cmd: int, payload: bytes, timeout: float) -> bytes:
        self.transport.purge()
        request = build_request(self.addr, cmd, payload)
        if self.transport.write(request) != len(request):
            raise ProtocolError(f"short write sending command 0x{cmd:02X}")
        while True:
            reply_cmd, body = self._read_reply(timeout)
            if reply_cmd == CMD_ERROR:
                code = struct.unpack(">H", body[:2])[0] if len(body) >= 2 else -1
                raise EcuError(cmd, code)
            if reply_cmd == cmd:
                return body

    def _read_reply(self, timeout: float) -> tuple[int, bytes]:
        header = self.transport.read(6, timeout)
        if len(header) != 6:
            raise ProtocolError(f"no reply from ECU (got {len(header)} of 6 header bytes)")
        addr, marker, cmd, length = struct.unpack(">BBHH", header)
        if addr != self.addr or marker not in (0x5A, 0x01):
            self.transport.purge()
            raise ProtocolError(f"bad reply header {header.hex()}")
        rest = self.transport.read(length, timeout)
        if len(rest) != length:
            raise ProtocolError(
                f"truncated reply from ECU (got {len(rest)} of {length} bytes for command 0x{cmd:02X})"
            )
        if crc16(header + rest) != 0:
            raise ProtocolError("reply failed CRC check")
        return cmd, rest[:-2]
