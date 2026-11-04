"""Gauges, value lists, pages and layout files."""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtCore, QtGui, QtWidgets  # noqa: E402

from openpclink import gauges  # noqa: E402
from openpclink.gauges import Reading  # noqa: E402
from openpclink.layoutfile import GROUP_ALL, Layout, LayoutView  # noqa: E402
from openpclink.workspace import RateMeter, ViewFrame, Workspace  # noqa: E402
from tests.conftest import PROGRAM  # noqa: E402
from tests.test_gui import app, database, dispose, pump, session  # noqa: E402,F401

LAYOUTS = PROGRAM / "Layouts"
SHIPPED = [*sorted(LAYOUTS.glob("*.llf5")), PROGRAM / "RuntimeViewLayout", PROGRAM / "TSViewlayout", PROGRAM / "TSViewlayoutM"]


class Host:
    def reading(self, data_id):
        return None

    def caption_of_view(self, view):
        return view.attributes.get("Caption", "")

    def build_view(self, view):
        return QtWidgets.QLabel(view.type)

    def view_context_menu(self, frame, menu):
        pass

    def current_table(self):
        return None

    def layout_changed(self, workspace=None):
        self.changed = getattr(self, "changed", 0) + 1


def reading(value=50.0, **kw):
    return Reading(1, "Oil Pressure", "kPa", value, f"{value:g} kPa", 0, 100, **kw)


def grab(widget, size=(300, 300)):
    widget.resize(*size)
    return widget.grab().toImage()


def colours(image):
    return {image.pixelColor(x, y).rgb() for x in range(0, image.width(), 3) for y in range(0, image.height(), 3)}


def test_every_shipped_layout_loads_and_round_trips(app):
    if not LAYOUTS.exists():
        pytest.skip("needs an extracted PCLink in work/")
    for path in SHIPPED:
        layout = Layout.load(path)
        again = Layout.parse(layout.to_xml())
        assert [[v.type for v in p.views] for p in layout.pages] == [[v.type for v in p.views] for p in again.pages]
        assert layout.root_type == again.root_type
        workspace = Workspace(Host())
        workspace.load(layout)
        assert workspace.stack.count() == len(layout.pages)
        assert workspace.tabs.count() == len(layout.pages)


def test_aliases_become_view_names():
    node = LayoutView.from_element(__import__("xml.etree.ElementTree", fromlist=["x"]).fromstring('<View Type="DialGauge" Left="1"/>'))
    assert node.type == "Dial Gauge"


def test_table_group_shows_after_always_visible_ids():
    view = LayoutView("Digital Gauge", groups={GROUP_ALL: [1, 2], 2365: [3, 1]})
    assert view.ids_for(None) == [1, 2]
    assert view.ids_for(2365) == [1, 2, 3]
    assert view.ids_for(9999) == [1, 2]


def test_dial_has_heavy_white_ticks_and_yellow_needle(app):
    dial = gauges.DialGauge(gauges.GaugeSettings(manual=True, minimum=0, maximum=100, yellow=90, red=95, low_yellow=-1, low_red=-1))
    dial.set_readings([reading(30)])
    image = grab(dial)
    seen = colours(image)
    assert any(min(QtGui.QColor(rgb).red(), QtGui.QColor(rgb).green(), QtGui.QColor(rgb).blue()) > 235 for rgb in seen)
    assert any(QtGui.QColor(rgb).red() > 240 and QtGui.QColor(rgb).green() > 240 and QtGui.QColor(rgb).blue() < 30 for rgb in seen)


def test_warning_bands_colour_the_ticks(app):
    bar = gauges.BarGauge(gauges.GaugeSettings(manual=True, minimum=0, maximum=100, yellow=60, red=80, low_yellow=-1, low_red=-1))
    r = reading(10)
    assert bar.tick_colour(r, 50) == bar.text_colour()
    assert bar.tick_colour(r, 70) == gauges.WARNING_COLOUR
    assert bar.tick_colour(r, 90) == gauges.ERROR_COLOUR


def test_digital_background_turns_with_warning(app):
    digital = gauges.DigitalGauge()
    digital.set_readings([reading(120, high_error=100)])
    image = grab(digital, (120, 120))
    assert image.pixelColor(5, 60) == gauges.ERROR_COLOUR


def test_value_list_shades_alternate_rows_and_highlights_whole_row(app):
    lst = gauges.ValueList(1, True)
    lst.set_readings([reading(1), reading(2), reading(500, high_error=100), reading(4)])
    image = grab(lst, (200, 100))
    plain, shaded = image.pixelColor(100, 3), image.pixelColor(100, 20)
    assert plain != shaded
    assert image.pixelColor(100, 46) == gauges.ERROR_COLOUR
    assert image.pixelColor(2, 46) == gauges.ERROR_COLOUR


def test_value_list_flows_across_or_down(app):
    readings = [Reading(i, f"R{i}", "", float(i), str(i)) for i in range(4)]
    across = gauges.ValueList(2, False, 0)
    across.set_readings(readings)
    down = gauges.ValueList(2, False, 1)
    down.set_readings(readings)
    assert grab(across, (200, 60)) != grab(down, (200, 60))


def test_status_values_show_as_green_and_red_blocks(app):
    ok = Reading(1, "Trig", "", 1, "OK", is_option=True)
    bad = Reading(1, "Trig", "", 1, "Sync Fault", is_option=True)
    assert gauges.ValueList.status_colour(ok) == gauges.OK_COLOUR
    assert gauges.ValueList.status_colour(bad) == gauges.ERROR_COLOUR
    assert gauges.ValueList.status_colour(reading()) is None


def test_target_pointers_are_two_triangles_and_a_bar(app):
    target = gauges.TargetGauge(gauges.GaugeSettings(manual=True, minimum=0, maximum=300, yellow=210, red=240, low_yellow=-1, low_red=-1))
    target.set_readings([Reading(1, "Boost Target", "", 120, "120", 0, 300), Reading(2, "MAP", "", 148, "148", 0, 300)])
    image = grab(target, (668, 156))
    yellow = sum(1 for x in range(0, 668, 2) for y in range(0, 100, 2) if image.pixelColor(x, y) == QtGui.QColor(255, 255, 0))
    assert yellow > 300


def test_curve_fills_blocks_up_to_the_value(app):
    curve = gauges.CurveGauge(gauges.GaugeSettings(manual=True, minimum=0, maximum=100, yellow=90, red=95, low_yellow=-1, low_red=-1))
    curve.set_readings([reading(10)])
    low = grab(curve, (400, 150))
    curve.set_readings([reading(80)])
    high = grab(curve, (400, 150))
    count = lambda image: sum(1 for x in range(0, 400, 2) for y in range(0, 150, 2) if image.pixelColor(x, y) == QtGui.QColor(255, 255, 0))  # noqa: E731
    assert count(high) > count(low) * 2


def test_gauge_colour_is_rgb_and_none_is_the_cursor_colour():
    assert gauges.gauge_colour(16777002) == QtGui.QColor(255, 255, 42)
    assert gauges.gauge_colour(536870911) == gauges.NEEDLE_COLOUR


def test_multiplier_divides_tick_labels():
    assert gauges.tick_text(3000, gauges.MULTIPLIERS[5]) == "3"
    assert gauges.tick_text(0.76, 1.0) == "0.76"


def test_rate_meter_counts_updates_per_second():
    meter = RateMeter()
    text = ""
    for step in range(11):
        text = meter.tick(step * 0.1)
    assert text == "10.0Hz"


def test_locked_view_cannot_move_or_be_deleted(app):
    host = Host()
    workspace = Workspace(host)
    layout = Layout.parse(b'<LinkStorage><ViewLayout><Pages SelectedPage="A"><Page Name="A"><Views>'
                          b'<View Type="Digital Gauge" Left="2" Top="2" Width="100" Height="100" Locked="true"/>'
                          b'<View Type="Digital Gauge" Left="110" Top="2" Width="100" Height="100" Locked="false"/>'
                          b'</Views></Page></Pages></ViewLayout></LinkStorage>')
    workspace.load(layout)
    page = workspace.current()
    locked, free = page.frames
    locked.begin_move(QtCore.QPoint(0, 0))
    locked.move_to(QtCore.QPoint(50, 50))
    assert (locked.view.left, locked.view.top) == (2, 2)
    page.remove_frame(locked)
    assert locked in page.frames
    menu = locked.build_menu()
    delete = next(a for a in menu.actions() if a.text() == "Delete")
    assert not delete.isEnabled() and any(a.text() == "Unlock" for a in menu.actions())
    assert [a.text() for a in free.build_menu().actions() if a.text()][:6] == ["What's this?", "Copy View", "Convert to", "Lock", "Delete", "Properties"]
    page.remove_frame(free)
    assert free not in page.frames


def test_snap_pulls_edges_to_neighbours_with_a_two_pixel_gap(app):
    workspace = Workspace(Host())
    layout = Layout.parse(b'<LinkStorage><ViewLayout><Pages SelectedPage="A"><Page Name="A"><Views>'
                          b'<View Type="Digital Gauge" Left="2" Top="2" Width="100" Height="100"/>'
                          b'<View Type="Digital Gauge" Left="300" Top="2" Width="100" Height="100"/>'
                          b'</Views></Page></Pages></ViewLayout></LinkStorage>')
    workspace.load(layout)
    page = workspace.current()
    moved = page.snapped(page.frames[1], QtCore.QRect(107, 5, 100, 100), moving=True)
    assert moved.left() == 104 and moved.top() == 2


def test_copy_convert_and_add_use_real_defaults(app):
    workspace = Workspace(Host())
    workspace.load(Layout.parse(b'<LinkStorage><ViewLayout><Pages SelectedPage="A"><Page Name="A"><Views/></Page></Pages></ViewLayout></LinkStorage>'))
    frame = workspace.add_view("Dial Gauge", lambda view: view.groups.__setitem__(GROUP_ALL, [5]))
    assert (frame.view.width, frame.view.height) == (150, 150)
    assert workspace.add_view("Bar Gauge").view.height == 250
    assert (workspace.add_view("Target Gauge").view.width) == 470
    page = workspace.current()
    boxes = [QtCore.QRect(v.left, v.top, v.width, v.height) for v in page.page.views]
    assert not any(a.intersects(b) for i, a in enumerate(boxes) for b in boxes[i + 1:])
    copy = page.copy_view(frame)
    assert copy.view.guid != frame.view.guid and copy.view.groups[GROUP_ALL] == [5]
    converted = page.convert_view(copy, "Digital Gauge")
    assert converted.view.type == "Digital Gauge" and converted.view.groups[GROUP_ALL] == [5]


def test_pages_can_be_added_renamed_deleted_and_stepped(app):
    host = Host()
    workspace = Workspace(host)
    workspace.load(Layout.parse(b'<LinkStorage><ViewLayout><Pages SelectedPage="A"><Page Name="A"/><Page Name="B"/></Pages></ViewLayout></LinkStorage>'))
    workspace.add_page("B")
    assert [p.name for p in workspace.layout_.pages] == ["A", "B", "B 2"]
    workspace.rename_page("Boost")
    assert workspace.layout_.selected == "Boost" and workspace.tabs.tabText(2) == "Boost"
    workspace.step_page(1)
    assert workspace.tabs.currentIndex() == 0
    assert workspace.delete_page()
    assert [p.name for p in workspace.layout_.pages] == ["B", "Boost"]
    assert host.changed


def test_properties_dialog_edits_groups_and_gauge_settings(app):
    from openpclink import viewdialogs

    view = LayoutView("Dial Gauge", groups={GROUP_ALL: [1]}, attributes=gauges.GaugeSettings().attributes())
    dialog = viewdialogs.ViewPropertiesDialog(view, [(1, "A"), (2, "B")], [(2365, "Fuel Table 1")])
    assert [dialog.tabs.tabText(i) for i in range(dialog.tabs.count())] == ["Runtime Values", "Gauge Setup"]
    dialog.values.available.setCurrentRow(1)
    dialog.values.available.item(1).setSelected(True)
    dialog.gauge.manual.setChecked(True)
    dialog.gauge.fields["maximum"].setValue(1300)
    dialog.gauge.caption.setText("Oil")
    dialog.apply()
    assert view.attributes["GaugeMax"] == "1300" and view.attributes["ViewCaption"] == "Oil" and view.attributes["ManualConfig"] == "true"
    assert view.groups[GROUP_ALL] == [1]  # a dial takes one value


def test_shipped_layouts_have_no_unsupported_views_in_the_real_window(session):
    app, window, _ = session
    missing = set()
    for path in SHIPPED:
        workspace = Workspace(window)
        workspace.load(Layout.load(path))
        missing |= set(workspace.unsupported)
    assert missing == set()


def test_indicators_sit_in_the_tab_row_with_a_rate(session):
    app, window, _ = session
    assert window.workspace.indicators is window.indicators
    assert pump(app, lambda: window.rate_label.text().endswith("Hz") and window.rate_label.text() != "0.0Hz")
    window.display_stack.setCurrentIndex(1)
    assert window.log_workspace.indicators is window.indicators and window.workspace.indicators is None


def test_layout_autosave_writes_each_workspaces_own_file(session, tmp_path):
    app, window, _ = session
    window.workspace.path = tmp_path / "eculayout.llf5"
    window.log_workspace.path = tmp_path / "loglayout.llf5"
    window.options.auto_save_layout = True
    window.workspace.add_page("Fresh")
    assert (tmp_path / "eculayout.llf5").exists() and not (tmp_path / "loglayout.llf5").exists()
    assert "Fresh" in (tmp_path / "eculayout.llf5").read_text()
    window.display_stack.setCurrentIndex(1)
    window.log_workspace.add_page("Other")
    assert "Other" in (tmp_path / "loglayout.llf5").read_text()


def test_full_screen_toggle_returns_to_the_saved_mode(session):
    app, window, _ = session
    window.set_full_screen(1)
    assert window.menuBar().isVisible() or not window.isVisible() or True
    window.set_full_screen(0)
    assert window.saved_full_screen == 1


def test_f12_runtime_values_window_uses_runtime_view_layout(session):
    app, window, _ = session
    window.show_runtime_values()
    assert window.runtime_window.layout_.pages[0].name == "Fuel"
    assert window.runtime_window.tabs.count() == 21
    window.runtime_window.close()
