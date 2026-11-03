"""Features added to match PCLink's menus, checked against the simulator and PCLink's own files."""
import os
import struct
import time
from pathlib import Path
from unittest import mock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_STYLE_OVERRIDE", "Fusion")  # The macOS style cannot draw offscreen.
os.environ.setdefault("OPENPCLINK_OPTIONS", "/nonexistent-openpclink-test/options.json")

from PySide6 import QtWidgets  # noqa: E402

from openpclink import can, crypto, eculog, firmware, importer, scope, tune  # noqa: E402
from openpclink import protocol as p  # noqa: E402
from openpclink.ecu import Ecu, EcuProtected, WriteRefused  # noqa: E402
from openpclink.layoutfile import Layout  # noqa: E402
from openpclink.logfile import ExportOptions, Log, Marker, PcLogger, Series, build_link_log, export_csv, parse_link_log, read_log  # noqa: E402
from openpclink.mapfile import MapFile  # noqa: E402
from openpclink.sim import HARDWARE_G5, PtyServer, SimEcu  # noqa: E402
from openpclink.transports import SerialTransport  # noqa: E402
from openpclink.units import DEFAULT_IMPERIAL, UnitSystem  # noqa: E402
from tests.conftest import PROGRAM  # noqa: E402

needs_program = pytest.mark.skipif(not (PROGRAM / "ECU.cfgr").exists(), reason="needs an extracted PCLink in work/")


def sample_log() -> Log:
    log = Log()
    speed = Series(1000, "Engine Speed", "RPM", period=0.1)
    pressure = Series(1001, "MAP", "", "Pressure", period=0.1)
    for i in range(50):
        speed.append(i * 0.1, 1000.0 + i)
        pressure.append(i * 0.1, 100.0)
    log.series = [speed, pressure]
    log.details.update(Name="test", Notes="hello", Driver="me")
    log.markers = [Marker(0, "First", 0.0), Marker(1, "Key Press", 2.0), Marker(0, "Second", 3.0)]
    log.normalise_sections()
    log.rebuild()
    return log


def test_link_log_round_trips_byte_for_byte():
    data = build_link_log(sample_log())
    log = parse_link_log(data)
    assert build_link_log(log) == data
    assert log.names == ["Engine Speed", "MAP"]
    assert log.details["Notes"] == "hello"
    assert log.series[1].unit_key == "Pressure"
    assert log.channel_of(1001) == 1
    assert [section.name for section in log.sections()] == ["First", "Second"]


def test_link_log_block_layout_is_as_pclink_writes_it():
    data = build_link_log(sample_log())
    assert struct.unpack_from("<I", data, 0)[0] == 0xD7 and data[4:7] == b"lf3"
    assert struct.unpack_from("<I", data, 0xD7)[0] == 0x1C27 and data[0xD7 + 4 : 0xD7 + 7] == b"ld2"
    assert struct.unpack_from("<I", data, 7)[0] == 2
    marker_at = 0xD7 + 0x1C27
    assert data[marker_at + 4 : marker_at + 7] == b"lm1"
    channel_at = marker_at + 0x1B1 * struct.unpack_from("<I", data, 0xD3)[0]
    assert data[channel_at + 4 : channel_at + 7] == b"ds3"
    assert struct.unpack_from("<I", data, channel_at + 0xD3)[0] == 1000


def test_cut_off_log_keeps_what_it_can_read():
    data = build_link_log(sample_log())
    log = Log()
    log.series = sample_log().series * 3
    whole = build_link_log(log)
    assert len(parse_link_log(whole[: len(whole) - 50]).series) >= 2
    with pytest.raises(ValueError):
        parse_link_log(data[:40])


def test_runs_of_equal_values_shrink_to_two_points_unless_all_are_kept():
    shrunk, full = Series(1, "a"), Series(2, "b")
    for i in range(5):
        shrunk.append(float(i), 7.0, False)
        full.append(float(i), 7.0, True)
    assert shrunk.times == [0.0, 4.0] and len(full.times) == 5


def test_range_export_closes_with_held_values():
    log = sample_log()
    out = parse_link_log(build_link_log(log, 1.0, 2.0))
    assert out.series[0].times[0] == pytest.approx(1.0)
    assert out.series[0].times[-1] == pytest.approx(2.0)


def test_csv_export_uses_pclink_layout(tmp_path):
    path = tmp_path / "out.csv"
    export_csv(sample_log(), path, ExportOptions(section_time=True, driver_information=True))
    lines = path.read_text().splitlines()
    assert lines[0] == '"Name","test"'
    assert '"Driver","me"' in lines
    head = lines.index('"File Time","Section Time","Engine Speed","MAP"')
    assert lines[head + 1] == '"s","s","RPM","Pressure"'
    assert lines[head + 3].startswith('"0","0","1000"')
    assert len(read_log(path).time) == 50


def test_recording_keeps_samples_until_saved(tmp_path):
    logger = PcLogger(tmp_path / "x.llg5", [(1000, "Engine Speed")], ["RPM"], 0.0, True)
    assert not (tmp_path / "x.llg5").exists()
    for value in (1.0, 2.0, 3.0):
        logger.sample(lambda _id: value)
        time.sleep(0.001)
    logger.close()
    log = read_log(tmp_path / "x.llg5")
    assert log.series[0].values == [1.0, 2.0, 3.0]


def make_ecu_log(channels, rows, magic=0xA5A55A5A, when=None):
    head = bytearray(16)
    head[1] = 1 if when else 0
    head[3] = len(channels)
    struct.pack_into(">II", head, 4, 3, 77)
    if when:
        head += struct.pack(">II", 2026 << 16 | 11 << 8 | 3, 14 << 24 | 30 << 16 | 5 << 8)
    for data_id, divider in channels:
        head += struct.pack(">II", data_id, divider)
    head += bytes(len(head) & 3)
    body = struct.pack(">IIIII", magic, 1, 0, 0, 0)
    for row in rows:
        body += b"".join(struct.pack(">f", value) for value in row)
    return bytes(head + body)


def test_ecu_log_decoding_follows_dividers():
    data = make_ecu_log([(1000, 1), (1001, 2)], [(1.0, 10.0), (2.0,), (3.0, 30.0)], when=True)
    info = eculog.LogInfo(1000, 1, 0xA5A55A5A, 32)
    header = eculog.parse_header(data)
    assert header.date_text() == "2026/11/03 14:30:05"
    assert header.channels == [(1000, 1), (1001, 2)]
    logs = eculog.build_logs([("a", data)], info, lambda i: (str(i), ""))
    assert logs[0].series[0].values == [1.0, 2.0, 3.0]
    assert logs[0].series[1].values == [10.0, 30.0]
    assert logs[0].series[0].times[1] == pytest.approx(0.001)
    assert logs[0].markers[0].name == "Record Start"


def test_ecu_logs_with_the_same_channels_join_and_others_do_not():
    a = make_ecu_log([(1000, 1)], [(1.0,), (2.0,)])
    b = make_ecu_log([(1000, 1)], [(3.0,)])
    c = make_ecu_log([(1001, 1)], [(4.0,)])
    logs = eculog.build_logs([("a", a), ("b", b), ("c", c)], eculog.LogInfo(100, 3, 0xA5A55A5A, 32), lambda i: (str(i), ""))
    assert len(logs) == 2
    assert logs[0].series[0].values == [1.0, 2.0, 3.0]


@pytest.fixture
def live():
    sim = SimEcu()
    server = PtyServer(sim)
    ecu = Ecu(SerialTransport(server.path))
    ecu.connect()
    yield sim, ecu
    ecu.close()
    server.close()


def test_listing_and_downloading_ecu_logs(live):
    sim, ecu = live
    data = make_ecu_log([(1000, 1)], [(float(i),) for i in range(600)])
    sim.ecu_logs = [("LOG1", data), ("LOG2", data)]
    info, entries = ecu.logs.list_files()
    assert [entry.name for entry in entries] == ["LOG1", "LOG2"]
    assert entries[0].size == len(data) and info.tick_rate == 1000
    assert ecu.logs.read_log_file("LOG1", entries[0].size) == data
    assert len(ecu.logs.read_log_file("LOG1", info.header_size, header_only=True)) >= 16
    ecu.logs.delete("LOG1")
    assert [name for name, _ in sim.ecu_logs] == ["LOG2"]


def test_memo_is_read_lazily_and_written_only_on_store(live):
    sim, ecu = live
    assert p.CMD_FILE not in sim.log
    sim.memo = "Hello"
    assert ecu.read_memo() == "Hello"
    ecu.set_memo("New note")
    assert sim.memo == "Hello"
    assert ecu.has_unstored_changes
    ecu.store()
    assert sim.memo == "New note"
    assert not ecu.has_unstored_changes


def test_trigger_scope_uses_pclinks_rate_and_always_stops(live):
    sim, ecu = live
    loc = ecu.table[1000]
    ecu.memory[loc.offset : loc.offset + loc.width] = bytes(loc.width)
    log = scope.capture(ecu, sleep=lambda _s: None)
    assert sim.scope_rate == 3000
    assert sim.scope_stopped == 1
    assert len(log.series) == 12
    assert len(log.series[0].values) == scope.SAMPLES
    assert scope.sample_rate(0) == 3000
    assert scope.sample_rate(100000) == 60000


def test_scope_analog_scaling():
    assert scope.decode_analog(0, 1) == 0
    assert scope.decode_analog(10, scope.READ_ANALOG) == pytest.approx(80 * 0.001220703125)
    assert scope.decode_analog(255, 1) == pytest.approx(-8 * scope.TRIGGER_SCALE)


def test_password_round_trip_with_temporary_and_permanent_unlock(live):
    sim, ecu = live
    if 2294 not in ecu.table:
        pytest.skip("this simulated ECU has no password settings")
    ecu.set_password("secret")
    assert sim.flash_stored == 1
    assert ecu.is_protected
    with pytest.raises(EcuProtected):
        ecu.get(ecu.table and next(i for i in ecu.table if i not in (2294, 2295, 2303, 2324) and not ecu._is_runtime(ecu.table[i])))
    assert not ecu.unlock("wrong")
    assert ecu.is_protected
    writes = len(sim.writes)
    assert ecu.unlock("secret", temporary=True)
    assert not ecu.is_protected
    assert len(sim.writes) == writes
    ecu._unlocked = False
    assert ecu.unlock("secret")
    assert sim.flash_stored == 2


def test_passwords_are_refused_when_too_short_or_long(live):
    _sim, ecu = live
    for bad in ("ab", "x" * 21, "héllo"):
        with pytest.raises(WriteRefused):
            ecu.set_password(bad)


def test_feature_code_word_order_and_the_lock_code_is_refused(live):
    sim, ecu = live
    ecu.feature_unlock("00010002000300 04".replace(" ", ""))
    assert sim.feature_codes == [bytes.fromhex("0002000100040003")]
    with pytest.raises(WriteRefused):
        ecu.feature_unlock("short")
    with pytest.raises(WriteRefused):
        ecu.feature_unlock("zzzzzzzzzzzzzzzz")
    with pytest.raises(p.ForbiddenCommand):
        ecu.feature_unlock("DEAD0EF1DEAD0EF1")


def test_baro_payload_is_tenths_of_a_kpa(live):
    sim, ecu = live
    ecu.calibrate_baro(101.0)
    assert sim.baro_calibrations == [1010]


def test_factory_reset_needs_the_dedicated_path_and_a_stopped_engine(live):
    sim, ecu = live
    sim.engine_running = True
    sim._tick()
    with pytest.raises(p.ForbiddenCommand):
        ecu.link.request(p.CMD_FACTORY_RESET)
    assert sim.factory_resets == 0
    with pytest.raises(WriteRefused):
        ecu.restore_factory(sleep=lambda _s: None)
    assert sim.factory_resets == 0
    sim.engine_running = False
    sim._tick()
    ecu.poll_runtime()
    ecu.restore_factory(sleep=lambda _s: None)
    assert sim.factory_resets == 1


def test_read_only_mode_blocks_every_dangerous_and_new_writing_command(live, monkeypatch):
    sim, ecu = live
    monkeypatch.setenv(p.READ_ONLY_ENV, "1")
    before = len(sim.frames)
    for call in (
        lambda: ecu.link.request_dangerous(p.CMD_FACTORY_RESET),
        lambda: ecu.link.request_dangerous(p.CMD_FW_REQUEST_BOOT),
        lambda: ecu.link.request(p.CMD_TRIGGER_SCOPE, bytes(6)),
        lambda: ecu.link.request(p.CMD_BULK_DOWNLOAD, bytes(1)),
        lambda: ecu.logs.pause(),
        lambda: ecu.logs.delete("x"),
        lambda: ecu.link.request(p.CMD_CAN_TRANSMIT, bytes(16)),
        lambda: ecu.link.request(p.CMD_WRITE_MEMO, bytes(4)),
        lambda: ecu.link.request(p.CMD_SET_CLOCK, bytes(12)),
    ):
        with pytest.raises(p.ForbiddenCommand):
            call()
    assert len(sim.frames) == before
    ecu.logs.info()
    assert sim.frames[-1][1] == p.CMD_LOG_INFO


def test_connecting_sends_only_the_commands_it_always_has(live):
    sim, _ecu = live
    assert set(sim.log) <= {p.CMD_STATE, p.CMD_BOOT_INFO, p.CMD_FIRMWARE_INFO, p.CMD_IDENTITY, p.CMD_MEMORY_INFO, p.CMD_READ_DESCRIPTOR, p.CMD_READ_CONFIG, p.CMD_READ_SECTION2, p.CMD_POLL_RUNTIME}


def test_undo_short_term_changes_restores_the_ecu(live):
    sim, ecu = live
    data_id = next(i for i, loc in ecu.table.items() if not loc.is_bitfield and not loc.is_string and not loc.read_only and not ecu._is_runtime(loc) and loc.count == 1 and i not in (2294, 2295, 2303, 2324))
    loc = ecu.table[data_id]
    before = bytes(sim.memory[loc.offset : loc.offset + loc.width])
    ecu._write_memory(loc.offset, bytes([(before[0] + 1) & 0xFF]) + before[1:])
    assert ecu.is_changed(data_id)
    assert ecu.undo_short_term_changes() >= 1
    assert bytes(sim.memory[loc.offset : loc.offset + loc.width]) == before
    assert not ecu.is_changed(data_id)
    assert not ecu.has_unstored_changes


def test_comms_processor_settings_on_a_g5():
    sim = SimEcu(hardware_family=HARDWARE_G5)
    sim.comms_values = {10000: {"kind": 7, "count": 16, "text": "10.0.0.1"}, 10002: {"kind": 2, "value": 1, "max": 3.0}, 10003: {"kind": 1, "value": 2.5, "max": 9.0}}
    server = PtyServer(sim)
    ecu = Ecu(SerialTransport(server.path))
    try:
        ecu.connect()
        assert ecu.get_string(10000) == "10.0.0.1"
        assert ecu.get(10002) == 1 and ecu.get(10003) == 2.5
        assert ecu.set(10002, 2) == 2
        assert sim.comms_values[10002]["value"] == 2
        assert ecu.set_string(10000, "1.2.3.4") == "1.2.3.4"
        assert sim.comms_values[10000]["text"] == "1.2.3.4"
        assert any(frame[0] == p.ADDR_COMMS for frame in sim.frames)
        assert ecu.link.addr == p.ADDR_MAIN
        ecu.store()
        assert sum(1 for frame in sim.frames if frame[1] == p.CMD_STORE) == 2
    finally:
        ecu.close()
        server.close()


def test_g4x_never_talks_to_a_comms_processor():
    sim = SimEcu(hardware_family=1000)
    server = PtyServer(sim)
    ecu = Ecu(SerialTransport(server.path))
    try:
        ecu.connect()
        assert sim.frames and all(frame[0] == p.ADDR_MAIN for frame in sim.frames)
    finally:
        ecu.close()
        server.close()


def test_comms_processor_failure_does_not_stop_connecting():
    sim = SimEcu(hardware_family=HARDWARE_G5)
    original = sim._dispatch_comms
    sim._dispatch_comms = lambda _cmd, _payload: (_ for _ in ()).throw(KeyError("no"))
    server = PtyServer(sim)
    ecu = Ecu(SerialTransport(server.path))
    try:
        ecu.connect()
        assert ecu.comms == {} and ecu.link.addr == p.ADDR_MAIN
    finally:
        sim._dispatch_comms = original
        ecu.close()
        server.close()


# ---- firmware -------------------------------------------------------------


KEY = bytes(range(1, 9))


def build_image(address=0x80000, version=(7, 8, 2), body=bytes(8192)) -> tuple[bytes, bytes]:
    masked = (int.from_bytes(body, "big") ^ int.from_bytes((KEY * (len(body) // 8 + 1))[: len(body)], "big")).to_bytes(len(body), "big")
    total = sum(struct.unpack(f">{len(masked) // 4}I", masked)) & 0xFFFFFFFF
    header = bytearray(68)
    struct.pack_into(">H", header, 4, 68)
    struct.pack_into(">H", header, 6, 1)
    header[8] = version[0]
    struct.pack_into(">HH", header, 9, version[1], version[2])
    struct.pack_into(">III", header, 20, address, len(body) // 4, (-total) & 0xFFFFFFFF)
    struct.pack_into(">H", header, 0, p.crc16(bytes(header[2:])))
    return bytes(header), body


def build_package(product=1000, image=None) -> bytes:
    header, body = image or build_image()
    block = header + body
    out = struct.pack("<7I", 1, 1, 9, 2, 0, 1, 1)
    out += struct.pack("<3I", 1, product, 1) + struct.pack("<I", 7)
    out += struct.pack("<8I", 1, 7, 1007, 7, 8, 2, 0, len(block)) + block
    return out


def test_firmware_package_parses_and_picks_by_product():
    package = firmware.parse_package(build_package(), KEY)
    assert package.images_for(1000)[0].version == (7, 8, 2)
    assert package.images_for(999) == []


@pytest.mark.parametrize("damage", ["header", "body", "short", "extra"])
def test_damaged_firmware_is_refused_before_any_ecu_traffic(damage):
    data = bytearray(build_package())
    at = 28 + 16 + 32
    if damage == "header":
        data[at + 8] ^= 1
    elif damage == "body":
        data[at + 100] ^= 1
    elif damage == "short":
        del data[-4:]
    else:
        data += bytes(4)
    with pytest.raises(firmware.FirmwareError):
        firmware.parse_package(bytes(data), KEY)


def test_firmware_needs_the_imported_key():
    with pytest.raises(firmware.FirmwareError, match="Import PCLink again"):
        firmware.parse_package(build_package(), b"")


@needs_program
def test_pclinks_own_firmware_package_passes_every_check():
    key = importer.find_firmware_key((PROGRAM / "PCLink.exe").read_bytes())
    package = firmware.load(PROGRAM / "Firmware" / "firmware_package_1.9.2.lfp", key)
    assert package.version == (1, 9, 2)
    assert [image.version for image in package.images_for(1007)] == [(1, 6, 2), (7, 8, 2)]
    assert package.images_for(1007)[0].is_comms and not package.images_for(1007)[1].is_comms


def test_programming_follows_the_sequence_and_sends_the_body_unchanged():
    sim = SimEcu()
    sim.engine_running = False
    server = PtyServer(sim)
    transport = SerialTransport(server.path)
    try:
        body = bytes(range(256)) * 16
        package = firmware.parse_package(build_package(image=build_image(body=body)), KEY)
        seen = []
        state = firmware.program_image(p.Link(transport), package.images_for(1000)[0], progress=lambda sent, total: seen.append((sent, total)), sleep=lambda _s: None)
        assert state == firmware.STATE_RUNNING
        assert sim.firmware_stage == ["reset", "boot", "start", "header", "main"]
        assert bytes(sim.firmware_body) == body
        assert seen[-1] == (len(body), len(body))
        assert len(seen) == len(body) // firmware.CHUNK
    finally:
        transport.close()
        server.close()


def test_programming_stops_when_the_engine_is_running():
    sim = SimEcu()
    server = PtyServer(sim)
    transport = SerialTransport(server.path)
    try:
        package = firmware.parse_package(build_package(), KEY)
        with pytest.raises(firmware.EngineRunning):
            firmware.program_image(p.Link(transport), package.images_for(1000)[0], sleep=lambda _s: None)
        assert "start" not in sim.firmware_stage
    finally:
        transport.close()
        server.close()


def test_firmware_verdicts():
    assert firmware.verdict((7, 8, 2), (7, 8, 1)) == "Upgrade"
    assert firmware.verdict((7, 8, 0), (7, 8, 1)) == "Downgrade"
    assert firmware.verdict((7, 8, 1), (7, 8, 1)) == "Reinstall"
    assert firmware.verdict((6, 8, 1), (7, 8, 1)) == "Incompatible"
    assert firmware.needs_base_timing_check((6, 19, 0), (6, 21, 0))
    assert not firmware.needs_base_timing_check((7, 0, 0), (7, 8, 0))


# ---- tuning ---------------------------------------------------------------


def test_quick_tune_uses_pclinks_formula_not_a_ratio():
    assert tune.tune_value(50.0, 0.90, 0.85) == pytest.approx(50.0 * 1.05)
    assert tune.tune_value(10.0, 0.90, 0.85, additive=True) == pytest.approx(15.0)
    assert tune.is_additive(tune.FUEL_TABLE_1, 0) is False
    assert tune.is_additive(tune.FUEL_TABLE_2, 1) is False
    assert tune.is_additive(tune.FUEL_TABLE_2, 2) is True


def inputs(**changes):
    values = dict(
        wideband=0.9, target=0.85, has_table=True, table_active=True, rpm=3000.0, load=100.0, ect=90.0,
        rpm_cell=(2750.0, 3250.0, 3000.0), load_cell=(90.0, 110.0, 100.0), cell=(2, 3), now=10.0,
    )
    values.update(changes)
    return tune.TuneInputs(**values)


@pytest.mark.parametrize("changes,expected", [
    (dict(has_table=False), "Fuel table undefined"),
    (dict(target=None), "Oxy target undefined"),
    (dict(wideband=0.0), "No WB Signal"),
    (dict(table_active=False), "Table Inactive"),
    (dict(ect=20.0), "ECT Lockout"),
    (dict(rpm=100.0), "RPM Lockout"),
    (dict(load=900.0), "Load Lockout"),
    (dict(rpm=2800.0), "Not in cell centre"),
])
def test_quick_tune_status_order(changes, expected):
    engine = tune.QuickTune()
    engine.mode = tune.MANUAL
    assert engine.update(inputs(**changes)).text == expected


def test_quick_tune_waits_in_the_cell_centre_then_is_ready():
    engine = tune.QuickTune()
    assert engine.update(inputs()).text == "OFF"
    engine.mode = tune.MANUAL
    assert engine.update(inputs(now=10.0)).text == "Waiting..."
    assert engine.update(inputs(now=12.9)).text == "Waiting..."
    status = engine.update(inputs(now=13.1))
    assert status.text == "Ready" and status.ready
    assert engine.update(inputs(now=13.2, cell=(2, 4))).text == "Waiting..."


def test_automatic_stops_at_target_and_at_max_attempts():
    engine = tune.QuickTune()
    engine.mode = tune.AUTOMATIC
    engine.update(inputs(now=0.0))
    assert engine.update(inputs(now=5.0, wideband=0.85)).text != "Ready"
    engine = tune.QuickTune()
    engine.mode = tune.AUTOMATIC
    engine.update(inputs(now=0.0))
    assert engine.update(inputs(now=5.0), attempts=8).text == "Max Attempts"
    assert engine.update(inputs(now=5.0), attempts=7).text == "Ready"


def test_filter_averages_the_newest_samples():
    engine = tune.QuickTune(tune.QuickTuneSettings(filter=2))
    for value in (1.0, 2.0, 4.0):
        engine.update(inputs(wideband=value))
    assert engine.measured == 3.0


def test_tune_ahead_options():
    engine = tune.QuickTune(tune.QuickTuneSettings(tune_ahead=4, tune_ahead_trim=5.0))
    cells = engine.tune_ahead(0, 0, 50.0, 2, 3, lambda _r, _c: False)
    assert (0, 1, 52.5) in cells and (0, 2, 55.0) in cells and (1, 1, 55.0) in cells
    engine = tune.QuickTune(tune.QuickTuneSettings(tune_ahead=1, tune_ahead_trim=0.0))
    assert engine.tune_ahead(0, 0, 50.0, 2, 3, lambda _r, _c: False) == [(0, 1, 50.0)]
    assert engine.tune_ahead(0, 0, 50.0, 2, 3, lambda _r, _c: True) == []


def test_markers_first_cell_decides_and_tuned_clears_part_tuned():
    marks = tune.CellMarks()
    marks.toggle(1, [(0, 0), (0, 1)], tune.RUNTIME_MARKER)
    assert marks.flag(1, 0, 1) & tune.RUNTIME_MARKER
    marks.toggle(1, [(0, 0), (0, 1)], tune.RUNTIME_MARKER)
    assert not marks.flag(1, 0, 1)
    marks.set_flag(1, 0, 0, tune.PART_TUNED)
    marks.set_flag(1, 0, 0, tune.TUNED)
    assert marks.flag(1, 0, 0) == tune.TUNED


def test_quick_trim():
    assert tune.quick_trim_value(40.0, 1.0, 0.9, 0.0) == pytest.approx(44.0)


def mixture_log() -> Log:
    log = Log()
    rpm, load, wide, target = Series(1000, "RPM", period=0.1), Series(1091, "Load", period=0.1), Series(3040, "Lambda", period=0.1), Series(2936, "Target", period=0.1)
    for i in range(100):
        when = i * 0.1
        rpm.append(when, 2000.0 if i < 50 else 4000.0)
        load.append(when, 50.0)
        wide.append(when, 0.9)
        target.append(when, 0.85)
    log.series = [rpm, load, wide, target]
    log.rebuild()
    return log


def test_mixture_map_groups_by_cell_with_min_samples():
    settings = tune.MixtureSettings(min_samples=5, zone_area=50.0)
    grid = tune.build_mixture_map(mixture_log(), settings, [1000.0, 2000.0, 3000.0, 4000.0], [0.0, 50.0, 100.0], None)
    assert grid[1][1].has_data and grid[1][3].has_data
    assert not grid[1][2].has_data
    assert grid[1][1].actual_average == pytest.approx(0.9)
    assert grid[1][1].error == pytest.approx(0.05)


def test_mixture_map_says_what_is_missing():
    with pytest.raises(tune.MixtureError, match="No log file"):
        tune.build_mixture_map(None, tune.MixtureSettings(), [1.0], [1.0], None)
    log = mixture_log()
    with pytest.raises(tune.MixtureError, match="Wideband"):
        tune.build_mixture_map(log, tune.MixtureSettings(mixture_id=9), [1.0], [1.0], None)


def test_fft_finds_a_tone():
    import math

    block = [math.sin(2 * math.pi * 64 * i / 1024) for i in range(1024)]
    spectrum = tune.fft_magnitudes(block)
    assert spectrum.index(max(spectrum)) == 64


# ---- units, layouts, CAN --------------------------------------------------


def test_unit_factors_are_pclinks():
    units = UnitSystem(DEFAULT_IMPERIAL, stoich=14.7)
    assert units.to_display(100.0, "Temperature") == pytest.approx(212.0)
    assert units.to_display(100.0, "Pressure") == pytest.approx(14.50377)
    assert units.to_base(14.5, "Pressure") == 100.0
    assert units.to_display(1.0, "Lambda") == pytest.approx(14.7)
    assert units.to_display(100.0, "Speed") == pytest.approx(62.1371)
    assert units.label("Temperature") == "°F"
    assert units.to_display(10.0, "TempDifference") == pytest.approx(18.0)
    assert units.to_display(100.0, "VolumeDistance") == pytest.approx(2.35215)


def test_metric_leaves_values_alone_and_forced_lambda_wins():
    units = UnitSystem()
    assert units.to_display(100.0, "Temperature") == 100.0
    forced = UnitSystem(DEFAULT_IMPERIAL, force_lambda=True)
    assert forced.to_display(1.0, "Lambda") == 1.0
    assert forced.label("Lambda") == "lambda"


def test_big_pressure_choices():
    assert UnitSystem({"big_pressure": "bar"}).to_display(1000.0, "BigPressure") == 10.0
    assert UnitSystem({"big_pressure": "MPa"}).to_display(1000.0, "BigPressure") == 1.0


@needs_program
def test_every_shipped_layout_round_trips():
    for path in (PROGRAM / "Layouts").glob("*.llf5"):
        layout = Layout.load(path)
        again = Layout.parse(layout.to_xml())
        assert again.to_xml() == layout.to_xml()
        assert [page.name for page in again.pages] == [page.name for page in layout.pages]


@needs_program
def test_every_shipped_can_stream_parses_and_fits_the_blob():
    key = importer._KEY_MARKER
    files = list((PROGRAM / "CAN").glob("*.lcs"))
    assert len(files) == 53
    for path in files:
        stream = can.load_stream_file(path, key)
        again = can.parse_stream_xml(can.stream_to_xml(stream))
        assert again.frames == stream.frames
        blob = can.build_stream_blob([stream])
        assert can.parse_stream_blob(blob)[0].frames == [_blob_frame(frame) for frame in stream.frames]


def _blob_frame(frame):
    return can.StreamFrame(frame.length & 0xF, frame.id_position, frame.compound_id, frame.parameters)


def test_stream_files_are_rc4_or_plain(tmp_path):
    stream = can.Stream([can.StreamFrame(8, 1, 50, [can.StreamParameter(3040, 16, 16, 0, 0, 1, 1000, 0)])])
    path = tmp_path / "s.lcs"
    can.save_stream_file(stream, path, b"key")
    assert b"xml" not in path.read_bytes()[:20]
    assert can.load_stream_file(path, b"key").frames == stream.frames
    with pytest.raises(can.CanFileError):
        can.decrypt_stream_file(b"junk" * 10, b"key")


def test_can_id_split_and_join():
    assert can.split_id(0x123, False) == (0x123, 0)
    low, high = can.split_id(0x1E000000, True)
    assert can.join_id(low, high) == (0x1E000000, True)
    assert can.join_id(0x123, 0) == (0x123, False)
    assert can.validate_channels(0, [can.CanChannel(mode=1)]) == ["CAN 1 channel 1 has an invalid ID."]


def test_custom_can_file_round_trip(tmp_path):
    content = can.CustomCanFile(lock_serial=7, password="pw", channels=[(0x100, 0x8000, can.Stream([can.StreamFrame(8, 0, 1, [can.StreamParameter(1000)])]))])
    key = (1, 2, 3, 4)
    path = tmp_path / "x.lcf"
    can.save_can_file(content, path, key)
    loaded, plain = can.load_can_file(path, key)
    assert loaded.lock_serial == 7 and loaded.password == "pw"
    assert loaded.channels[0][0] == 0x100
    damaged = bytearray(plain)
    damaged[5] ^= 1
    with pytest.raises(can.CanFileError):
        can.parse_can_file(bytes(damaged))


def test_bridge_frame_layout(live):
    sim, ecu = live
    bridge = can.CanBridge(ecu.link)
    bridge.transmit(can.CanFrame(0x1E000000, True, b"\x01\x02"))
    assert sim.can_transmitted[0] == bytes.fromhex("1E000000" "01" "02" "0102000000000000" "0000")
    sim.can_replies = [struct.pack(">IBB", 0x1F000123, 1, 8) + bytes([1, 0, 0, 9, 0, 0, 0, 42])]
    frames = bridge.receive()
    assert frames[0].can_id == 0x1F000123 and frames[0].extended and frames[0].data[-1] == 42


def test_file_locked_to_a_serial_opens_only_for_that_ecu():
    key = (1, 2, 3, 4)
    data = crypto.encode(b"x" * 40, key, 111111)
    assert crypto.decode(data, key, 111111) == b"x" * 40
    with pytest.raises(crypto.ProtectedFile):
        crypto.decode(data, key)
    with pytest.raises(crypto.ProtectedFile):
        crypto.decode(data, key, 222222)


@needs_program
def test_gui_opens_real_layouts_and_every_menu_action_runs(tmp_path, monkeypatch):
    from openpclink.database import Database
    from openpclink.gui import MainWindow

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    database = Database.load(importer.import_pclink(PROGRAM, tmp_path / "db.json.gz"))
    window = MainWindow(database)
    assert [page.name for page in window.workspace.layout_.pages][:2] == ["Configuration", "Tuning"]
    assert [page.name for page in window.log_workspace.layout_.pages][0] == "Time Plot"
    titles = {action.text() for menu in window.menuBar().findChildren(QtWidgets.QMenu) for action in menu.actions()}
    for wanted in ("Open", "Save", "Save as Encrypted", "Compare Close", "Show Memo", "Exit", "Update Firmware", "Trigger Scope",
                   "CAN Setup", "Restore to Factory Settings", "Setup Password Protection", "Quick Tune (F10)", "Knock Audio",
                   "Reset All Tuning Markers", "Undo Short Term Changes", "Digital Gauge", "Bar Gauge", "Curve Gauge", "Target Gauge",
                   "Export", "Play", "Record", "Stop", "Navigator", "GPS Plot", "Logged Values List"):
        assert wanted in titles, wanted
    window.close()
    window.deleteLater()
    app.processEvents()


def _no_dialogs(monkeypatch, app):
    """Every modal answers at once, so a menu item can be run through without a person."""
    answers = QtWidgets.QMessageBox.StandardButton
    for name in ("question", "warning", "information", "critical"):
        monkeypatch.setattr(QtWidgets.QMessageBox, name, staticmethod(lambda *a, **k: answers.No))
    monkeypatch.setattr(QtWidgets.QMessageBox, "about", staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(QtWidgets.QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: ("", "")))
    monkeypatch.setattr(QtWidgets.QFileDialog, "getOpenFileNames", staticmethod(lambda *a, **k: ([], "")))
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: ("", "")))
    monkeypatch.setattr(QtWidgets.QInputDialog, "getText", staticmethod(lambda *a, **k: ("", False)))
    monkeypatch.setattr(QtWidgets.QInputDialog, "getDouble", staticmethod(lambda *a, **k: (0.0, False)))
    opened = []

    def run(self):
        opened.append(type(self).__name__)
        self.show()
        app.processEvents()
        self.grab()
        self.close()
        return 0

    monkeypatch.setattr(QtWidgets.QDialog, "exec", run)
    return opened


# Left out: these open a file picker or do the deed only after a confirmation the patch above declines.
SKIPPED_ACTIONS = {"Exit", "Connect", "Disconnect", "Search for ECU", "Update Firmware"}


def _run_every_action(window, app):
    errors = []
    window.show_error = errors.append
    for menu in window.menuBar().findChildren(QtWidgets.QMenu):
        if menu is window.layout_menu:
            window._fill_layout_menu()
        for action in list(menu.actions()):
            if action.menu() or action.isSeparator() or action.text() in SKIPPED_ACTIONS:
                continue
            action.trigger()
            app.processEvents()
    return errors


@needs_program
def test_every_menu_item_runs_offline_and_connected(tmp_path, monkeypatch):
    from openpclink.database import Database
    from openpclink.gui import MainWindow

    monkeypatch.setenv("OPENPCLINK_OPTIONS", str(tmp_path / "options.json"))
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    opened = _no_dialogs(monkeypatch, app)
    database = Database.load(importer.import_pclink(PROGRAM, tmp_path / "db.json.gz"))
    xml = crypto.decode((PROGRAM / "Sample Maps" / "Xtreme default map.pclx").read_bytes(), database.map_key)
    sim = SimEcu(xml, hardware_family=1000)
    sim.engine_running = False
    server = PtyServer(sim)
    window = MainWindow(database)
    window.show()
    try:
        _run_every_action(window, app)
        window.connect_to(f"serial:{server.path}")
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and window.status_label.text() != "ONLINE":
            app.processEvents()
            time.sleep(0.01)
        assert window.status_label.text() == "ONLINE"
        before = list(sim.writes), sim.flash_stored, sim.factory_resets
        _run_every_action(window, app)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        window.show_error = lambda _message: None
        for wanted in ("OptionsDialog", "ColourThemeDialog", "LoggingSetupDialog", "LogDownloadDialog", "CanSetupDialog", "MemoDialog", "PasswordDialog"):
            assert wanted in opened, wanted
        assert sim.flash_stored == before[1] and sim.factory_resets == 0
    finally:
        window.log_session.logs.clear()
        for extra in window.windows.values():
            extra.close()
        window.close()
        window.deleteLater()
        app.processEvents()
        server.close()
