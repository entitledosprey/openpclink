"""Drives the real main window against the simulator, headless."""
import os
import struct
import time
from unittest import mock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtCore, QtWidgets  # noqa: E402

from openpclink import crypto  # noqa: E402
from openpclink.database import Database  # noqa: E402
from openpclink.gauges import Gauge, fraction_of  # noqa: E402
from openpclink.gui import MainWindow  # noqa: E402
from openpclink.importer import import_pclink  # noqa: E402
from openpclink.sim import PtyServer, SimEcu  # noqa: E402
from openpclink.workspace import LiveView  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture(scope="module")
def database(tmp_path_factory):
    from tests.conftest import PROGRAM

    if not (PROGRAM / "ECU.cfgr").exists():
        pytest.skip("needs an extracted PCLink in work/")
    return Database.load(import_pclink(PROGRAM, tmp_path_factory.mktemp("db") / "db.json.gz"))


os.environ.setdefault("OPENPCLINK_OPTIONS", "/nonexistent-openpclink-test/options.json")

_kept_windows = []


def dispose(app, window):
    """Tear down in a fixed order; Python's garbage collector must not pick the moment."""
    window.log_session.logs.clear()
    window.close()
    window.deleteLater()
    _kept_windows.append(window)
    app.processEvents()


def pump(app, until, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if until():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def session(app, database):
    from tests.conftest import PROGRAM

    xml = crypto.decode((PROGRAM / "Sample Maps" / "Xtreme default map.pclx").read_bytes(), database.map_key)
    sim = SimEcu(xml)
    server = PtyServer(sim)
    window = MainWindow(database)
    window.show()
    window.errors = []
    window.show_error = window.errors.append
    window.worker.failed.disconnect()
    window.worker.failed.connect(window.show_error)
    window.connect_to(f"serial:{server.path}")
    assert pump(app, lambda: window.status_label.text() == "ONLINE"), "did not connect"
    yield app, window, sim
    if window.worker.ecu:
        window.worker.ecu._dirty = False
    dispose(app, window)
    server.close()


def find_item(window, *path):
    items = [window.tree.topLevelItem(i) for i in range(window.tree.topLevelItemCount())]
    item = None
    for name in path:
        item = next(candidate for candidate in items if candidate.text(0) == name)
        items = [item.child(i) for i in range(item.childCount())]
    return item


def test_switching_forms_with_an_editor_focused_is_safe(session):
    app, window, sim = session
    window.tree.setCurrentItem(find_item(window, "Configuration"))
    spin = next(e for e in window.form.editors.values() if isinstance(e, (QtWidgets.QDoubleSpinBox, QtWidgets.QComboBox)))
    spin.setFocus()
    app.processEvents()
    window.tree.setCurrentItem(find_item(window, "Engine Protection", "RPM Limit"))
    for _ in range(20):
        app.processEvents()
    assert sim.writes == []


def test_connects_and_builds_full_menu(session):
    _, window, _ = session
    names = {window.tree.topLevelItem(i).text(0) for i in range(window.tree.topLevelItemCount())}
    assert {"Fuel", "Ignition", "Engine Protection"} <= names


def test_live_values_update(session):
    app, window, _ = session
    assert window.reading(1000) is not None
    page = window.workspace.current()
    lists = page.bodies(LiveView)
    assert lists
    assert pump(app, lambda: any(view.body.readings for view in lists))


def test_change_enum_through_ui_reaches_ecu_and_stores(session):
    app, window, sim = session
    window.tree.setCurrentItem(find_item(window, "Engine Protection", "RPM Limit"))
    data_id = next(i for i in window.form.editors if window.database.parameter(i).caption == "RPM Limit Mode")
    combo = window.form.editors[data_id]
    combo.setCurrentIndex(1)
    combo.activated.emit(1)
    assert pump(app, lambda: sim.writes)
    assert sim.writes == [bytes.fromhex("37b3000120")]
    assert pump(app, lambda: "Unstored" in window.unstored_label.text())
    window.worker.post("store")
    assert pump(app, lambda: sim.flash_stored == 1)
    assert sim.factory_resets == 0
    assert pump(app, lambda: window.unstored_label.text() == "")


def test_table_shows_ecu_axis_breakpoints(session):
    app, window, _ = session
    item = window.database.parameter(2365)
    window.form.show_parameters([item])
    grid = window.form.editors[2365].grid
    assert grid.rowCount() == 20 and grid.columnCount() == 22
    expected = window.worker.ecu.get_all(item.column_axis_id)[0]
    assert grid.horizontalHeaderItem(0).text() == f"{expected:g}"
    assert grid.item(0, 0).text() == f"{window.worker.ecu.get(2365, 0):.1f}"


def test_moving_focus_past_a_field_writes_nothing(session):
    app, window, sim = session
    window.form.show_parameters([window.database.parameter(i) for i in (1601, 2319, 1878)])
    spins = [e for e in window.form.editors.values() if isinstance(e, QtWidgets.QDoubleSpinBox)]
    for spin in spins:
        spin.editingFinished.emit()
    pump(app, lambda: False, timeout=0.3)
    assert sim.writes == []


def test_scroll_wheel_cannot_change_a_setting(session):
    from PySide6 import QtGui

    app, window, sim = session
    window.tree.setCurrentItem(find_item(window, "Engine Protection", "RPM Limit"))
    combo = next(e for e in window.form.editors.values() if isinstance(e, QtWidgets.QComboBox))
    before = combo.currentIndex()
    event = QtGui.QWheelEvent(
        QtCore.QPointF(5, 5), QtCore.QPointF(5, 5), QtCore.QPoint(0, 0), QtCore.QPoint(0, -120),
        QtCore.Qt.MouseButton.NoButton, QtCore.Qt.KeyboardModifier.NoModifier,
        QtCore.Qt.ScrollPhase.NoScrollPhase, False,
    )
    app.sendEvent(combo, event)
    pump(app, lambda: False, timeout=0.2)
    assert combo.currentIndex() == before
    assert sim.writes == []


def test_refused_write_shows_an_error_and_sends_nothing(session):
    app, window, sim = session
    window.write(1000, 500.0, 0)
    assert pump(app, lambda: window.errors)
    assert "read-only" in window.errors[0]
    assert sim.writes == []


def test_locked_ecu_hides_settings(app, database):
    sim = SimEcu()
    at = sim.offsets[2294]
    sim.memory[at : at + 2] = bytes([0, 0x61])
    server = PtyServer(sim)
    window = MainWindow(database)
    window.connect_to(f"serial:{server.path}")
    assert pump(app, lambda: window.status_label.text() == "ONLINE")
    assert window.tree.topLevelItemCount() == 0
    assert "Password protection" in window.placeholder.text()
    assert not window.store_action.isEnabled()
    dispose(app, window)
    server.close()


def test_connection_loss_goes_offline(app, database):
    sim = SimEcu()
    server = PtyServer(sim)
    window = MainWindow(database)
    errors = []
    window.show_error = errors.append
    window.worker.disconnected.disconnect()
    window.worker.disconnected.connect(window._on_disconnected)
    window.connect_to(f"serial:{server.path}")
    assert pump(app, lambda: window.status_label.text() == "ONLINE")
    server._stop.set()
    server._thread.join()
    assert pump(app, lambda: window.status_label.text() == "OFFLINE", timeout=5)
    assert errors
    dispose(app, window)
    server.close()


def test_table_keys_change_only_the_selected_cells(session):
    from PySide6 import QtGui

    app, window, sim = session
    window.form.show_parameters([window.database.parameter(2365)])
    editor = window.form.editors[2365]
    editor.grid.setCurrentCell(2, 3)
    assert editor.grid.currentItem().isSelected()
    index = 2 * 22 + 3
    before = window.worker.ecu.get(2365, index)
    event = QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_Q, QtCore.Qt.KeyboardModifier.NoModifier, "q")
    app.sendEvent(editor.grid, event)
    assert pump(app, lambda: sim.writes)
    assert len(sim.writes) == 1
    assert pump(app, lambda: window.worker.ecu.get(2365, index) > before)


def test_live_cell_tracking_never_writes_to_the_ecu(session):
    app, window, sim = session
    window.form.show_parameters([window.database.parameter(2365)])
    editor = window.form.editors[2365]
    positions = [(0, 0), (1, 1), (2, 2)]
    original = window.table_position
    window.table_position = lambda *_args: positions.pop() if positions else (3, 3)
    for _ in range(5):
        editor.update_trace()
    pump(app, lambda: False, timeout=0.3)
    window.table_position = original
    assert editor.trace == (3, 3)
    assert sim.writes == []


def test_settings_tree_starts_like_real_pclink(session):
    _, window, _ = session
    names = [window.tree.topLevelItem(i).text(0) for i in range(window.tree.topLevelItemCount())]
    assert names[:4] == ["Configuration", "Fuel", "Ignition", "Engine Protection"]


def test_runtime_tree_lists_live_values_and_adds_gauges(session):
    app, window, _ = session
    assert window.runtime_tree.topLevelItemCount() > 0
    leaf = window.runtime_tree.topLevelItem(0)
    while leaf.childCount():
        leaf = leaf.child(0)
    assert isinstance(leaf.data(0, QtCore.Qt.ItemDataRole.UserRole), int)
    before = len(window.gauge_ids)
    window._toggle_gauge(leaf, 0)
    assert len(window.gauge_ids) == before + 1


def test_offline_map_opens_edits_and_saves_without_any_ecu(app, database, tmp_path):
    from openpclink.mapfile import MapFile
    from tests.conftest import PROGRAM

    window = MainWindow(database)
    window.show_error = lambda message: pytest.fail(message)
    window.load_map(PROGRAM / "Sample Maps" / "Xtreme default map.pclx")
    assert pump(app, lambda: window.status_label.text() == "MAP FILE")
    assert not window.store_action.isEnabled()
    window.write(2365, 66.0, 0)
    assert pump(app, lambda: "Unsaved" in window.unstored_label.text())
    target = tmp_path / "edited.pclx"
    window.save_map_to(target)
    assert MapFile.load(target, database.map_key).entries[2365].values[0] == 66.0
    dispose(app, window)


def test_pc_logging_records_while_connected(session, tmp_path):
    from openpclink.logfile import read_log

    app, window, _ = session
    window.start_logging(tmp_path / "log.csv")
    assert pump(app, lambda: window.worker.logger and window.worker.logger.samples >= 3)
    window.stop_logging()
    assert len(read_log(tmp_path / "log.csv").time) >= 3


def test_gauges_follow_live_values_and_never_write(session):
    app, window, sim = session
    window.workspace.show_page("Tuning")
    page = window.workspace.current()
    gauges = [view for view in page.bodies(LiveView) if isinstance(view.body, Gauge)]
    assert gauges
    assert pump(app, lambda: any(view.body.readings for view in gauges))
    for view in gauges:
        view.body.grab()
    assert sim.writes == []


def test_gauge_position_pins_at_the_ends_of_its_range():
    assert fraction_of(4000, 0, 8000) == 0.5
    assert fraction_of(65535, 0, 8000) == 1.0
    assert fraction_of(-5, 0, 8000) == 0.0

def _captions(window):
    return [window.database.parameter(i).caption for i in window.form.editors]


def test_pclinks_rules_decide_what_the_form_shows_like_the_real_program(session):
    """Real PCLink 7.8.2, same map: only the mode when OFF, then these three with Ignition %cut."""
    app, window, sim = session
    assert window.worker.rules is not None
    window.tree.setCurrentItem(find_item(window, "Engine Protection", "RPM Limit"))
    assert _captions(window) == ["RPM Limit Mode"]
    combo = window.form.editors[2754]
    combo.setCurrentIndex(1)
    combo.activated.emit(1)
    assert pump(app, lambda: _captions(window) == ["RPM Limit Mode", "Dual Tables", "Advanced Mode"])


def test_only_what_real_pclink_sends_is_sent_when_a_mode_changes(session):
    """Real PCLink, same map and change: one write, and T1 stays OFF, whatever the rules ask."""
    app, window, sim = session
    window.write(2754, 1, 0)
    assert pump(app, lambda: sim.writes)
    pump(app, lambda: False, timeout=0.5)
    assert sim.writes == [bytes.fromhex("37b3000120")]
    assert window.worker.ecu.get(3460) == 0


def test_a_rules_warning_reaches_the_user_when_a_change_triggers_it(session):
    app, window, sim = session
    rule = ((), (), (), ("Test warning from the rules",), ())
    window.worker.rules.table.rules[2754] = {1: ([rule], {})}
    window.write(2754, 1, 0)
    assert pump(app, lambda: window.errors)
    assert window.errors == ["Test warning from the rules"]


def test_rules_warnings_are_silent_while_connecting(session):
    _, window, _ = session
    assert window.errors == []


def test_status_window_shows_the_active_fault_and_sends_nothing(session):
    app, window, sim = session
    window.show_status()
    status = window.status_window
    assert status.fault.text() == "No fault codes."
    code = window.database.parameter(1162).options[3]
    at = sim.offsets[1162]
    location = window.worker.ecu.table[1162]
    sim.memory[at] = (sim.memory[at] & ~location.mask & 0xFF) | ((3 << location.shift) & location.mask)
    assert pump(app, lambda: code in status.fault.text())
    assert sim.writes == []
    status.close()


def test_log_views_draw_with_real_data_and_edge_cases(app):
    from openpclink.logfile import Log
    from openpclink.logviews import LOG_VIEW_TYPES, LogSession, VehiclePlan

    for log in (
        Log(["RPM", "MAP"], [0.0, 0.1, 0.2], [[800.0, 900.0, 1000.0], [40.0, 40.0, 40.0]]),
        Log(["RPM"], [0.0], [[800.0]]),
        Log(["RPM"], [], [[]]),
    ):
        session = LogSession()
        views = [kind(session) for kind in LOG_VIEW_TYPES.values()] + [VehiclePlan(session)]
        session.add(log)
        session.set_cursor(0.1)
        for view in views:
            view.resize(400, 300)
            view.grab()


def test_clear_faults_writes_the_flag_without_marking_the_tune_changed(session):
    app, window, sim = session
    window.worker.ecu._dirty = False
    sim.writes.clear()
    with mock.patch.object(QtWidgets.QMessageBox, "question", return_value=QtWidgets.QMessageBox.StandardButton.Yes):
        window.clear_faults()
    assert pump(app, lambda: sim.writes)
    location = window.worker.ecu.table[1171]
    offset, size = struct.unpack(">HH", sim.writes[0][:4])
    assert offset + sim.runtime_size == location.offset and size == 1
    assert sim.writes[0][4] & location.mask == location.mask
    assert window.worker.ecu.memory[location.offset] & location.mask == 0
    assert not window.worker.ecu.has_unstored_changes
    sim.writes.clear()


def test_clear_statistics_needs_a_yes(session):
    app, window, sim = session
    before = sim.statistics_cleared
    with mock.patch.object(QtWidgets.QMessageBox, "question", return_value=QtWidgets.QMessageBox.StandardButton.No):
        window.clear_statistics()
    pump(app, lambda: False, 0.3)
    assert sim.statistics_cleared == before
    with mock.patch.object(QtWidgets.QMessageBox, "question", return_value=QtWidgets.QMessageBox.StandardButton.Yes):
        window.clear_statistics()
    assert pump(app, lambda: sim.statistics_cleared == before + 1)


def _set_raw(sim, location, raw):
    sim.memory[location.offset : location.offset + location.width] = raw.to_bytes(location.width, "big")


def test_tps_setup_saves_the_voltage_rounded_as_pclink_writes_it(session):
    from openpclink.ecu import ETHROTTLE_MODE_ID, TPS_OPEN_ID, TPS_RAW_ID

    app, window, sim = session
    ecu = window.worker.ecu
    mode = ecu.table.get(ETHROTTLE_MODE_ID)
    sim.engine_running = False
    if mode is not None:
        sim.memory[mode.offset] &= ~mode.mask & 0xFF
        window.worker.post("control", lambda e: e.reload(), "")
    _set_raw(sim, ecu.table[TPS_RAW_ID], 1444)  # PCLink wrote 0x05A0 when this read 1.444 V.
    sim.writes.clear()
    with mock.patch.object(QtWidgets.QMessageBox, "information", return_value=QtWidgets.QMessageBox.StandardButton.Ok):
        window.tps_setup()
    assert pump(app, lambda: len(sim.writes) == 2), window.errors
    location = ecu.table[TPS_OPEN_ID]
    assert sim.writes[0] == struct.pack(">HHH", location.offset - sim.runtime_size, 2, 1440)
    sim.writes.clear()


def test_tps_setup_and_map_calibration_refuse_while_the_engine_runs(session):
    app, window, sim = session
    sim.engine_running = True
    before = sim.map_calibrations
    sim.writes.clear()
    failures = []
    window.worker.failed.connect(failures.append)
    with (
        mock.patch.object(QtWidgets.QMessageBox, "information", return_value=QtWidgets.QMessageBox.StandardButton.Ok),
        mock.patch.object(QtWidgets.QMessageBox, "question", return_value=QtWidgets.QMessageBox.StandardButton.Yes),
        mock.patch.object(QtWidgets.QMessageBox, "warning"),
    ):
        window.tps_setup()
        window.calibrate_map()
        assert pump(app, lambda: len(failures) == 3), failures
    assert sim.map_calibrations == before and sim.writes == []
    window.worker.failed.disconnect(failures.append)
    sim.engine_running = False
    with mock.patch.object(QtWidgets.QMessageBox, "question", return_value=QtWidgets.QMessageBox.StandardButton.Yes):
        window.calibrate_map()
    assert pump(app, lambda: sim.map_calibrations == before + 1)
