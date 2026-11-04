"""Logging Setup: log set files, the ECU Apply sequence, and PC recording with sample history."""
import os
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtWidgets  # noqa: E402

from openpclink import crypto, logsetup, tools  # noqa: E402
from openpclink.sim import SimEcu  # noqa: E402
from openpclink.logfile import KIND_SECTION, PcLogger, SampleHistory, read_log, save_log  # noqa: E402
from tests.test_gui import app, database, pump, session  # noqa: E402,F401

REAL_LOG_SET = (
    b'<?xml version="1.0"?>\n<LinkStorage Version="101" Type=".xml">\n\t<Parameters>\n\t\t<Parameter DataId="1" Name="1081">\n'
    b'\t\t\t<Properties>\n\t\t\t\t<Property Name="Rate" Type="3" Value="5 Hz"/>\n\t\t\t</Properties>\n\t\t</Parameter>\n\t</Parameters>\n</LinkStorage>\n'
)
KEY = (1, 2, 3, 4)
XTREME = Path(__file__).parent.parent / "work" / "decoded" / "xtreme.xml"


@pytest.fixture
def sim():
    if not XTREME.exists():
        pytest.skip("needs work/decoded/xtreme.xml")
    sim = SimEcu(XTREME.read_bytes(), hardware_family=1000)
    sim.engine_running = False
    return sim


def test_log_set_xml_matches_the_file_real_pclink_saved():
    assert logsetup.log_set_xml([(1081, "5 Hz")]) == REAL_LOG_SET


def test_log_set_round_trip_and_ids_without_rates(tmp_path):
    path = tmp_path / "set.lls5"
    logsetup.write_log_set(path, [(1000, "100 Hz"), (1081, "0.5 Hz")], KEY)
    assert logsetup.read_log_set(path, KEY) == [(1000, "100 Hz"), (1081, "0.5 Hz")]
    logsetup.write_log_set(path, [(1000, "")], KEY)
    assert logsetup.read_log_set(path, KEY) == [(1000, "")]
    assert crypto.decode(path.read_bytes(), KEY).startswith(b'<?xml version="1.0"?>')


def test_bad_log_set_is_refused(tmp_path):
    path = tmp_path / "bad.lls5"
    path.write_bytes(b"not a container at all, just text")
    with pytest.raises(logsetup.LogSetError):
        logsetup.read_log_set(path, KEY)


def test_rate_text_and_value():
    assert logsetup.rate_text(100) == "100 Hz" and logsetup.rate_text(0.5) == "0.5 Hz" and logsetup.rate_text(33.333) == "33.3 Hz"
    assert logsetup.rate_value("0.5 Hz") == 0.5 and logsetup.rate_value("") == 0.0


def test_usage_labels():
    assert logsetup.UsageLabels.texts([100.0] * 5, 1000)[:2] == ("5 Logged Parameters", "50% of maximum combined rate")
    assert logsetup.UsageLabels.texts([], 0)[1] == "0% of maximum combined rate"


def test_apply_writes_the_list_pauses_and_resumes(ecu, sim):
    ids = tools.ecu_log_settings(ecu)
    rows = [(1000, 100.0), (1081, 5.0)]
    tools.apply_ecu_logging(ecu, ids, rows, {"rpm": 3000, "map": 0, "tps": 5, "off_delay": 2}, "log", True, 0, 0, 100)
    parameters, rates = ecu.get_all(ids["parameters"]), ecu.get_all(ids["rates"])
    assert parameters[:3] == [1000, 1081, 0] and rates[:3] == [1, 20, 0]
    assert ecu.get(ids["mode"]) == 1 and ecu.get(ids["rpm"]) == 3000 and ecu.get(ids["off_delay"]) == 2
    assert not sim.logging_paused


def test_apply_skips_unchanged_scalars_but_rewrites_the_list(ecu, sim):
    ids = tools.ecu_log_settings(ecu)
    values = {"rpm": 3000, "map": 0, "tps": 0, "off_delay": 0}
    tools.apply_ecu_logging(ecu, ids, [(1000, 10.0)], values, "log", True, 0, 0, 100)
    before = len(sim.writes)
    tools.apply_ecu_logging(ecu, ids, [(1000, 10.0)], values, "log", True, 0, 0, 100)
    capacity = ecu.table[ids["parameters"]].count
    assert len(sim.writes) - before == 2 * capacity + 1  # the list and the label, nothing else


def test_apply_disabled_leaves_logging_off(ecu, sim):
    ids = tools.ecu_log_settings(ecu)
    tools.apply_ecu_logging(ecu, ids, [(1000, 10.0)], {}, "log", False, 0, 0, 100)
    assert ecu.get(ids["mode"]) == 0


def test_history_goes_before_the_start_of_the_recording(tmp_path):
    history = SampleHistory([1000, 1081], 3)
    for value in range(5):
        history.add({1000: 1000.0 + value, 1081: 13.0}.get)
        time.sleep(0.002)
    assert len(history.samples) == 3
    snapshot = history.snapshot()
    assert all(when < 0 for when, _ in snapshot)
    logger = PcLogger(None, [(1000, "Engine Speed"), (1081, "Batt Voltage")], None, 0.01, True, snapshot)
    logger.sample({1000: 1500.0, 1081: 13.5}.get)
    log = logger.finish()
    markers = [(m.name, m.time) for m in log.markers if m.kind == KIND_SECTION]
    assert markers[0][0] == "History" and markers[0][1] < 0
    assert ("PC Logged Data", 0.0) in markers and markers[-1][0] == "EOL"
    assert log.series[0].times[0] < 0 <= log.series[0].times[-1]
    path = tmp_path / "h.llg5"
    save_log(log, path)
    names = [(m.name, m.time < 0) for m in read_log(path).markers if m.kind == KIND_SECTION]
    assert names[0] == ("History", True) and names[1][0] == "PC Logged Data"


def test_recording_without_history_is_unchanged():
    logger = PcLogger(None, [(1000, "Engine Speed")], None, 0.01, True)
    logger.sample({1000: 1500.0}.get)
    log = logger.finish()
    assert [m.name for m in log.markers if m.kind == KIND_SECTION] == ["PC Logged Data", "EOL"]


def test_playback_interval_follows_the_rate():
    assert logsetup.playback_interval({"rate": "40 Hz"}) == 0.025
    assert logsetup.playback_interval({"rate": "0.5 Hz"}) == 2.0
    assert logsetup.playback_interval({}) == 0.025


def test_pc_settings_default_to_loggingoptions_values():
    settings = logsetup.pc_settings({})
    assert settings["rate"] == "40 Hz" and settings["history"] is True and settings["history_size"] == 100 and settings["display_range"] == 40
    assert logsetup.pc_settings({"rate": "5 Hz"})["rate"] == "5 Hz"


def test_dialog_opens_on_the_ecu_tab_with_the_pcLink_fields(session):
    app, window, sim = session
    dialog = tools.LoggingSetupDialog(window, window)
    pump(app, lambda: dialog.info is not None)
    assert [dialog.tabs.tabText(i) for i in range(2)] == ["PC Logging", "ECU Logging"]
    assert dialog.tabs.currentIndex() == 1
    assert dialog.info.max_rate == sim.log_max_rate
    assert [dialog.default_rate.itemText(i) for i in range(dialog.default_rate.count())] == ["100 Hz", "50 Hz", "20 Hz", "10 Hz", "5 Hz", "2 Hz", "1 Hz", "0.5 Hz"]
    assert [dialog.rate.itemText(i) for i in range(dialog.rate.count())][:3] == ["40 Hz", "20 Hz", "15 Hz"]
    assert dialog.rate.currentText() == "40 Hz" and dialog.history_size.value() == 100 and dialog.display_range.value() == 40
    assert not dialog.default_rate.isEnabled()
    dialog.ecu_enabled.setChecked(True)
    assert dialog.default_rate.isEnabled() and dialog.io_on.isEnabled()
    assert 0 not in dialog.pc_available.all_ids() and 0 not in dialog._pc_ids()
    dialog.deleteLater()


def test_dialog_add_checks_and_ok_applies(session):
    app, window, sim = session
    dialog = tools.LoggingSetupDialog(window, window)
    pump(app, lambda: dialog.info is not None)
    warnings = []
    dialog._warn = warnings.append
    assert dialog._ecu_add(1000, 100.0, True) and dialog.usage[0].text() == "1 Logged Parameters"
    assert not dialog._ecu_add(1000, 100.0, True) and warnings[-1] == "Parameter(s) already in list."
    assert not dialog._ecu_add(1081, 33.3, True) and warnings[-1] == "Frequency is not an allowed value."
    dialog.max_rate = 120
    assert not dialog._ecu_add(1081, 50.0, True) and warnings[-1].startswith("Maximum combined logging rate exceeded")
    dialog.max_rate = 100000
    assert dialog._ecu_add(1081, 50.0, True)
    dialog.ecu_enabled.setChecked(True)
    dialog._ok()
    ids = tools.ecu_log_settings(window.worker.ecu)
    assert pump(app, lambda: window.worker.ecu.get(ids["mode"]) == 1)
    assert window.worker.ecu.get_all(ids["parameters"])[:2] == [1000, 1081]
    assert window.worker.ecu.get_all(ids["rates"])[:2] == [round(dialog.tick_rate / 100), round(dialog.tick_rate / 50)]


def test_dialog_saves_and_loads_a_log_set(session, tmp_path, monkeypatch):
    app, window, _ = session
    dialog = tools.LoggingSetupDialog(window, window)
    pump(app, lambda: dialog.info is not None)
    path = tmp_path / "x.lls5"
    dialog._ecu_add(1000, 20.0, True)
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName", lambda *a, **k: (str(path), ""))
    monkeypatch.setattr(QtWidgets.QFileDialog, "getOpenFileName", lambda *a, **k: (str(path), ""))
    dialog._save(True)
    assert logsetup.read_log_set(path, window.database.map_key) == [(1000, "20 Hz")]
    other = tools.LoggingSetupDialog(window, window)
    pump(app, lambda: other.info is not None)
    other._load(True)
    other._load(True)  # loading never removes or duplicates
    assert other.ecu_rows() == [(1000, 20.0)]
    other._save(False)
    assert logsetup.read_log_set(path, window.database.map_key)[0][1] == "5 Hz"
