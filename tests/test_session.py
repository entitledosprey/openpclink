import json

import pytest

from openpclink import protocol as p
from openpclink.ecu import Ecu, EcuProtected, WriteMismatch, WriteRefused
from openpclink.sim import PtyServer, SimEcu
from openpclink.transports import SerialTransport


def test_connect_reads_identity(ecu):
    assert ecu.info.firmware == "7.8.1"
    assert ecu.info.serial == 111111
    assert 278 in ecu.table


def test_reads_scaled_signed_and_offset_values(ecu):
    assert ecu.get(300) == pytest.approx(5.0)
    assert ecu.get(301) == pytest.approx(-10.0)
    assert ecu.get(1004) == pytest.approx(85.0)


def test_set_changes_ecu_memory_and_reads_back(ecu, sim):
    assert ecu.set(300, 7.5) == pytest.approx(7.5)
    fresh = Ecu(ecu.transport)
    fresh.connect()
    assert fresh.get(300) == pytest.approx(7.5)


def test_table_cell_write_only_touches_that_cell(ecu):
    before = ecu.get_all(278)
    ecu.set(278, 42.0, index=17)
    after = ecu.get_all(278)
    assert after[17] == pytest.approx(42.0)
    assert after[:17] == pytest.approx(before[:17])
    assert after[18:] == pytest.approx(before[18:])


def test_bitfield_write_preserves_neighbours_in_the_byte(ecu):
    assert ecu.get(311) == 1
    ecu.set(310, 3)
    assert ecu.get(310) == 3
    assert ecu.get(311) == 1


def test_string_round_trip(ecu):
    assert ecu.get_string(320) == "Sim"
    assert ecu.set_string(320, "Hello") == "Hello"


def test_changes_are_lost_on_reboot_until_stored(ecu, sim):
    ecu.set(300, 8.0)
    assert ecu.has_unstored_changes
    sim.reboot()
    ecu.reload()
    assert ecu.get(300) == pytest.approx(5.0)

    ecu.set(300, 8.0)
    ecu.store()
    assert sim.flash_stored == 1
    assert sim.factory_resets == 0
    assert not ecu.has_unstored_changes
    sim.reboot()
    ecu.reload()
    assert ecu.get(300) == pytest.approx(8.0)


def test_runtime_values_update_on_poll(ecu):
    ecu.poll_runtime()
    first = ecu.get(1000)
    import time
    time.sleep(0.2)
    ecu.poll_runtime()
    assert ecu.get(1000) != first


def test_read_only_runtime_write_refused_and_nothing_sent(ecu, sim):
    sim.log.clear()
    with pytest.raises(WriteRefused):
        ecu.set(1000, 9000)
    assert p.CMD_WRITE_CONFIG not in sim.log


@pytest.mark.parametrize("value", [1e9, -1e9, float("nan"), float("inf")])
def test_unrepresentable_values_refused_and_nothing_sent(ecu, sim, value):
    sim.log.clear()
    with pytest.raises(WriteRefused):
        ecu.set(300, value)
    assert p.CMD_WRITE_CONFIG not in sim.log


def test_bad_bitfield_option_refused(ecu, sim):
    sim.log.clear()
    with pytest.raises(WriteRefused):
        ecu.set(310, 9)
    with pytest.raises(WriteRefused):
        ecu.set(310, 1.5)
    assert p.CMD_WRITE_CONFIG not in sim.log


def test_table_index_out_of_range_refused(ecu):
    with pytest.raises(WriteRefused):
        ecu.set(278, 1.0, index=176)


def test_mismatched_echo_is_reported_and_shadow_shows_ecu_value(ecu, sim):
    original = sim._dispatch

    def stubborn(cmd, payload):
        if cmd == p.CMD_WRITE_CONFIG:
            return b"\x00\x00"  # ECU clamps to zero regardless of request.
        return original(cmd, payload)

    sim._dispatch = stubborn
    with pytest.raises(WriteMismatch):
        ecu.set(300, 7.5)
    assert ecu.get(300) == pytest.approx(5.0)  # What the ECU holds, not the reply.


def test_write_succeeds_if_reply_differs_but_ecu_holds_the_value(ecu, sim):
    # Guards against real hardware acknowledging in a form other than an echo.
    original = sim._dispatch

    def acknowledging(cmd, payload):
        result = original(cmd, payload)
        return b"\x00\x01" if cmd == p.CMD_WRITE_CONFIG else result

    sim._dispatch = acknowledging
    assert ecu.set(300, 7.5) == pytest.approx(7.5)


def test_large_write_is_split_to_ecu_packet_size(sim):
    server = PtyServer(sim)
    session = Ecu(SerialTransport(server.path))
    session.connect()
    session.link.max_packet = 16
    session._write_memory(session.location(278).offset, bytes(100))
    assert sim.log.count(p.CMD_WRITE_CONFIG) == 9
    session.close()
    server.close()


def test_protected_ecu_is_read_only_and_hides_the_tune(sim):
    sim.memory[sim.offsets[2324]] |= 0x80
    server = PtyServer(sim)
    session = Ecu(SerialTransport(server.path))
    session.connect()
    assert session.is_protected
    sim.log.clear()
    with pytest.raises(EcuProtected):
        session.get(300)
    with pytest.raises(EcuProtected):
        session.set(300, 1.0)
    with pytest.raises(EcuProtected):
        session.get_all(278)
    assert p.CMD_WRITE_CONFIG not in sim.log
    assert session.get(1000) is not None  # Live data stays visible, as in PCLink.
    session.close()
    server.close()


def test_real_pclink_password_layout_locks_the_ecu(sim):
    # Real PCLink writes the password as u16 characters to 2294 and clears 2295.
    at = sim.offsets[2294]
    for i, char in enumerate(b"abc123"):
        sim.memory[at + i * 2 : at + i * 2 + 2] = bytes([0, char])
    server = PtyServer(sim)
    session = Ecu(SerialTransport(server.path))
    session.connect()
    assert session.is_protected
    with pytest.raises(EcuProtected):
        session.get(300)
    session.close()
    server.close()


def test_ecu_is_not_locked_when_entered_password_matches(sim):
    for data_id in (2294, 2295):
        sim.memory[sim.offsets[data_id] : sim.offsets[data_id] + 2] = bytes([0, 0x61])
    server = PtyServer(sim)
    session = Ecu(SerialTransport(server.path))
    session.connect()
    assert not session.is_protected
    session.close()
    server.close()


@pytest.mark.parametrize("data_id", [2294, 2295, 2303, 2324])
def test_password_parameters_cannot_be_read_or_written(ecu, sim, data_id):
    sim.log.clear()
    with pytest.raises(EcuProtected):
        ecu.get(data_id)
    with pytest.raises(EcuProtected):
        ecu.set(data_id, 0)
    assert p.CMD_WRITE_CONFIG not in sim.log


def test_bootloader_state_is_refused(sim):
    sim.state = 3
    server = PtyServer(sim)
    session = Ecu(SerialTransport(server.path))
    with pytest.raises(p.ProtocolError, match="bootloader"):
        session.connect()
    session.close()
    server.close()


def test_backup_excludes_secrets(ecu, tmp_path):
    target = tmp_path / "backup.json"
    ecu.backup(target)
    saved = json.loads(target.read_text())
    assert "300" in saved["values"]
    assert "2324" not in saved["values"]
    assert "1000" not in saved["values"]


def test_wifi_transport_full_cycle(udp_ecu, sim):
    assert udp_ecu.info.firmware == "7.8.1"
    assert udp_ecu.set(300, 6.5) == pytest.approx(6.5)
    udp_ecu.store()
    assert sim.flash_stored == 1
