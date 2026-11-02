import struct

import pytest

from openpclink import protocol as p
from openpclink.layout import DescriptorError, parse_descriptor


class Scripted:
    def __init__(self, replies=b""):
        self.replies = bytearray(replies)
        self.sent = []

    def write(self, data):
        self.sent.append(data)
        return len(data)

    def read(self, size, timeout):
        out = bytes(self.replies[:size])
        del self.replies[:size]
        return out

    def purge(self):
        pass


def test_crc_matches_xmodem_check_value():
    assert p.crc16(b"123456789") == 0x31C3


def test_request_frame_layout():
    frame = p.build_request(0x5B, 0x03)
    assert frame[:6] == bytes([0x5A, 0x5B, 0, 3, 0, 2])
    assert p.crc16(frame) == 0


@pytest.mark.parametrize("cmd", [0x10, 0x11, 0x12, 0x14, 0x15, 0x21, 0x22, 0x1E, 0x1F, 0x2E, 0x7F])
def test_firmware_and_unknown_commands_cannot_be_sent(cmd):
    transport = Scripted()
    with pytest.raises(p.ForbiddenCommand):
        p.Link(transport).request(cmd)
    assert transport.sent == []


def test_store_is_0x20_as_observed_from_real_pclink():
    # 0x21 is Restore to Factory Settings; confusing them would wipe a tune.
    assert p.CMD_STORE == 0x20
    assert 0x21 not in p.ALLOWED_COMMANDS


def test_corrupt_reply_is_rejected():
    reply = bytearray(p.build_reply(0x5B, 0x03, b"\x00\x01"))
    reply[-1] ^= 1
    with pytest.raises(p.ProtocolError):
        p.Link(Scripted(reply)).request(0x03)


def test_ecu_error_reply_raises_with_code():
    reply = p.build_reply(0x5B, p.CMD_ERROR, struct.pack(">H", 0x1B))
    with pytest.raises(p.EcuError) as caught:
        p.Link(Scripted(reply)).request(0x03)
    assert caught.value.code == 0x1B


def test_no_reply_raises():
    with pytest.raises(p.ProtocolError):
        p.Link(Scripted()).request(0x03)


def _entry(data_id, info, *rest, read_only=False):
    head = struct.pack(">HB", (data_id << 2) | (2 if read_only else 0), info)
    return head + b"".join(rest)


def test_descriptor_assigns_consecutive_offsets_and_decodes_fields():
    scale = struct.pack(">ff", 0.5, -10.0)
    blob = (
        _entry(5, (0 << 4) | (2 << 2) | 2, b"\0", scale)          # signed u16 scalar
        + _entry(6, (1 << 4) | (1 << 2), b"\x03", scale)          # 3 x u8 array
        + _entry(7, (3 << 4) | 1, b"\x02")                        # 1-bit field
        + _entry(8, (5 << 4) | 2, b"\x04")                        # 2-bit field, same byte
        + _entry(9, (0 << 4) | (1 << 2), b"\0", scale, read_only=True)  # flushes the bit byte
        + _entry(0, (0 << 4) | (2 << 2), b"\0", scale)            # 2 bytes padding
        + _entry(10, (8 << 4) | 1, b"\x10")                       # 16-char string
        + _entry(11, (1 << 4) | (2 << 2) | 1, b"\x02")            # float array, compact
        + _entry(12, (9 << 4) | (2 << 2), b"\0")                  # unscaled scalar
        + b"\0\0\0"
    )
    table = parse_descriptor(blob, 100)
    assert (table[5].offset, table[5].width, table[5].signed) == (0, 2, True)
    assert (table[5].scale, table[5].add) == (0.5, -10.0)
    assert (table[6].offset, table[6].count) == (2, 3)
    assert (table[7].offset, table[7].mask, table[7].shift) == (5, 0x80, 7)
    assert (table[8].offset, table[8].mask, table[8].shift) == (5, 0x60, 5)
    assert (table[9].offset, table[9].read_only) == (6, True)
    assert (table[10].offset, table[10].count) == (9, 16)
    assert (table[11].offset, table[11].is_float, table[11].width) == (25, True, 4)
    assert (table[12].offset, table[12].width, table[12].scale) == (33, 2, 1.0)


def test_descriptor_ignores_ids_the_ecu_says_are_out_of_range():
    scale = struct.pack(">ff", 1.0, 0.0)
    blob = (
        _entry(50, (0 << 4) | (2 << 2), b"\0", scale)
        + _entry(5, (0 << 4) | (2 << 2), b"\0", scale)
        + b"\0\0\0"
    )
    table = parse_descriptor(blob, 10)
    assert 50 not in table
    assert table[5].offset == 0


def test_descriptor_large_array_uses_extended_count():
    scale = struct.pack(">ff", 1.0, 0.0)
    blob = _entry(5, (1 << 4) | (2 << 2), b"\0", scale, struct.pack(">I", 1000)) + b"\0\0\0"
    assert parse_descriptor(blob, 10)[5].count == 1000


@pytest.mark.parametrize("blob", [
    _entry(5, (0 << 4) | (2 << 2), b"\0", b"\0\0") + b"\0",        # scale cut short
    _entry(5, (3 << 4) | 9, b"\x02") + b"\0\0\0",                  # 9-bit field
    _entry(5, (3 << 4) | 0, b"\x02") + b"\0\0\0",                  # 0-bit field
])
def test_malformed_descriptor_is_refused(blob):
    with pytest.raises(DescriptorError):
        parse_descriptor(blob, 100)
