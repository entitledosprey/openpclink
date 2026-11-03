"""The separate USB link process, with the device faked."""
from multiprocessing import Pipe
from threading import Thread

from openpclink.link_helper import serve
from openpclink.transports import TransportError


class FakeDevice:
    def __init__(self):
        self.incoming = bytearray()
        self.sent = []
        self.purged = 0
        self.fail = False

    def write(self, data):
        self.sent.append(data)
        self.incoming += b"reply:" + data
        return len(data)

    def read(self, size, timeout):
        if self.fail:
            raise TransportError("unplugged")
        out = bytes(self.incoming[:size])
        del self.incoming[:size]
        return out

    def purge(self):
        self.purged += 1
        self.incoming.clear()


def _start(device):
    parent, child = Pipe()
    thread = Thread(target=serve, args=(child, device), daemon=True)
    thread.start()
    return parent, thread


def _call(conn, *request):
    conn.send(request)
    assert conn.poll(3), "helper did not answer"
    return conn.recv()


def test_bytes_arriving_between_requests_are_kept_in_order():
    device = FakeDevice()
    conn, thread = _start(device)
    assert _call(conn, "write", b"abc") == ("ok", 3)
    assert _call(conn, "read", 6, 0.1) == ("ok", b"reply:")
    assert _call(conn, "read", 3, 0.1) == ("ok", b"abc")
    assert _call(conn, "close") == ("ok", None)
    thread.join(3)
    assert not thread.is_alive()


def test_purge_also_clears_what_the_helper_already_drained():
    device = FakeDevice()
    conn, _ = _start(device)
    _call(conn, "write", b"stale")
    _call(conn, "read", 1, 0.1)
    _call(conn, "purge")
    assert device.purged == 1
    assert _call(conn, "read", 4, 0.0) == ("ok", b"")
    _call(conn, "close")


def test_device_failure_during_a_request_reaches_the_parent():
    device = FakeDevice()
    conn, _ = _start(device)
    original = device.write

    def broken(_data):
        raise TransportError("USB write failed")

    device.write = broken
    assert _call(conn, "write", b"x") == ("error", "USB write failed")
    device.write = original
    _call(conn, "close")


def test_helper_stops_when_the_parent_goes_away():
    device = FakeDevice()
    conn, thread = _start(device)
    conn.close()
    thread.join(3)
    assert not thread.is_alive()
