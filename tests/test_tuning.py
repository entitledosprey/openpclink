"""Quick Tune, Quick Trim, Mixture Map and Knock Audio against the simulator, headless."""
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_STYLE_OVERRIDE", "Fusion")

from PySide6 import QtWidgets  # noqa: E402

from openpclink import tune, tuningtools  # noqa: E402
from openpclink.options import Options  # noqa: E402
from openpclink.sim import PtyServer, SimEcu  # noqa: E402
from tests.test_gui import app, database, dispose, pump  # noqa: E402,F401
from tests.conftest import ROOT  # noqa: E402

XTREME = ROOT / "work" / "decoded" / "xtreme.xml"


@pytest.fixture
def online(app, database, tmp_path):  # noqa: F811
    if not XTREME.exists():
        pytest.skip("needs the decoded Xtreme map")
    from openpclink.gui import MainWindow

    sim = SimEcu(XTREME.read_bytes(), hardware_family=1000)
    sim.engine_running = False
    server = PtyServer(sim)
    window = MainWindow(database, options=Options())
    window.options_path = tmp_path / "options.json"
    window.show()
    window.worker.failed.disconnect()
    window.worker.failed.connect(lambda message: None)
    window.connect_to(f"serial:{server.path}")
    assert pump(app, lambda: window.status_label.text() == "ONLINE")
    yield app, window, sim, tmp_path
    if window.worker.ecu:
        window.worker.ecu._dirty = False
    dispose(app, window)
    server.close()


def hold(sim, **values):
    for name, value in values.items():
        sim.set_value(int(name[1:]), value)


def test_neighbour_bounds_and_centre_test():
    axis = [0, 500, 1000, 2000]
    assert tune.neighbour_bounds(axis, 0) == (0, 500, 0)
    assert tune.neighbour_bounds(axis, 2) == (500, 2000, 1000)
    assert tune.neighbour_bounds(axis, 3) == (1000, 2000, 2000)
    cell = tune.neighbour_bounds(axis, 2)
    assert tune.within_centre(1000, cell, 20) and tune.within_centre(1300, cell, 20)
    assert not tune.within_centre(1301, cell, 20) and not tune.within_centre(700, cell, 20)


def test_settings_round_trip_through_options_names():
    s = tune.QuickTuneSettings(filter=4, precision=0.05, tune_ahead=3)
    saved = s.to_options()
    assert saved["Filter"] == 4 and saved["Precision"] == 0.05 and saved["LoadCenterTollerace"] == 20.0
    other = tune.QuickTuneSettings()
    other.load_options({**saved, "Filter": "7", "Bogus": 1})
    assert other.filter == 7 and other.tune_ahead == 3 and isinstance(other.filter, int)


def test_target_from_the_table_when_the_runtime_target_is_missing():
    table = ([0.0, 1000.0], [0.0, 100.0], [1.0, 1.0, 0.8, 0.8])
    assert tune.lookup_target(table, 500.0, 50.0) == pytest.approx(0.9)
    assert tune.lookup_target(table, 500.0, 50.0, overlay=0.05) == pytest.approx(0.95)
    assert tune.lookup_target(None, 0, 0) is None


def test_mixture_palette_is_eight_bands_green_to_red():
    palette = tune.mixture_palette()
    assert palette[0] == (0, 255, 0) and palette[4] == (255, 255, 0) and palette[7] == (255, 0, 0)
    assert tune.mixture_band(0.0, 0.27) == palette[0]
    assert tune.mixture_band(0.27, 0.27) == palette[7] and tune.mixture_band(5.0, 0.27) == palette[7]
    assert tune.mixture_band(0.14, 0.27) == palette[4]


def test_quick_tune_reaches_ready_and_writes_pclinks_value(online):
    app, window, sim, tmp_path = online
    window.quick_tune()
    qt = window.quick_tune_window
    qt.engine.settings.time_in_centre = 0.0
    qt.set_mode(tune.MANUAL)
    ecu = window.worker.ecu
    parameter = window.database.parameter(tune.FUEL_TABLE_1)
    rpm_axis = ecu.get_all(parameter.column_axis_id)
    load_axis = ecu.get_all(parameter.row_axis_id)
    hold(sim, d1000=rpm_axis[6], d1004=85, d1091=load_axis[8], d3040=1.0, d2936=0.9)
    assert pump(app, lambda: (qt.refresh() or True) and qt.last.text == "Ready", 5)
    assert qt.status.styleSheet() == "color:#008000"
    cell = qt.engine.cell
    old = ecu.get(tune.FUEL_TABLE_1, cell[0] * parameter.columns + cell[1])
    qt.tune_button.click()
    pump(app, lambda: False, 0.5)
    new = ecu.get(tune.FUEL_TABLE_1, cell[0] * parameter.columns + cell[1])
    assert new == pytest.approx(round((1.0 - 0.9 + 1.0) * old, 1), abs=0.11)


def test_quick_tune_settings_persist_in_options(online):
    app, window, sim, tmp_path = online
    window.quick_tune()
    qt = window.quick_tune_window
    qt.edits["filter"].setText("5")
    qt.edits["filter"].editingFinished.emit()
    qt.show_settings.setChecked(True)
    qt.close()
    saved = Options.load(tmp_path / "options.json")
    assert saved.quick_tune["Filter"] == 5 and saved.quick_tune["ShowSettings"] is True
    assert saved.quick_tune["TuneAheadTrim"] == 2.5


def test_quick_tune_afr_units_convert_boxes_and_back(online):
    app, window, sim, tmp_path = online
    window.options.imperial = dict(window.options.imperial, **{"lambda": "AFR"})
    window.options.system = "Imperial"
    window.quick_tune()
    qt = window.quick_tune_window
    assert qt.suffixes["gauge_minimum"].text() == "AFR"
    assert float(qt.edits["gauge_minimum"].text()) == pytest.approx(0.7 * 14.7, abs=0.06)
    qt.edits["gauge_maximum"].setText("16.0")
    qt.edits["gauge_maximum"].editingFinished.emit()
    assert qt.engine.settings.gauge_maximum == pytest.approx(16.0 / 14.7)


def test_quick_trim_dialog_returns_lambda(app):  # noqa: F811
    from openpclink.units import UnitSystem

    dialog = tuningtools.QuickTrimDialog(0.9, 1.0, 0.0, units=UnitSystem({"lambda": "AFR"}, 14.7))
    assert dialog.windowTitle() == "Quick Trim"
    assert dialog.target.value() == pytest.approx(13.23, abs=0.001)
    assert dialog.target_value() == pytest.approx(0.9, abs=0.001)
    assert dialog.measured_value() == pytest.approx(1.0, abs=0.001)


def test_knock_audio_opens_without_multimedia(app):  # noqa: F811
    import sys
    from unittest import mock

    with mock.patch.dict(sys.modules, {"PySide6.QtMultimedia": None}):
        window = tuningtools.KnockAudioWindow()
        window.show()
        assert window.inputs.itemText(0) == "No input devices"
        window.add_block([0] * window.BLOCK)
        window.close()


def mix_log():
    from openpclink.logfile import Log, Series

    log = Log()
    series = [Series(1000, "Engine Speed", period=0.1), Series(1091, "MGP", period=0.1), Series(3040, "Lambda 1", period=0.1), Series(2936, "Lambda Target", period=0.1)]
    k = 0
    for rpm, load, mixture, target in ((2500, 30, 1.0, 0.9), (3000, 60, 0.95, 0.9), (2000, 0, 0.7, 0.9), (2500, 30, 1.0, 0.9)):
        for _ in range(40):
            for s, v in zip(series, (rpm, load, mixture, target)):
                s.append(k * 0.1, v)
            k += 1
    log.series = series
    log.rebuild()
    return log


def test_mixture_map_numbers_match_real_pclink():
    """Counts and averages PCLink 7.8.2 showed for this log: it reads the mixture a step ahead and stops a step short."""
    xs = [0.0, 500.0, 750.0, 1000.0, 1500.0, 2000.0, 2500.0, 3000.0]
    ys = [-85.0, -80.0, -60.0, -55.0, -50.0, -40.0, -20.0, 0.0, 30.0, 60.0]
    settings = tune.MixtureSettings(rpm_id=1000, load_id=1091, load2_id=1091, mixture_id=3040)
    grid = tune.build_mixture_map(mix_log(), settings, xs, ys, None)
    found = {(ys[r], xs[c]): cell for r, row in enumerate(grid) for c, cell in enumerate(row) if cell.has_data}
    assert set(found) == {(0.0, 2000.0), (30.0, 2500.0), (60.0, 3000.0)}
    assert found[(0.0, 2000.0)].samples == 40 and f"{found[(0.0, 2000.0)].actual_average:.2f}" == "0.71" and f"{found[(0.0, 2000.0)].error:.2f}" == "0.19"
    assert found[(30.0, 2500.0)].samples == 78 and f"{found[(30.0, 2500.0)].error:.2f}" == "0.10"


def test_quick_trim_matches_the_value_real_pclink_wrote():
    # Real PCLink: a cell of 62.0 with target 0.9 and measured 1.0 was written as 68.2.
    assert round(tune.quick_trim_value(62.0, 1.0, 0.9, 0.0), 1) == 68.2


def test_mixture_view_orders_rows_low_load_first_and_says_why_it_cannot_draw(app):  # noqa: F811
    from openpclink.logviews import LogSession, MixtureMapView

    session = LogSession()
    view = MixtureMapView(session, host=None)
    assert view.message == "Right click to update map."

    class Host:
        def build_mixture(self, settings, log, progress=None):
            return [], "No log file found."

    view.host = Host()
    view.rebuild()
    assert view.message == "Unable to draw mixture map.\nLog File error: No log file found.\nRight click to update map."
