"""Log files: PCLink's binary .llg5/.llgx, CSV, and recording live values.

The binary layout is in docs/PROTOCOL.md. Channels keep their own sample times, as
PCLink stores them; the views use a common time axis built from them.
"""
from __future__ import annotations

import bisect
import csv
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path

HEADER_SIZE = 0xD7
DETAILS_SIZE = 0x1C27
MARKER_SIZE = 0x1B1
CHANNEL_SIZE = 0x285
MAX_COMMON_POINTS = 400_000
KIND_SECTION = 0
SECTION_EPSILON = 1e-4

# (name, offset, slot size in bytes). Labels are PCLink's.
DETAIL_FIELDS = (
    ("Driver", 0x007, 200), ("Vehicle", 0x0CF, 200), ("Engine", 0x197, 200), ("ECU", 0x25F, 200),
    ("Notes", 0x327, 2000), ("Location", 0xAF7, 200), ("Event", 0xBBF, 200), ("Log Date", 0xC87, 200),
    ("Log Time", 0xD4F, 200), ("Weather", 0xE17, 200), ("Environment", 0xEDF, 2000),
    ("Download Date", 0x16AF, 200), ("Download Time", 0x1777, 200), ("Firmware Version", 0x183F, 200),
    ("Software Version", 0x1907, 200), ("Data Source", 0x19CF, 200), ("Serial Number", 0x1A97, 200),
    ("Name", 0x1B5F, 200),
)
READ_ONLY_DETAILS = frozenset({"ECU", "Download Date", "Download Time", "Firmware Version", "Software Version", "Data Source", "Serial Number"})


class LogFormatError(ValueError):
    pass


@dataclass
class Series:
    data_id: int
    name: str
    units: str = ""
    unit_key: str = ""
    period: float = 1.0
    times: list[float] = field(default_factory=list)
    values: list[float] = field(default_factory=list)
    _repeating: bool = False

    def append(self, when: float, value: float, keep_all: bool = True) -> None:
        """Runs of equal values shrink to two points unless every point is wanted."""
        if not keep_all and self.values and self.values[-1] == value:
            if self._repeating:
                self.times[-1] = when
                return
            self._repeating = True
        else:
            self._repeating = False
        self.times.append(when)
        self.values.append(value)

    def value_at(self, when: float) -> float:
        """The nearest sample, as PCLink looks one up."""
        if not self.times:
            return 0.0
        at = bisect.bisect_left(self.times, when)
        if at <= 0:
            return self.values[0]
        if at >= len(self.times):
            return self.values[-1]
        return self.values[at] if self.times[at] - when < when - self.times[at - 1] else self.values[at - 1]


@dataclass
class Marker:
    kind: int
    name: str
    time: float


@dataclass
class Section:
    name: str
    start: float
    end: float


class Log:
    """A log. `time` and `channels` hold the values on one time axis for drawing."""

    def __init__(self, names: list[str] | None = None, times: list[float] | None = None, channels: list[list[float]] | None = None) -> None:
        self.series: list[Series] = []
        self.markers: list[Marker] = []
        self.details: dict[str, str] = {}
        self.path: Path | None = None
        self.modified = False
        self.names: list[str] = list(names or [])
        self.time: list[float] = list(times or [])
        self.channels: list[list[float]] = [list(c) for c in (channels or [])]
        self._ids: dict[int, int] = {}
        if names:
            self.series = [Series(0, name, times=list(times or []), values=list(data)) for name, data in zip(names, channels or [])]
            self.normalise_sections()

    def channel_of(self, data_id: int) -> int | None:
        return self._ids.get(data_id)

    def unit_of(self, index: int) -> str:
        return self.series[index].units if index < len(self.series) else ""

    def rebuild(self) -> None:
        """Lay every channel on one time axis, at the fastest channel's rate."""
        self.names = [series.name for series in self.series]
        self._ids = {series.data_id: index for index, series in enumerate(self.series) if series.data_id}
        low, high = self.time_range()
        filled = [series for series in self.series if series.times]
        if not filled or high <= low:
            self.time = [low] if filled else []
            self.channels = [[series.value_at(low)] if filled else [] for series in self.series]
            return
        step = min((s.period for s in filled if s.period > 0), default=1.0)
        for series in filled:
            if len(series.times) > 1:
                step = min(step, (series.times[-1] - series.times[0]) / (len(series.times) - 1))
        count = min(int((high - low) / step) + 1 if step > 0 else 2, MAX_COMMON_POINTS)
        step = (high - low) / max(count - 1, 1)
        self.time = [low + i * step for i in range(count)]
        self.channels = [[series.value_at(when) for when in self.time] for series in self.series]

    def time_range(self) -> tuple[float, float]:
        starts = [series.times[0] for series in self.series if series.times]
        ends = [series.times[-1] for series in self.series if series.times]
        return (min(starts), max(ends)) if starts else (0.0, 0.0)

    def add_marker(self, marker: Marker) -> None:
        at = bisect.bisect_right([m.time for m in self.markers], marker.time)
        self.markers.insert(at, marker)

    def normalise_sections(self) -> None:
        """Make sure section markers span the log, as PCLink does on loading."""
        low, high = self.time_range()
        if not self.series or low == high:
            return
        if not self.markers:
            self.markers = [Marker(KIND_SECTION, "SOL", low), Marker(KIND_SECTION, "EOL", high)]
            return
        if abs(low - self.markers[0].time) >= SECTION_EPSILON:
            self.markers.insert(0, Marker(KIND_SECTION, "SOL", low))
        else:
            self.markers[0].time = low
        if abs(high - self.markers[-1].time) > SECTION_EPSILON:
            self.markers.append(Marker(KIND_SECTION, "EOL", high))

    def sections(self) -> list[Section]:
        bounds = [m for m in self.markers if m.kind == KIND_SECTION]
        return [
            Section(a.name, a.time, b.time)
            for a, b in zip(bounds, bounds[1:])
            if b.time - a.time > 1e-6
        ]


def _utf16(blob: bytes, start: int, size: int) -> str:
    raw = blob[start : start + size]
    end = 0
    while end + 1 < len(raw) and raw[end : end + 2] != b"\0\0":
        end += 2
    return raw[:end].decode("utf-16-le", "replace")


def _put_utf16(blob: bytearray, start: int, size: int, text: str) -> None:
    raw = text.encode("utf-16-le")[: size - 2]
    blob[start : start + len(raw)] = raw


def _block(data: bytes, at: int, tag: bytes, versions: bytes) -> tuple[bytes, int, int]:
    """Returns the block's bytes, its version digit and where the next block starts."""
    if at + 7 > len(data):
        raise LogFormatError("the log file ends early")
    size = struct.unpack_from("<I", data, at)[0]
    if data[at + 4 : at + 6] != tag or data[at + 6] not in versions:
        raise LogFormatError(f"expected a {tag.decode()} block, found {data[at + 4 : at + 7]!r}")
    if size < 7 or at + size > len(data):
        raise LogFormatError("a block in the log file has an impossible size")
    return data[at : at + size], data[at + 6], at + size


def parse_link_log(data: bytes) -> Log:
    header, version, at = _block(data, 0, b"lf", b"123")
    channel_count = struct.unpack_from("<I", header, 0x07)[0]
    marker_count = struct.unpack_from("<I", header, 0xD3)[0] if len(header) >= 0xD7 else 0
    details, _details_version, at = _block(data, at, b"ld", b"12")
    log = Log()
    for name, offset, size in DETAIL_FIELDS:
        if offset < len(details):
            log.details[name] = _utf16(details, offset, size)
    if version > ord("1"):
        for _ in range(marker_count):
            block, _v, at = _block(data, at, b"lm", b"1")
            log.markers.append(Marker(block[7], _utf16(block, 8, 200), struct.unpack_from("<d", block, 0xD0)[0]))
    for _ in range(channel_count):
        try:
            block, channel_version, at = _block(data, at, b"ds", b"123")
            count = struct.unpack_from("<I", block, 0x07)[0]
            end = at + count * 8
            if end > len(data):
                raise LogFormatError("a channel in the log file is cut short")
        except LogFormatError:
            if len(log.series) > 1:
                break  # PCLink keeps a part-loaded log the same way.
            raise
        flagged = channel_version == ord("3") and block[0x267] == 1
        units = _utf16(block, 0x19F, 200)
        series = Series(
            struct.unpack_from("<I", block, 0xD3)[0], _utf16(block, 0xD7, 200),
            units="" if flagged else units, unit_key=units if flagged else "",
            period=struct.unpack_from("<d", block, 0x26C)[0] if len(block) >= 0x274 else 1.0,
        )
        pairs = sorted(struct.iter_unpack("<ff", data[at:end]), key=lambda pair: pair[0])
        series.times = [float(t) for t, _ in pairs]
        series.values = [float(v) for _, v in pairs]
        log.series.append(series)
        at = end
    log.normalise_sections()
    log.rebuild()
    return log


def _marker_block(marker: Marker, when: float) -> bytes:
    block = bytearray(MARKER_SIZE)
    struct.pack_into("<I", block, 0, MARKER_SIZE)
    block[4:7] = b"lm1"
    block[7] = marker.kind & 0xFF
    _put_utf16(block, 8, 200, marker.name)
    struct.pack_into("<d", block, 0xD0, when)
    return bytes(block)


def _series_samples(series: Series, start: float | None, end: float | None) -> list[tuple[float, float]]:
    """All samples, or a time range closed with held values, as PCLink exports a range."""
    if start is None or end is None:
        return list(zip(series.times, series.values))
    out: list[tuple[float, float]] = []
    previous = series.values[0] if series.values else 0.0
    for when, value in zip(series.times, series.values):
        if when <= start:
            previous = value
            continue
        if not out:
            out.append((start, previous))
        if when > end:
            out.append((end, previous))
            return out
        out.append((when, value))
        previous = value
    if not out and series.values:
        out.append((start, previous))
    return out


def build_link_log(log: Log, start: float | None = None, end: float | None = None) -> bytes:
    if start is None or end is None:
        markers = [(m, m.time) for m in log.markers]
    else:
        markers = [(m, m.time) for m in log.markers if start <= m.time < end]
        if not any(when == start for _m, when in markers):
            earlier = [m for m in log.markers if m.kind == KIND_SECTION and m.time < start]
            if earlier:
                markers.insert(0, (earlier[-1], start))
    out = bytearray(HEADER_SIZE)
    struct.pack_into("<I", out, 0, HEADER_SIZE)
    out[4:7] = b"lf3"
    struct.pack_into("<I", out, 0x07, len(log.series))
    struct.pack_into("<I", out, 0xD3, len(markers))
    details = bytearray(DETAILS_SIZE)
    struct.pack_into("<I", details, 0, DETAILS_SIZE)
    details[4:7] = b"ld2"
    for name, offset, size in DETAIL_FIELDS:
        _put_utf16(details, offset, size, log.details.get(name, ""))
    out += details
    for marker, when in markers:
        out += _marker_block(marker, when)
    for series in log.series:
        samples = _series_samples(series, start, end)
        block = bytearray(CHANNEL_SIZE)
        struct.pack_into("<I", block, 0, CHANNEL_SIZE)
        block[4:7] = b"ds3"
        struct.pack_into("<I", block, 0x07, len(samples))
        struct.pack_into("<I", block, 0xD3, series.data_id)
        _put_utf16(block, 0xD7, 200, series.name)
        _put_utf16(block, 0x19F, 200, series.unit_key or series.units)
        block[0x267] = 1 if series.unit_key else 0
        struct.pack_into("<d", block, 0x26C, series.period)
        out += block
        out += b"".join(struct.pack("<ff", when, value) for when, value in samples)
    return bytes(out)


def read_link_log(path: Path) -> Log:
    log = parse_link_log(Path(path).read_bytes())
    log.path = Path(path)
    return log


def save_link_log(log: Log, path: Path) -> None:
    Path(path).write_bytes(build_link_log(log))
    log.path = Path(path)
    log.modified = False


@dataclass
class ExportOptions:
    """What PCLink's Export dialog offers for CSV."""

    start: float | None = None
    end: float | None = None
    file_time: bool = True
    section_time: bool = False
    device_information: bool = False
    file_information: bool = False
    driver_information: bool = False


def _number(value: float, places: int) -> str:
    text = f"{value:.{places}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def export_csv(log: Log, path: Path, options: ExportOptions | None = None) -> None:
    """Writes PCLink's CSV layout: every cell quoted, labels first."""
    options = options or ExportOptions()
    low, high = log.time_range()
    start = low if options.start is None else options.start
    end = high if options.end is None else options.end
    periods = [s.period for s in log.series if s.period > 0]
    step = min(periods + [1.0])
    lines: list[str] = []

    def label(name: str, value: str) -> None:
        lines.append(f'"{name}","{value}"\r\n')

    label("Name", log.details.get("Name", ""))
    if options.file_information:
        label("FileName", log.path.name if log.path else "")
        for name in ("Download Date", "Download Time"):
            label(name, log.details.get(name, ""))
    if options.driver_information:
        for name in ("Driver", "Vehicle", "Engine", "Notes", "Location", "Event", "Log Date", "Log Time", "Weather", "Environment"):
            label(name, log.details.get(name, ""))
    if options.device_information:
        for name in ("ECU", "Firmware Version", "Software Version", "Data Source", "Serial Number"):
            label(name, log.details.get(name, ""))
    names, units = [], []
    if options.file_time:
        names.append("File Time")
        units.append("s")
    if options.section_time:
        names.append("Section Time")
        units.append("s")
    names += [s.name for s in log.series]
    units += [s.units or s.unit_key for s in log.series]
    lines.append(",".join(f'"{name}"' for name in names) + "\r\n")
    lines.append(",".join(f'"{unit}"' for unit in units) + "\r\n\r\n")
    sections = log.sections()
    when = start
    while when <= end + step / 2:
        row = []
        if options.file_time:
            row.append(_number(when, 5))
        if options.section_time:
            section = next((s for s in sections if s.start <= when <= s.end), None)
            row.append(_number(when - (section.start if section else start), 5))
        row += [_number(series.value_at(when), 3) for series in log.series]
        lines.append(",".join(f'"{cell}"' for cell in row) + "\r\n")
        when += step
    Path(path).write_text("".join(lines), encoding="utf-8", newline="")


def read_csv_log(path: Path) -> Log:
    """Reads the older CSV logs this program wrote, and PCLink's own CSV export."""
    with open(path, newline="", encoding="utf-8") as stream:
        rows = list(csv.reader(stream))
    head = next((i for i, row in enumerate(rows) if row and row[0] in ("Time (s)", "File Time")), None)
    if head is None:
        raise LogFormatError("this CSV has no time column")
    header = rows[head]
    first = 1
    if len(header) > 1 and header[1] == "Section Time":
        first = 2
    names = header[first:]
    times: list[float] = []
    channels: list[list[float]] = [[] for _ in names]
    for row in rows[head + 1 :]:
        if len(row) != len(header):
            continue  # A log cut off by a crash can end mid-line.
        try:
            parsed = [float(cell) for cell in row[first:]]
            when = float(row[0])
        except ValueError:
            continue  # The units line.
        times.append(when)
        for channel, value in zip(channels, parsed):
            channel.append(value)
    log = Log(names, times, channels)
    log.path = Path(path)
    log._ids = {}
    return log


def read_log(path: Path) -> Log:
    return read_csv_log(path) if Path(path).suffix.lower() == ".csv" else read_link_log(path)


def save_log(log: Log, path: Path) -> None:
    if Path(path).suffix.lower() == ".csv":
        export_csv(log, path)
    else:
        save_link_log(log, path)


class PcLogger:
    """Records live values. Nothing reaches disk until it is saved, as in PCLink."""

    def __init__(self, path: Path | None, channels: list[tuple[int, str]], units: list[str] | None = None, period: float = 0.0, keep_all: bool = False) -> None:
        self.channels = channels
        self.path = Path(path) if path else None
        self.period = period
        self.keep_all = keep_all
        self.log = Log()
        self.log.modified = True
        units = units or [""] * len(channels)
        self.log.series = [Series(data_id, name, unit, period=period or 0.1) for (data_id, name), unit in zip(channels, units)]
        self._start = time.monotonic()
        self._last = -1e9
        self.samples = 0

    def sample(self, read) -> None:
        """`read(data_id)` returns the current value."""
        elapsed = time.monotonic() - self._start
        if elapsed - self._last < self.period:
            return
        self._last = elapsed
        for series in self.log.series:
            series.append(elapsed, read(series.data_id), self.keep_all)
        self.samples += 1

    def add_marker(self, name: str) -> None:
        self.log.add_marker(Marker(1, name, time.monotonic() - self._start))

    def finish(self) -> Log:
        if not self.period:
            for series in self.log.series:
                if len(series.times) > 1:
                    series.period = (series.times[-1] - series.times[0]) / (len(series.times) - 1) or series.period
        low, high = self.log.time_range()
        self.log.add_marker(Marker(KIND_SECTION, "PC Logged Data", low))
        self.log.add_marker(Marker(KIND_SECTION, "EOL", high))
        self.log.details.setdefault("Name", "PC Datalog - " + time.strftime("%Y-%m-%d %H;%M;%S"))
        self.log.details.setdefault("Data Source", "PC Datalog")
        self.log.rebuild()
        return self.log

    def close(self) -> None:
        log = self.finish()
        if self.path is not None:
            save_log(log, self.path)
