"""An ECU simulator for testing without hardware.

It answers the same wire protocol from a memory image built out of a map file,
over a pseudo-terminal or UDP. It encodes this project's reading of the
protocol, so passing against it is not proof of hardware compatibility on its
own; docs/VERIFICATION.md says what has been checked against what.
"""
from __future__ import annotations

import json
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


class SimRefusal(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


ERROR_ENGINE_RUNNING = 4


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
        self.frames: list[tuple[int, int, bytes]] = []
        self.addr = p.ADDR_MAIN
        self.bulk_mode = 0
        self.baro_calibrations: list[int] = []
        self.feature_codes: list[bytes] = []
        self.ecu_logs: list[tuple[str, bytes]] = []
        self.log_cursor = 0
        self.log_tick_rate = 1000
        self.log_magic = 0xA5A55A5A
        self.log_max_channels = 32
        self.log_max_rate = 100000
        self.logging_paused = False
        self.memo = ""
        self.memo_buffer = bytearray()
        self.can_file = b""
        self.can_buffer = bytearray()
        self.can_transmitted: list[bytes] = []
        self.can_replies: list[bytes] = []
        self.scope_rate = 0
        self.scope_stopped = 0
        self.firmware_stage: list[str] = []
        self.firmware_body = bytearray()
        self.firmware_header = b""
        self.comms_values: dict[int, dict] = {}
        self.clock_sets: list[bytes] = []
        self.writes: list[bytes] = []
        self.state = 1
        self.comms_state = 1
        self.comms_firmware: tuple[int, int, int] | None = None
        self.boot_info_answers = True
        self.chunk_naks: dict[tuple[int, int], list[int]] = {}
        self._image_index = -1
        self._chunk_number = 0
        self._build(map_xml)
        self.flash = bytes(self.memory)
        self.factory_image = bytes(self.memory)
        self.flash_updates_version = True
        self._start = time.monotonic()

    def _build(self, map_xml: bytes | None) -> None:
        entries = self._parse_map(map_xml) if map_xml else self._demo_entries()
        if self.hardware_family == 0x3EF or self.hardware_family > 0x3F3:
            self._take_comms(entries)
        runtime = [entry for entry in entries if entry["runtime"]]
        config = [entry for entry in entries if not entry["runtime"]]
        descriptor = bytearray()
        image_rt, image_cfg = bytearray(), bytearray()
        self.offsets: dict[int, int] = {}
        self.encodings: dict[int, tuple[int, bool, float, float]] = {}
        self.held: dict[int, float] = {}
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

    def _take_comms(self, entries: list[dict]) -> None:
        """A G5 keeps its network, clock and logging settings (ids from 10000) on the comms processor."""
        for entry in [entry for entry in entries if entry["id"] >= 10000]:
            entries.remove(entry)
            values = entry["values"]
            if entry["kind"] == 8:
                item = {"kind": 7, "count": entry["count"], "text": entry["text"]}
            elif len(values) > 1:
                item = {"kind": 5 if entry["kind"] == 1 and entry["multi"] != 1 else 8, "count": len(values), "values": [int(v) for v in values]}
            else:
                item = {"kind": 2, "count": 1, "value": int(values[0]) if values else 0}
            self.comms_values.setdefault(entry["id"], item)

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
        if not array:
            self.encodings[data_id] = (width, signed, scale, add)
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
        self.frames.append((addr, cmd, bytes(payload)))
        self.addr = addr
        if cmd == p.CMD_FILE and addr == p.ADDR_MAIN and struct.unpack_from(">I", payload)[0] == p.FILE_STREAM:
            return self._stream_file(addr, payload)
        if addr == p.ADDR_COMMS and cmd in (p.CMD_COMMS_POLL_A, p.CMD_COMMS_POLL_B) and self.comms_values:
            return self._comms_changes(cmd)
        try:
            return p.build_reply(addr, cmd, self._dispatch(cmd, payload))
        except SimRefusal as refusal:
            return p.build_reply(addr, p.CMD_ERROR, struct.pack(">H", refusal.code))
        except KeyError:
            return p.build_reply(addr, p.CMD_ERROR, struct.pack(">H", p.ERR_UNSUPPORTED))

    def add_comms_settings(self, ecu_xml: bytes) -> None:
        """Answer for every id from 10000 in PCLink's ECU.xml that the map did not already supply."""
        params = {int(n.get("DataId")): {q.get("Name"): q.get("Value") for q in n.iter("Property")} for n in ET.fromstring(ecu_xml).find("Parameters") if n.get("DataId", "").isdigit()}

        def prop(data_id: int, name: str) -> str | None:
            node = params.get(data_id, {})
            if name in node:
                return node[name]
            return prop(int(node["CopyFromID"]), name) if node.get("CopyFromID") else None

        for data_id in sorted(i for i in params if i >= 10000 and i not in self.comms_values):
            kind, columns = prop(data_id, "Type"), int(prop(data_id, "ColCount") or 1)
            low, high = float(prop(data_id, "Min") or 0), float(prop(data_id, "Max") or 0)
            if kind == "8":
                item = {"kind": 7, "count": 24 if data_id > 10100 else 50, "text": ""}
            elif columns > 1:
                item = {"kind": 8, "count": columns, "values": [0] * columns}
            elif kind == "0":
                item = {"kind": 1, "count": 1, "value": 0.0}
            else:
                item = {"kind": 2, "count": 1, "value": 0}
            item.update(min=low, max=high)
            self.comms_values[data_id] = item

    def _comms_changes(self, cmd: int) -> bytes:
        """07: the values that changed since the last poll, then a 07 reply. 0A: every value, then a 0A reply."""
        now = time.localtime()
        for data_id, field in zip(range(10118, 10124), now[:6]):
            if data_id in self.comms_values:
                self.comms_values[data_id]["value"] = field
        ids = sorted(self.comms_values) if cmd == p.CMD_COMMS_POLL_B else [i for i in range(10118, 10124) if i in self.comms_values]
        frames = []
        for data_id in ids:
            item = self.comms_values[data_id]
            frames.append(p.build_reply(p.ADDR_COMMS, p.CMD_COMMS_READ, struct.pack(">HHHhH", 0, item["kind"], data_id, -1, item.get("count", 1)) + self._comms_value(item)))
        return b"".join(frames) + p.build_reply(p.ADDR_COMMS, cmd, struct.pack(">H", 1))

    def _stream_file(self, addr: int, payload: bytes) -> bytes:
        """PCLink's faster download: many replies to one request, then a 00 01 reply."""
        offset, count = struct.unpack_from(">II", payload, 6)
        name = payload[14:].split(b"\0", 1)[0].decode("latin-1")
        data = dict(self.ecu_logs).get(name, b"")[offset : offset + count]
        frames = [p.build_reply(addr, p.CMD_FILE, data[at : at + 512]) for at in range(0, len(data), 512)]
        return b"".join(frames) + p.build_reply(addr, p.CMD_FILE, struct.pack(">H", 1))

    def _dispatch(self, cmd: int, payload: bytes) -> bytes:
        if self.addr == p.ADDR_COMMS:
            return self._dispatch_comms(cmd, payload)
        extra = self._dispatch_tools(cmd, payload)
        if extra is not None:
            return extra
        rt, cfg = self.runtime_size, self.config_size
        if cmd == p.CMD_STATE:
            return struct.pack(">H", self.state)
        if cmd == p.CMD_COMMS_STATUS and self.comms_values:
            return _crc_block(0, bytes(0x54 - 8))
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
            if self.engine_running:
                raise SimRefusal(ERROR_ENGINE_RUNNING)
            self.factory_resets += 1
            self.memory[rt : rt + cfg] = self.factory_image[rt : rt + cfg]
            self.flash = bytes(self.memory)
            return struct.pack(">H", 1)
        raise KeyError(cmd)

    def _dispatch_tools(self, cmd: int, payload: bytes) -> bytes | None:
        ok = struct.pack(">H", 1)
        if cmd == p.CMD_LOG_INFO:
            body = bytearray(0x17)
            struct.pack_into(">IH", body, 0, self.log_tick_rate, len(self.ecu_logs))
            struct.pack_into(">IH", body, 0x0D, self.log_magic, self.log_max_channels)
            struct.pack_into(">I", body, 0x13, self.log_max_rate)
            self.log_cursor = 0
            return bytes(body)
        if cmd == p.CMD_FILE:
            return self._file(payload)
        if cmd == p.CMD_WRITE_MEMO:
            return self._blob(payload, self.memo_buffer, "memo")
        if cmd == p.CMD_WRITE_CAN_FILE:
            return self._blob(payload, self.can_buffer, "can")
        if cmd == p.CMD_BULK_DOWNLOAD:
            if self.engine_running:
                raise SimRefusal(ERROR_ENGINE_RUNNING)
            self.bulk_mode = payload[0]
            return ok
        if cmd == p.CMD_CALIBRATE_BARO:
            self.baro_calibrations.append(struct.unpack(">I", payload)[0])
            return ok
        if cmd == p.CMD_FEATURE_UNLOCK:
            self.feature_codes.append(bytes(payload))
            return ok
        if cmd == p.CMD_EXCEPTION_DUMP:
            return b"trap"
        if cmd == p.CMD_TRIGGER_SCOPE:
            return self._scope(payload)
        if cmd == p.CMD_CAN_CONTROL:
            return ok
        if cmd == p.CMD_CAN_RECEIVE:
            frames, self.can_replies = b"".join(self.can_replies), []
            return frames
        if cmd == p.CMD_CAN_TRANSMIT:
            self.can_transmitted.append(bytes(payload))
            return ok
        if cmd == p.CMD_SET_CLOCK:
            return ok
        if cmd in (p.CMD_FW_RESET, p.CMD_FW_REQUEST_BOOT, p.CMD_FW_START, p.CMD_FW_HEADER, p.CMD_FW_DATA, p.CMD_FW_REQUEST_MAIN):
            return self._firmware_step(cmd, payload)
        return None

    def _firmware_step(self, cmd: int, payload: bytes) -> bytes:
        ok = struct.pack(">H", 1)
        comms = self.addr == p.ADDR_COMMS
        state = self.comms_state if comms else self.state
        if cmd == p.CMD_FW_RESET:
            self.firmware_stage.append("reset")
            if state == 3 and self.firmware_body:
                self._flashed_version(comms)
            state = 1
        elif cmd == p.CMD_FW_REQUEST_BOOT:
            if self.engine_running:
                raise SimRefusal(ERROR_ENGINE_RUNNING)
            state = 3
            self.firmware_stage.append("boot")
        elif state != 3:
            raise SimRefusal(3)
        elif cmd == p.CMD_FW_START:
            self.firmware_stage.append("start")
            self.firmware_body = bytearray()
            self._image_index += 1
            self._chunk_number = 0
        elif cmd == p.CMD_FW_HEADER:
            self.firmware_stage.append("header")
            self.firmware_header = bytes(payload)
        elif cmd == p.CMD_FW_DATA:
            naks = self.chunk_naks.get((self._image_index, self._chunk_number))
            if naks:
                raise SimRefusal(naks.pop(0))
            self._chunk_number += 1
            address, words = struct.unpack_from(">II", payload)
            expected = struct.unpack_from(">I", self.firmware_header, 20)[0] + len(self.firmware_body)
            if address != expected or words * 4 != len(payload) - 8:
                raise SimRefusal(0x27)
            self.firmware_body += payload[8:]
        elif cmd == p.CMD_FW_REQUEST_MAIN:
            self.firmware_stage.append("main")
            state = 1
            self._flashed_version(comms)
        if comms:
            self.comms_state = state
        else:
            self.state = state
        return ok

    def _flashed_version(self, comms: bool) -> None:
        """A programmed image becomes the running version, as it should on a real ECU."""
        if self.flash_updates_version and len(self.firmware_header) >= 13:
            version = (self.firmware_header[8], *struct.unpack_from(">HH", self.firmware_header, 9))
            if comms:
                self.comms_firmware = version
            else:
                self.firmware = version

    def _scope(self, payload: bytes) -> bytes:
        sub, a, b = struct.unpack(">HHH", payload)
        if sub == 0x0A:
            self.scope_rate = b
            return struct.pack(">H", 1)
        if sub == 6:
            self.scope_stopped += 1
            return struct.pack(">H", 1)
        return bytes((sub * 7 + i) & 0x7F for i in range(a))

    def _blob(self, payload: bytes, buffer: bytearray, target: str) -> bytes:
        offset = struct.unpack_from(">H", payload)[0]
        data = payload[2:]
        if offset == 1 and not data:
            if target == "memo":
                self.memo = bytes(buffer).decode("utf-16-le")
            else:
                self.can_file = bytes(buffer)
            buffer.clear()
        else:
            buffer[offset:offset + len(data)] = data
        return struct.pack(">H", 1)

    def _file(self, payload: bytes) -> bytes:
        operation = struct.unpack_from(">I", payload)[0]
        if operation == p.FILE_PAUSE_LOGGING:
            self.logging_paused = True
            return struct.pack(">H", 1)
        if operation == p.FILE_RESUME_LOGGING:
            self.logging_paused = False
            return struct.pack(">H", 1)
        if operation == p.FILE_NEXT:
            name, data = self.ecu_logs[self.log_cursor]
            self.log_cursor += 1
            return struct.pack(">BxxxI", 0, len(data)) + name.encode("latin-1") + b"\0"
        offset = struct.unpack_from(">I", payload, 6)[0]
        name = payload[14:].split(b"\0", 1)[0].decode("latin-1")
        if operation == p.FILE_DELETE:
            self.ecu_logs = [entry for entry in self.ecu_logs if entry[0] != name]
            return struct.pack(">H", 1)
        if not name:
            return struct.pack(">H", 1)
        if name == "Memo":
            data = self.memo.encode("utf-16-le")
            return data[offset:] + (b"\0\0" if offset < len(data) or (not offset and data) else b"") if offset < len(data) else b""
        if name == "CANFile":
            return self.can_file[offset : offset + 512]
        data = dict(self.ecu_logs).get(name)
        if data is None:
            raise KeyError(name)
        return data[offset : offset + 512]

    def _dispatch_comms(self, cmd: int, payload: bytes) -> bytes:
        """The G5 comms processor: settings from id 10000, plain floats and text."""
        ok = struct.pack(">H", 1)
        if cmd in (p.CMD_BOOT_INFO, p.CMD_FIRMWARE_INFO):
            return self._info(cmd)
        if cmd == p.CMD_MEMORY_INFO:
            body = bytearray(0x14)
            struct.pack_into(">H", body, 0x12, 10000 + max((i - 10000 + 1 for i in self.comms_values), default=0))
            return bytes(body)
        if cmd == p.CMD_COMMS_READ_RECORD:
            data_id = struct.unpack_from(">H", payload)[0]
            item = self.comms_values.get(data_id)
            if item is None:
                raise SimRefusal(2)
            body = bytearray(0x16)
            body[3] = item["kind"]
            struct.pack_into(">H", body, 4, item.get("count", 1))
            struct.pack_into(">ff", body, 0xA, item.get("min", 0.0), item.get("max", 0.0))
            return bytes(body) + self._comms_value(item)
        if cmd == p.CMD_COMMS_READ:
            data_id = struct.unpack_from(">H", payload)[0]
            item = self.comms_values[data_id]
            return struct.pack(">HHHhH", 0, item["kind"], data_id, -1, item.get("count", 1)) + self._comms_value(item)
        if cmd == p.CMD_COMMS_WRITE:
            data_id, where = struct.unpack_from(">HH", payload)
            item = self.comms_values[data_id]
            raw = payload[4:]
            if item["kind"] in (4, 7):
                item["text"] = raw.decode("utf-16-be").split("\0", 1)[0]
            else:
                code = ">f" if item["kind"] in (1, 5) else ">i" if item["kind"] == 3 else ">I"
                value = struct.unpack_from(code, raw)[0]
                if where == 0xFFFF:
                    item["value"] = value
                else:
                    item["values"][where] = value
            return struct.pack(">HHHhH", 0, item["kind"], data_id, -1, item.get("count", 1)) + self._comms_value(item)
        if cmd == p.CMD_COMMS_STAMP:
            body = bytearray(2 + 2 + 2 + 2 + 4 + 20 + 64 + 2 + 2)
            struct.pack_into(">H", body, 4, len(body))
            return bytes(body)
        if cmd == p.CMD_SET_CLOCK:
            self.clock_sets.append(payload)
            return ok
        if cmd == p.CMD_STORE:
            return ok
        if cmd == CMD_FACTORY_RESET:
            self.factory_resets += 1
            return ok
        if cmd in (p.CMD_FW_RESET, p.CMD_FW_REQUEST_BOOT, p.CMD_FW_START, p.CMD_FW_HEADER, p.CMD_FW_DATA, p.CMD_FW_REQUEST_MAIN):
            return self._firmware_step(cmd, payload)
        if cmd == p.CMD_STATE:
            return struct.pack(">H", self.comms_state)
        raise KeyError(cmd)

    @staticmethod
    def _comms_value(item: dict) -> bytes:
        if item["kind"] in (4, 7):
            return item.get("text", "").ljust(item.get("count", 1), "\0").encode("utf-16-be")
        code = ">f" if item["kind"] in (1, 5) else ">i" if item["kind"] == 3 else ">I"
        values = item.get("values", [item.get("value", 0)])
        return b"".join(struct.pack(code, value) for value in values)

    def _info(self, cmd: int) -> bytes:
        if not self.boot_info_answers and (self.comms_state if self.addr == p.ADDR_COMMS else self.state) == 3:
            raise SimRefusal(3)
        major, minor, patch = self.comms_firmware if self.addr == p.ADDR_COMMS and self.comms_firmware else self.firmware
        body = bytearray(0x52 - 8)
        body[0] = major
        struct.pack_into(">HH", body, 1, minor, patch)
        if cmd == p.CMD_BOOT_INFO:
            struct.pack_into(">I", body, 0x44 - 8, 111111)
            struct.pack_into(">H", body, 0x48 - 8, self.hardware_family)
        return _crc_block(0 if cmd == p.CMD_BOOT_INFO else 1, bytes(body))

    def set_value(self, data_id: int, value: float) -> None:
        """Hold a scalar parameter at a value, encoded the way the firmware would."""
        self.held[data_id] = value
        self._store(data_id, value)

    def _store(self, data_id: int, value: float) -> None:
        width, signed, scale, add = self.encodings[data_id]
        raw = round((value - add) / scale) if scale else 0
        low, high = (-(1 << (8 * width - 1)), (1 << (8 * width - 1)) - 1) if signed else (0, (1 << (8 * width)) - 1)
        offset = self.offsets[data_id]
        self.memory[offset : offset + width] = max(low, min(high, raw)).to_bytes(width, "big", signed=signed)

    def _control(self) -> None:
        """Test harness: OPENPCLINK_SIM_CONTROL names a JSON file of {data id: value} to hold live."""
        path = os.environ.get("OPENPCLINK_SIM_CONTROL")
        if not path:
            return
        try:
            with open(path) as handle:
                values = json.load(handle)
        except (OSError, ValueError):
            return
        self.held.update({int(i): float(v) for i, v in values.items() if int(i) in self.encodings})

    def _tick(self) -> None:
        self._control()
        offset = self.offsets.get(RPM_ID)
        if offset is not None and RPM_ID not in self.held:
            rpm = int(2500 + 1500 * math.sin(time.monotonic() - self._start)) if self.engine_running else 0
            self.memory[offset : offset + 2] = struct.pack(">H", max(rpm, 0) & 0xFFFF)
        for data_id, value in self.held.items():
            self._store(data_id, value)

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
