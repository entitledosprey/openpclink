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
    window.worker.ecu._dirty = False
    window.close()
    server.close()


def find_item(window, *path):
    items = [window.tree.topLevelItem(i) for i in range(window.tree.topLevelItemCount())]
    item = None
    for name in path:
        item = next(candidate for candidate in items if candidate.text(0) == name)
        items = [item.child(i) for i in range(item.childCount())]
    return item


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
    grid = window.form.editors[2365]
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
    window.close()
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
    window.close()
    server.close()
