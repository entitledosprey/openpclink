"""An ECU simulator for testing without hardware.

It answers the same wire protocol from a memory image built out of a map file,
over a pseudo-terminal or UDP. It encodes this project's reading of the
protocol, so passing against it is not proof of hardware compatibility on its
own; docs/VERIFICATION.md says what has been checked against what.
"""
from __future__ import annotations

import math
import os
import select
import socket
import struct
import threading
import time
import tty
import xml.etree.ElementTree as ET

from . import protocol as p

HARDWARE_G5 = 1012
CMD_FACTORY_RESET = 0x21  # The client must never send this; see protocol.py.
RPM_ID = 1000


def _crc_block(block_type: int, body: bytes) -> bytes:
    length = 8 + len(body)
    rest = struct.pack(">HHH", 0, length, block_type) + body
    # The ECU's check: crc(rest) continued over the stored CRC must be zero.
    crc = p.crc16(rest)
    return struct.pack(">H", crc) + rest


def _width_for(raws: list[int], signed: bool) -> int:
    for width in (1, 2, 4):
        bits = width * 8
        low, high = (-(1 << (bits - 1)), (1 << (bits - 1)) - 1) if signed else (0, (1 << bits) - 1)
        if all(low <= raw <= high for raw in raws):
            return 2 if width == 1 else width
    return 4


class SimEcu:
    """Holds memory and answers request frames."""

    def __init__(self, map_xml: bytes | None = None, firmware: tuple[int, int, int] = (7, 8, 1), hardware_family: int = HARDWARE_G5) -> None:
        self.firmware = firmware
        self.hardware_family = hardware_family
        self.flash_stored = 0
        self.factory_resets = 0
        self.statistics_cleared = 0
        self.map_calibrations = 0
        self.engine_running = True
        self.log: list[int] = []
        self.writes: list[bytes] = []
        self.state = 1
        self._build(map_xml)
        self.flash = bytes(self.memory)
        self._start = time.monotonic()

    def _build(self, map_xml: bytes | None) -> None:
        entries = self._parse_map(map_xml) if map_xml else self._demo_entries()
        runtime = [entry for entry in entries if entry["runtime"]]
        config = [entry for entry in entries if not entry["runtime"]]
        descriptor = bytearray()
        image_rt, image_cfg = bytearray(), bytearray()
        self.offsets: dict[int, int] = {}
        for ordered, image in ((runtime, image_rt), (config, image_cfg)):
            bit_pos = 0
            for entry in ordered:
                bit_pos = self._emit(entry, descriptor, image, bit_pos)
        self.runtime_size = len(image_rt)
        self.config_size = len(image_cfg)
        self.section2_size = 0
        self.memory = bytearray(image_rt + image_cfg)
        self.descriptor = bytes(descriptor)
        self.max_id = max((entry["id"] for entry in entries), default=0) + 1
        for data_id, offset in list(self.offsets.items()):
            if data_id in self._config_ids:
                self.offsets[data_id] = offset + self.runtime_size

    _config_ids: set[int] = set()

    def _emit(self, entry: dict, descriptor: bytearray, image: bytearray, bit_pos: int) -> int:
        data_id, kind = entry["id"], entry["kind"]
        read_only = 2 if entry["read_only"] else 0
        descriptor += struct.pack(">H", (data_id << 2) | read_only)
        if not entry["runtime"]:
            self._config_ids = self._config_ids | {data_id}
        if kind in (3, 5) and entry["count"] <= 256:
            bits = max(1, math.ceil(math.log2(max(entry["count"], 2))))
            descriptor += bytes([(kind << 4) | bits, entry["count"] & 0xFF])
            if not image or bit_pos == 0 or bit_pos + bits > 8:
                image.append(0)
                bit_pos = 0
            shift = 8 - bit_pos - bits
            mask = ((1 << bits) - 1) << shift
            value = int(entry["values"][0]) if entry["values"] else 0
            image[-1] |= (value << shift) & mask
            self.offsets[data_id] = len(image) - 1
            return bit_pos + bits
        if kind == 8:
            raw = entry["text"].encode("latin-1", "replace")[: entry["count"]].ljust(entry["count"], b"\0")
            descriptor += bytes([(8 << 4) | 1, entry["count"] & 0xFF])
            self.offsets[data_id] = len(image)
            image += raw
            return 0
        scale, add = entry["multi"], entry["offset"]
        raws = [round((value - add) / scale) if scale else 0 for value in entry["values"]] or [0]
        signed = any(raw < 0 for raw in raws) or entry["min"] < 0
        width = _width_for(raws, signed)
        code = {1: 1, 2: 2, 4: 0}[width]
        array = kind == 1
        count = len(raws)
        if kind in (3, 5):  # Too many options for a bitfield; stored as a number.
            kind = 0
        if kind == 9:  # Unscaled scalar: no count, scale or offset on the wire.
            raw = round(entry["values"][0]) if entry["values"] else 0
            width = _width_for([raw], raw < 0)
            descriptor += bytes([(9 << 4) | ({1: 1, 2: 2, 4: 0}[width] << 2) | (2 if raw < 0 else 0), 0])
            self.offsets[data_id] = len(image)
            image += int(raw).to_bytes(width, "big", signed=raw < 0)
            return 0
        descriptor += bytes(
            [((1 if array else kind) << 4) | (code << 2) | (2 if signed else 0), count if array and count < 256 else 0]
        )
        descriptor += struct.pack(">ff", scale, add)
        if array and count >= 256:
            descriptor += struct.pack(">I", count)
        self.offsets[data_id] = len(image)
        for raw in raws:
            image += int(raw).to_bytes(width, "big", signed=signed)
        return 0

    @staticmethod
    def _parse_map(map_xml: bytes) -> list[dict]:
        entries = []
        for node in ET.fromstring(map_xml).find("Map"):
            kind = int(node.get("Type"))
            rows = [row.get("Value", "") for row in node]
            flat = ",".join(rows) if rows else node.get("Value", "0")
            is_text = kind == 8
            values = []
            if not is_text:
                for item in flat.split(","):
                    try:
                        values.append(float(item))
                    except ValueError:
                        values.append(0.0)
            read_only = node.get("ReadOnly") == "true"
            entries.append(
                {
                    "id": int(node.get("DataId")),
                    "kind": kind,
                    "count": int(node.get("Count", "1")),
                    "multi": float(node.get("Multi", "1")),
                    "offset": float(node.get("Offset", "0")),
                    "values": values,
                    "text": flat if is_text else "",
                    "read_only": read_only,
                    "runtime": read_only,
                    "min": min(values) if values else 0,
                }
            )
        return sorted(entries, key=lambda entry: (not entry["runtime"], entry["id"]))

    @staticmethod
    def _demo_entries() -> list[dict]:
        def entry(data_id, kind, values, multi=1.0, offset=0.0, runtime=False, count=1, text=""):
            return {
                "id": data_id, "kind": kind, "count": count, "multi": multi, "offset": offset,
                "values": values, "text": text, "read_only": runtime, "runtime": runtime,
                "min": min(values) if values else 0,
            }

        return [
            entry(RPM_ID, 4, [850.0], runtime=True),
            entry(1004, 4, [85.0], offset=-50.0, runtime=True),
            entry(278, 1, [float(i) * 0.5 for i in range(176)], multi=0.1, count=176),
            entry(300, 0, [5.0], multi=0.1),
            entry(301, 0, [-10.0], multi=0.5),
            entry(2324, 5, [0.0], count=2),
            entry(2303, 3, [0.0], count=2),
            entry(2294, 1, [0.0] * 20, count=20),
            entry(2295, 1, [0.0] * 20, count=20),
            entry(310, 5, [1.0], count=4),
            entry(311, 5, [1.0], count=2),
            entry(320, 8, [], count=16, text="Sim"),
        ]

    # ---- requests ---------------------------------------------------------

    def handle(self, frame: bytes) -> bytes:
        if len(frame) < 8 or frame[0] != 0x5A or p.crc16(frame) != 0:
            return p.build_reply(0x5B, p.CMD_ERROR, struct.pack(">H", 5))
        addr, cmd, length = frame[1], *struct.unpack(">HH", frame[2:6])
        payload = frame[6 : 6 + length - 2]
        self.log.append(cmd)
        try:
            return p.build_reply(addr, cmd, self._dispatch(cmd, payload))
        except KeyError:
            return p.build_reply(addr, p.CMD_ERROR, struct.pack(">H", p.ERR_UNSUPPORTED))

    def _dispatch(self, cmd: int, payload: bytes) -> bytes:
        rt, cfg = self.runtime_size, self.config_size
        if cmd == p.CMD_STATE:
            return struct.pack(">H", self.state)
        if cmd in (p.CMD_BOOT_INFO, p.CMD_FIRMWARE_INFO):
            return self._info(cmd)
        if cmd == p.CMD_IDENTITY:
            return struct.pack(">HH", 54000, 1)
        if cmd == p.CMD_MEMORY_INFO:
            body = bytearray(0x14)
            struct.pack_into(">HHH", body, 4, cfg, rt, self.section2_size)
            body[0x0B] = 12
            struct.pack_into(">H", body, 0x12, self.max_id)
            return bytes(body)
        if cmd == p.CMD_READ_DESCRIPTOR:
            offset, size = struct.unpack(">HH", payload)
            return self.descriptor[offset : offset + size]
        if cmd == p.CMD_READ_CONFIG:
            offset, size = struct.unpack(">HH", payload)
            return bytes(self.memory[rt + offset : rt + offset + size])
        if cmd == p.CMD_READ_SECTION2:
            return b""
        if cmd == p.CMD_POLL_RUNTIME:
            self._tick()
            return bytes(self.memory[:rt])
        if cmd == p.CMD_WRITE_CONFIG:
            self.writes.append(payload)
            offset, size = struct.unpack(">HH", payload[:4])
            data = payload[4 : 4 + size]
            self.memory[rt + offset : rt + offset + len(data)] = data
            return bytes(self.memory[rt + offset : rt + offset + len(data)])
        if cmd == p.CMD_STORE:
            self.flash = bytes(self.memory)
            self.flash_stored += 1
            return struct.pack(">H", 1)
        if cmd == p.CMD_CLEAR_STATISTICS:
            self.statistics_cleared += 1
            return struct.pack(">H", 1)
        if cmd == p.CMD_CALIBRATE_MAP:
            self.map_calibrations += 1
            return struct.pack(">H", 1)
        if cmd == CMD_FACTORY_RESET:
            self.factory_resets += 1
            self.memory[rt : rt + cfg] = bytes(cfg)
            self.flash = bytes(self.memory)
            return struct.pack(">H", 1)
        raise KeyError(cmd)

    def _info(self, cmd: int) -> bytes:
        major, minor, patch = self.firmware
        body = bytearray(0x52 - 8)
        body[0] = major
        struct.pack_into(">HH", body, 1, minor, patch)
        if cmd == p.CMD_BOOT_INFO:
            struct.pack_into(">I", body, 0x44 - 8, 111111)
            struct.pack_into(">H", body, 0x48 - 8, self.hardware_family)
        return _crc_block(0 if cmd == p.CMD_BOOT_INFO else 1, bytes(body))

    def _tick(self) -> None:
        offset = self.offsets.get(RPM_ID)
        if offset is not None:
            rpm = int(2500 + 1500 * math.sin(time.monotonic() - self._start)) if self.engine_running else 0
            self.memory[offset : offset + 2] = struct.pack(">H", max(rpm, 0) & 0xFFFF)

    def reboot(self) -> None:
        """Lose RAM changes, as an ECU power cycle does."""
        self.memory = bytearray(self.flash)


def _next_frame(buffer: bytearray) -> bytes | None:
    while buffer and buffer[0] != 0x5A:
        del buffer[0]
    if len(buffer) < 6:
        return None
    total = 6 + struct.unpack(">H", buffer[4:6])[0]
    if len(buffer) < total:
        return None
    frame = bytes(buffer[:total])
    del buffer[:total]
    return frame


class PtyServer:
    """Serves a SimEcu on a pseudo-terminal, which looks like a serial port."""

    def __init__(self, ecu: SimEcu) -> None:
        self.ecu = ecu
        self._master, slave = os.openpty()
        tty.setraw(self._master)
        tty.setraw(slave)
        self.path = os.ttyname(slave)
        self._slave = slave
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        buffer = bytearray()
        while not self._stop.is_set():
            ready, _, _ = select.select([self._master], [], [], 0.05)
            if not ready:
                continue
            try:
                buffer += os.read(self._master, 65536)
            except OSError:
                return
            while (frame := _next_frame(buffer)) is not None:
                reply = self.ecu.handle(frame)
                view = memoryview(reply)
                while len(view):
                    written = os.write(self._master, view[:1024])
                    view = view[written:]

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1)
        os.close(self._master)
        os.close(self._slave)


class UdpServer:
    """Serves a SimEcu with the WiFi handshake."""

    def __init__(self, ecu: SimEcu, host: str = "127.0.0.1", port: int = 0) -> None:
        self.ecu = ecu
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind((host, port))
        self._sock.settimeout(0.05)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                data, peer = self._sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                return
            if data in (b"Connect", b"Discover"):
                self._sock.sendto(b"OK", peer)
            elif data != b"Disconnect":
                self._sock.sendto(self.ecu.handle(data), peer)

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1)
        self._sock.close()
