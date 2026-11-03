"""Keeps USB reads queued in the kernel, so a pause in Python cannot lose bytes.

The FT232H in a G4X holds about 1 KB at 4.8 Mbaud with no flow control. With one read at a
time, any gap over about 2 ms overflows it. Several queued transfers leave no gap.
"""
from __future__ import annotations

import ctypes
from ctypes import CFUNCTYPE, POINTER, Structure, byref, c_int, c_long, c_ubyte, c_uint, c_void_p, cast

_QUEUED = 8
_TRANSFER_SIZE = 4096
_COMPLETED = 0
_TIMED_OUT = 2
_CANCELLED = 3
_BULK = 2
_ENDPOINT_IN = 0x80


class _Timeval(Structure):
    _fields_ = [("sec", c_long), ("usec", c_long)]


class _Transfer(Structure):
    pass


_CALLBACK = CFUNCTYPE(None, POINTER(_Transfer))
_Transfer._fields_ = [
    ("dev_handle", c_void_p),
    ("flags", ctypes.c_uint8),
    ("endpoint", ctypes.c_uint8),
    ("type", ctypes.c_uint8),
    ("timeout", c_uint),
    ("status", c_int),
    ("length", c_int),
    ("actual_length", c_int),
    ("callback", _CALLBACK),
    ("user_data", c_void_p),
    ("buffer", POINTER(c_ubyte)),
    ("num_iso_packets", c_int),
]


def strip_status(data: bytes, packet_size: int) -> bytes:
    """FTDI starts every USB packet with two modem status bytes."""
    return b"".join(data[start + 2 : start + packet_size] for start in range(0, len(data), packet_size))


class QueuedReader:
    def __init__(self, lib, ctx, handle, endpoint: int, packet_size: int) -> None:
        if not endpoint & _ENDPOINT_IN:  # A queue on an output endpoint streams its buffers at the ECU.
            raise ValueError(f"USB endpoint {endpoint:#x} is not an input")
        self._lib, self._ctx = lib, ctx
        self._packet_size = packet_size
        self.data = bytearray()
        self.failed = False
        self._active = 0
        self._stopping = False
        lib.libusb_alloc_transfer.restype = POINTER(_Transfer)
        lib.libusb_alloc_transfer.argtypes = [c_int]
        lib.libusb_submit_transfer.argtypes = [POINTER(_Transfer)]
        lib.libusb_cancel_transfer.argtypes = [POINTER(_Transfer)]
        lib.libusb_free_transfer.argtypes = [POINTER(_Transfer)]
        lib.libusb_handle_events_timeout_completed.argtypes = [c_void_p, POINTER(_Timeval), c_void_p]
        self._callback = _CALLBACK(self._on_done)
        self._buffers = []
        self._transfers = []
        for _ in range(_QUEUED):
            buffer = (c_ubyte * _TRANSFER_SIZE)()
            transfer = lib.libusb_alloc_transfer(0)
            item = transfer.contents
            item.dev_handle = handle
            item.endpoint = endpoint
            item.type = _BULK
            item.timeout = 0
            item.length = _TRANSFER_SIZE
            item.callback = self._callback
            item.buffer = cast(buffer, POINTER(c_ubyte))
            item.num_iso_packets = 0
            self._buffers.append(buffer)
            self._transfers.append(transfer)
            if lib.libusb_submit_transfer(transfer) != 0:
                self.failed = True
                break
            self._active += 1

    def _on_done(self, pointer) -> None:
        item = pointer.contents
        self._active -= 1
        if item.status not in (_COMPLETED, _TIMED_OUT) or self._stopping:
            if item.status != _CANCELLED and not self._stopping:
                self.failed = True
            return
        if item.actual_length > 2:
            self.data += strip_status(ctypes.string_at(item.buffer, item.actual_length), self._packet_size)
        if self._lib.libusb_submit_transfer(pointer) == 0:
            self._active += 1
        else:
            self.failed = True

    def pump(self, seconds: float) -> None:
        wait = _Timeval(int(seconds), int((seconds % 1) * 1_000_000))
        self._lib.libusb_handle_events_timeout_completed(self._ctx, byref(wait), None)

    def close(self) -> None:
        self._stopping = True
        for transfer in self._transfers:
            self._lib.libusb_cancel_transfer(transfer)
        for _ in range(200):
            if self._active <= 0:
                break
            self.pump(0.01)
        if self._active <= 0:  # Otherwise freeing would race the kernel, so a small leak is safer.
            for transfer in self._transfers:
                self._lib.libusb_free_transfer(transfer)
