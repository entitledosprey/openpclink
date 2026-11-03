"""Runs an FTDI link in its own process.

The FT232H in a G4X holds about 1 KB and has no flow control, so at 4.8 Mbaud it
overflows if not read for about 2 ms. Painting in the GUI can stall Python for
longer than that, which lost bytes in about 20% of polls. A separate process has
no GUI to wait for. Everything that decides what may be sent stays in the parent.
"""
from __future__ import annotations

import os
import subprocess
import sys
from multiprocessing.connection import Connection

from .transports import FtdiTransport, Transport, TransportError

HELPER_ARG = "--ftdi-helper"
_IDLE_READ = 4096


def serve(conn: Connection, transport: Transport) -> None:
    """Keep draining the device, answering requests from the parent between reads."""
    buffer = bytearray()
    while True:
        if conn.poll(0):
            try:
                name, *args = conn.recv()
            except EOFError:
                return
            try:
                if name == "write":
                    conn.send(("ok", transport.write(args[0])))
                elif name == "read":
                    size, timeout = args
                    if len(buffer) < size:
                        buffer += transport.read(size - len(buffer), timeout)
                    conn.send(("ok", bytes(buffer[:size])))
                    del buffer[:size]
                elif name == "purge":
                    buffer.clear()
                    transport.purge()
                    conn.send(("ok", None))
                elif name == "close":
                    conn.send(("ok", None))
                    return
            except TransportError as exc:
                conn.send(("error", str(exc)))
        else:
            try:
                buffer += transport.read(_IDLE_READ, 0)
            except TransportError as exc:
                # Reported on the next request, when the parent is listening.
                buffer.clear()
                pending = str(exc)
                if conn.poll(1):
                    conn.recv()
                    conn.send(("error", pending))
                return


class IsolatedFtdiTransport:
    """The parent's side: the same interface as any other transport."""

    def __init__(self, pid: int) -> None:
        self._process = subprocess.Popen(_helper_command(pid), stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        # Connection owns and closes its descriptor, so give it a copy of Popen's.
        self._to_child = Connection(os.dup(self._process.stdin.fileno()), readable=False, writable=True)
        self._from_child = Connection(os.dup(self._process.stdout.fileno()), readable=True, writable=False)
        self._closed = False
        try:
            self._answer()
        except TransportError:
            self.close()
            raise

    def _answer(self):
        try:
            status, value = self._from_child.recv()
        except (EOFError, OSError) as exc:
            raise TransportError("the USB link process stopped") from exc
        if status == "error":
            raise TransportError(value)
        return value

    def _call(self, *request):
        try:
            self._to_child.send(request)
        except (OSError, ValueError) as exc:
            raise TransportError("the USB link process stopped") from exc
        return self._answer()

    def write(self, data: bytes) -> int:
        return self._call("write", data)

    def read(self, size: int, timeout: float) -> bytes:
        return self._call("read", size, timeout)

    def purge(self) -> None:
        self._call("purge")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._to_child.send(("close",))
            self._from_child.recv()
        except (EOFError, OSError, ValueError):
            pass
        try:
            self._process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait()
        self._to_child.close()
        self._from_child.close()
        self._process.stdin.close()
        self._process.stdout.close()


def _helper_command(pid: int) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, HELPER_ARG, f"{pid:x}"]
    return [sys.executable, "-m", "openpclink.link_helper", f"{pid:x}"]


def helper_main(pid_text: str) -> int:
    # The pipes carry binary messages, so nothing else may print to stdout.
    to_parent = Connection(os.dup(sys.stdout.fileno()), readable=False, writable=True)
    from_parent = Connection(os.dup(sys.stdin.fileno()), readable=True, writable=False)
    sys.stdout = sys.stderr
    try:
        transport = FtdiTransport(int(pid_text, 16))
    except TransportError as exc:
        to_parent.send(("error", str(exc)))
        return 1
    to_parent.send(("ok", None))

    class Duplex:
        poll = from_parent.poll
        recv = from_parent.recv
        send = to_parent.send

    try:
        serve(Duplex, transport)
    finally:
        transport.close()
    return 0


if __name__ == "__main__":
    sys.exit(helper_main(sys.argv[1]))
