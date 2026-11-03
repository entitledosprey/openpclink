"""The ECU's internal log files: listing, downloading and decoding. See docs/PROTOCOL.md."""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Callable

from . import protocol as p
from .logfile import KIND_SECTION, Log, Marker, Series

MAX_LOG_CHANNELS = 1000
MAX_DIVIDER = 10000
RETRIES = 10
RECORD_SIZE = 20
RECORD_NAMES = {1: "Record Start", 3: "Record Resume", 4: "ECU Log Marker"}
RECORD_RESYNC = 2


class EcuLogError(p.ProtocolError):
    pass


@dataclass
class LogInfo:
    tick_rate: int
    file_count: int
    record_magic: int
    max_channels: int

    @classmethod
    def parse(cls, body: bytes) -> "LogInfo":
        if len(body) < 0x13:
            raise EcuLogError("the ECU's log information is too short")
        return cls(
            struct.unpack_from(">I", body, 0)[0], struct.unpack_from(">H", body, 4)[0],
            struct.unpack_from(">I", body, 0x0D)[0], struct.unpack_from(">H", body, 0x11)[0],
        )

    @property
    def header_size(self) -> int:
        return self.max_channels * 8 + 8


@dataclass
class EcuLogEntry:
    name: str
    size: int
    kind: int = 0
    header: "EcuLogHeader | None" = None


@dataclass
class EcuLogHeader:
    channels: list[tuple[int, int]] = field(default_factory=list)
    engine_starts: int = 0
    start_time: int = 0
    date: tuple[int, int, int] | None = None
    clock: tuple[int, int, int] | None = None
    data_start: int = 0

    def bytes_per_second(self, tick_rate: int) -> float:
        return 4 * sum(tick_rate / divider for _id, divider in self.channels if divider)

    def duration(self, size: int, tick_rate: int) -> float:
        rate = self.bytes_per_second(tick_rate)
        return size / rate if rate else 0.0

    def date_text(self) -> str:
        if self.date is None or self.clock is None:
            return "????"
        return "{:04d}/{:02d}/{:02d} {:02d}:{:02d}:{:02d}".format(*self.date, *self.clock)


def parse_header(data: bytes) -> EcuLogHeader:
    if len(data) < 0x10:
        raise EcuLogError("the ECU log header is too short")
    has_date = data[1] != 0
    count = data[3]
    if not 0 < count <= MAX_LOG_CHANNELS:
        raise EcuLogError(f"the ECU log header gives {count} channels")
    base = 0x18 if has_date else 0x10
    if len(data) < base + count * 8:
        raise EcuLogError("the ECU log header is cut short")
    engine_starts, start_time = struct.unpack_from(">II", data, 4)
    header = EcuLogHeader(engine_starts=engine_starts, start_time=0 if start_time == 0xFFFFFFFF else start_time)
    if has_date:
        date, clock = struct.unpack_from(">II", data, 0x10)
        header.date = (date >> 16, (date >> 8) & 0xFF, date & 0xFF)
        header.clock = ((clock >> 24) & 0xFF, (clock >> 16) & 0xFF, (clock >> 8) & 0xFF)
    for at in range(count):
        data_id, divider = struct.unpack_from(">II", data, base + at * 8)
        header.channels.append((data_id, min(divider, MAX_DIVIDER)))
    end = base + count * 8
    header.data_start = end + (end & 3)  # Not a normal round-up to four, but what PCLink does.
    return header


def decode_samples(data: bytes, header: EcuLogHeader, info: LogInfo, series: list[Series], log: Log, start: float = 0.0) -> float:
    """Adds one file's samples to the series and returns the time it ends at."""
    dt = 1.0 / info.tick_rate if info.tick_rate else 1.0
    used = len(data)
    pos = header.data_start
    tick = 0
    when = start

    def at_record() -> bool:
        return pos + 4 <= used and struct.unpack_from(">I", data, pos)[0] == info.record_magic

    while pos + 4 <= used:
        if at_record():
            if pos + RECORD_SIZE > used:
                break
            kind = struct.unpack_from(">I", data, pos + 4)[0]
            if not 1 <= kind <= 4:
                break
            tick = struct.unpack_from(">I", data, pos + 16)[0]
            pos += RECORD_SIZE
            if kind != RECORD_RESYNC:
                log.add_marker(Marker(1 if kind == 4 else KIND_SECTION, RECORD_NAMES[kind], when))
        before = pos
        while pos + 4 <= used and not at_record():
            for (_id, divider), channel in zip(header.channels, series):
                if at_record() or pos + 4 > used:
                    break
                if divider and tick % divider == 0:
                    channel.append(when, struct.unpack_from(">f", data, pos)[0])
                    pos += 4
            when += dt
            tick += 1
            if pos == before and tick > MAX_DIVIDER * 2:
                raise EcuLogError("this ECU log has no channels with data")
    return when


class EcuLogs:
    """File operations on a connected ECU, by polling. Works over every kind of link."""

    def __init__(self, link: p.Link) -> None:
        self.link = link

    @staticmethod
    def _file_request(operation: int, b4: int, b5: int, offset: int = 0, name: str | None = None) -> bytes:
        payload = struct.pack(">IBBII", operation, b4, b5, offset, 0)
        return payload if name is None else payload + name.encode("latin-1", "replace") + b"\0"

    def info(self) -> LogInfo:
        return LogInfo.parse(self.link.request(p.CMD_LOG_INFO))

    def pause(self) -> None:
        self.link.request(p.CMD_FILE, self._file_request(p.FILE_PAUSE_LOGGING, 1, 0) + bytes(4))

    def resume(self) -> None:
        self.link.request(p.CMD_FILE, self._file_request(p.FILE_RESUME_LOGGING, 1, 0) + bytes(4))

    def list_files(self) -> tuple[LogInfo, list[EcuLogEntry]]:
        info = self.info()
        entries = []
        for _ in range(info.file_count):
            body = self.link.request(p.CMD_FILE, self._file_request(p.FILE_NEXT, 1, 1) + bytes(1))
            if len(body) < 9:
                raise EcuLogError("a log file entry from the ECU is too short")
            size = struct.unpack_from(">I", body, 4)[0]
            entries.append(EcuLogEntry(body[8:].split(b"\0", 1)[0].decode("latin-1"), size, body[0]))
        return info, entries

    def delete(self, name: str) -> None:
        self.link.request(p.CMD_FILE, self._file_request(p.FILE_DELETE, 1, 1, 0, name))

    def read_log_file(self, name: str, size: int, progress: Callable[[int], bool] | None = None, header_only: bool = False) -> bytes:
        """`progress(bytes)` may return False to cancel."""
        data = bytearray()
        failures = 0
        while size > 0:
            try:
                chunk = self.link.request(p.CMD_FILE, self._file_request(p.FILE_READ, 1, 0, len(data), name), 1.0)
            except p.ProtocolError:
                failures += 1
                if failures >= RETRIES:
                    raise
                continue
            failures = 0
            if not chunk:
                break
            data += chunk[: size - len(data)]
            if header_only or len(data) >= size:
                break
            if progress and progress(len(data)) is False:
                break
        if not header_only:
            self.link.request(p.CMD_FILE, self._file_request(p.FILE_READ, 1, 0, 0, ""), 1.0)
        return bytes(data)

    def read_named(self, name: str) -> bytes:
        """An ECU file other than a log: the memo or the CAN file."""
        data = bytearray()
        while True:
            chunk = self.link.request(p.CMD_FILE, self._file_request(p.FILE_READ, 0, 0, len(data), name), 1.0)
            if not chunk:
                return bytes(data)
            data += chunk

    def write_named(self, command: int, data: bytes) -> None:
        limit = (max(self.link.max_packet - 4, 2)) & ~1
        for offset in range(0, len(data), limit):
            self.link.request(command, struct.pack(">H", offset) + data[offset : offset + limit], 1.0)
        self.link.request(command, struct.pack(">H", 1), 1.0)


def build_logs(files: list[tuple[str, bytes]], info: LogInfo, describe: Callable[[int], tuple[str, str]]) -> list[Log]:
    """Decode files, joining neighbours with the same channels into one log, as PCLink does."""
    logs: list[Log] = []
    previous: list[tuple[int, int]] | None = None
    when = 0.0
    log: Log | None = None
    series: list[Series] = []
    for _name, data in files:
        header = parse_header(data)
        if log is None or header.channels != previous:
            log = Log()
            series = []
            dt = 1.0 / info.tick_rate if info.tick_rate else 1.0
            for data_id, divider in header.channels:
                name, units = describe(data_id)
                series.append(Series(data_id, name, units, period=divider * dt))
            log.series = series
            log.details["Data Source"] = "ECU Internal Datalog"
            logs.append(log)
            previous = header.channels
            when = 0.0
        when = decode_samples(data, header, info, series, log, when)
    for done in logs:
        done.markers.sort(key=lambda marker: marker.time)
        done.normalise_sections()
        done.rebuild()
        done.modified = True
    return logs
