"""Log views: statistics formulas, settings round trips, units and keyboard handling."""
import math
import os
import xml.etree.ElementTree as ET
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6 import QtCore, QtGui, QtWidgets

from openpclink.layoutfile import Layout, LayoutPage, LayoutView
from openpclink.logfile import Log, Marker, Series
from openpclink.logviews import (
    Histogram, LoggedValues, LogSession, Navigator, ParameterList, StatisticsView, TimePlot, VehiclePlan, XYPlot, XYSettings,
    hash_number, hms, series_statistics, wheel_slip,
)

SAMPLE = Path(__file__).resolve().parent.parent / "work" / "wine" / "out_log" / "mine.llg5"


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def make_log() -> Log:
    log = Log()
    times = [i * 0.05 for i in range(400)]
    for data_id, name, unit, key, fn in (
        (1000, "Engine Speed", "RPM", "", lambda t: 2000 + 1500 * math.sin(t / 3)),
        (1001, "MAP", "kPa", "Pressure", lambda t: 100 + 50 * math.sin(t / 2)),
        (1078, "TPS (Main)", "%", "", lambda t: 50 + 40 * math.cos(t / 3)),
    ):
        log.series.append(Series(data_id, name, unit, key, 0.05, list(times), [fn(t) for t in times]))
    log.rebuild()
    log.normalise_sections()
    return log


def session_with(log: Log) -> LogSession:
    session = LogSession()
    session.add(log)
    return session


def test_statistics_match_real_pclink_on_the_sample_log():
    if not SAMPLE.exists():
        pytest.skip("sample log missing")
    from openpclink.logfile import read_log

    series = read_log(SAMPLE).series[0]
    figures = series_statistics(series.times, series.values)
    assert figures.samples == 399
    assert hash_number(figures.length) == "19.95"
    assert hash_number(figures.minimum) == "500.015"
    assert hash_number(figures.maximum) == "3499.988"
    assert hash_number(figures.maximum - figures.minimum) == "2999.973"
    assert hash_number(figures.average) == "2039.429"
    assert hash_number(figures.deviation) == "1048.066"


def test_statistics_edge_cases():
    assert series_statistics([0.0], [1.0]).samples == 0
    flat = series_statistics([0, 1, 2, 3], [5, 5, 5, 5])
    assert (flat.minimum, flat.maximum, flat.average, flat.deviation) == (5, 5, 5, 0)
    assert hash_number(2.0) == "2" and hash_number(-0.0001) == "0" and hash_number(1.23456) == "1.235"
    assert hms(9.001) == "0:09.001" and hms(70.5) == "1:10.500"


def test_wheel_slip_formula_from_vehicle_plan_code():
    assert wheel_slip(90, 100) == pytest.approx(10.0)
    assert wheel_slip(100, 100) == 0
    assert wheel_slip(50, 0) == -100.0
    assert wheel_slip(0, 0) == 0


def test_time_plot_settings_round_trip_into_layout(app):
    log = make_log()
    session = session_with(log)
    view = LayoutView("Time Plot", attributes={"ZoomLinked": "true", "CursorLinked": "true"}, log_parameters=[(1000, 0), (1001, 1), (1078, 2)])
    plot = TimePlot(session, view)
    plot.resize(800, 400)
    plot.set_option("ShowOverlays", False)
    plot.set_option("OffsetAxisVisible", False)
    plot.toggle_display_mode()
    plot.set_proportions([0.5, -1, -1])
    page = Layout([LayoutPage("p", [view])], "p")
    node = ET.fromstring(page.to_xml()).find(".//TimePlot")
    assert node.get("ShowOverlays") == "false" and node.get("OffsetAxisVisible") == "false"
    group = node.find("PlotSurfaceGroup")
    assert group.get("DisplayMode") == "1"
    assert [s.get("Proportion") for s in group.findall("PlotSurface")] == ["0.5", "-1", "-1"]
    again = TimePlot(session, Layout.parse(page.to_xml()).pages[0].views[0])
    assert again.display_mode() == 1 and not again.option("ShowOverlays") and again.proportions() == [0.5, -1, -1]


def test_time_plot_keys(app):
    session = session_with(make_log())
    plot = TimePlot(session, LayoutView("Time Plot", log_parameters=[(1000, 0), (1001, 1)]))
    plot.resize(800, 400)

    def press(key, mods=QtCore.Qt.KeyboardModifier.NoModifier):
        plot.keyPressEvent(QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress, key, mods))

    K = QtCore.Qt.Key
    press(K.Key_Right)
    assert session.cursor == pytest.approx(0.05, abs=1e-3)
    press(K.Key_Right, QtCore.Qt.KeyboardModifier.ControlModifier)
    assert session.cursor > 0.2
    width = session.end - session.start
    press(K.Key_Up)
    assert session.end - session.start < width
    press(K.Key_Z)
    assert session.end - session.start == pytest.approx(width)
    press(K.Key_G)
    assert plot.display_mode() == 1
    press(K.Key_L)
    assert not plot.option("ShowOverlays")
    press(K.Key_O)
    assert not plot.option("OffsetAxisVisible")
    press(K.Key_PageDown)
    assert plot.selected_group == 1
    before = plot.y_range(plot.current_key(), plot.current_members(plot.current_key()), *plot.window_indices())
    press(K.Key_Up, QtCore.Qt.KeyboardModifier.AltModifier)
    after = plot.y_range(plot.current_key(), plot.current_members(plot.current_key()), *plot.window_indices())
    assert after[1] - after[0] < before[1] - before[0]
    plot.grab()


def test_double_click_drag_zoom(app):
    session = session_with(make_log())
    plot = TimePlot(session, LayoutView("Time Plot", log_parameters=[(1000, 0)]))
    plot.resize(800, 400)
    plot.show()
    rect = plot.plot_rect()
    y = rect.center().y()
    plot.mouseDoubleClickEvent(QtGui.QMouseEvent(QtCore.QEvent.Type.MouseButtonDblClick, QtCore.QPointF(rect.left() + 100, y), QtCore.Qt.MouseButton.LeftButton, QtCore.Qt.MouseButton.LeftButton, QtCore.Qt.KeyboardModifier.NoModifier))
    plot.mouseMoveEvent(QtGui.QMouseEvent(QtCore.QEvent.Type.MouseMove, QtCore.QPointF(rect.left() + 300, y), QtCore.Qt.MouseButton.NoButton, QtCore.Qt.MouseButton.NoButton, QtCore.Qt.KeyboardModifier.NoModifier))
    plot.mousePressEvent(QtGui.QMouseEvent(QtCore.QEvent.Type.MouseButtonPress, QtCore.QPointF(rect.left() + 300, y), QtCore.Qt.MouseButton.LeftButton, QtCore.Qt.MouseButton.LeftButton, QtCore.Qt.KeyboardModifier.NoModifier))
    assert session.end - session.start < 19
    assert session.start > 0


def test_unlinked_view_keeps_its_own_window(app):
    session = session_with(make_log())
    plot = TimePlot(session, LayoutView("Time Plot", attributes={"ZoomLinked": "false", "CursorLinked": "false"}, log_parameters=[(1000, 0)]))
    session.set_range(2, 4)
    assert plot.time_window() == (session.log.time[0], session.log.time[-1])
    plot.set_time_window(5, 10)
    assert plot.view.attributes["DisplayTimeRangeStart"] != "0"
    plot.move_cursor(3.0)
    assert plot.cursor() == pytest.approx(3.0, abs=0.06) and session.cursor == 0.0


def test_units_apply_through_session_callbacks(app):
    log = make_log()
    session = session_with(log)
    session.display = lambda series, value: value * 0.1450377 if series.unit_key == "Pressure" else value
    session.unit_label = lambda series: "psi" if series.unit_key == "Pressure" else series.units
    session.span = lambda series, value: value * 0.1450377 if series.unit_key == "Pressure" else value
    view = LayoutView("Statistics", log_parameters=[(1000, 0), (1001, 0), (9999, 0)])
    stats = StatisticsView(session, view)
    rows = stats.rows()
    assert rows[1][1] == "psi" and float(rows[1][6]) < 30
    assert rows[2][3] == "NO DATA"
    parameters = ParameterList(session, LayoutView("Parameter List", log_parameters=[(1001, 0), (9999, 0)]))
    parameters.refresh()
    assert parameters.table.item(0, 0).text() == "Section Time" and parameters.table.item(1, 0).text() == "File Time"
    assert parameters.table.item(3, 2).text() == "psi"
    assert parameters.table.item(4, 1).text() == "NO DATA"


def test_every_view_paints_and_saves(app):
    log = make_log()
    log.markers.insert(1, Marker(1, "Key Press", 5.0))
    session = session_with(log)
    ids = [(1000, 0), (1001, 1), (1078, 2)]
    views = {
        "Time Plot": TimePlot, "Navigator": Navigator, "Value List": LoggedValues, "Parameter List": ParameterList, "Statistics": StatisticsView,
        "XY Plot": XYPlot, "Histogram": Histogram,
    }
    for name, kind in views.items():
        view = LayoutView(name, log_parameters=list(ids), attributes={"ZoomLinked": "true", "CursorLinked": "true"})
        body = kind(session, view)
        body.resize(700, 400)
        body.grab()
        session.set_cursor(3.0)
        body.grab()
        ET.tostring(view.to_element())
    plan = VehiclePlan(session)
    plan.resize(600, 500)
    plan.grab()


def test_xy_settings_round_trip(app):
    session = session_with(make_log())
    view = LayoutView("XY Plot", log_parameters=[(1001, 0), (1078, 1)])
    plot = XYPlot(session, view)
    plot.settings.x_id = 1000
    plot.settings.use_colour = True
    plot.settings.colour_id = 1078
    plot.settings.manual, plot.settings.manual_min, plot.settings.manual_max = True, 500, 3500
    plot.settings.y_manual, plot.settings.y_min, plot.settings.y_max = True, 10, 200
    plot.settings.point_style, plot.settings.point_size, plot.settings.show_lines, plot.settings.line_width = 2, 3, True, 2
    plot.settings.spans = ((0, 50, QtGui.QColor(0, 255, 0)), (50, 100, QtGui.QColor(255, 0, 0)))
    plot.saved()
    plot.keyPressEvent(QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_G, QtCore.Qt.KeyboardModifier.NoModifier))
    layout = Layout([LayoutPage("p", [view])], "p")
    again = XYSettings.from_node(ET.fromstring(layout.to_xml()).find(".//XYPlot"))
    assert again.x_id == 1000 and again.use_colour and again.colour_id == 1078
    assert (again.manual_min, again.manual_max, again.y_min, again.y_max) == (500, 3500, 10, 200)
    assert (again.point_style, again.point_size, again.show_lines, again.line_width) == (2, 3, True, 2)
    assert [(a, b, c.name()) for a, b, c in again.spans] == [(0, 50, "#00ff00"), (50, 100, "#ff0000")]
    assert ET.fromstring(layout.to_xml()).find(".//XYPlot/PlotSurfaceGroup").get("DisplayMode") == "1"
    plot.keyPressEvent(QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_H, QtCore.Qt.KeyboardModifier.NoModifier))
    assert not plot.settings.use_colour
    plot.resize(600, 400)
    plot.grab()


def test_overlay_selection_and_offsets(app):
    first, second = make_log(), make_log()
    session = LogSession()
    session.add(first)
    session.add(second)
    session.select(0)
    section = second.sections()[0]
    session.set_overlay(second, section, True, 1.5)
    assert session.overlay_selection() == [(second, section, 1.5)]
    plot = TimePlot(session, LayoutView("Time Plot", log_parameters=[(1000, 0)]))
    plot.resize(600, 300)
    plot.grab()
    session.set_overlay(second, section, False)
    assert not session.overlay_selection()
    session.set_overlay(second, section, True)
    session.close_log(second)
    assert not session.overlay_selection()


def test_properties_dialogs_apply_to_views_and_layout(app):
    from openpclink.logdialogs import ColourRangeDialog, ViewPropertiesDialog, build_spans, default_colours, interpolate, load_colour_range, save_colour_range

    session = session_with(make_log())
    for kind, name in ((TimePlot, "Time Plot"), (Navigator, "Navigator"), (XYPlot, "XY Plot"), (Histogram, "Histogram"), (StatisticsView, "Statistics"), (ParameterList, "Parameter List"), (LoggedValues, "Value List")):
        view = LayoutView(name, log_parameters=[(1000, 0), (1001, 1)])
        body = kind(session, view)
        dialog = ViewPropertiesDialog(body)
        dialog.caption.setText("Mine")
        dialog.zoom_linked.setChecked(False)
        dialog._ok()
        assert view.attributes["Caption"] == "Mine" and view.attributes["ZoomLinked"] == "false"
    view = LayoutView("Time Plot", log_parameters=[(1000, 0), (1001, 1)])
    plot = TimePlot(session, view)
    dialog = ViewPropertiesDialog(plot)
    dialog.rows.append([1078, 2])
    dialog.mode.setCurrentIndex(1)
    dialog.group_checks["YAxisVisible"].setChecked(False)
    dialog.checks["XScrollBarVisible"].setChecked(False)
    dialog._ok()
    assert view.log_parameters[-1] == (1078, 2)
    assert not plot.group_flag("YAxisVisible") and not plot.option("XScrollBarVisible") and plot.display_mode() == 1
    assert len(plot.proportions()) == 3
    colours = default_colours(8)
    assert colours[0].green() == 255 and colours[-1].red() == 255
    spans = build_spans(0, 80, colours)
    assert spans[0][:2] == (0, 10) and spans[-1][:2] == (70, 80)
    colours[3] = QtGui.QColor(0, 0, 255)
    blended = interpolate(colours, [0, 1, 2, 3])
    assert blended[1] != colours[1] and blended[0] == colours[0]
    ranged = ColourRangeDialog(session, spans)
    assert len(ranged.spans()) == 8
    ranged.parameter.setCurrentIndex(ranged.parameter.findData(1000))
    assert ranged.low.value() == pytest.approx(500, abs=1) and ranged.high.value() > 3000
    assert len(ranged.spans()) == 8
    path = Path(os.environ.get("TMPDIR", "/tmp")) / "lv_test_range.clr"
    save_colour_range(path, "Mine", spans)
    name, again = load_colour_range(path)
    assert name == "Mine" and len(again) == 8 and again[3][2].name() == spans[3][2].name()


def test_export_dialog_defaults_match_real_pclink(app):
    from openpclink.tools import ExportDialog

    dialog = ExportDialog()
    assert dialog.range.currentIndex() == 2
    assert not dialog.file_time.isChecked() and dialog.section_time.isChecked()
    assert not (dialog.device.isChecked() or dialog.file_info.isChecked() or dialog.driver.isChecked())
    assert dialog.format.itemText(0) == "Comma Separated Value (.CSV)"
    assert dialog.driver.text() == "Driver / Vehicle / Venue Information"


def test_export_matches_real_pclink_bytes(app, tmp_path):
    """The real program's CSV for the sample log, Full Log with every option on, is in work/wine/out_logviews."""
    from openpclink.logfile import ExportOptions, export_csv, read_log

    real = SAMPLE.parent.parent / "out_logviews" / "exp_full.csv"
    if not (SAMPLE.exists() and real.exists()):
        pytest.skip("real PCLink export missing")
    log = read_log(SAMPLE)
    log.path = Path(r"Z:\tmp\app\Logs\mine.llg5")
    out = tmp_path / "out.csv"
    export_csv(log, out, ExportOptions(file_time=True, section_time=True, device_information=True, file_information=True, driver_information=True))
    assert out.read_bytes() == real.read_bytes()


def test_log_details_dialog_read_only_fields(app):
    from openpclink.logfile import READ_ONLY_DETAILS
    from openpclink.tools import LogDetailsDialog

    log = make_log()
    log.details["Driver"] = "me"
    dialog = LogDetailsDialog(log)
    for name in READ_ONLY_DETAILS:
        assert dialog.edits[name].isReadOnly()
    assert not dialog.edits["Driver"].isReadOnly() and not dialog.edits["Notes"].isReadOnly()
    dialog.edits["Driver"].setText("you")
    dialog._ok()
    assert log.details["Driver"] == "you" and log.modified


def test_log_file_manager_sections_overlay_and_offset(app):
    from openpclink import tools

    class Host:
        def __init__(self):
            self.log_session = LogSession()

        def __getattr__(self, name):
            return lambda *a, **k: None

    host = Host()
    first, second = make_log(), make_log()
    second.details["Name"] = "Second"
    host.log_session.add(first)
    host.log_session.add(second)
    host.log_session.select(0)
    manager = tools.LogFileManager(host)
    manager.refresh()
    assert manager.tree.topLevelItemCount() == 2
    parent = manager.tree.topLevelItem(1)
    assert parent.text(0) == "Second" and parent.text(1) == "0:19.950"
    child = parent.child(0)
    assert child.text(1) == "0:19.950" and child.text(3) == "0:00.000"
    child.setCheckState(2, QtCore.Qt.CheckState.Checked)
    child.setText(3, "0:01.500")
    QtWidgets.QApplication.processEvents()
    assert host.log_session.overlay_selection()[0][2] == 1.5
    assert tools.parse_clock("1:02.5") == 62.5 and tools.parse_clock("x") is None
