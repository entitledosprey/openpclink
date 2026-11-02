"""How a session behaves when real-world replies go missing or arrive damaged."""
import pytest

from openpclink import protocol as p
from openpclink.ecu import Ecu, StoreUnconfirmed, WriteMismatch
from openpclink.sim import SimEcu


class FaultyLink:
    """Connects a session straight to a simulator, damaging chosen replies."""

    def __init__(self, sim):
        self.sim = sim
        self.rx = bytearray()
        self.faults = {}
        self.sent = []

    def fail_next(self, cmd, count=1, late_bytes=b""):
        self.faults[cmd] = [count, late_bytes]

    def write(self, data):
        cmd = int.from_bytes(data[2:4], "big")
        self.sent.append(cmd)
        reply = self.sim.handle(data)
        fault = self.faults.get(cmd)
        if fault and fault[0] > 0:
            fault[0] -= 1
            reply = reply[: len(reply) // 2]
        self.rx += reply
        return len(data)

    def read(self, size, timeout):
        out = bytes(self.rx[:size])
        del self.rx[:size]
        return out

    def purge(self):
        self.rx.clear()

    def close(self):
        pass


@pytest.fixture
def link():
    return FaultyLink(SimEcu())


@pytest.fixture
def session(link):
    ecu = Ecu(link)
    ecu.connect()
    return ecu


def test_poll_survives_a_truncated_reply(session, link):
    link.fail_next(p.CMD_POLL_RUNTIME)
    session.poll_runtime()
    assert session.get(1004) == pytest.approx(85.0)


def test_poll_survives_two_truncated_replies_in_a_row(session, link):
    link.fail_next(p.CMD_POLL_RUNTIME, count=2)
    session.poll_runtime()


def test_persistent_failure_still_reports_an_error(session, link):
    link.fail_next(p.CMD_POLL_RUNTIME, count=99)
    with pytest.raises(p.ProtocolError, match="truncated"):
        session.poll_runtime()


def test_connect_survives_damaged_replies(link):
    for cmd in (p.CMD_STATE, p.CMD_BOOT_INFO, p.CMD_READ_DESCRIPTOR, p.CMD_READ_CONFIG):
        link.fail_next(cmd)
    ecu = Ecu(link)
    ecu.connect()
    assert ecu.get(300) == pytest.approx(5.0)


def test_write_with_lost_reply_is_sent_once_and_confirmed_by_read_back(session, link):
    link.sent.clear()
    link.fail_next(p.CMD_WRITE_CONFIG)
    assert session.set(300, 7.5) == pytest.approx(7.5)
    assert link.sent.count(p.CMD_WRITE_CONFIG) == 1
    assert p.CMD_READ_CONFIG in link.sent


def test_write_lost_before_reaching_ecu_is_reported(session, link):
    original = link.sim._dispatch
    link.sim._dispatch = lambda cmd, payload: b"" if cmd == p.CMD_WRITE_CONFIG else original(cmd, payload)
    link.fail_next(p.CMD_WRITE_CONFIG)
    with pytest.raises(WriteMismatch):
        session.set(300, 7.5)
    assert session.get(300) == pytest.approx(5.0)


def test_store_with_lost_reply_is_not_repeated_and_is_reported(session, link):
    link.sent.clear()
    session.set(300, 7.5)
    link.fail_next(p.CMD_STORE)
    with pytest.raises(StoreUnconfirmed):
        session.store()
    assert link.sent.count(p.CMD_STORE) == 1
    assert session.has_unstored_changes


def test_ecu_error_replies_are_not_retried(session, link):
    link.sent.clear()
    original = link.sim._dispatch

    def refuse(cmd, payload):
        raise KeyError(cmd)

    link.sim._dispatch = refuse
    with pytest.raises(p.EcuError):
        session.poll_runtime()
    assert link.sent.count(p.CMD_POLL_RUNTIME) == 1
    link.sim._dispatch = original
