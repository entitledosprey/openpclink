"""CAN setup: PCLink's stream files, the ECU's stream settings, custom CAN files and the bus bridge."""
from __future__ import annotations

import struct
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from . import crypto
from . import protocol as p

MODULES = 2
CHANNELS = 10
STREAMS = 10
STREAM_PARAMETER_ID = 1125
RESET_STREAMS_ID = 1129
OBD_MODE_ID = 1729
MODE_IDS = (1730, 1731)
BIT_RATE_IDS = (1734, 1735)
CHANNEL_MODE_BASE = (808, 1744)
CHANNEL_RATE_BASE = (818, 1756)
CHANNEL_ID_LOW_BASE = (828, 1768)
CHANNEL_ID_HIGH_BASE = (940, 950)
BIT_RATES = ("1 Mbit/s", "500 kbit/s", "250 kbit/s", "125 kbit/s", "100 kbit/s")
EXTENDED = 0x80000000
LINK_DEVICE_FIND_ID = 0x1E000000
LINK_DEVICE_FIND_DATA = bytes.fromhex("EBAC1922EBAC1922")
MAX_CAN_FILE = 0x5DD


class CanFileError(ValueError):
    pass


@dataclass
class StreamParameter:
    data_id: int = 0
    start: int = 0
    width: int = 16
    byte_order: int = 1
    int_type: int = 0
    multiplier: int = 1
    divider: int = 1
    offset: int = 0

    def display(self, bus_value: float) -> float:
        return bus_value * self.multiplier / (self.divider or 1) + self.offset


@dataclass
class StreamFrame:
    length: int = 8
    id_position: int = 0
    compound_id: int = 0
    parameters: list[StreamParameter] = field(default_factory=list)


@dataclass
class Stream:
    frames: list[StreamFrame] = field(default_factory=list)
    source: str = ""


def decrypt_stream_file(data: bytes, key: bytes) -> bytes:
    """The whole file is RC4, or it can be plain XML."""
    if b"xml version" in data[:20]:
        return data
    plain = crypto.rc4(data, key)
    if b"xml version" not in plain[:20]:
        raise CanFileError("this is not a CAN stream file")
    return plain


def parse_stream_xml(xml: bytes) -> Stream:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise CanFileError(f"this is not a CAN stream file: {exc}") from exc
    node = root.find("CANStream")
    if node is None:
        raise CanFileError("The file you tried to open was not a CAN Stream file.")
    stream = Stream()

    def number(element: ET.Element, name: str, default: int) -> int:
        return int(float(element.get(name, default)))

    for frame_node in node.findall("Frame"):
        frame = StreamFrame(number(frame_node, "FrameLength", 0), number(frame_node, "IDPosition", 0), number(frame_node, "CompoundID", 0))
        for item in frame_node.findall("Parameter"):
            frame.parameters.append(StreamParameter(
                number(item, "DataID", 0), number(item, "StartPosition", 0), number(item, "Width", 0),
                number(item, "ByteOrder", 1), number(item, "IntType", 0), number(item, "Multiplier", 1),
                number(item, "Divider", 1), number(item, "Offset", 0),
            ))
        stream.frames.append(frame)
    return stream


def stream_to_xml(stream: Stream) -> bytes:
    root = ET.Element("LinkStorage", {"Version": "100", "Type": ".lcs"})
    node = ET.SubElement(root, "CANStream", {"SoftwareVersion": "1", "Electronz": "false", "CANVersion": "1.0"})
    for frame in stream.frames:
        frame_node = ET.SubElement(node, "Frame", {"FrameLength": str(frame.length), "IDPosition": str(frame.id_position), "CompoundID": str(frame.compound_id)})
        for item in frame.parameters:
            ET.SubElement(frame_node, "Parameter", {
                "DataID": str(item.data_id), "StartPosition": str(item.start), "Width": str(item.width),
                "ByteOrder": str(item.byte_order), "IntType": str(item.int_type), "Multiplier": str(item.multiplier),
                "Divider": str(item.divider), "Offset": str(item.offset),
            })
    ET.indent(root, "\t")
    return b'<?xml version="1.0"?>\r\n' + ET.tostring(root, encoding="utf-8")


def load_stream_file(path: Path, key: bytes) -> Stream:
    stream = parse_stream_xml(decrypt_stream_file(Path(path).read_bytes(), key))
    stream.source = Path(path).stem
    return stream


def save_stream_file(stream: Stream, path: Path, key: bytes) -> None:
    Path(path).write_bytes(crypto.rc4(stream_to_xml(stream), key))


def _stream_bytes(stream: Stream) -> bytes:
    out = bytearray([1, len(stream.frames)])
    for frame in stream.frames:
        out += bytes([len(frame.parameters), (frame.length & 0xF) | (frame.id_position << 4) & 0xFF, frame.compound_id & 0xFF, 0])
        for item in frame.parameters:
            flags = (item.width & 0x3F) | (item.int_type << 6) & 0x40 | (item.byte_order << 7) & 0x80
            out += struct.pack("<HBBhhh", item.data_id, item.start, flags, item.multiplier, item.divider, item.offset)
    return bytes(out)


def _checksum(data: bytes) -> int:
    return (-sum(data)) & 0xFFFF


def build_stream_blob(streams: list[Stream]) -> bytes:
    """Parameter 1125: all ten user streams, little-endian, with a negative byte sum."""
    body = b"".join(_stream_bytes(streams[i] if i < len(streams) else Stream()) for i in range(STREAMS))
    head = struct.pack("<H", len(body) + 2)
    return head + body + struct.pack("<H", _checksum(head + body))


def _parse_stream(blob: bytes, at: int) -> tuple[Stream, int]:
    frame_count = blob[at + 1]
    at += 2
    stream = Stream()
    for _ in range(frame_count):
        count, packed, compound, _pad = blob[at : at + 4]
        at += 4
        frame = StreamFrame(packed & 0xF, packed >> 4, compound)
        for _ in range(count):
            data_id, start, flags, multiplier, divider, offset = struct.unpack_from("<HBBhhh", blob, at)
            at += 10
            frame.parameters.append(StreamParameter(data_id, start, flags & 0x3F, flags >> 7, (flags >> 6) & 1, multiplier, divider, offset))
        stream.frames.append(frame)
    return stream, at


def parse_stream_blob(blob: bytes) -> list[Stream]:
    """An unset or inconsistent blob gives ten empty streams, as PCLink ignores it too."""
    empty = [Stream() for _ in range(STREAMS)]
    if len(blob) < 4:
        return empty
    total = struct.unpack_from("<H", blob, 0)[0]
    if blob[2] != 1 or total + 2 > len(blob) or total < 2:
        return empty
    if struct.unpack_from("<H", blob, total)[0] != _checksum(blob[:total]):
        return empty
    streams = []
    at = 2
    try:
        for _ in range(STREAMS):
            stream, at = _parse_stream(blob, at)
            streams.append(stream)
    except (IndexError, struct.error, ValueError):
        return empty
    return streams


@dataclass
class CanChannel:
    mode: int = 0
    rate: int = 0
    can_id: int = 0
    extended: bool = False


def split_id(can_id: int, extended: bool) -> tuple[int, int]:
    value = (can_id & 0x1FFFFFFF) | EXTENDED if extended else can_id & 0x7FF
    return value & 0xFFFF, value >> 16


def join_id(low: int, high: int) -> tuple[int, bool]:
    value = int(low) + int(high) * 0x10000
    return (value & 0x1FFFFFFF, True) if value & EXTENDED else (value & 0x7FF, False)


def read_channels(ecu, module: int) -> list[CanChannel]:
    channels = []
    for i in range(CHANNELS):
        can_id, extended = join_id(ecu.get(CHANNEL_ID_LOW_BASE[module] + i), ecu.get(CHANNEL_ID_HIGH_BASE[module] + i))
        channels.append(CanChannel(int(ecu.get(CHANNEL_MODE_BASE[module] + i)), int(ecu.get(CHANNEL_RATE_BASE[module] + i)), can_id, extended))
    return channels


def validate_channels(module: int, channels: list[CanChannel]) -> list[str]:
    return [
        f"CAN {module + 1} channel {i + 1} has an invalid ID."
        for i, channel in enumerate(channels) if channel.mode and not channel.can_id
    ]


def apply_channels(ecu, module: int, channels: list[CanChannel]) -> None:
    """In PCLink's order: rate, mode, then the two halves of the id."""
    for i, channel in enumerate(channels):
        low, high = split_id(channel.can_id, channel.extended)
        ecu.set(CHANNEL_RATE_BASE[module] + i, channel.rate)
        ecu.set(CHANNEL_MODE_BASE[module] + i, channel.mode)
        ecu.set(CHANNEL_ID_LOW_BASE[module] + i, low)
        ecu.set(CHANNEL_ID_HIGH_BASE[module] + i, high)


def read_streams(ecu) -> list[Stream]:
    if STREAM_PARAMETER_ID not in ecu.table:
        return [Stream() for _ in range(STREAMS)]
    return parse_stream_blob(bytes(int(value) & 0xFF for value in ecu.get_all(STREAM_PARAMETER_ID)))


def write_streams(ecu, streams: list[Stream]) -> None:
    blob = build_stream_blob(streams)
    capacity = ecu.table[STREAM_PARAMETER_ID].count
    if len(blob) > capacity:
        raise CanFileError("Too many parameters set in Streams")
    for index, value in enumerate(blob):
        if int(ecu.get(STREAM_PARAMETER_ID, index)) != value:
            ecu.set(STREAM_PARAMETER_ID, value, index)


@dataclass
class CustomCanFile:
    """A .lcf file. One with a password stays closed unless it is known."""

    version: int = 1
    lock_serial: int = 0
    password: str = ""
    channels: list[tuple[int, int, Stream]] = field(default_factory=list)


def parse_can_file(plain: bytes) -> CustomCanFile:
    if len(plain) < 12:
        raise CanFileError("this CAN file is too short")
    total, version, count, serial, password_bytes = struct.unpack_from("<HBBII", plain, 0)
    if total > len(plain) or _checksum(plain[: total - 2]) != struct.unpack_from("<H", plain, total - 2)[0]:
        raise CanFileError("this CAN file failed its checksum")
    at = 12 + password_bytes
    result = CustomCanFile(version, serial, plain[12:at].decode("utf-16-le", "replace"))
    for _ in range(count):
        can_id, flags, stream_bytes = struct.unpack_from("<IHH", plain, at)
        at += 8
        stream, _end = _parse_stream(plain, at)
        at += stream_bytes
        result.channels.append((can_id, flags, stream))
    return result


def build_can_file(content: CustomCanFile) -> bytes:
    password = content.password.encode("utf-16-le")
    body = struct.pack("<BBII", content.version, len(content.channels), content.lock_serial, len(password)) + password
    for can_id, flags, stream in content.channels:
        record = _stream_bytes(stream)
        body += struct.pack("<IHH", can_id, flags, len(record)) + record
    total = len(body) + 4
    head = struct.pack("<H", total) + body
    return head + struct.pack("<H", _checksum(head))


def load_can_file(path: Path, key: tuple[int, ...]) -> tuple[CustomCanFile, bytes]:
    data = Path(path).read_bytes()
    plain = crypto.decode(data, key)
    if len(plain) > MAX_CAN_FILE:
        raise CanFileError("this CAN file is too large")
    return parse_can_file(plain), plain


def save_can_file(content: CustomCanFile, path: Path, key: tuple[int, ...]) -> None:
    Path(path).write_bytes(crypto.encode(build_can_file(content), key))


@dataclass
class CanFrame:
    can_id: int
    extended: bool
    data: bytes


class CanBridge:
    """Sends and hears frames through the ECU. Whatever is transmitted reaches the vehicle's bus."""

    FRAME_SIZE = 14

    def __init__(self, link: p.Link, module: int = 0) -> None:
        self.link, self.module = link, module

    def start(self) -> None:
        self.link.request(p.CMD_CAN_CONTROL, bytes(4))
        self.link.request(p.CMD_CAN_CONTROL, bytes([self.module, 0, 0, 0]))

    def stop(self) -> None:
        self.link.request(p.CMD_CAN_CONTROL, bytes(4))
        time.sleep(0.2)

    def receive(self) -> list[CanFrame]:
        body = self.link.request(p.CMD_CAN_RECEIVE, bytes(1))
        frames = []
        for at in range(0, len(body) - self.FRAME_SIZE + 1, self.FRAME_SIZE):
            can_id, flags, dlc = struct.unpack_from(">IBB", body, at)
            frames.append(CanFrame(can_id, bool(flags & 1), body[at + 6 : at + 6 + min(dlc, 8)]))
        return frames

    def transmit(self, frame: CanFrame) -> None:
        payload = struct.pack(">IBB", frame.can_id, 1 if frame.extended else 0, len(frame.data)) + frame.data.ljust(8, b"\0") + bytes(2)
        self.link.request(p.CMD_CAN_TRANSMIT, payload)

    def find_link_devices(self, sleep=time.sleep) -> list[tuple[int, int, int]]:
        """Asks Link devices on the bus to answer; returns (id, device code, serial)."""
        self.receive()
        probe = CanFrame(LINK_DEVICE_FIND_ID, True, LINK_DEVICE_FIND_DATA)
        self.transmit(probe)
        sleep(0.2)
        self.receive()
        self.transmit(probe)
        found: dict[int, tuple[int, int, int]] = {}
        for _ in range(10):
            sleep(0.005)
            for frame in self.receive():
                if frame.can_id & LINK_DEVICE_FIND_ID == LINK_DEVICE_FIND_ID and len(frame.data) == 8 and frame.data[0] == 1:
                    code = struct.unpack_from(">H", frame.data, 2)[0]
                    serial = struct.unpack_from(">I", frame.data, 4)[0]
                    found[frame.can_id] = (frame.can_id, code, serial)
        return list(found.values())
