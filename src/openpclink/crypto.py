"""XXTEA container used by PCLink's config and map files.

A container is XXTEA over the whole file. The plaintext starts with three
little-endian u32s: timestamp, payload size, and a protection tag.
"""
from __future__ import annotations

import struct
import time

_DELTA = 0x9E3779B9
_MASK = 0xFFFFFFFF
HEADER_SIZE = 12


class ProtectedFile(Exception):
    """The file was encrypted by a tuner with "Save as Encrypted"."""


class BadContainer(ValueError):
    """The data does not decode to a valid container with this key."""


def _mx(y: int, z: int, total: int, key: tuple[int, ...], p: int, e: int) -> int:
    return (((z >> 5 ^ y << 2) + (y >> 3 ^ z << 4)) ^ ((total ^ y) + (key[(p & 3) ^ e] ^ z))) & _MASK


def _decrypt_words(v: list[int], key: tuple[int, ...]) -> None:
    n = len(v)
    total = ((6 + 52 // n) * _DELTA) & _MASK
    y = v[0]
    while total:
        e = (total >> 2) & 3
        for p in range(n - 1, 0, -1):
            z = v[p - 1]
            y = v[p] = (v[p] - _mx(y, z, total, key, p, e)) & _MASK
        z = v[n - 1]
        y = v[0] = (v[0] - _mx(y, z, total, key, 0, e)) & _MASK
        total = (total - _DELTA) & _MASK


def _encrypt_words(v: list[int], key: tuple[int, ...]) -> None:
    n = len(v)
    total = 0
    z = v[n - 1]
    for _ in range(6 + 52 // n):
        total = (total + _DELTA) & _MASK
        e = (total >> 2) & 3
        for p in range(n - 1):
            y = v[p + 1]
            z = v[p] = (v[p] + _mx(y, z, total, key, p, e)) & _MASK
        y = v[0]
        z = v[n - 1] = (v[n - 1] + _mx(y, z, total, key, n - 1, e)) & _MASK


def decode(data: bytes, key: tuple[int, ...], serial: int | None = None) -> bytes:
    """Return the payload of a container.

    A non-zero tag locks a file to one ECU's serial number. As in PCLink it opens
    only for that ECU, so it is refused without a matching connected serial.
    """
    n = len(data) // 4
    if n < 4:
        raise BadContainer("file too short")
    words = list(struct.unpack(f"<{n}I", data[: n * 4]))
    _decrypt_words(words, key)
    plain = struct.pack(f"<{n}I", *words)
    _, size, tag = struct.unpack_from("<3I", plain)
    if not 0 < size <= len(plain) - HEADER_SIZE or len(plain) - HEADER_SIZE - size >= 32:
        raise BadContainer("size field is invalid; wrong key or not a container")
    if tag != 0 and tag != serial:
        raise ProtectedFile("this file is locked to a different ECU's serial number and cannot be opened")
    return plain[HEADER_SIZE : HEADER_SIZE + size]


def encode(payload: bytes, key: tuple[int, ...], tag: int = 0) -> bytes:
    """Build a container, locked to an ECU serial number if a tag is given."""
    n = (len(payload) // 4) + 4
    plain = bytearray(n * 4)
    struct.pack_into("<3I", plain, 0, int(time.time()), len(payload), tag)
    plain[HEADER_SIZE : HEADER_SIZE + len(payload)] = payload
    words = list(struct.unpack(f"<{n}I", bytes(plain)))
    _encrypt_words(words, key)
    return struct.pack(f"<{n}I", *words)


def rc4(data: bytes, key: bytes) -> bytes:
    """PCLink's CAN stream files use plain RC4."""
    box = list(range(256))
    j = 0
    for i in range(256):
        j = (j + box[i] + key[i % len(key)]) & 0xFF
        box[i], box[j] = box[j], box[i]
    out = bytearray(len(data))
    i = j = 0
    for at, byte in enumerate(data):
        i = (i + 1) & 0xFF
        j = (j + box[i]) & 0xFF
        box[i], box[j] = box[j], box[i]
        out[at] = byte ^ box[(box[i] + box[j]) & 0xFF]
    return bytes(out)
