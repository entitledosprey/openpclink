"""CAN Setup: the Apply writes, stream blob and .lcf bytes seen from real PCLink 7.8.2, and the dialog."""
import os
import struct
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtWidgets  # noqa: E402

from openpclink import can, crypto  # noqa: E402
from openpclink import protocol as p  # noqa: E402
from openpclink.ecu import Ecu  # noqa: E402
from openpclink.sim import PtyServer, SimEcu  # noqa: E402
from openpclink.transports import SerialTransport  # noqa: E402
from tests.test_gui import app, database, dispose, pump, session  # noqa: E402,F401

XTREME = Path(__file__).parent.parent / "work" / "decoded" / "xtreme.xml"

# Captured from real PCLink, loading CANchecked_TCCxx_1-4_id1797.lcs into stream 1 and pressing Apply.
REAL_BLOB = bytes.fromhex(
    "440001010408000095050010010004000000960510100100040000009705201001000400000098053010010004000000"
    "01000100010001000100010001000100010083fc"
)
# The same stream as the one file channel of a .lcf that real PCLink saved (ID 1797, transmit, 1 Hz).
REAL_LCF = bytes.fromhex(
    "44000101000000000000000005070000e8832e0001010408000095050010010004000000960510100100040000009705"
    "201001000400000098053010010004000000e5fa"
)


def tcc_stream() -> can.Stream:
    return can.Stream([can.StreamFrame(8, 0, 0, [can.StreamParameter(1429 + i, i * 16, 16, 0, 0, 1, 4, 0) for i in range(4)])])


def test_stream_blob_matches_real_pclink():
    assert can.build_stream_blob([tcc_stream()]) == REAL_BLOB
    assert can.parse_stream_blob(REAL_BLOB)[0].frames == tcc_stream().frames


def test_custom_can_file_bytes_match_real_pclink():
    content = can.CustomCanFile(channels=[(1797, 0x8000 | 1000, tcc_stream())])
    assert can.build_can_file(content) == REAL_LCF
    parsed = can.parse_can_file(REAL_LCF)
    assert parsed.channels[0][:2] == (1797, 0x83E8) and not parsed.has_password


def test_transmit_rate_is_milliseconds_stored_in_5ms_steps():
    assert [can.rate_value(h) for h in can.RATES_HZ] == [1000, 500, 200, 100, 50, 20, 10, 5]
    assert can.rate_index(200) == 2 and can.rate_index(999) == -1


def test_scale_generator_reduces_the_ratio():
    assert can.scale_factors(0, 1000, 0, 100, True) == (10, 1, 0)
    assert can.scale_factors(0, 255, 0, 100, True)[:2] == (51, 20)
    assert can.scale_factors(0, 255, 0, 100, False)[:2] == (20, 51)
    assert can.calculate(100, 51, 20, 0) == pytest.approx(255)


def test_password_stays_closed_without_the_right_one():
    content = can.CustomCanFile(password="secret", channels=[(1, 0, can.Stream())])
    assert "secret" not in repr(content)
    assert content.has_password and not content.check_password("guess") and content.check_password("secret")


def test_validation_messages():
    streams = [can.Stream() for _ in range(10)]
    streams[0] = tcc_stream()
    modules = [can.ModuleSettings(1, 0, [can.CanChannel(128, 0, 5)] + [can.CanChannel() for _ in range(9)]), can.ModuleSettings()]
    problems = can.validate_modules(modules, streams, 0, lambda data_id: False)
    assert problems == ["CAN 1 channel 1 has a parameter that cannot be written by CAN."]
    modules[0].channels[0] = can.CanChannel(1, 200, 0)
    assert can.validate_modules(modules, streams, 0, lambda data_id: True) == ["CAN 1 channel 1 has an invalid ID."]
    assert "ODB" in can.validate_modules(modules, streams, 2, lambda data_id: True)[-1]


class Recording(SimEcu):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.config_writes: list[tuple[int, int, bytes]] = []

    def handle(self, frame):
        if len(frame) > 8 and struct.unpack_from(">H", frame, 2)[0] == p.CMD_WRITE_CONFIG:
            offset, count = struct.unpack_from(">HH", frame, 6)
            self.config_writes.append((offset, count, bytes(frame[10 : 10 + count])))
        return super().handle(frame)


@pytest.fixture
def xtreme():
    if not XTREME.exists():
        pytest.skip("needs work/decoded/xtreme.xml")
    sim = Recording(XTREME.read_bytes(), hardware_family=1000)
    server = PtyServer(sim)
    ecu = Ecu(SerialTransport(server.path))
    ecu.connect()
    yield sim, ecu
    ecu.close()
    server.close()


def location(ecu, data_id):
    return ecu.table[data_id].offset - ecu.info.runtime_size


def test_apply_writes_what_real_pclink_writes(xtreme):
    sim, ecu = xtreme
    streams = [tcc_stream()] + [can.Stream() for _ in range(9)]
    module = can.ModuleSettings(1, 0)
    module.channels[0] = can.CanChannel(1, 1000, 1797)
    can.apply_modules(ecu, [module, can.ModuleSettings(ecu.get(1731), ecu.get(1735))], 0, streams)
    base = location(ecu, 1125)
    blob = b"".join(data for offset, count, data in sim.config_writes if base <= offset < base + 2 * len(REAL_BLOB))
    assert blob == b"".join(bytes([0, b]) for b in REAL_BLOB)
    rest = [w for w in sim.config_writes if not base <= w[0] < base + 2 * len(REAL_BLOB)]
    assert rest == [
        (location(ecu, 1730), 1, b"\x01"),
        (location(ecu, 818), 2, b"\x00\xc8"),
        (location(ecu, 808), 1, b"\x01"),
        (location(ecu, 828), 2, b"\x07\x05"),
        (location(ecu, 1129), 1, b"\x80"),
        (location(ecu, 1129), 1, b"\x00"),
    ]


def test_apply_order_with_second_module_obd_and_extended_id(xtreme):
    sim, ecu = xtreme
    first = can.ModuleSettings(ecu.get(1730), ecu.get(1734))
    second = can.ModuleSettings(1, 1)
    second.channels[1] = can.CanChannel(2, can.rate_value(5), 5000000, True)
    can.apply_modules(ecu, [first, second], 1, [can.Stream() for _ in range(10)])
    base = location(ecu, 1125)
    rest = [w for w in sim.config_writes if not base <= w[0] < base + 2000]
    assert rest[:5] == [
        (location(ecu, 1731), 1, b"\x01"),
        (location(ecu, 1735), 1, b"\x04"),
        (location(ecu, 1757), 2, b"\x00\x28"),
        (location(ecu, 1745), 1, b"\x02"),
        (location(ecu, 1769), 2, b"\x4b\x40"),
    ]
    assert rest[5] == (location(ecu, 951), 2, b"\x80\x4c")
    assert [w[0] for w in rest[6:]] == [location(ecu, 1729), location(ecu, 1129), location(ecu, 1129)]


def test_bridge_start_names_the_module_from_one(xtreme):
    sim, ecu = xtreme
    seen = []
    original = ecu.link.request
    ecu.link.request = lambda cmd, payload=b"", *a, **k: (seen.append((cmd, payload)), original(cmd, payload, *a, **k))[1]
    can.CanBridge(ecu.link, 1).start()
    assert seen == [(p.CMD_CAN_CONTROL, bytes(4)), (p.CMD_CAN_CONTROL, bytes([2, 0, 0, 0]))]


def test_read_only_mode_blocks_every_can_command(xtreme, monkeypatch):
    sim, ecu = xtreme
    monkeypatch.setenv(p.READ_ONLY_ENV, "1")
    bridge = can.CanBridge(ecu.link)
    with pytest.raises(p.ForbiddenCommand):
        bridge.transmit(can.CanFrame(0x1E000000, True, b"\x01"))
    with pytest.raises(p.ForbiddenCommand):
        bridge.start()
    assert sim.can_transmitted == []


def test_link_device_request_and_keypad_frames(xtreme):
    sim, ecu = xtreme
    bridge = can.CanBridge(ecu.link)
    can.keypad_node_id(bridge, 21, 22)
    can.set_lambda(bridge, 0x1E000123, 2, 0)
    assert sim.can_transmitted[0][:4] == struct.pack(">I", 0x615) and sim.can_transmitted[0][6:11] == bytes.fromhex("2f13200016")
    assert sim.can_transmitted[1][6:9] == bytes([0x18, 2, 4])
    with pytest.raises(can.DeviceError):
        can.keypad_node_id(bridge, 21, 0x80)


def test_device_definition_parses_parameters():
    xml = b'<LinkStorage><Parameters><Parameter DataId="24" Name="Terminator" Type="3" DecPlaces="0"/><Parameter DataId="30" Name="Gain" Type="0" DecPlaces="2"/></Parameters></LinkStorage>'
    fields = can.parse_device_definition(xml)
    assert [(f.register, f.name, f.kind, f.decimals) for f in fields] == [(24, "Terminator", 3, 0), (30, "Gain", 0, 2)]


def test_dialog_opens_and_applies_through_the_window(session):
    app, window, sim = session
    from openpclink.cansetup import CanSetupDialog

    dialog = CanSetupDialog(window)
    assert [dialog.tabs.tabText(i) for i in range(dialog.tabs.count())] == ["Mode", "Streams", "Test Calculator", "CAN Devices", "Custom CAN File"]
    dialog.module_mode.setCurrentIndex(dialog.module_mode.findData(1))
    dialog.channel_list.setCurrentRow(0)
    dialog.channel_mode.setCurrentIndex(dialog.channel_mode.findData(1))
    dialog.can_id.setText("1797")
    dialog.can_id.textEdited.emit("1797")
    assert dialog.channel_list.item(0).text() == "1: Transmit User Stream 1"
    dialog.extended.setChecked(True)
    dialog.can_id.textEdited.emit("5000000")
    assert dialog._channel().extended and dialog._channel().can_id == 5000000
    dialog.normal.setChecked(True)
    assert not dialog._channel().extended and dialog._channel().can_id == 0
    dialog.can_id.setText("1797")
    dialog.can_id.textEdited.emit("1797")
    assert dialog.apply()
    ecu = window.worker.ecu
    assert pump(app, lambda: ecu.get(808) == 1 and ecu.get(828) == 1797)
    assert ecu.get(1730) == 1 and ecu.get(818) == 1000
    dialog.deleteLater()


def test_dialog_blocks_a_bad_id_and_read_only_blocks_find(session, monkeypatch):
    app, window, sim = session
    from openpclink import cansetup

    shown = []
    monkeypatch.setattr(cansetup, "_message", lambda parent, text, warn=True: shown.append(text))
    dialog = cansetup.CanSetupDialog(window)
    dialog.module_mode.setCurrentIndex(dialog.module_mode.findData(1))
    dialog.channel_list.setCurrentRow(0)
    dialog.channel_mode.setCurrentIndex(dialog.channel_mode.findData(1))
    assert dialog.apply() is False and shown == ["CAN 1 channel 1 has an invalid ID."]
    monkeypatch.setenv(p.READ_ONLY_ENV, "1")
    dialog.find_devices()
    assert shown[-1] == cansetup.READ_ONLY_TEXT and sim.can_transmitted == []
    dialog.deleteLater()


def test_file_tab_edits_and_reloads_a_custom_can_file(session, tmp_path, monkeypatch):
    app, window, sim = session
    from openpclink import cansetup

    shown = []
    monkeypatch.setattr(cansetup, "_message", lambda parent, text, warn=True: shown.append(text))
    dialog = cansetup.CanSetupDialog(window)
    channel = dialog.file_channels[0]
    channel.stream = tcc_stream()
    dialog._refresh_file_panel((0, -1))
    dialog.file_transmit.setChecked(True)
    dialog.file_id.setText("1797")
    dialog.file_id.textEdited.emit("1797")
    assert dialog._file_problems() == []
    content = cansetup.file_from_channels(dialog.file_channels)
    assert can.build_can_file(content) == REAL_LCF
    assert cansetup.channels_from_file(can.parse_can_file(REAL_LCF))[0].can_id == 1797
    dialog.file_extended.setChecked(True)
    assert cansetup.file_from_channels(dialog.file_channels).channels[0][0] == 1797 | can.EXTENDED
    dialog.deleteLater()
