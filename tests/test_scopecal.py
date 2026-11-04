"""Trigger scope, calibrations and settings-tree action buttons, against the simulator."""
import os
from unittest import mock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtWidgets  # noqa: E402

from openpclink import scope, tools  # noqa: E402
from openpclink.ecu import Ecu, TrapMode  # noqa: E402
from openpclink.gui import ID_ROLE  # noqa: E402
from openpclink.layout import parse_descriptor  # noqa: E402
from openpclink.sim import PtyServer, SimEcu  # noqa: E402
from openpclink.transports import SerialTransport  # noqa: E402
from tests.test_gui import _kept_windows, app, database, dispose, find_item, pump, session  # noqa: E402,F401


def tree_ids(window):
    found = {}
    stack = [window.tree.topLevelItem(i) for i in range(window.tree.topLevelItemCount())]
    while stack:
        item = stack.pop()
        for data_id in item.data(0, ID_ROLE) or []:
            found[data_id] = item
        stack.extend(item.child(i) for i in range(item.childCount()))
    return found


def reload(app_, window):
    done = []
    window.call(lambda ecu: ecu.reload(), lambda _r: done.append(1))
    assert pump(app_, lambda: done)


def set_param(sim, data_id, value):
    location = parse_descriptor(sim.descriptor, sim.max_id)[data_id]
    at = sim.offsets[data_id]
    if location.mask:
        sim.memory[at] = (sim.memory[at] & ~location.mask & 0xFF) | ((value << location.shift) & location.mask)
    else:
        sim.memory[at] = value


@pytest.fixture
def stopped(session):
    app_, window, sim = session
    sim.engine_running = False
    assert pump(app_, lambda: window.worker.ecu.get(1000) == 0)
    return app_, window, sim


def test_action_parameters_appear_in_settings_tree(session):
    _, window, _ = session
    found = tree_ids(window)
    for data_id in (3550, 2035, 1679, 1647):
        assert data_id in found, data_id
    assert found[3550].parent().text(0) == "Triggers"
    assert found[2035].parent().text(0) == "Analog Inputs"


def test_scope_capture_matches_real_pclink_series(ecu, sim):
    """Values, names and sparse on/off series as real PCLink saved them for the same simulator replies."""
    log = scope.capture(ecu, sleep=lambda _t: None)
    series = {s.data_id: s for s in log.series}
    assert series[1334].name == "An Volt 1" and series[3430].name == "Ign 1" and series[3422].name == "Inj 1"
    assert series[1334].units == "Volts"
    assert [len(series[i].values) for i in (2541, 2542, 2543, 2544, 3430, 3422)] == [3500, 1752, 878, 440, 220, 2]
    assert series[1334].values[:3] == [0.205078125, 0.21484375, 0.224609375]
    assert series[1385].values[:3] == [35.0, 36.0, 37.0]
    assert series[2541].values[:5] == [1.0, 1.0, 0.0, 0.0, 1.0]
    assert [round(t / series[2541].period) for t in series[2541].times[:6]] == [0, 3, 4, 7, 8, 11]
    assert sim.scope_stopped == 1
    assert scope.sample_rate(0) == 3000 and scope.sample_rate(5000) == 60000 and scope.sample_rate(2000) == 60000


def test_scope_window_pauses_and_restores_logging_mode(stopped):
    app_, window, sim = stopped
    set_param(sim, 3544, 1)
    reload(app_, window)
    scope_window = tools.ScopeWindow(window)
    ecu = window.worker.ecu
    assert pump(app_, lambda: ecu.get(3544) == 0)
    scope_window.close()
    _kept_windows.append(scope_window)
    assert pump(app_, lambda: ecu.get(3544) == 1)
    assert scope_window.save.isEnabled() is False


def test_scope_layout_variant(stopped):
    app_, window, _ = stopped
    ecu = window.worker.ecu
    assert scope.layout_name(ecu) == "TSViewlayout"
    scope_window = tools.ScopeWindow(window)
    assert [scope_window.tabs.tabText(i) for i in range(3)] == ["Triggers", "DI", "Trigger 1 State"]
    pump(app_, lambda: False, 0.3)
    scope_window.close()
    _kept_windows.append(scope_window)


def test_throttle_calibration_follows_ecu_and_restores(stopped):
    app_, window, sim = stopped
    ecu = window.worker.ecu
    set_param(sim, 1663, 6)
    reload(app_, window)
    dialog = tools.ThrottleCalibration(window, 0)
    assert pump(app_, lambda: ecu.get(1670) == 1)
    assert pump(app_, lambda: dialog.label.text() == "TPS Calibrated.")
    assert len(dialog.bars) == 2
    dialog.button.click()
    assert pump(app_, lambda: ecu.get(1670) == 0)
    assert ecu.get(1691) == 0 and ecu.get(1690) == 0 and ecu.get(918) == 0


def test_throttle_calibration_requires_setup_mode(stopped):
    app_, window, sim = stopped
    set_param(sim, 1692, 1)
    reload(app_, window)
    with mock.patch.object(tools.ThrottleCalibration, "__init__", side_effect=AssertionError):
        window.calibrate_throttle(0)
    assert window.errors[-1] == "Ethrottle TPS can only be Calibrated in Setup Mode."


def test_pedal_calibration_steps(stopped):
    app_, window, sim = stopped
    ecu = window.worker.ecu
    dialog = tools.PedalCalibration(window)
    assert pump(app_, lambda: ecu.get(1691) == 1)
    assert dialog.label.text() == "Press pedal down" and dialog.button.text() == "Next"
    dialog.button.click()
    assert pump(app_, lambda: ecu.get(1690) == 1 and ecu.get(1691) == 0)
    assert dialog.label.text() == "Release pedal"
    dialog.button.click()
    assert pump(app_, lambda: ecu.get(1690) == 0)
    assert dialog.label.text() == "APS cal complete" and dialog.button.text() == "Done"
    dialog.button.click()
    assert not dialog.isVisible()


def test_source_millivolts_uses_analog_volts(stopped):
    app_, window, sim = stopped
    ecu = window.worker.ecu
    set_param(sim, 734, 2)
    reload(app_, window)
    assert tools.source_millivolts(ecu, 734) == round(ecu.get(1334) * 1000)


def test_base_timing_modes_and_cancel(stopped):
    app_, window, sim = stopped
    ecu = window.worker.ecu
    set_param(sim, 3501, 0)
    reload(app_, window)
    dialog = tools.BaseTimingDialog(window)
    assert 3540 in dialog.spins and 3526 not in dialog.spins
    before = ecu.get(3540)
    dialog.spins[3540].setText(f"{before + 3:.0f}")
    dialog.spins[3540].returnPressed.emit()
    assert pump(app_, lambda: ecu.get(3541) == 1)
    assert pump(app_, lambda: ecu.get(3540) == before + 3)
    dialog.reject()
    assert pump(app_, lambda: ecu.get(3541) == 0 and ecu.get(3540) == before)
    _kept_windows.append(dialog)


def test_base_timing_other_modes_use_reference_and_offset(stopped):
    app_, window, sim = stopped
    ecu = window.worker.ecu
    set_param(sim, 3501, 1)
    reload(app_, window)
    dialog = tools.BaseTimingDialog(window)
    assert set(dialog.spins) == {3538, 3526, 1466}
    assert pump(app_, lambda: ecu.get(3537) == 1)
    with mock.patch.object(QtWidgets.QMessageBox, "warning") as warning:
        dialog.spins[3538].setText("500")
        dialog.spins[3538].returnPressed.emit()
    assert "Please enter a value between" in warning.call_args.args[2]
    assert pump(app_, lambda: ecu.get(3538) == pytest.approx(30.0, abs=0.01))
    dialog.reject()
    assert pump(app_, lambda: ecu.get(3537) == 0 and ecu.get(3538) == pytest.approx(10.0, abs=0.01))
    _kept_windows.append(dialog)


def test_trap_mode_connect_offers_dump(database, app, program_dir):
    sim = SimEcu(None)
    sim.state = 4
    server = PtyServer(sim)
    session_ = Ecu(SerialTransport(server.path))
    with pytest.raises(TrapMode):
        session_.connect()
    report = session_.trap_report()
    assert report.endswith("tech@linkecu.com\r\n\r\n\r\n\r\ntrap")
    session_.close()
    server.close()


def test_tps_setup_menu_follows_the_throttle_dispatcher(stopped):
    app_, window, sim = stopped
    set_param(sim, 1692, 1)
    reload(app_, window)
    window.tps_setup()
    assert window.errors[-1] == "Ethrottle TPS can only be Calibrated in Setup Mode."
    set_param(sim, 1692, 0)
    reload(app_, window)
    with mock.patch.object(QtWidgets.QMessageBox, "information", side_effect=[QtWidgets.QMessageBox.StandardButton.Ok] * 2):
        window.tps_setup()
    assert pump(app_, lambda: len([w for w in sim.writes if len(w) == 6]) >= 2)


def test_map_and_baro_calibration_messages(stopped):
    app_, window, sim = stopped
    with mock.patch.object(QtWidgets.QMessageBox, "question", return_value=QtWidgets.QMessageBox.StandardButton.Yes) as asked, \
            mock.patch.object(QtWidgets.QMessageBox, "information") as told:
        window.calibrate_map()
        assert pump(app_, lambda: told.called)
    assert asked.call_args.args[2] == "Do you want to perform a MAP Sensor Calibration"
    assert told.call_args.args[2] == "MAP Sensor Calibration command completed successfully."
    assert sim.map_calibrations == 1
    with mock.patch.object(QtWidgets.QInputDialog, "getDouble", return_value=(101.0, True)) as asked, \
            mock.patch.object(QtWidgets.QMessageBox, "information") as told:
        window.calibrate_baro()
        assert pump(app_, lambda: told.called)
    assert asked.call_args.args[2] == "Equals" and asked.call_args.args[3] == 101.0
    assert told.call_args.args[2] == "Baro Calibration command completed successfully."
    assert sim.baro_calibrations == [1010]
