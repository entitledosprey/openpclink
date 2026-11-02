"""Byte transports to the ECU: USB CDC, FTDI (libusb) and WiFi UDP."""
from __future__ import annotations

import ctypes.util
import errno
import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

G5_VID = 0x0483
G5_PID = 0xA451
FTDI_VID = 0x0403
G4X_PIDS = range(0x7068, 0x7070)

SERIAL_BAUD = 1_171_875
FALLBACK_BAUD = 115_200
FT232H_BAUD = 4_700_000
FTDI_X_BAUD = 1_171_875
WIFI_PORT = 10470


def _use_bundled_libusb() -> None:
    """Point pyusb at the libusb shipped inside a packaged app."""
    bundle = getattr(sys, "_MEIPASS", None)
    if not bundle:
        return
    matches = sorted(Path(bundle).glob("libusb-1.0*"))
    if matches:
        system_lookup = ctypes.util.find_library
        ctypes.util.find_library = lambda name: (
            str(matches[0]) if "usb" in name else system_lookup(name)
        )


_use_bundled_libusb()


class TransportError(Exception):
    """The connection could not be opened or was lost."""


class Transport(Protocol):
    def write(self, data: bytes) -> int: ...
    def read(self, size: int, timeout: float) -> bytes: ...
    def purge(self) -> None: ...
    def close(self) -> None: ...


def _ftdi_open_message(exc: Exception) -> str:
    message = f"cannot open Link USB device: {exc}"
    if getattr(exc, "errno", None) != errno.EACCES and "Access denied" not in str(exc):
        return message
    if sys.platform == "darwin":
        return (
            f"{message}. Another driver has probably claimed the ECU. Unplug and replug it, then "
            "look for a Link entry with a /dev/cu port in the connection list. If there is none, "
            "run from a terminal with sudo: "
            "sudo /Applications/OpenPCLink.app/Contents/MacOS/OpenPCLink"
        )
    return (
        f"{message}. Your user lacks permission for the USB device. Install the rule in "
        "packaging/70-link-ecu.rules (included in the .deb from version 0.1.1), then unplug and "
        "replug the ECU."
    )


class SerialTransport:
    """G5 USB (CDC-ACM, needs no driver) and RS-232 ports."""

    def __init__(self, path: str, baud: int = SERIAL_BAUD) -> None:
        import serial

        try:
            self._port = self._open(path, baud)
        except (serial.SerialException, OSError, ValueError) as exc:
            raise TransportError(f"cannot open {path}: {exc}") from exc

    @staticmethod
    def _open(path: str, baud: int):
        import serial

        try:
            return serial.Serial(path, baud, timeout=0, write_timeout=2, exclusive=True)
        except OSError as exc:
            # Some drivers reject non-standard rates. CDC links ignore the rate.
            if exc.errno not in (errno.ENOTTY, errno.EINVAL):
                raise
            return serial.Serial(path, FALLBACK_BAUD, timeout=0, write_timeout=2, exclusive=True)

    def write(self, data: bytes) -> int:
        import serial

        try:
            return self._port.write(data) or 0
        except (serial.SerialException, OSError) as exc:
            raise TransportError(f"serial write failed: {exc}") from exc

    def read(self, size: int, timeout: float) -> bytes:
        import serial

        out = bytearray()
        deadline = time.monotonic() + timeout
        self._port.timeout = 0.01
        try:
            while len(out) < size and time.monotonic() < deadline:
                out += self._port.read(size - len(out))
        except (serial.SerialException, OSError) as exc:
            raise TransportError(f"serial read failed: {exc}") from exc
        return bytes(out)

    def purge(self) -> None:
        import serial

        try:
            self._port.reset_input_buffer()
        except (serial.SerialException, OSError, TypeError) as exc:
            raise TransportError(f"serial purge failed: {exc}") from exc

    def close(self) -> None:
        self._port.close()


class FtdiTransport:
    """G4X USB. These ECUs use private FTDI product IDs that macOS and Linux
    serial drivers do not bind to, so they are driven through libusb."""

    def __init__(self, pid: int, serial_number: str | None = None) -> None:
        from pyftdi.ftdi import Ftdi, FtdiError
        from pyftdi.usbtools import UsbToolsError

        self._error = FtdiError
        self._ftdi = Ftdi()
        try:
            self._ftdi.open(FTDI_VID, pid, serial=serial_number)
            hi_speed = self._ftdi.is_H_series
            self._ftdi.set_baudrate(FT232H_BAUD if hi_speed else FTDI_X_BAUD, constrain=False)
            self._ftdi.set_line_property(8, 1, "N")
            self._ftdi.set_flowctrl("")
            self._ftdi.set_latency_timer(2)
        except (FtdiError, UsbToolsError, ValueError, OSError) as exc:
            raise TransportError(_ftdi_open_message(exc)) from exc

    def write(self, data: bytes) -> int:
        try:
            return self._ftdi.write_data(data)
        except (self._error, OSError) as exc:
            raise TransportError(f"USB write failed: {exc}") from exc

    def read(self, size: int, timeout: float) -> bytes:
        out = bytearray()
        deadline = time.monotonic() + timeout
        try:
            while len(out) < size and time.monotonic() < deadline:
                out += self._ftdi.read_data(size - len(out))
        except (self._error, OSError) as exc:
            raise TransportError(f"USB read failed: {exc}") from exc
        return bytes(out)

    def purge(self) -> None:
        try:
            self._ftdi.purge_rx_buffer()
        except (self._error, OSError) as exc:
            raise TransportError(f"USB purge failed: {exc}") from exc

    def close(self) -> None:
        self._ftdi.close()


class UdpTransport:
    """G5 WiFi: a text handshake on UDP, then raw protocol frames."""

    def __init__(self, host: str, port: int = WIFI_PORT) -> None:
        self._addr = (host, port)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._buffer = b""
        try:
            for _ in range(2):
                self._sock.sendto(b"Connect", self._addr)
                self._sock.settimeout(0.5)
                try:
                    if self._sock.recvfrom(2048)[0]:
                        return
                except socket.timeout:
                    continue
        except OSError as exc:
            self._sock.close()
            raise TransportError(f"cannot reach {host}: {exc}") from exc
        self._sock.close()
        raise TransportError(f"no answer from ECU at {host}:{port}")

    def write(self, data: bytes) -> int:
        try:
            return self._sock.sendto(data, self._addr)
        except OSError as exc:
            raise TransportError(f"WiFi send failed: {exc}") from exc

    def read(self, size: int, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        while len(self._buffer) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self._sock.settimeout(remaining)
            try:
                self._buffer += self._sock.recvfrom(65535)[0]
            except socket.timeout:
                break
            except OSError as exc:
                raise TransportError(f"WiFi receive failed: {exc}") from exc
        out, self._buffer = self._buffer[:size], self._buffer[size:]
        return out

    def purge(self) -> None:
        self._buffer = b""
        self._sock.setblocking(False)
        try:
            while self._sock.recvfrom(65535):
                pass
        except OSError:
            pass
        self._sock.setblocking(True)

    def close(self) -> None:
        try:
            self._sock.sendto(b"Disconnect", self._addr)
        except OSError:
            pass
        self._sock.close()


@dataclass(frozen=True)
class PortInfo:
    """A connectable ECU location. `uri` is what open_transport() accepts."""

    uri: str
    description: str


def list_ports(all_serial: bool = False) -> list[PortInfo]:
    """Find Link ECUs: G5 by CDC USB ID, G4X by private FTDI product ID."""
    from serial.tools import list_ports as serial_ports

    found: list[PortInfo] = []
    for port in sorted(serial_ports.comports(), key=lambda p: p.device):
        if port.vid == G5_VID and port.pid == G5_PID:
            found.append(PortInfo(f"serial:{port.device}", f"Link G5 ECU ({port.device})"))
        elif port.vid == FTDI_VID and port.pid in G4X_PIDS:
            # A system driver has claimed it. The right rate depends on the chip,
            # and only the matching one will get an answer.
            for baud in (FTDI_X_BAUD, FT232H_BAUD):
                found.append(
                    PortInfo(f"serial:{port.device}@{baud}", f"Link G4X ECU ({port.device} at {baud} baud)")
                )
        elif all_serial and port.vid is not None:
            found.append(PortInfo(f"serial:{port.device}", f"{port.description} ({port.device})"))
    try:
        import usb.core

        for device in usb.core.find(find_all=True, idVendor=FTDI_VID) or []:
            if device.idProduct in G4X_PIDS:
                found.append(
                    PortInfo(f"ftdi:{device.idProduct:04x}", f"Link G4X ECU (PID {device.idProduct:04x})")
                )
    except (ImportError, OSError, ValueError):
        pass  # No libusb means no G4X discovery; CDC and WiFi still work.
    except Exception as exc:  # usb.core.NoBackendError has no public base class.
        if type(exc).__name__ != "NoBackendError":
            raise
    return found


def open_transport(uri: str) -> Transport:
    """Open `serial:/dev/x[@baud]`, `ftdi:<hex pid>` or `udp:<host>[:port]`."""
    scheme, _, rest = uri.partition(":")
    if scheme == "serial" and rest:
        path, _, baud = rest.partition("@")
        return SerialTransport(path, int(baud) if baud else SERIAL_BAUD)
    if scheme == "ftdi" and rest:
        return FtdiTransport(int(rest, 16))
    if scheme == "udp" and rest:
        host, _, port = rest.partition(":")
        return UdpTransport(host, int(port) if port else WIFI_PORT)
    raise TransportError(f"unrecognised connection '{uri}'")
