"""G5 comms processor connect (address 0x7D), the clock, WiFi discovery, and imported-data checks."""
import logging
import struct
from pathlib import Path

import pytest

from openpclink import assets, cli
from openpclink import protocol as p
from openpclink.ecu import WIFI_PASSWORD_ID, Ecu
from openpclink.mapfile import MapFile
from openpclink.sim import PtyServer, SimEcu, UdpServer
from openpclink.transports import SerialTransport, discover_wifi

ROOT = Path(__file__).parent.parent
NEO4 = ROOT / "work" / "decoded" / "neo4.xml"
ECU_XML = ROOT / "work" / "decoded" / "ECU.xml"
needs_maps = pytest.mark.skipif(not (NEO4.exists() and ECU_XML.exists()), reason="needs work/decoded")


def g5(family=1012):
    sim = SimEcu(NEO4.read_bytes(), hardware_family=family)
    if sim.comms_values:
        sim.add_comms_settings(ECU_XML.read_bytes())
    return sim


def connect(sim):
    server = PtyServer(sim)
    ecu = Ecu(SerialTransport(server.path))
    ecu.connect()
    return ecu, server


def sequence(sim):
    out = []
    for addr, cmd, _payload in sim.frames:
        if not out or out[-1][:2] != (addr, cmd):
            out.append((addr, cmd, 1))
        else:
            out[-1] = (addr, cmd, out[-1][2] + 1)
    return out


@needs_maps
def test_g5_connect_asks_the_comms_processor_in_the_order_real_pclink_does():
    sim = g5()
    ecu, server = connect(sim)
    try:
        steps = sequence(sim)
        head = [(a, c) for a, c, _ in steps[:11]]
        assert head == [(0x5B, 3), (0x5B, 0x18), (0x5B, 0x16), (0x7D, 0x18), (0x7D, 0x16), (0x7D, 0x3D), (0x7D, 0x0B),
                        (0x5B, 0x3B), (0x5B, 0x1A), (0x5B, 0x0B), (0x5B, 0x0D)]
        tail = [(a, c) for a, c, _ in steps if a == 0x7D][-3:]
        assert tail == [(0x7D, 4), (0x7D, 0x39), (0x7D, 7)]
        assert sum(n for a, c, n in steps if (a, c) == (0x7D, 4)) == 202
        assert ecu.comms_firmware and ecu.comms_boot
        assert ecu.get_string(10013) == "LinkSSID"
        assert [ecu.get(10000, i) for i in range(4)] == [169, 254, 0, 0]
    finally:
        ecu.close()
        server.close()


@needs_maps
def test_clock_payload_matches_the_captured_frame():
    sim = g5()
    ecu, server = connect(sim)
    try:
        ecu.set_clock(__import__("time").struct_time((2026, 11, 3, 21, 32, 32, 1, 307, 0)))
        assert sim.clock_sets[-1] == bytes.fromhex("00000000" "07ea" "0b" "03" "15" "20" "20" "03")
    finally:
        ecu.close()
        server.close()


@needs_maps
def test_read_only_mode_blocks_the_clock_but_connect_still_works(monkeypatch):
    monkeypatch.setenv(p.READ_ONLY_ENV, "1")
    sim = g5()
    ecu, server = connect(sim)
    try:
        assert sim.clock_sets == []
        assert ecu.comms and not any(cmd == p.CMD_SET_CLOCK for _, cmd, _ in sim.frames)
        assert any(cmd == p.CMD_COMMS_POLL_A for _, cmd, _ in sim.frames)
    finally:
        ecu.close()
        server.close()


@needs_maps
def test_a_g4x_never_gets_a_comms_frame():
    sim = g5(family=1000)
    sim.comms_values = {10000: {"kind": 2, "value": 1}}
    ecu, server = connect(sim)
    try:
        ecu.poll_runtime()
        ecu.poll_comms()
        assert all(addr == p.ADDR_MAIN for addr, _, _ in sim.frames)
        assert not ecu.comms
    finally:
        ecu.close()
        server.close()


@needs_maps
def test_comms_poll_brings_in_the_clock_and_the_password_stays_out_of_logs_and_maps(caplog):
    caplog.set_level(logging.DEBUG)
    sim = g5()
    sim.comms_values[WIFI_PASSWORD_ID]["text"] = "hunter2pass"
    ecu, server = connect(sim)
    try:
        ecu.poll_comms()
        assert ecu.get(10118) >= 2026
        assert "hunter2pass" not in caplog.text
        entry = MapFile.from_ecu(ecu, _NoDatabase()).entries[WIFI_PASSWORD_ID]
        assert entry.text == "*" * 12
    finally:
        ecu.close()
        server.close()


class _NoDatabase:
    def known(self, _id):
        return False


def test_wifi_discovery_lists_who_answers():
    sim = SimEcu()
    server = UdpServer(sim)
    try:
        assert discover_wifi(0.5, ("127.0.0.1",), server.port) == [("127.0.0.1", "OK")]
        assert discover_wifi(0.2, ("127.0.0.1",), server.port + 1) == []
    finally:
        server.close()


def test_selection_dialog_reports_status_and_choice():
    from PySide6 import QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from openpclink.wifi_dialog import WifiSelectionDialog

    dialog = WifiSelectionDialog(None, lambda: [("10.0.0.5", "Link G5")])
    dialog.refresh()
    assert dialog.status.text() == "Refreshed" and dialog.chosen() == "udp:10.0.0.5"
    empty = WifiSelectionDialog(None, lambda: [])
    empty.refresh()
    assert empty.status.text() == "No ECUs found" and not empty.ok_button.isEnabled() and empty.chosen() is None
    assert app


def test_old_imports_are_reported_with_what_they_lack():
    class Old:
        firmware_key = b""
        stream_key = b""
        file_key = ()

    gaps = assets.data_gaps(Old())
    assert any("firmware key" in gap for gap in gaps) and any("CAN file keys" in gap for gap in gaps)


def test_check_command_exists():
    assert "check" in cli.build_parser().format_help()
