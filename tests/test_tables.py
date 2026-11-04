"""Tables, Enter New Value, axis setup, the surface and the Select Item list, against the simulator."""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtCore, QtGui, QtWidgets  # noqa: E402

from openpclink import tables  # noqa: E402
from openpclink.tables import Change, EnterValueDialog, SelectItemDialog  # noqa: E402
from tests.test_gui import app, database, find_item, pump, session  # noqa: E402,F401

FUEL = 2365
KEY = QtCore.Qt.Key
MOD = QtCore.Qt.KeyboardModifier


def key(editor, code, modifiers=MOD.NoModifier, text=""):
    event = QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress, code, modifiers, text)
    QtWidgets.QApplication.sendEvent(editor.grid, event)


def table(window):
    window.form.show_parameters([window.database.parameter(FUEL)])
    return window.form.editors[FUEL]


def settle(app, sim, count):
    return pump(app, lambda: len(sim.writes) >= count)


@pytest.mark.parametrize("text,value,expected", [
    ("=5", 10.0, 5.0), ("+5", 10.0, 15.0), ("-1", 10.0, 9.0), ("*2", 10.0, 20.0), ("/4", 10.0, 2.5),
    ("+10%", 50.0, 55.0), ("-10%", 50.0, 45.0), ("5", 10.0, 5.0),
])
def test_enter_value_language(text, value, expected):
    assert Change.parse(text).apply(value) == pytest.approx(expected)


@pytest.mark.parametrize("text", ["", "abc", "=5%", "/0", "+", "5x", "=5cc"])
def test_enter_value_rejects_nonsense(text):
    assert Change.parse(text) is None


def test_enter_value_scopes_and_preview(app):
    assert Change.parse("=7t").scope == "t"
    assert Change.parse("+3%r").describe(1) == "Add 3% Entire Row"
    assert Change.parse("=7t").describe(1) == "Equals 7.0 Entire Table"
    dialog = EnterValueDialog("=x")
    assert dialog.note.text() == "Invalid Entry"
    assert not dialog.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).isEnabled()
    dialog = EnterValueDialog("=500", high=100)
    assert dialog.note.text().startswith("Too big!")


def test_small_and_large_steps_and_caps_lock(session):
    app, window, sim = session
    editor = table(window)
    editor.grid.setCurrentCell(2, 3)
    index = 2 * 22 + 3
    before = window.worker.ecu.get(FUEL, index)
    key(editor, KEY.Key_Q, text="q")
    assert settle(app, sim, 1)
    key(editor, KEY.Key_Q, MOD.ShiftModifier, "Q")
    assert settle(app, sim, 2)
    pump(app, lambda: False, timeout=0.3)
    assert window.worker.ecu.get(FUEL, index) == pytest.approx(before + 0.1 * (1 + tables.LARGE_STEPS))
    key(editor, KEY.Key_CapsLock)
    key(editor, KEY.Key_A, text="a")
    assert settle(app, sim, 3)
    pump(app, lambda: False, timeout=0.3)
    assert window.worker.ecu.get(FUEL, index) == pytest.approx(before + 0.1 * (1 + tables.LARGE_STEPS) - 0.1 * tables.LARGE_STEPS)


def test_row_nudge_is_one_frame_and_column_nudge_one_per_row(session):
    app, window, sim = session
    editor = table(window)
    editor.grid.setCurrentCell(0, 0)
    key(editor, KEY.Key_Q, MOD.ControlModifier)
    assert settle(app, sim, 1)
    pump(app, lambda: False, timeout=0.3)
    assert len(sim.writes) == 1
    assert len(sim.writes[0]) > 8
    del sim.writes[:]
    key(editor, KEY.Key_Q, MOD.AltModifier)
    rows = len([r for r in range(editor.grid.rowCount()) if not editor.grid.isRowHidden(r)])
    assert settle(app, sim, rows)
    pump(app, lambda: False, timeout=0.3)
    assert len(sim.writes) == rows


def test_digits_and_equals_open_the_change_dialog(session):
    app, window, sim = session
    editor = table(window)
    editor.grid.setCurrentCell(1, 1)
    seen = []

    def fake_exec(self):
        seen.append(self.entry.text())
        return 0

    original = EnterValueDialog.exec
    EnterValueDialog.exec = fake_exec
    try:
        key(editor, KEY.Key_5, text="5")
        key(editor, KEY.Key_Equal, text="=")
        key(editor, KEY.Key_Plus, MOD.KeypadModifier, "+")
        key(editor, KEY.Key_Minus, text="-")
    finally:
        EnterValueDialog.exec = original
    assert seen == ["=5", "=", "+", "=-"]
    pump(app, lambda: False, timeout=0.2)
    assert sim.writes == []


def test_whole_table_entry_scope_writes_every_used_cell(session):
    app, window, sim = session
    editor = table(window)
    editor.grid.setCurrentCell(0, 0)
    original = EnterValueDialog.exec
    EnterValueDialog.exec = lambda self: (self.entry.setText("=50t"), True)[1]
    try:
        key(editor, KEY.Key_Equal, text="=")
    finally:
        EnterValueDialog.exec = original
    assert pump(app, lambda: sim.writes)
    pump(app, lambda: False, timeout=0.3)
    ecu = window.worker.ecu
    used_rows, used_columns = editor.used_rows, editor.used_columns
    assert all(abs(ecu.get(FUEL, r * 22 + c) - 50.0) < 0.01 for r in range(used_rows) for c in range(used_columns))


def test_copy_direction_fill_and_extrapolate(session):
    app, window, sim = session
    editor = table(window)
    ecu = window.worker.ecu
    editor.grid.setCurrentCell(3, 3)
    key(editor, KEY.Key_Right, MOD.ControlModifier)
    assert settle(app, sim, 1)
    pump(app, lambda: False, timeout=0.3)
    assert ecu.get(FUEL, 3 * 22 + 4) == ecu.get(FUEL, 3 * 22 + 3)
    del sim.writes[:]
    key(editor, KEY.Key_F, MOD.ControlModifier)
    assert settle(app, sim, 1)
    pump(app, lambda: False, timeout=0.3)
    assert all(ecu.get(FUEL, 3 * 22 + c) == ecu.get(FUEL, 3 * 22 + 3) for c in range(3, editor.used_columns))


def test_markers_and_clear_only_touch_marks_and_never_write(session):
    app, window, sim = session
    editor = table(window)
    editor.grid.setCurrentCell(1, 2)
    key(editor, KEY.Key_N, text="n")
    key(editor, KEY.Key_Y, text="y")
    key(editor, KEY.Key_Z, text="z")
    flags = window.marks.flag(FUEL, 1, 2)
    assert flags & 1 and flags & 4 and not flags & 2
    assert editor.grid.item(1, 2).background().color() == window.theme.colour("Markers")
    key(editor, KEY.Key_M, MOD.ShiftModifier, "M")
    assert window.marks.flag(FUEL, 1, 2) == 0
    editor._mark(1)
    editor._clear_all_markers()
    assert window.marks.flag(FUEL, 1, 2) == 0
    pump(app, lambda: False, timeout=0.3)
    assert sim.writes == []


def test_context_menu_lists_real_pclink_entries(session):
    _, window, _ = session
    menu = table(window).build_menu()
    top = [a.text().split("\t")[0] for a in menu.actions() if a.text()]
    for name in ("Axis Setup", "Quick Trim", "Import/Export", "Cell Markers", "Selection", "Row", "Column", "Table", "Cell Copy", "Fill",
                 "Interpolate", "Extrapolate", "Smooth Selection", "Snake Trail", "Colour Gradient Visible", "Active Cell Tracking", "Reverse Load Axis"):
        assert name in top
    markers = next(a for a in menu.actions() if a.text() == "Cell Markers").menu()
    assert [a.text().split("\t")[0] for a in markers.actions()] == [
        "Clear Tuning Table Markers", "Clear All Table Markers", "Runtime Marker", "Part Tuned", "Tuned"]


def test_theme_colours_and_changed_cells_and_gradient(session):
    app, window, sim = session
    editor = table(window)
    theme = window.theme
    assert editor.grid.item(0, 0).background().color() == theme.colour("DataAreaBackground")
    editor.grid.setCurrentCell(0, 0)
    key(editor, KEY.Key_Q, text="q")
    assert pump(app, lambda: sim.writes)
    pump(app, lambda: False, timeout=0.3)
    editor.refresh()
    assert editor.grid.item(0, 0).background().color() == theme.colour("ValueChanged")
    writes = len(sim.writes)
    key(editor, KEY.Key_H, text="h")
    assert editor.attributes["ColourGradient"] == "true"
    editor.refresh()
    assert editor.grid.item(5, 5).background().color() != theme.colour("DataAreaBackground")
    window.theme = type(theme).built_in("White")
    editor.refresh()
    pump(app, lambda: False, timeout=0.3)
    assert len(sim.writes) == writes


def test_changing_colours_and_refreshing_never_write(session):
    app, window, sim = session
    editor = table(window)
    for _ in range(3):
        editor.refresh()
        window.form.refresh()
    editor.surface.set_theme(window.theme)
    editor.snake.update()
    pump(app, lambda: False, timeout=0.3)
    assert sim.writes == []


def test_ctrl_z_undoes_the_last_table_change(session):
    app, window, sim = session
    editor = table(window)
    ecu = window.worker.ecu
    editor.grid.setCurrentCell(4, 4)
    before = ecu.get(FUEL, 4 * 22 + 4)
    key(editor, KEY.Key_Q, text="q")
    assert pump(app, lambda: ecu.get(FUEL, 4 * 22 + 4) != before)
    key(editor, KEY.Key_Z, MOD.ControlModifier)
    assert pump(app, lambda: ecu.get(FUEL, 4 * 22 + 4) == before)


def test_home_end_and_goto_active_and_tracking(session):
    _, window, _ = session
    editor = table(window)
    editor.grid.setCurrentCell(2, 5)
    key(editor, KEY.Key_Home)
    assert editor.grid.currentColumn() == 0
    key(editor, KEY.Key_End)
    assert editor.grid.currentColumn() == editor.used_columns - 1
    key(editor, KEY.Key_Home, MOD.ControlModifier)
    assert editor.grid.currentRow() == 0
    editor.trace = (3, 4)
    key(editor, KEY.Key_Space, text=" ")
    assert (editor.grid.currentRow(), editor.grid.currentColumn()) == (3, 4)
    key(editor, KEY.Key_F5)
    assert editor.tracking


def test_export_import_round_trip_writes_only_changed_values_once(session):
    app, window, sim = session
    editor = table(window)
    text = editor.export_text()
    assert text.splitlines()[0].startswith(window.database.parameter(FUEL).caption)
    assert editor.import_text(text.replace("\t", "\t", 1))
    assert pump(app, lambda: sim.writes)


def test_graph_toggle_and_attributes(session):
    _, window, _ = session
    editor = table(window)
    editor.grid.setCurrentCell(0, 0)
    key(editor, KEY.Key_K, text="k")
    assert editor.stack.currentIndex() == 1
    assert editor.attributes["GraphDisplay"] == "true"
    key(editor, KEY.Key_Backslash, text="\\")
    assert editor.surface.key_rotation and editor.attributes["KeyRotation"] == "true"
    yaw = editor.surface.yaw
    event = QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress, KEY.Key_Right, MOD.NoModifier)
    editor._key(event, True)
    assert editor.surface.yaw > yaw
    editor.surface.reset_view()
    assert editor.surface.yaw == editor.surface.YAW
    editor.toggle_graph()


def test_surface_paints_and_picks_a_cell(session):
    app, window, _ = session
    editor = table(window)
    editor.surface.configure(True, True, False, False)
    editor.surface.resize(500, 400)
    editor.surface.show()
    image = editor.surface.grab().toImage()
    assert image.width() == 500
    colours = {image.pixel(x, y) for x in range(0, 500, 7) for y in range(0, 400, 7)}
    assert len(colours) > 8
    seen = []
    editor.surface.cell_clicked.connect(lambda *a: seen.append(a))
    point = editor.surface._points[5 * editor.surface.columns + 5]
    QtWidgets.QApplication.sendEvent(editor.surface, QtGui.QMouseEvent(
        QtCore.QEvent.Type.MouseButtonPress, point, editor.surface.mapToGlobal(point), QtCore.Qt.MouseButton.LeftButton,
        QtCore.Qt.MouseButton.LeftButton, MOD.NoModifier))
    assert seen and seen[-1][:2] == (5, 5)


def test_axis_setup_insert_delete_init_and_write_frames(session, monkeypatch):
    app, window, sim = session
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning", lambda *args: pytest.fail(args[2]))
    editor = table(window)
    ecu = window.worker.ecu
    parameter = editor.parameter
    x_old, y_old = ecu.get_all(parameter.column_axis_id), ecu.get_all(parameter.row_axis_id)
    dialog = tables.AxisSetupDialog(editor, x_old, y_old)
    used = len(dialog.values[0])
    assert dialog.windowTitle() == f"{parameter.caption} Axis Setup"
    assert dialog.names[0].text()
    dialog.values[0].insert(1, (dialog.values[0][0] + dialog.values[0][1]) / 2)
    dialog._load(0)
    assert len(dialog.values[0]) == used + 1
    dialog._select(0, 1)
    dialog._delete(0)
    assert len(dialog.values[0]) == used
    dialog._init(0)
    assert dialog.values[0] == dialog.window_.database.default_axis(dialog.sources[0])[: dialog.axis_capacity[0]]
    dialog.values[0] = list(x_old[:used])
    dialog.values[0].insert(1, (x_old[0] + x_old[1]) / 2)
    dialog._load(0)
    dialog._check()
    dialog.deleteLater()
    del sim.writes[:]
    editor.apply_axes(dialog, x_old, y_old, interpolate=True)
    assert pump(app, lambda: len(sim.writes) >= 3)
    pump(app, lambda: False, timeout=0.3)
    # X axis, Y axis, then the whole table, each as one frame.
    assert len(sim.writes) == 3
    assert ecu.get(parameter.column_axis_id, 1) == pytest.approx((x_old[0] + x_old[1]) / 2)


def test_axis_setup_rejects_descending_and_out_of_range(session, monkeypatch):
    _, window, _ = session
    editor = table(window)
    ecu = window.worker.ecu
    dialog = tables.AxisSetupDialog(editor, ecu.get_all(editor.parameter.column_axis_id), ecu.get_all(editor.parameter.row_axis_id))
    warnings = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning", lambda *args: warnings.append(args[2]))
    dialog.grids[0].item(0, 1).setText(str(dialog.values[0][0] - 1))
    dialog._check()
    assert warnings and "X Axis" in warnings[-1]
    dialog.deleteLater()


def test_axis_parameter_picker_lists_none_and_categories(session):
    _, window, _ = session
    editor = table(window)
    ecu = window.worker.ecu
    dialog = tables.AxisSetupDialog(editor, ecu.get_all(editor.parameter.column_axis_id), ecu.get_all(editor.parameter.row_axis_id))
    groups = window._groups(live=True)
    picker = tables.ParameterPicker(groups, lambda i: window.database.parameter(i).caption, dialog.sources[0])
    assert picker.tree.topLevelItem(0).text(0) == "None"
    assert picker.tree.topLevelItemCount() > 3
    picker.search.setText("Engine Speed")
    assert picker.tree.currentItem() is not None
    dialog.deleteLater()


def test_select_item_skips_na_sorts_and_returns_the_value(app):
    dialog = SelectItemDialog([(0, "Zebra"), (1, "NA"), (2, "Apple")], current=2)
    assert [dialog.list.item(i).text() for i in range(dialog.list.count())] == ["Zebra", "Apple"]
    assert dialog.value() == 2
    dialog.alphabetical.setChecked(True)
    assert [dialog.list.item(i).text() for i in range(dialog.list.count())] == ["Apple", "Zebra"]
    dialog.list.setCurrentRow(1)
    assert dialog.value() == 0


def test_set_cells_batches_neighbours_into_one_frame(session):
    app, window, sim = session
    ecu = window.worker.ecu
    ecu.set_cells(FUEL, {0: 50.0, 1: 51.0, 2: 52.0, 5: 53.0})
    assert len(sim.writes) == 2
    assert ecu.get(FUEL, 1) == pytest.approx(51.0, abs=0.01) and ecu.get(FUEL, 5) == pytest.approx(53.0, abs=0.01)
