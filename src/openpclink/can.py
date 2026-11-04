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
    if not key:
        raise CanFileError("the CAN file key is missing; import it from your own PCLink installer")
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
    """Parameter 1125: all ten user streams, little-endian, with a negative byte sum.

    The leading length counts the whole blob, checksum included (confirmed against real PCLink)."""
    body = b"".join(_stream_bytes(streams[i] if i < len(streams) else Stream()) for i in range(STREAMS))
    head = struct.pack("<H", len(body) + 4)
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
    if blob[2] != 1 or total > len(blob) or total < 4:
        return empty
    if struct.unpack_from("<H", blob, total - 2)[0] != _checksum(blob[: total - 2]):
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
    password: str = field(default="", repr=False)
    channels: list[tuple[int, int, Stream]] = field(default_factory=list)

    @property
    def has_password(self) -> bool:
        return bool(self.password)

    def check_password(self, entered: str) -> bool:
        """The owner's flow only: the typed text must equal the file's own password."""
        import hmac

        return hmac.compare_digest(entered.encode("utf-8"), self.password.encode("utf-8"))


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
        """Real PCLink opens the bridge with a zero word, then names the module 1 or 2."""
        self.link.request(p.CMD_CAN_CONTROL, bytes(4))
        self.link.request(p.CMD_CAN_CONTROL, bytes([self.module + 1, 0, 0, 0]))

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


# ---- Mode tab settings ------------------------------------------------------

RATES_HZ = (1, 2, 5, 10, 20, 50, 100, 200)
USER_DEFINED_MODES = (1, 200)
TRANSMIT_MODES = range(1, 128)
RECEIVE_USER_STREAM = 0x80
NODE_LIMIT = 0x7F


def rate_value(hertz: int) -> int:
    """A transmit rate in milliseconds. The ECU stores it in 5 ms steps (confirmed: 1 Hz is raw 200, 5 Hz raw 40)."""
    return 1000 // hertz


def rate_index(value: int) -> int:
    return next((i for i, hertz in enumerate(RATES_HZ) if rate_value(hertz) == int(value)), -1)


def is_receive(mode: int) -> bool:
    return mode >= RECEIVE_USER_STREAM


@dataclass
class ModuleSettings:
    mode: int = 0
    bit_rate: int = 0
    channels: list[CanChannel] = field(default_factory=lambda: [CanChannel() for _ in range(CHANNELS)])


def read_module(ecu, module: int) -> ModuleSettings:
    def number(data_id: int) -> int:
        return int(ecu.get(data_id)) if data_id in ecu.table else 0

    if CHANNEL_MODE_BASE[module] not in ecu.table:
        return ModuleSettings()
    return ModuleSettings(number(MODE_IDS[module]), number(BIT_RATE_IDS[module]), read_channels(ecu, module))


def read_obd(ecu) -> int:
    return int(ecu.get(OBD_MODE_ID)) if OBD_MODE_ID in ecu.table else 0


def stream_is_writable(stream: Stream, writable) -> bool:
    return all(writable(item.data_id) for frame in stream.frames for item in frame.parameters)


def validate_modules(modules: list[ModuleSettings], streams: list[Stream], obd: int, writable) -> list[str]:
    """PCLink's three checks, with its wording (S4798, S4799, S4800)."""
    problems = []
    for number, module in enumerate(modules, 1):
        if module.mode not in USER_DEFINED_MODES:
            continue
        for index, channel in enumerate(module.channels, 1):
            stream = channel.mode - RECEIVE_USER_STREAM
            if is_receive(channel.mode) and 0 <= stream < STREAMS and not stream_is_writable(streams[stream], writable):
                problems.append(f"CAN {number} channel {index} has a parameter that cannot be written by CAN.")
            if channel.mode and not channel.can_id:
                problems.append(f"CAN {number} channel {index} has an invalid ID.")
    if obd in (1, 2) and modules[obd - 1].mode == 0:
        problems.append("ODB will not function as it is set to a module that is not enabled.")
    return problems


def _set_changed(ecu, data_id: int, value: int, index: int = 0) -> None:
    if data_id in ecu.table and int(ecu.get(data_id, index)) != int(value):
        ecu.set(data_id, value, index)


def apply_modules(ecu, modules: list[ModuleSettings], obd: int, streams: list[Stream]) -> None:
    """PCLink's order, seen on the wire: the stream blob in full, each module's mode and bit rate then its
    channels (rate, mode, id low, id high; only when the module is User Defined), OBD, then the stream reset."""
    if STREAM_PARAMETER_ID in ecu.table:
        blob = build_stream_blob(streams)
        if len(blob) > ecu.table[STREAM_PARAMETER_ID].count:
            raise CanFileError("Too many parameters set in Streams")
        for index, value in enumerate(blob):
            ecu.set(STREAM_PARAMETER_ID, value, index)
    for number, module in enumerate(modules):
        if CHANNEL_MODE_BASE[number] not in ecu.table:
            continue
        _set_changed(ecu, MODE_IDS[number], module.mode)
        _set_changed(ecu, BIT_RATE_IDS[number], module.bit_rate)
        if module.mode not in USER_DEFINED_MODES:
            continue
        for i, channel in enumerate(module.channels):
            low, high = split_id(channel.can_id, channel.extended)
            _set_changed(ecu, CHANNEL_RATE_BASE[number] + i, channel.rate)
            _set_changed(ecu, CHANNEL_MODE_BASE[number] + i, channel.mode)
            _set_changed(ecu, CHANNEL_ID_LOW_BASE[number] + i, low)
            _set_changed(ecu, CHANNEL_ID_HIGH_BASE[number] + i, high)
    _set_changed(ecu, OBD_MODE_ID, obd)
    if RESET_STREAMS_ID in ecu.table:
        ecu.set(RESET_STREAMS_ID, 1)
        ecu.set(RESET_STREAMS_ID, 0)


# ---- Test Calculator --------------------------------------------------------

def scale_factors(bus_min: float, bus_max: float, shown_min: float, shown_max: float, transmit: bool) -> tuple[int, int, float]:
    """The Scale Generator: multiplier, divider and offset taking one range onto the other.

    Transmit maps what PCLink shows onto the bus, receive the other way. The ratio is a reduced fraction
    that fits PCLink's 16-bit multiplier and divider."""
    from fractions import Fraction

    src_min, src_max, dst_min, dst_max = (shown_min, shown_max, bus_min, bus_max) if transmit else (bus_min, bus_max, shown_min, shown_max)
    if src_max == src_min:
        return 1, 1, 0.0
    ratio = Fraction(dst_max - dst_min) / Fraction(src_max - src_min)
    ratio = ratio.limit_denominator(32767)
    while abs(ratio.numerator) > 32767:
        ratio = Fraction(round(float(ratio)), 1)
    return ratio.numerator, ratio.denominator, dst_min - float(src_min * ratio)


def calculate(value: float, multiplier: float, divider: float, offset: float) -> float:
    return value * multiplier / (divider or 1) + offset


def stored_in_ecu(ecu, data_id: int) -> str:
    """The 'Stored in ECU with:' text of the parameter description (S3676-S3679)."""
    loc = ecu.table.get(data_id)
    if loc is None:
        return "Unknown"
    bits = 32 if loc.is_float else loc.width * 8 if loc.width else bin(loc.mask).count("1") or 8
    return f"Stored in ECU with:\nECU Resolution {loc.scale:g}\nECU Width (bits) {bits}\nDisplay Offset {loc.add:g}"


# ---- Link CAN devices -------------------------------------------------------

OP_WRITE_INT, OP_READ_INT, OP_WRITE_FLOAT, OP_READ_FLOAT = 0x02, 0x03, 0x04, 0x05
OP_IDENTIFY, OP_STORE, OP_WRITE_TEXT, OP_READ_TEXT, OP_COMMIT_TEXT = 0x06, 0x07, 0x09, 0x0A, 0x0B
REGISTER_TERMINATOR, REGISTER_BIT_RATE = 0x18, 0x19
FIELD_INTEGER, FIELD_FLOAT, FIELD_CHOICE = 1, 0, 3
LAMBDA_COMMAND = 0x18
LINK_REPLY_ID = 0x1F000000


@dataclass
class DeviceField:
    """One row of a device definition: <Parameter DataId Name Type DecPlaces>. DataId is the register.

    Type 1 is an unsigned integer, 3 a switch, anything else a float with DecPlaces digits
    (FUN_0040c1b8, FUN_0040d950)."""

    register: int
    name: str
    kind: int = FIELD_FLOAT
    decimals: int = 0


# The only registers traced in PCLink itself. The rest come from the definition file PCLink loads
# for the device code, which this installation does not carry.
KNOWN_DEVICE_FIELDS = (
    DeviceField(REGISTER_TERMINATOR, "Enable terminator", FIELD_CHOICE),
    DeviceField(REGISTER_BIT_RATE, "Bit rate", FIELD_INTEGER),
)


def parse_device_definition(xml: bytes) -> list[DeviceField]:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise CanFileError(f"this is not a device definition: {exc}") from exc
    fields = []
    for node in root.iter("Parameter"):
        fields.append(DeviceField(int(node.get("DataId", 0)), node.get("Name", ""), int(node.get("Type", 0)), int(node.get("DecPlaces", 0))))
    return fields


class DeviceError(RuntimeError):
    pass


class LinkDevice:
    """A Link CAN device found on the bus. All requests are extended frames to the device id."""

    POLLS, POLL_S = 15, 0.005

    def __init__(self, bridge: CanBridge, device_id: int, code: int = 0, serial: int = 0) -> None:
        self.bridge, self.device_id, self.code, self.serial = bridge, device_id, code, serial

    def request(self, op: int, register: int = 0, value: bytes = bytes(4), index: int = 0, sleep=time.sleep) -> bytes:
        data = bytes([op, index]) + struct.pack(">H", register) + value[:4].ljust(4, b"\0")
        self.bridge.transmit(CanFrame(self.device_id, True, data))
        for _ in range(self.POLLS):
            sleep(self.POLL_S)
            for frame in self.bridge.receive():
                if frame.can_id == self.device_id | LINK_REPLY_ID and len(frame.data) == 8 and frame.data[0] == op:
                    if frame.data[1] == 0:
                        raise DeviceError("the device refused the request")
                    return frame.data[4:8]
        raise DeviceError("the device did not answer")

    def read_int(self, register: int) -> int:
        return struct.unpack(">I", self.request(OP_READ_INT, register))[0]

    def write_int(self, register: int, value: int) -> None:
        self.request(OP_WRITE_INT, register, struct.pack(">I", value & 0xFFFFFFFF))

    def read_float(self, register: int) -> float:
        return struct.unpack(">f", self.request(OP_READ_FLOAT, register))[0]

    def write_float(self, register: int, value: float) -> None:
        self.request(OP_WRITE_FLOAT, register, struct.pack(">f", value))

    def identify(self, on: bool) -> None:
        self.request(OP_IDENTIFY, 0, struct.pack(">I", 1 if on else 0))

    def store(self) -> None:
        self.request(OP_STORE)

    def read_text(self, limit: int = 32) -> str:
        units = []
        for at in range(0, limit, 2):
            chunk = self.request(OP_READ_TEXT, 0, bytes(4), at)
            pair = struct.unpack(">HH", chunk)
            for unit in pair:
                if unit == 0:
                    return "".join(map(chr, units))
                units.append(unit)
        return "".join(map(chr, units))

    def write_text(self, text: str) -> None:
        units = [ord(c) for c in text] + [0]
        for at in range(0, len(units), 2):
            pair = units[at : at + 2] + [0] * (2 - len(units[at : at + 2]))
            self.request(OP_WRITE_TEXT, 0, struct.pack(">HH", *pair), at)
        self.request(OP_COMMIT_TEXT)


def set_lambda(bridge: CanBridge, device_id: int, lambda_index: int, bit_rate_index: int) -> None:
    """CAN Lambda setup (FUN_0041472c): lambda 1-5 and the bit rate, as a three byte frame to the device."""
    bridge.transmit(CanFrame(device_id, True, bytes([LAMBDA_COMMAND, lambda_index, abs(bit_rate_index - 4)])))


# ---- CANopen keypads --------------------------------------------------------

KEYPAD_LIMIT = 8
SDO_REQUEST, SDO_REPLY = 0x600, 0x580
SDO_IDENTITY = bytes.fromhex("4018100100000000")
SDO_IDENTITY_REPLY = bytes.fromhex("43181001")
KEYPAD_COLOURS = ("Red", "Green", "Blue", "Yellow", "Cyan", "Violet", "White", "Amber/Orange", "Yellow/Green")
KEYPAD_SHOWS = ("Complete", "Fast Flash")
KEYPAD_BIT_RATES = ("1 Mbit/s", "500 kbit/s", "250 kbit/s", "125 kbit/s")
KEYPAD_MAX_LEVEL = 0x3F


@dataclass
class Keypad:
    node: int
    product: int


def scan_keypads(bridge: CanBridge, sleep=time.sleep) -> list[Keypad]:
    """Asks every CANopen node for its identity object and keeps up to eight answers."""
    found: list[Keypad] = []
    for node in range(NODE_LIMIT + 1):
        bridge.receive()
        bridge.transmit(CanFrame(SDO_REQUEST + node, False, SDO_IDENTITY))
        sleep(0.005)
        for frame in bridge.receive():
            if frame.can_id == SDO_REPLY + node and frame.data[:4] == SDO_IDENTITY_REPLY and not any(k.node == node for k in found):
                found.append(Keypad(node, struct.unpack_from("<H", frame.data, 4)[0]))
        if len(found) >= KEYPAD_LIMIT:
            break
    return found


def sdo_write(bridge: CanBridge, node: int, header: int, value: int = 0, size: int = 1) -> None:
    """FUN_00411a10: header bytes then the value, to 0x600 + node."""
    bridge.transmit(CanFrame(SDO_REQUEST + node, False, struct.pack(">I", header) + value.to_bytes(4, "little")))


def keypad_node_id(bridge: CanBridge, node: int, new_id: int) -> None:
    if new_id >= 0x80:
        raise DeviceError("Invalid Id")
    sdo_write(bridge, node, 0x2F132000, new_id)


def keypad_bit_rate(bridge: CanBridge, node: int, index: int) -> None:
    sdo_write(bridge, node, 0x2F102000, {1: 2, 2: 3, 3: 4}.get(index, 0))


def keypad_backlight(bridge: CanBridge, node: int, colour: int, level: int, variant: int = 0) -> None:
    """Colour 0 is off, 1-9 are KEYPAD_COLOURS. The register order depends on the keypad model (inferred)."""
    level = min(level, KEYPAD_MAX_LEVEL)
    if variant == 0:
        steps = [(0x2F032006, 0), (0x2F032002, 0)] if colour == 0 else [(0x2F032004, colour), (0x2F032006, level), (0x2F032002, level)]
    else:
        steps = [(0x2F032002, 0), (0x2F032005, 0), (0x2F032007, 0)] if colour == 0 else [(0x2F032003, colour)] + [(0x2F032000 | r, level) for r in (2, 5, 7)]
    for header, value in steps:
        sdo_write(bridge, node, header, value)


def keypad_key_brightness(bridge: CanBridge, node: int, level: int, variant: int = 0) -> None:
    level = min(level, KEYPAD_MAX_LEVEL)
    for register in (5, 1) if variant == 0 else (1, 4, 6):
        sdo_write(bridge, node, 0x2F032000 | register, level)


def keypad_startup_show(bridge: CanBridge, node: int, show: int) -> None:
    sdo_write(bridge, node, 0x2F142000, show)


def keypad_default_comms(bridge: CanBridge, node: int) -> None:
    """Heartbeat off, periodic state every 1500 ms, active at power on, then 'save' (inferred layout)."""
    sdo_write(bridge, node, 0x2B171000, 0)
    bridge.transmit(CanFrame(SDO_REQUEST + node, False, bytes.fromhex("2B001805") + struct.pack("<H", 1500) + bytes(2)))
    for header, value in ((0x2F112000, 0), (0x2F122000, 1), (0x2F002100, 0)):
        sdo_write(bridge, node, header, value)
    bridge.transmit(CanFrame(SDO_REQUEST + node, False, bytes.fromhex("23101001") + b"save"))
