"""Drives the real main window against the simulator, headless."""
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtCore, QtWidgets  # noqa: E402

from openpclink import crypto  # noqa: E402
from openpclink.database import Database  # noqa: E402
from openpclink.gui import MainWindow  # noqa: E402
from openpclink.importer import import_pclink  # noqa: E402
from openpclink.sim import PtyServer, SimEcu  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture(scope="module")
def database(tmp_path_factory):
    from tests.conftest import PROGRAM

    if not (PROGRAM / "ECU.cfgr").exists():
        pytest.skip("needs an extracted PCLink in work/")
    return Database.load(import_pclink(PROGRAM, tmp_path_factory.mktemp("db") / "db.json.gz"))


_kept_windows = []


def dispose(app, window):
    """Tear down in a fixed order; Python's garbage collector must not pick the moment."""
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
    window.tree.setCurrentItem(find_item(window, "Engine Protection", "RPM Limit"))
    spin = next(e for e in window.form.editors.values() if isinstance(e, QtWidgets.QDoubleSpinBox))
    spin.setFocus()
    app.processEvents()
    window.tree.setCurrentItem(find_item(window, "Configuration"))
    for _ in range(20):
        app.processEvents()
    assert sim.writes == []


def test_connects_and_builds_full_menu(session):
    _, window, _ = session
    names = {window.tree.topLevelItem(i).text(0) for i in range(window.tree.topLevelItemCount())}
    assert {"Fuel", "Ignition", "Engine Protection"} <= names


def test_live_values_update(session):
    app, window, _ = session
    assert pump(app, lambda: any(window.gauges.item(r, 1).text() for r in range(window.gauges.rowCount())))


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
    window.tree.setCurrentItem(find_item(window, "Engine Protection", "RPM Limit"))
    spins = [e for e in window.form.editors.values() if isinstance(e, QtWidgets.QDoubleSpinBox)]
    assert spins
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
    event = QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_Plus, QtCore.Qt.KeyboardModifier.NoModifier, "+")
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
