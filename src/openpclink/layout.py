"""Decodes the ECU's memory descriptor into a DataId -> location table.

The ECU sends the layout of its own memory, so this works across firmware
versions. Entries are consecutive and a cursor assigns each one its address.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from .protocol import ProtocolError

TYPE_ARRAY = 1
TYPES_BITFIELD = (3, 5)
TYPES_NO_STORAGE = (6, 7)
TYPE_STRING = 8
TYPE_SCALAR_NO_SCALE = 9


@dataclass(frozen=True)
class Location:
    data_id: int
    kind: int
    offset: int
    width: int = 0
    count: int = 1
    scale: float = 1.0
    add: float = 0.0
    signed: bool = False
    is_float: bool = False
    read_only: bool = False
    mask: int = 0
    shift: int = 0

    @property
    def is_bitfield(self) -> bool:
        return self.kind in TYPES_BITFIELD

    @property
    def is_string(self) -> bool:
        return self.kind == TYPE_STRING


def _be_float(blob: bytes, pos: int) -> float:
    return struct.unpack_from(">f", blob, pos)[0]


class DescriptorError(ProtocolError):
    """The memory layout from the ECU cannot be trusted, so writes would not be safe."""


def parse_descriptor(blob: bytes, max_id: int) -> dict[int, Location]:
    try:
        return _parse(blob, max_id)
    except (IndexError, struct.error) as exc:
        raise DescriptorError("the ECU's memory layout description is truncated") from exc


def _parse(blob: bytes, max_id: int) -> dict[int, Location]:
    table: dict[int, Location] = {}
    cursor = 0
    bit_pos = 0
    pos = 0
    end = len(blob) - 3

    def flush_bits() -> None:
        nonlocal cursor, bit_pos
        if bit_pos:
            cursor += 1
            bit_pos = 0

    while pos < end:
        id_word = (blob[pos] << 8) | blob[pos + 1]
        data_id = id_word >> 2
        read_only = bool(id_word & 2)
        info = blob[pos + 2]
        kind = info >> 4
        pos += 3
        known = data_id < max_id

        if kind in TYPES_BITFIELD:
            option_count = blob[pos]
            pos += 1
            bits = info & 0x0F
            if not 1 <= bits <= 8:
                raise DescriptorError(f"parameter {data_id} has an invalid size of {bits} bits")
            if known:
                if bit_pos + bits > 8:
                    cursor += 1
                    bit_pos = 0
                shift = 8 - bit_pos - bits
                mask = ((1 << bits) - 1) << shift
                bit_pos += bits
                table[data_id] = Location(
                    data_id, kind, cursor, count=option_count, scale=0.0,
                    read_only=read_only, mask=mask, shift=shift,
                )
        elif kind == TYPE_STRING:
            count = blob[pos]
            pos += 1
            width = info & 0x0F
            if known:
                flush_bits()
                table[data_id] = Location(
                    data_id, kind, cursor, width=width, count=count, scale=0.0, read_only=read_only
                )
                cursor += width * count
        elif kind == TYPE_SCALAR_NO_SCALE:
            pos += 1
            width = (info >> 2) & 3 or 4
            if known:
                flush_bits()
                table[data_id] = Location(
                    data_id, kind, cursor, width=width, signed=bool(info & 2), read_only=read_only
                )
                cursor += width
        elif kind in TYPES_NO_STORAGE or kind >= 10:
            pass
        else:
            width = (info >> 2) & 3 or 4
            signed = bool(info & 2)
            count = blob[pos]
            pos += 1
            is_float = bool(info & 1)
            scale, add = 1.0, 0.0
            if not is_float:
                scale = _be_float(blob, pos)
                add = _be_float(blob, pos + 4)
                pos += 8
                if data_id != 0 and kind == TYPE_ARRAY and count == 0:
                    count = struct.unpack_from(">I", blob, pos)[0]
                    pos += 4
            else:
                width = 4 if data_id != 0 else width
            if data_id == 0:
                flush_bits()
                cursor += width
            elif known:
                flush_bits()
                entries = count if kind == TYPE_ARRAY else 1
                table[data_id] = Location(
                    data_id, kind, cursor, width=width, count=entries, scale=scale, add=add,
                    signed=signed, is_float=is_float, read_only=read_only,
                )
                cursor += width * entries
    return table
