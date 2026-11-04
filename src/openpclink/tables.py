"""Tuning tables, their axis setup and the settings form that hosts them."""
from __future__ import annotations

import bisect
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from PySide6 import QtCore, QtGui, QtWidgets

from . import tools, tune
from .database import Parameter
from .ecu import SECRET_IDS, Ecu, EcuProtected
from .views import SurfaceView, used_axis_length

if TYPE_CHECKING:
    from .gui import MainWindow

ID_ROLE = QtCore.Qt.ItemDataRole.UserRole
MANY_OPTIONS = 12
SNAKE_LENGTH = 64
LARGE_STEPS = 5
KEYS = QtCore.Qt.Key
MODS = QtCore.Qt.KeyboardModifier
ARROWS = {KEYS.Key_Up: (-1, 0), KEYS.Key_Down: (1, 0), KEYS.Key_Left: (0, -1), KEYS.Key_Right: (0, 1)}


class _IgnoreWheel(QtCore.QObject):
    """Stops the scroll wheel changing a setting while scrolling past it."""

    def eventFilter(self, _watched: QtCore.QObject, event: QtCore.QEvent) -> bool:
        return event.type() == QtCore.QEvent.Type.Wheel


class SelectItemDialog(QtWidgets.QDialog):
    """Select Item: the list PCLink offers beside a setting with very many options."""

    def __init__(self, items: list[tuple[int, str]], current: int | None = None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Select Item")
        self.resize(320, 480)
        self.items = [(value, text) for value, text in items if text != "NA"]
        self.current = current
        self.alphabetical = QtWidgets.QToolButton()
        self.alphabetical.setText("A-Z")
        self.alphabetical.setToolTip("Alphabetical")
        self.alphabetical.setCheckable(True)
        self.alphabetical.toggled.connect(self._fill)
        self.list = QtWidgets.QListWidget()
        self.list.itemDoubleClicked.connect(lambda _item: self.accept())
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        column = QtWidgets.QVBoxLayout(self)
        column.addWidget(self.alphabetical, 0, QtCore.Qt.AlignmentFlag.AlignLeft)
        column.addWidget(self.list)
        column.addWidget(buttons)
        self._fill()

    def _fill(self) -> None:
        self.list.clear()
        shown = sorted(self.items, key=lambda pair: pair[1].lower()) if self.alphabetical.isChecked() else self.items
        for value, text in shown:
            item = QtWidgets.QListWidgetItem(text)
            item.setData(ID_ROLE, value)
            self.list.addItem(item)
            if value == self.current:
                self.list.setCurrentItem(item)

    def value(self) -> int | None:
        item = self.list.currentItem()
        return item.data(ID_ROLE) if item else None


class _PickableCombo(QtWidgets.QWidget):
    """A setting with very many options, with PCLink's Select Item list beside it."""

    def __init__(self, combo: QtWidgets.QComboBox, window: "MainWindow", data_id: int) -> None:
        super().__init__()
        self.combo, self.window_, self.data_id = combo, window, data_id
        pick = QtWidgets.QToolButton()
        pick.setText("…")
        pick.clicked.connect(self._pick)
        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(combo, 1)
        row.addWidget(pick)

    def _pick(self) -> None:
        items = [(self.combo.itemData(i), self.combo.itemText(i)) for i in range(self.combo.count())]
        dialog = SelectItemDialog(items, self.combo.currentData(), self)
        if dialog.exec() and dialog.value() is not None:
            self.window_.choose_option(self.data_id, dialog.value())


def wrench_icon() -> QtGui.QIcon:
    """The mark PCLink puts beside a setting that runs something when clicked."""
    pixmap = QtGui.QPixmap(16, 16)
    pixmap.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(pixmap)
    painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
    pen = QtGui.QPen(QtGui.QColor("#808080"), 3)
    pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.drawLine(3, 13, 10, 6)
    painter.setPen(QtGui.QPen(QtGui.QColor("#808080"), 2))
    painter.drawEllipse(QtCore.QPointF(11, 5), 3.5, 3.5)
    painter.end()
    return QtGui.QIcon(pixmap)


class ParameterForm(QtWidgets.QWidget):
    """Edits the parameters of one menu entry."""

    def __init__(self, window: "MainWindow") -> None:
        super().__init__()
        self.window_ = window
        self.layout_ = QtWidgets.QFormLayout(self)
        self.editors: dict[int, QtWidgets.QWidget] = {}
        self.parameters: list[Parameter] = []
        self.prefers_graph = False
        self.attributes: dict[str, str] = {}
        self._wheel_guard = _IgnoreWheel(self)

    def show_parameters(self, parameters: list[Parameter]) -> None:
        # Deferred: an editor being replaced may be mid-signal or hold focus.
        while self.layout_.rowCount():
            row = self.layout_.takeRow(0)
            for item in (row.labelItem, row.fieldItem):
                if item is not None and item.widget() is not None:
                    item.widget().blockSignals(True)
                    item.widget().hide()
                    item.widget().deleteLater()
        self.editors.clear()
        self.parameters = parameters
        for parameter in sorted(parameters, key=lambda item: -item.priority):
            if not self.window_.parameter_visible(parameter.data_id):
                continue
            editor = self._build_editor(parameter)
            if editor is not None:
                self.layout_.addRow(parameter.caption, self.window_.compare_field(parameter, editor))
                self.editors[parameter.data_id] = editor
        self.refresh()

    def _build_editor(self, parameter: Parameter) -> QtWidgets.QWidget | None:
        ecu = self.window_.worker.ecu
        if parameter.data_id in self.window_.ACTION_PARAMETERS:
            button = QtWidgets.QPushButton(wrench_icon(), "")
            button.setFlat(True)
            button.setFixedWidth(28)
            button.setToolTip(parameter.caption)
            button.clicked.connect(lambda: self.window_.run_action_parameter(parameter.data_id))
            return button
        if ecu is None or parameter.data_id not in ecu.table or parameter.data_id in SECRET_IDS:
            return None
        loc = ecu.table[parameter.data_id]
        data_id = parameter.data_id
        read_only = loc.read_only or loc.offset < ecu.info.runtime_size
        if loc.is_string and loc.width == 1:
            edit = QtWidgets.QLineEdit()
            edit.setMaxLength(loc.count)
            edit.setReadOnly(read_only)
            edit.editingFinished.connect(lambda: self._commit_text(data_id, edit))
            return edit
        if loc.is_bitfield and parameter.options:
            combo = QtWidgets.QComboBox()
            limit = (loc.mask >> loc.shift) + 1
            link_id = ecu.info.link_id
            for index, option in self.window_.database.visible_options(data_id, link_id):
                if index < limit:
                    combo.addItem(option, index)
            combo.setEnabled(not read_only)
            combo.activated.connect(lambda _: self.window_.choose_option(data_id, combo.currentData()))
            combo.installEventFilter(self._wheel_guard)
            if combo.count() > MANY_OPTIONS:
                return _PickableCombo(combo, self.window_, data_id)
            return combo
        if loc.count > 1 and not loc.is_bitfield:
            if len(self.parameters) > 1 and parameter.is_table:
                button = QtWidgets.QPushButton("Open table…")
                button.clicked.connect(lambda: self.window_.open_table_entry(data_id))
                return button
            table = TableEditor(self.window_, parameter, loc.count, read_only, self.attributes)
            if (self.prefers_graph or self.attributes.get("GraphDisplay") == "true") and table.grid.rowCount() > 1:
                table.toggle_graph()
            return table
        spin = QtWidgets.QDoubleSpinBox()
        spin.setKeyboardTracking(False)
        if loc.is_bitfield:
            decimals = 0
        else:
            decimals = parameter.decimals if parameter.decimals is not None else (0 if loc.scale == 1 else 3)
        spin.setDecimals(decimals)
        kind = self.window_.kind_of(parameter)
        spin.setDecimals(self.window_.units.decimals(decimals, kind))
        low = self.window_.units.to_display(parameter.minimum, kind) if parameter.minimum is not None else -1e9
        high = self.window_.units.to_display(parameter.maximum, kind) if parameter.maximum is not None else 1e9
        spin.setRange(min(low, high), max(low, high))
        spin.setSingleStep(abs(loc.scale) or 1)
        units = self.window_.units_of(parameter)
        spin.setSuffix(f" {units}" if units else "")
        spin.setReadOnly(read_only)
        spin.installEventFilter(self._wheel_guard)
        spin.editingFinished.connect(lambda: self._commit_number(data_id, spin))
        return spin

    def _commit_text(self, data_id: int, edit: QtWidgets.QLineEdit) -> None:
        if edit.isModified():
            edit.setModified(False)
            self.window_.write_text(data_id, edit.text())

    def _commit_number(self, data_id: int, spin: QtWidgets.QDoubleSpinBox) -> None:
        # Compare with what was displayed, not the ECU's value: a value rounded or
        # clamped for display must never be written just because focus moved.
        if not spin.isReadOnly() and spin.value() != spin.property("shown"):
            spin.setProperty("shown", spin.value())
            parameter = self.window_.database.parameter(data_id)
            self.window_.write(data_id, self.window_.units.to_base(spin.value(), self.window_.kind_of(parameter)), 0)

    def refresh(self, only: int | None = None) -> None:
        ecu = self.window_.worker.ecu
        if ecu is None:
            return
        for data_id, editor in self.editors.items():
            if only is not None and data_id != only:
                continue
            try:
                self._refresh_editor(ecu, data_id, editor)
            except EcuProtected:
                editor.setEnabled(False)

    def _refresh_editor(self, ecu: Ecu, data_id: int, editor: QtWidgets.QWidget) -> None:
        if isinstance(editor, QtWidgets.QPushButton):
            return
        if isinstance(editor, QtWidgets.QLineEdit):
            if not editor.hasFocus():
                editor.setText(ecu.get_string(data_id))
        elif isinstance(editor, _PickableCombo):
            editor.combo.setCurrentIndex(editor.combo.findData(int(ecu.get(data_id))))
        elif isinstance(editor, QtWidgets.QComboBox):
            editor.setCurrentIndex(editor.findData(int(ecu.get(data_id))))
        elif isinstance(editor, TableEditor):
            editor.refresh()
        elif isinstance(editor, QtWidgets.QDoubleSpinBox) and not editor.hasFocus():
            editor.blockSignals(True)
            parameter = self.window_.database.parameter(data_id)
            editor.setValue(self.window_.units.to_display(ecu.get(data_id), self.window_.kind_of(parameter)))
            editor.setProperty("shown", editor.value())
            editor.blockSignals(False)


# ---- Enter New Value ------------------------------------------------------

_CHANGE = re.compile(r"^\s*([=+\-*/])?\s*(-?\d*\.?\d+)\s*(%)?\s*([crt])?\s*$", re.IGNORECASE)
_OPERATIONS = {"=": "Equals", "+": "Add", "-": "Subtract", "*": "Multiply", "/": "Divide"}
_SCOPES = {"c": "Entire Column", "r": "Entire Row", "t": "Entire Table"}


@dataclass(frozen=True)
class Change:
    """What PCLink's Enter New Value box asks for: operator, number, percent and how much of the table."""

    operator: str
    number: float
    percent: bool
    scope: str

    @classmethod
    def parse(cls, text: str) -> "Change | None":
        found = _CHANGE.match(text)
        if found is None:
            return None
        operator, number, percent, scope = found.groups()
        change = cls(operator or "=", float(number), bool(percent), (scope or "").lower())
        if change.operator == "/" and change.number == 0:
            return None
        if change.percent and change.operator == "=":
            return None
        return change

    def apply(self, value: float) -> float:
        number = self.number
        if self.percent:
            number = value * number / 100.0 if self.operator in "+-" else number / 100.0
        return {"=": lambda: self.number, "+": lambda: value + number, "-": lambda: value - number, "*": lambda: value * number, "/": lambda: value / number}[self.operator]()

    def describe(self, decimals: int) -> str:
        shown = f"{self.number:g}%" if self.percent else f"{self.number:.{decimals}f}"
        text = f"{_OPERATIONS[self.operator]} {shown}"
        return f"{text} {_SCOPES[self.scope]}" if self.scope else text


class EnterValueDialog(QtWidgets.QDialog):
    """Enter New Value: type =, +, -, * or / then a number, optionally % and c, r or t."""

    def __init__(self, text: str = "=", parent=None, decimals: int = 1, low: float | None = None, high: float | None = None,
                 scopes: bool = True, caret: int | None = None, selected: bool = False) -> None:
        super().__init__(parent)
        self.setWindowTitle("Enter New Value")
        self.decimals, self.low, self.high, self.scopes = decimals, low, high, scopes
        self.entry = QtWidgets.QLineEdit(text)
        font = self.entry.font()
        font.setPointSize(font.pointSize() + 6)
        self.entry.setFont(font)
        self.entry.setFixedWidth(100)
        self.entry.textChanged.connect(self._check)
        self.entry.returnPressed.connect(self._accept)
        self.note = QtWidgets.QLabel()
        self.buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self._accept)
        self.buttons.rejected.connect(self.reject)
        legend = QtWidgets.QLabel(self._legend())
        legend.setEnabled(False)
        left = QtWidgets.QVBoxLayout()
        left.addWidget(self.entry)
        left.addWidget(self.note)
        left.addWidget(self.buttons)
        row = QtWidgets.QHBoxLayout(self)
        row.addLayout(left)
        row.addWidget(legend)
        self._check()
        if selected:
            self.entry.setSelection(1, len(text))
        elif caret is not None:
            self.entry.setCursorPosition(caret)

    def _legend(self) -> str:
        lines = ["Before Number:", "= To Value"]
        if self.scopes:
            lines += ["-  Offset Subtract", "+  Offset Add", "*  Multiply", "/  Divide", "After Number:", "%  Percent",
                      "c    Do Entire Column", "r      Do Entire Row", "t      Do Entire Table"]
        return "\n".join(lines)

    def change(self) -> Change | None:
        change = Change.parse(self.entry.text())
        if change is not None and not self.scopes and (change.operator != "=" or change.scope or change.percent):
            return None
        return change

    def _problem(self, change: Change) -> str:
        if change.operator == "=":
            if self.low is not None and change.number < self.low:
                return f"Too small! Min is {self.low:g}"
            if self.high is not None and change.number > self.high:
                return f"Too big! Max is {self.high:g}"
        return ""

    def _check(self) -> None:
        change = self.change()
        problem = self._problem(change) if change else ""
        ok = self.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok)
        ok.setEnabled(change is not None and not problem)
        if change is None:
            self.note.setText("Invalid Entry")
            self.note.setStyleSheet("color:#c02020")
        elif problem:
            self.note.setText(problem)
            self.note.setStyleSheet("color:#c02020")
        else:
            self.note.setText(change.describe(self.decimals))
            self.note.setStyleSheet("color:#1b7f3b")

    def _accept(self) -> None:
        if self.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).isEnabled():
            self.accept()


# ---- table painting -------------------------------------------------------

class _Header(QtWidgets.QHeaderView):
    """Axis labels in PCLink's grey, the editing row or column lit in the active-control colour."""

    def __init__(self, orientation: QtCore.Qt.Orientation, editor: "TableEditor") -> None:
        super().__init__(orientation, editor.grid)
        self.editor = editor
        self.setSectionsClickable(True)
        self.setHighlightSections(False)
        self.setDefaultAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)

    def paintSection(self, painter: QtGui.QPainter, rect: QtCore.QRect, logical: int) -> None:
        if not rect.isValid():
            return
        theme = self.editor.window_.theme
        lit = self.editor.line_is_selected(self.orientation(), logical)
        painter.save()
        painter.fillRect(rect, theme.colour("Active Control") if lit else theme.colour("GridLines"))
        painter.setPen(QtGui.QColor(255, 255, 255) if lit else theme.colour("ViewText"))
        text = self.model().headerData(logical, self.orientation(), QtCore.Qt.ItemDataRole.DisplayRole)
        painter.drawText(rect, int(QtCore.Qt.AlignmentFlag.AlignCenter), "" if text is None else str(text))
        painter.setPen(theme.colour("DataAreaBackground"))
        painter.drawRect(rect.adjusted(0, 0, -1, -1))
        painter.restore()


class _CellDelegate(QtWidgets.QStyledItemDelegate):
    """Cells keep their own colours: the editing selection is drawn as an outline instead."""

    def initStyleOption(self, option: QtWidgets.QStyleOptionViewItem, index: QtCore.QModelIndex) -> None:
        super().initStyleOption(option, index)
        option.state &= ~(QtWidgets.QStyle.StateFlag.State_Selected | QtWidgets.QStyle.StateFlag.State_HasFocus)


class _SnakeOverlay(QtWidgets.QWidget):
    """The trail of cells the engine has just been through, the cross hairs and the editing outline."""

    enabled_trail = True
    paused = False

    def __init__(self, grid: QtWidgets.QTableWidget, editor: "TableEditor | None" = None) -> None:
        super().__init__(grid.viewport())
        self.grid = grid
        self.editor = editor
        self.points: list[tuple[int, int]] = []
        self.cross: tuple[float, float] | None = None
        self.view_enabled = True
        self.view_paused = False
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        grid.viewport().installEventFilter(self)

    def eventFilter(self, _watched: QtCore.QObject, event: QtCore.QEvent) -> bool:
        if event.type() == QtCore.QEvent.Type.Resize:
            self.resize(self.grid.viewport().size())
        return False

    def visible_trail(self) -> bool:
        return _SnakeOverlay.enabled_trail and self.view_enabled

    def add(self, position: tuple[int, int]) -> None:
        if not self.visible_trail():
            if self.points:
                self.points.clear()
                self.update()
            return
        if _SnakeOverlay.paused or self.view_paused:
            return
        self.points.append(position)
        del self.points[: max(len(self.points) - SNAKE_LENGTH, 0)]
        self.update()

    def _centre(self, row: float, column: float) -> QtCore.QPointF | None:
        grid = self.grid
        r0, c0 = int(row), int(column)
        r1, c1 = min(r0 + 1, grid.rowCount() - 1), min(c0 + 1, grid.columnCount() - 1)

        def spot(r: int, c: int) -> QtCore.QPointF:
            return QtCore.QPointF(grid.columnViewportPosition(c) + grid.columnWidth(c) / 2, grid.rowViewportPosition(r) + grid.rowHeight(r) / 2)

        if grid.isRowHidden(r0) or grid.isColumnHidden(c0):
            return None
        a, b = spot(r0, c0), spot(r1, c1)
        return QtCore.QPointF(a.x() + (b.x() - a.x()) * (column - c0), a.y() + (b.y() - a.y()) * (row - r0))

    def paintEvent(self, _event: QtGui.QPaintEvent) -> None:
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        theme = self.editor.window_.theme if self.editor else None
        accent = theme.colour("Active Control") if theme else QtGui.QColor(48, 150, 255)
        if self.editor is not None:
            self._paint_selection(painter, accent)
        if self.cross is not None and self.editor is not None and self.editor.cross_hairs:
            spot = self._centre(*self.cross)
            if spot is not None:
                painter.setPen(QtGui.QPen(accent, 1))
                painter.drawLine(QtCore.QPointF(spot.x(), 0), QtCore.QPointF(spot.x(), self.height()))
                painter.drawLine(QtCore.QPointF(0, spot.y()), QtCore.QPointF(self.width(), spot.y()))
        if len(self.points) >= 2 and self.visible_trail():
            colour = theme.colour("Markers") if theme else QtGui.QColor(255, 0, 0)
            pen = QtGui.QPen(colour, 1.5)
            pen.setStyle(QtCore.Qt.PenStyle.DashLine)
            painter.setPen(pen)
            line = QtGui.QPolygonF()
            for row, column in self.points:
                spot = self._centre(row, column)
                if spot is not None:
                    line.append(spot)
            painter.drawPolyline(line)

    def _paint_selection(self, painter: QtGui.QPainter, accent: QtGui.QColor) -> None:
        cells = self.editor.selected_cells()
        if not cells:
            return
        rows = [row for row, _ in cells]
        columns = [column for _, column in cells]
        grid = self.grid
        left = grid.columnViewportPosition(min(columns))
        top = grid.rowViewportPosition(min(rows))
        right = grid.columnViewportPosition(max(columns)) + grid.columnWidth(max(columns))
        bottom = grid.rowViewportPosition(max(rows)) + grid.rowHeight(max(rows))
        painter.setPen(QtGui.QPen(accent, 2))
        painter.drawRect(QtCore.QRect(left + 1, top + 1, right - left - 2, bottom - top - 2))


class _SideLabel(QtWidgets.QWidget):
    """The row axis name, written up the left side."""

    def __init__(self) -> None:
        super().__init__()
        self.text = ""
        self.setFixedWidth(18)

    def set_text(self, text: str) -> None:
        if text != self.text:
            self.text = text
            self.update()

    def paintEvent(self, _event: QtGui.QPaintEvent) -> None:
        painter = QtGui.QPainter(self)
        painter.setPen(self.palette().color(QtGui.QPalette.ColorRole.WindowText))
        painter.translate(0, self.height())
        painter.rotate(-90)
        painter.drawText(QtCore.QRect(0, 0, self.height(), self.width()), int(QtCore.Qt.AlignmentFlag.AlignCenter), self.text)


# ---- axis setup -----------------------------------------------------------

def _icon(kind: str) -> QtGui.QIcon:
    pixmap = QtGui.QPixmap(16, 16)
    pixmap.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(pixmap)
    painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
    colour = QtGui.QColor(30, 160, 40) if kind == "plus" else QtGui.QColor(210, 30, 30)
    painter.setPen(QtGui.QPen(colour, 3.2))
    if kind == "plus":
        painter.drawLine(8, 2, 8, 14)
        painter.drawLine(2, 8, 14, 8)
    else:
        painter.drawLine(3, 3, 13, 13)
        painter.drawLine(13, 3, 3, 13)
    painter.end()
    return QtGui.QIcon(pixmap)


class ParameterPicker(QtWidgets.QDialog):
    """Pick the live value a table axis follows, or None."""

    def __init__(self, groups: dict[tuple[str, ...], list[int]], caption_of, current: int | None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Select Parameter")
        self.resize(300, 460)
        self.search = QtWidgets.QLineEdit()
        self.alphabetical = QtWidgets.QToolButton()
        self.alphabetical.setText("A-Z")
        self.alphabetical.setCheckable(True)
        self.alphabetical.setToolTip("Alphabetical")
        self.alphabetical.toggled.connect(self._fill)
        self.search.textChanged.connect(self._filter)
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.itemDoubleClicked.connect(lambda item, _c: self._maybe_accept(item))
        self.groups, self.caption_of, self.current = groups, caption_of, current
        top = QtWidgets.QHBoxLayout()
        top.addWidget(self.alphabetical)
        top.addWidget(self.search, 1)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        column = QtWidgets.QVBoxLayout(self)
        column.addLayout(top)
        column.addWidget(self.tree)
        column.addWidget(buttons)
        self._fill()

    def _fill(self) -> None:
        self.tree.clear()
        none = QtWidgets.QTreeWidgetItem(self.tree, ["None"])
        none.setIcon(0, _icon("cross"))
        none.setData(0, ID_ROLE, 0)
        nodes: dict[tuple[str, ...], QtWidgets.QTreeWidgetItem] = {}
        for path in sorted(self.groups):
            for depth in range(1, len(path) + 1):
                if path[:depth] not in nodes:
                    nodes[path[:depth]] = QtWidgets.QTreeWidgetItem(nodes.get(path[: depth - 1]) or self.tree, [path[depth - 1]])
            ids = list(self.groups[path])
            if self.alphabetical.isChecked():
                ids.sort(key=lambda i: self.caption_of(i).lower())
            for data_id in ids:
                item = QtWidgets.QTreeWidgetItem(nodes[path], [self.caption_of(data_id)])
                item.setData(0, ID_ROLE, data_id)
                if data_id == self.current:
                    self.tree.setCurrentItem(item)
        self._filter(self.search.text())

    def _filter(self, text: str) -> None:
        needle = text.lower()

        def visit(item: QtWidgets.QTreeWidgetItem) -> bool:
            show = not needle or needle in item.text(0).lower()
            for index in range(item.childCount()):
                show = visit(item.child(index)) or show
            item.setHidden(not show)
            if needle and show and item.childCount():
                item.setExpanded(True)
            return show

        for index in range(self.tree.topLevelItemCount()):
            visit(self.tree.topLevelItem(index))

    def _maybe_accept(self, item: QtWidgets.QTreeWidgetItem) -> None:
        if isinstance(item.data(0, ID_ROLE), int):
            self.accept()

    def value(self) -> int | None:
        item = self.tree.currentItem()
        data = item.data(0, ID_ROLE) if item else None
        return data if isinstance(data, int) else None


class _AxisPreview(QtWidgets.QWidget):
    """The relative spacing of the rows and columns."""

    def __init__(self) -> None:
        super().__init__()
        self.x: list[float] = []
        self.y: list[float] = []
        self.setMinimumSize(200, 150)

    def paintEvent(self, _event: QtGui.QPaintEvent) -> None:
        painter = QtGui.QPainter(self)
        painter.fillRect(self.rect(), QtGui.QColor(0, 0, 0))
        painter.setPen(QtGui.QColor(190, 190, 190))
        area = self.rect().adjusted(6, 6, -7, -7)
        for values, vertical in ((self.x, True), (self.y, False)):
            if not values:
                continue
            low, high = min(values), max(values)
            for value in values:
                fraction = (value - low) / ((high - low) or 1.0)
                if vertical:
                    at = area.left() + fraction * area.width()
                    painter.drawLine(QtCore.QPointF(at, area.top()), QtCore.QPointF(at, area.bottom()))
                else:
                    at = area.top() + fraction * area.height()
                    painter.drawLine(QtCore.QPointF(area.left(), at), QtCore.QPointF(area.right(), at))


class AxisSetupDialog(QtWidgets.QDialog):
    """Table Axis Setup: each axis's parameter, its breakpoints, and whether to reshape the table."""

    def __init__(self, editor: "TableEditor", x_axis: list[float], y_axis: list[float]) -> None:
        super().__init__(editor)
        window = editor.window_
        self.editor, self.window_ = editor, window
        self.setWindowTitle(f"{editor.parameter.caption} Axis Setup")
        self.controls: list[tuple] = []
        self.axis_capacity = (editor.grid.columnCount(), editor.grid.rowCount())
        self.sources = [self._source(editor.parameter.column_source_id), self._source(editor.parameter.row_source_id)]
        self.original_sources = list(self.sources)
        self.values = [list(x_axis[: used_axis_length(x_axis)]) if x_axis else [], list(y_axis[: used_axis_length(y_axis)]) if y_axis else []]
        self.grids = [self._grid(True), self._grid(False)]
        self.names = [QtWidgets.QLineEdit(), QtWidgets.QLineEdit()]
        self.preview = _AxisPreview()
        self.progress = QtWidgets.QProgressBar()
        self.progress.setTextVisible(False)
        self.buttons = QtWidgets.QDialogButtonBox()
        help_button = self.buttons.addButton("Help", QtWidgets.QDialogButtonBox.ButtonRole.HelpRole)
        help_button.clicked.connect(lambda: window.statusBar().showMessage("Table Axis Setup: see the Tables section of the manual.", 5000))
        self.ok_button = self.buttons.addButton(QtWidgets.QDialogButtonBox.StandardButton.Ok)
        self.buttons.addButton(QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self._check)
        self.buttons.rejected.connect(self.reject)
        self.interpolate_axes = False
        layout = QtWidgets.QGridLayout(self)
        layout.addWidget(self._group(0, "X Axis"), 0, 0, 1, 2)
        layout.addWidget(self._group(1, "Y Axis"), 1, 0)
        layout.addWidget(self.preview, 1, 1)
        layout.addWidget(self.progress, 2, 0, 1, 2)
        layout.addWidget(self.buttons, 3, 0, 1, 2)
        layout.setColumnStretch(1, 1)
        self.resize(500, 450)
        for which in (0, 1):
            self._load(which)

    def _source(self, source_id: int | None) -> int:
        ecu = self.window_.worker.ecu
        if source_id is None or ecu is None or source_id not in ecu.table:
            return 0
        return int(ecu.get(source_id))

    def _group(self, which: int, title: str) -> QtWidgets.QGroupBox:
        box = QtWidgets.QGroupBox(title)
        name = self.names[which]
        name.setReadOnly(True)
        pick = QtWidgets.QToolButton()
        pick.setText("…")
        pick.clicked.connect(lambda: self._pick(which))
        init = QtWidgets.QPushButton("Init Axis")
        init.clicked.connect(lambda: self._init(which))
        add = QtWidgets.QToolButton()
        add.setIcon(_icon("plus"))
        add.clicked.connect(lambda: self._insert(which))
        remove = QtWidgets.QToolButton()
        remove.setIcon(_icon("cross"))
        remove.clicked.connect(lambda: self._delete(which))
        self.controls.append((name, pick, init, add, remove, self.grids[which]))
        top = QtWidgets.QHBoxLayout()
        if which == 0:
            top.addWidget(QtWidgets.QLabel("Parameter"))
        top.addWidget(name, 1)
        top.addWidget(pick)
        top.addWidget(init)
        top.addWidget(add)
        top.addWidget(remove)
        column = QtWidgets.QVBoxLayout(box)
        column.addLayout(top)
        column.addWidget(self.grids[which])
        return box

    def _grid(self, horizontal: bool) -> QtWidgets.QTableWidget:
        grid = QtWidgets.QTableWidget(1, 0) if horizontal else QtWidgets.QTableWidget(0, 1)
        grid.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        if horizontal:
            grid.setVerticalHeaderLabels(["Value"])
            grid.setFixedHeight(76)
        else:
            grid.setHorizontalHeaderLabels(["Value"])
            grid.setMinimumHeight(160)
            grid.setMaximumWidth(130)
        grid.itemChanged.connect(lambda _item: self._edited())
        grid.keyPressEvent = lambda event, g=grid, h=horizontal: self._key(event, g, h)
        return grid

    def _key(self, event: QtGui.QKeyEvent, grid: QtWidgets.QTableWidget, horizontal: bool) -> None:
        which = 0 if horizontal else 1
        if event.key() == KEYS.Key_Insert:
            self._insert(which)
        elif event.key() == KEYS.Key_Delete and grid.state() != grid.State.EditingState:
            self._delete(which)
        else:
            QtWidgets.QTableWidget.keyPressEvent(grid, event)

    def _caption(self, live_id: int) -> str:
        database = self.window_.database
        return database.parameter(live_id).caption if live_id and database.known(live_id) else "None"

    def _load(self, which: int) -> None:
        grid, values = self.grids[which], self.values[which]
        grid.blockSignals(True)
        if which == 0:
            grid.setColumnCount(len(values))
            grid.setHorizontalHeaderLabels([str(i + 1) for i in range(len(values))])
        else:
            grid.setRowCount(len(values))
            grid.setVerticalHeaderLabels([str(i + 1) for i in range(len(values))])
        for index, value in enumerate(values):
            item = QtWidgets.QTableWidgetItem(f"{value:g}")
            grid.setItem(0, index, item) if which == 0 else grid.setItem(index, 0, item)
        grid.blockSignals(False)
        name = self.names[which]
        name.setText(self._caption(self.sources[which]))
        enabled = self.sources[which] != 0 or not self._has_source(which)
        for widget in self.controls[which][1:]:
            widget.setEnabled(enabled)
        self._preview()

    def _has_source(self, which: int) -> bool:
        parameter = self.editor.parameter
        ecu = self.window_.worker.ecu
        source = (parameter.column_source_id, parameter.row_source_id)[which]
        return source is not None and ecu is not None and source in ecu.table

    def _preview(self) -> None:
        self.preview.x, self.preview.y = (list(v) for v in self.values)
        self.preview.update()

    def _edited(self) -> None:
        for which, grid in enumerate(self.grids):
            values = []
            for index in range(len(self.values[which])):
                item = grid.item(0, index) if which == 0 else grid.item(index, 0)
                try:
                    values.append(float(item.text()))
                except (ValueError, AttributeError):
                    values.append(self.values[which][index])
            self.values[which] = values
        self._preview()

    def _selected(self, which: int) -> int:
        grid = self.grids[which]
        index = grid.currentColumn() if which == 0 else grid.currentRow()
        return max(index, 0)

    def _insert(self, which: int) -> None:
        values = self.values[which]
        if not values or len(values) >= self.axis_capacity[which]:
            return
        at = self._selected(which)
        dialog = EnterValueDialog(f"={values[at]:g}", self, decimals=self._decimals(which), scopes=False, selected=True)
        if not dialog.exec() or dialog.change() is None:
            return
        values.insert(at + 1, dialog.change().number)
        self._load(which)
        self._select(which, at + 1)

    def _delete(self, which: int) -> None:
        values = self.values[which]
        if len(values) <= 1:
            return
        at = self._selected(which)
        del values[at]
        self._load(which)
        self._select(which, min(at, len(values) - 1))

    def _select(self, which: int, index: int) -> None:
        self.grids[which].setCurrentCell(0, index) if which == 0 else self.grids[which].setCurrentCell(index, 0)

    def _decimals(self, which: int) -> int:
        live = self.sources[which]
        database = self.window_.database
        if live and database.known(live) and database.parameter(live).decimals is not None:
            return database.parameter(live).decimals
        return 0

    def _init(self, which: int) -> None:
        live = self.sources[which]
        defaults = self.window_.database.default_axis(live) if live and self.window_.database.known(live) else []
        if not defaults:
            return
        self.values[which] = defaults[: self.axis_capacity[which]]
        self._load(which)

    def _pick(self, which: int) -> None:
        window = self.window_
        groups = window._groups(live=True)
        groups = {path: [i for i in ids if window.parameter_visible(i)] for path, ids in groups.items()}
        dialog = ParameterPicker({path: ids for path, ids in groups.items() if ids}, lambda i: window.database.parameter(i).caption, self.sources[which] or None, self)
        if dialog.exec() and dialog.value() is not None:
            self.sources[which] = dialog.value()
            self._load(which)

    def _live_range(self, which: int) -> tuple[float | None, float | None]:
        live, database = self.sources[which], self.window_.database
        if not live or not database.known(live):
            return None, None
        parameter = database.parameter(live)
        return parameter.minimum, parameter.maximum

    def _check(self) -> None:
        self._edited()
        for which, name in enumerate("XY"):
            values = self.values[which]
            if any(b <= a for a, b in zip(values, values[1:])):
                QtWidgets.QMessageBox.warning(self, self.windowTitle(), f"{name} Axis values must rise from low to high.")
                return
            low, high = self._live_range(which)
            if values and ((low is not None and min(values) < low) or (high is not None and max(values) > high)):
                low_text = f"{low:.0f}" if low is not None else "-inf"
                high_text = f"{high:.0f}" if high is not None else "inf"
                QtWidgets.QMessageBox.warning(self, self.windowTitle(), f"{name} Axis values must range between {low_text} and {high_text}")
                return
            if self.sources[which] == 0 and self.original_sources[which] != 0:
                QtWidgets.QMessageBox.warning(self, self.windowTitle(), f"Setting the {name} axis to None is not supported.")
                return
        self.accept()

    def axes(self) -> tuple[list[float], list[float]]:
        return self.values[0], self.values[1]


# ---- the table ------------------------------------------------------------

class TableEditor(QtWidgets.QWidget):
    """A tuning table: PCLink's grid, live position, bulk edits, axis setup and 3D view."""

    last_change: tuple[int, dict[int, float]] | None = None

    def __init__(self, window: "MainWindow", parameter: Parameter, count: int, read_only: bool, attributes: dict[str, str] | None = None) -> None:
        super().__init__()
        self.window_ = window
        self.parameter = parameter
        self.read_only = read_only
        self.attributes = attributes if attributes is not None else {}
        self.tracking = False
        self.cross_hairs = True
        self.caps_large = False
        self.used_rows = self.used_columns = 0
        self.trace: tuple[int, int] | None = None
        columns = parameter.columns if parameter.columns > 1 else count
        self.grid = QtWidgets.QTableWidget(max(count // columns, 1), columns)
        self.grid.setHorizontalHeader(_Header(QtCore.Qt.Orientation.Horizontal, self))
        self.grid.setVerticalHeader(_Header(QtCore.Qt.Orientation.Vertical, self))
        self.grid.setItemDelegate(_CellDelegate(self.grid))
        self.grid.setMinimumHeight(min(90 + self.grid.rowCount() * 30, 480))
        self.grid.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.grid.horizontalHeader().setDefaultSectionSize(58)
        self.grid.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.grid.customContextMenuRequested.connect(lambda at: self.show_menu(self.grid.viewport().mapToGlobal(at)))
        if parameter.column_labels:
            self.grid.setHorizontalHeaderLabels(list(parameter.column_labels)[:columns])
        if parameter.row_labels:
            self.grid.setVerticalHeaderLabels(list(parameter.row_labels)[:self.grid.rowCount()])
        if read_only:
            self.grid.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.grid.itemChanged.connect(self._commit_cell)
        self.grid.installEventFilter(self)
        self.grid.horizontalHeader().sectionDoubleClicked.connect(lambda i: self._edit_axis(parameter.column_axis_id, i))
        self.grid.verticalHeader().sectionDoubleClicked.connect(lambda i: self._edit_axis(parameter.row_axis_id, i))
        self.grid.itemSelectionChanged.connect(self._selection_moved)
        self.surface = SurfaceView()
        self.surface.installEventFilter(self)
        self.surface.cell_clicked.connect(self._surface_selects)
        self.stack = QtWidgets.QStackedWidget()
        self.stack.addWidget(self.grid)
        self.stack.addWidget(self.surface)
        toggle = QtWidgets.QPushButton("Grid / 3D")
        toggle.clicked.connect(self.toggle_graph)
        self.snake = _SnakeOverlay(self.grid, self)
        self.snake.view_enabled = self.flag("SnakeTrail", True)
        toggle.setEnabled(self.grid.rowCount() > 1)
        self.caption = QtWidgets.QLabel()
        self.axis_caption = QtWidgets.QLabel()
        controls = QtWidgets.QHBoxLayout()
        controls.addWidget(self.caption)
        controls.addStretch()
        controls.addWidget(self.axis_caption)
        controls.addWidget(toggle)
        self.side = _SideLabel()
        body = QtWidgets.QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self.side)
        body.addWidget(self.stack, 1)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(controls)
        layout.addLayout(body)
        self.surface.configure(self.flag("GhostBox", False), self.flag("ColourGradient", False), self.flag("KeyRotation", False), self.flag("HingedAxis", False))
        self._themed = None

    # ---- view attributes --------------------------------------------------

    def flag(self, name: str, default: bool = False) -> bool:
        value = self.attributes.get(name)
        return default if value is None else value.lower() == "true"

    def set_flag(self, name: str, on: bool) -> None:
        self.attributes[name] = "true" if on else "false"

    def toggle_graph(self) -> None:
        self.stack.setCurrentIndex(1 - self.stack.currentIndex())
        self.set_flag("GraphDisplay", self.stack.currentIndex() == 1)
        if self.stack.currentIndex() == 1:
            self.surface.setFocus()
        else:
            self.grid.setFocus()

    def _toggle_attribute(self, name: str, default: bool = False) -> None:
        self.set_flag(name, not self.flag(name, default))
        if name == "ColourGradient":
            self.surface.colour_gradient = self.flag(name)
        elif name == "KeyRotation":
            self.surface.key_rotation = self.flag(name)
        self.refresh()
        self.snake.update()

    # ---- cells ------------------------------------------------------------

    def selected_cells(self) -> list[tuple[int, int]]:
        return sorted({(index.row(), index.column()) for index in self.grid.selectedIndexes()})

    def _targets(self) -> list[tuple[int, int]]:
        cells = self.selected_cells()
        if not cells and self.grid.currentRow() >= 0:
            cells = [(self.grid.currentRow(), self.grid.currentColumn())]
        return [cell for cell in cells if self._used(*cell)]

    def _used(self, row: int, column: int) -> bool:
        return not self.grid.isRowHidden(row) and not self.grid.isColumnHidden(column)

    def _cell_value(self, row: int, column: int) -> float:
        return float(self.grid.item(row, column).text())

    def _kind(self) -> str:
        return self.window_.kind_of(self.parameter)

    def _decimals(self) -> int:
        decimals = self.parameter.decimals
        if decimals is None:
            ecu = self.window_.worker.ecu
            scale = ecu.table[self.parameter.data_id].scale if ecu else 1
            decimals = 0 if abs(scale) >= 1 else 3
        return self.window_.units.decimals(decimals, self._kind())

    def _range(self) -> tuple[float | None, float | None]:
        units, kind = self.window_.units, self._kind()
        low = units.to_display(self.parameter.minimum, kind) if self.parameter.minimum is not None else None
        high = units.to_display(self.parameter.maximum, kind) if self.parameter.maximum is not None else None
        return (low, high) if low is None or high is None or low <= high else (high, low)

    def _write_cells(self, changes: dict[tuple[int, int], float]) -> None:
        """Send new display values for cells as one batch, remembering what they were for undo."""
        ecu = self.window_.worker.ecu
        if not changes or ecu is None or self.read_only:
            return
        columns = self.grid.columnCount()
        low, high = self._range()
        units, kind, data_id = self.window_.units, self._kind(), self.parameter.data_id
        cells: dict[int, float] = {}
        before: dict[int, float] = {}
        for (row, column), value in changes.items():
            if low is not None:
                value = max(value, low)
            if high is not None:
                value = min(value, high)
            index = row * columns + column
            cells[index] = units.to_base(round(value, self._decimals() + 2), kind)
            before[index] = ecu.get(data_id, index)
        TableEditor.last_change = (data_id, before)
        self.window_.worker.post("set_cells", data_id, cells)
        self._show_pending(changes, low, high)

    def _show_pending(self, changes: dict[tuple[int, int], float], low: float | None, high: float | None) -> None:
        """Show new values at once so a quick second key press builds on them; the next refresh confirms."""
        decimals = self._decimals()
        self.grid.blockSignals(True)
        for (row, column), value in changes.items():
            item = self.grid.item(row, column)
            if item is not None:
                value = min(max(value, low if low is not None else value), high if high is not None else value)
                item.setText(f"{value:.{decimals}f}" if self.parameter.decimals is not None else f"{value:g}")
        self.grid.blockSignals(False)

    def _write_display(self, value: float, index: int) -> None:
        columns = self.grid.columnCount()
        self._write_cells({divmod(index, columns): value})

    def _apply(self, change, cells: list[tuple[int, int]] | None = None) -> None:
        """Apply a function to each selected cell."""
        targets = self._targets() if cells is None else cells
        self._write_cells({cell: change(self._cell_value(*cell)) for cell in targets})

    def _scope_cells(self, scope: str) -> list[tuple[int, int]]:
        row, column = max(self.grid.currentRow(), 0), max(self.grid.currentColumn(), 0)
        every = [(r, c) for r in range(self.grid.rowCount()) for c in range(self.grid.columnCount()) if self._used(r, c)]
        if scope == "r":
            return [cell for cell in every if cell[0] == row]
        if scope == "c":
            return [cell for cell in every if cell[1] == column]
        if scope == "t":
            return every
        return self._targets()

    def _step(self) -> float:
        ecu = self.window_.worker.ecu
        scale = abs(ecu.table[self.parameter.data_id].scale) if ecu else 1
        if self.parameter.decimals is not None:
            return max(10.0 ** -self.window_.units.decimals(self.parameter.decimals, self._kind()), scale)
        return scale or 1.0

    def _index(self, item: QtWidgets.QTableWidgetItem) -> int:
        return item.row() * self.grid.columnCount() + item.column()

    def _commit_cell(self, item: QtWidgets.QTableWidgetItem) -> None:
        if self.grid.signalsBlocked():
            return
        try:
            value = float(item.text())
        except ValueError:
            self.window_.show_error(f"'{item.text()}' is not a number.")
            self.refresh()
            return
        # Only a real edit may write: the text must differ from what the ECU holds.
        ecu = self.window_.worker.ecu
        decimals = self.parameter.decimals
        held = ecu.get(self.parameter.data_id, self._index(item)) if ecu else None
        held = None if held is None else self.window_.units.to_display(held, self._kind())
        decimals = None if decimals is None else self.window_.units.decimals(decimals, self._kind())
        shown = None if held is None else (f"{held:.{decimals}f}" if decimals is not None else f"{held:g}")
        if item.text() != shown:
            self._write_display(value, self._index(item))

    # ---- editing keys -----------------------------------------------------

    def _enter_value(self, text: str, caret: int | None = None) -> None:
        if self.read_only or not self._targets():
            return
        low, high = self._range()
        dialog = EnterValueDialog(text, self, self._decimals(), low, high, caret=caret)
        if not dialog.exec():
            return
        change = dialog.change()
        if change is not None:
            self._apply(change.apply, self._scope_cells(change.scope))

    def _small_step(self, large: bool) -> float:
        return self._step() * (LARGE_STEPS if large != self.caps_large else 1)

    def _nudge(self, sign: int, large: bool, mode: str) -> None:
        step = sign * self._small_step(large)
        cells = self._targets()
        if mode == "row":
            rows = {row for row, _ in cells}
            cells = [(r, c) for r in sorted(rows) for c in range(self.grid.columnCount()) if self._used(r, c)]
        elif mode == "column":
            columns = {column for _, column in cells}
            cells = [(r, c) for c in sorted(columns) for r in range(self.grid.rowCount()) if self._used(r, c)]
        self._apply(lambda value: value + step, cells)

    def _copy_direction(self, key: int) -> None:
        dr, dc = ARROWS[key]
        values = {cell: self._cell_value(*cell) for cell in self._targets()}
        changes = {}
        for (row, column), value in values.items():
            target = (row + dr, column + dc)
            if 0 <= target[0] < self.grid.rowCount() and 0 <= target[1] < self.grid.columnCount() and self._used(*target):
                changes[target] = value
        self._write_cells(changes)

    def _fill(self, key: int) -> None:
        dr, dc = ARROWS[key]
        changes = {}
        for row, column in self._targets():
            value = self._cell_value(row, column)
            r, c = row + dr, column + dc
            while 0 <= r < self.grid.rowCount() and 0 <= c < self.grid.columnCount():
                if self._used(r, c):
                    changes[(r, c)] = value
                r, c = r + dr, c + dc
        self._write_cells(changes)

    def _fill_right(self) -> None:
        self._fill(KEYS.Key_Right)

    def _extrapolate(self, key: int) -> None:
        """Carry the trend of the last two cells of the selection on to the edge."""
        dr, dc = ARROWS[key]
        changes = {}
        for row, column in self._targets():
            back = (row - dr, column - dc)
            if not (0 <= back[0] < self.grid.rowCount() and 0 <= back[1] < self.grid.columnCount() and self._used(*back)):
                continue
            here = self._cell_value(row, column)
            step = here - self._cell_value(*back)
            r, c, value = row + dr, column + dc, here
            while 0 <= r < self.grid.rowCount() and 0 <= c < self.grid.columnCount():
                value += step
                if self._used(r, c):
                    changes[(r, c)] = value
                r, c = r + dr, c + dc
        self._write_cells(changes)

    def _mark(self, bit: int) -> None:
        self.window_.marks.toggle(self.parameter.data_id, self._targets(), bit)
        self.refresh()

    def _clear_markers(self) -> None:
        self.window_.marks.clear_table(self.parameter.data_id)
        self.refresh()

    def _clear_all_markers(self) -> None:
        self.window_.marks.clear_all()
        self.window_.form.refresh()

    def _interpolate_selection(self, by_row: bool, by_column: bool) -> None:
        """Straight lines between the corners of the selection."""
        cells = self._targets()
        if len(cells) < 2:
            return
        rows = sorted({row for row, _ in cells})
        columns = sorted({column for _, column in cells})
        top, bottom, left, right = rows[0], rows[-1], columns[0], columns[-1]
        value = self._cell_value
        changes = {}
        for row in rows:
            for column in columns:
                fx = (column - left) / ((right - left) or 1) if by_column else 0.0
                fy = (row - top) / ((bottom - top) or 1) if by_row else 0.0
                base_row, base_column = (row if not by_row else top), (column if not by_column else left)
                upper = value(base_row, left if by_column else base_column) + ((value(base_row, right) - value(base_row, left)) * fx if by_column else 0.0)
                lower = value(bottom if by_row else base_row, left if by_column else base_column) + ((value(bottom if by_row else base_row, right) - value(bottom if by_row else base_row, left)) * fx if by_column else 0.0)
                changes[(row, column)] = upper + (lower - upper) * fy
        self._write_cells(changes)

    def _smooth_selection(self) -> None:
        changes = {}
        for row, column in self._targets():
            around = [
                self._cell_value(r, c)
                for r in range(row - 1, row + 2) for c in range(column - 1, column + 2)
                if 0 <= r < self.grid.rowCount() and 0 <= c < self.grid.columnCount()
                and self.grid.item(r, c) is not None and self._used(r, c)
            ]
            changes[(row, column)] = sum(around) / len(around)
        self._write_cells(changes)

    def _quick_trim(self) -> None:
        window, ecu = self.window_, self.window_.worker.ecu
        if self.parameter.data_id not in (tune.FUEL_TABLE_1, tune.FUEL_TABLE_2) or ecu is None or self.read_only:
            return

        def live(data_id: int) -> float:
            return ecu.get(data_id) if data_id in ecu.table else 0.0

        correction = live(tune.CL_CORRECTION) if live(tune.CL_MODE) else 0.0
        from . import tuningtools

        dialog = tuningtools.QuickTrimDialog(live(tune.LAMBDA_TARGET_RUNTIME), live(tune.LAMBDA_AVERAGE), correction, self, tuningtools.unit_system(window))
        if dialog.exec():
            self._apply(lambda value: tune.quick_trim_value(value, dialog.measured_value(), dialog.target_value(), dialog.correction.value()))
            window.statusBar().showMessage("Quick Trim applied.", 4000)

    def _undo(self) -> None:
        last, ecu = TableEditor.last_change, self.window_.worker.ecu
        if last is None or ecu is None or self.read_only:
            return
        data_id, before = last
        TableEditor.last_change = None
        self.window_.worker.post("set_cells", data_id, before)

    # ---- copy, paste, export, import --------------------------------------

    def _copy(self) -> None:
        cells = self.grid.selectedIndexes()
        if not cells:
            return
        rows = sorted({cell.row() for cell in cells})
        columns = sorted({cell.column() for cell in cells})
        text = "\n".join(
            "\t".join(self.grid.item(r, c).text() if self.grid.item(r, c) else "" for c in columns) for r in rows
        )
        QtWidgets.QApplication.clipboard().setText(text)

    def _paste(self) -> None:
        current = self.grid.currentIndex()
        if not current.isValid():
            return
        try:
            rows = [
                [float(cell) for cell in line.split("\t")]
                for line in QtWidgets.QApplication.clipboard().text().strip().splitlines()
            ]
        except ValueError:
            self.window_.show_error("The clipboard does not hold a block of numbers.")
            return
        changes = {}
        for r, values in enumerate(rows):
            for c, value in enumerate(values):
                row, column = current.row() + r, current.column() + c
                if row < self.grid.rowCount() and column < self.grid.columnCount() and self._used(row, column):
                    changes[(row, column)] = value
        self._write_cells(changes)

    def export_text(self) -> str:
        """The whole table with its axes, tab separated."""
        ecu = self.window_.worker.ecu
        lines = []
        columns = [c for c in range(self.grid.columnCount()) if not self.grid.isColumnHidden(c)]
        header = [self.grid.horizontalHeaderItem(c).text() if self.grid.horizontalHeaderItem(c) else "" for c in columns]
        lines.append("\t".join([self.parameter.caption] + header))
        for row in range(self.grid.rowCount()):
            if self.grid.isRowHidden(row):
                continue
            label = self.grid.verticalHeaderItem(row).text() if self.grid.verticalHeaderItem(row) else str(row + 1)
            lines.append("\t".join([label] + [self.grid.item(row, c).text() for c in columns]))
        return "\n".join(lines) if ecu else ""

    def import_text(self, text: str) -> bool:
        """Cells from exported text. Axis labels are only checked for shape; the axes are not changed."""
        rows = [line.split("\t") for line in text.strip().splitlines()]
        try:
            numbers = [[float(cell) for cell in row[1:]] for row in rows[1:]]
        except ValueError:
            return False
        changes = {}
        for r, values in enumerate(numbers):
            for c, value in enumerate(values):
                if r < self.grid.rowCount() and c < self.grid.columnCount():
                    changes[(r, c)] = value
        self._write_cells(changes)
        return bool(changes)

    def export_clipboard(self) -> None:
        QtWidgets.QApplication.clipboard().setText(self.export_text())

    def import_clipboard(self) -> None:
        if not self.import_text(QtWidgets.QApplication.clipboard().text()):
            self.window_.show_error("The clipboard does not hold an exported table.")

    def export_file(self) -> None:
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Export table", f"{self.parameter.caption}.txt", "Text (*.txt)")
        if path:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(self.export_text())

    def import_file(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Import table", "", "Text (*.txt)")
        if path:
            with open(path, encoding="utf-8") as handle:
                if not self.import_text(handle.read()):
                    self.window_.show_error("That file does not hold an exported table.")

    # ---- axis setup -------------------------------------------------------

    def _axis_ids(self) -> tuple[int | None, int | None]:
        return self.parameter.column_axis_id, self.parameter.row_axis_id

    def _axis_setup(self) -> None:
        ecu = self.window_.worker.ecu
        ids = self._axis_ids()
        if ecu is None or self.read_only or not any(i in ecu.table for i in ids):
            return
        x_axis, y_axis = (ecu.get_all(i) if i in ecu.table else [] for i in ids)
        dialog = AxisSetupDialog(self, x_axis, y_axis)
        if not dialog.exec():
            return
        self.apply_axes(dialog, x_axis, y_axis)

    def apply_axes(self, dialog: AxisSetupDialog, x_axis: list[float], y_axis: list[float], interpolate: bool | None = None) -> None:
        ecu = self.window_.worker.ecu
        ids = self._axis_ids()
        sources = (self.parameter.column_source_id, self.parameter.row_source_id)
        new_axes = []
        for axis_id, source, old, used, chosen, original in zip(ids, sources, (x_axis, y_axis), dialog.axes(), dialog.sources, dialog.original_sources):
            if source is not None and source in ecu.table and chosen and chosen != original:
                self.window_.write(source, chosen, 0)
            new_axes.append(self._axis_block(old, used) if axis_id in ecu.table else old)
        if new_axes != [x_axis, y_axis]:
            for axis_id, new in zip(ids, new_axes):
                if axis_id in ecu.table:
                    self.window_.worker.post("set_cells", axis_id, dict(enumerate(new)))
        if interpolate is None:
            interpolate = QtWidgets.QMessageBox.question(
                self, dialog.windowTitle(),
                "Interpolate table values?\n\nThis will automatically correct the tables values\nfor altered or new rows and columns.",
            ) == QtWidgets.QMessageBox.StandardButton.Yes
        if interpolate and new_axes != [x_axis, y_axis]:
            self._reinterpolate(x_axis, y_axis, *new_axes)

    @staticmethod
    def _axis_block(old: list[float], used: list[float]) -> list[float]:
        """The whole axis as stored: the used breakpoints, then spare entries that do not rise."""
        spare = [value for value in old[len(used):] if value <= used[-1]] if used else []
        block = list(used) + spare
        return (block + [used[-1] if used else 0.0] * len(old))[: len(old)]

    def _reinterpolate(self, old_x: list[float], old_y: list[float], new_x: list[float], new_y: list[float]) -> None:
        """Keep the table's shape when its axes move; the whole table goes out in one write."""
        ecu = self.window_.worker.ecu
        values = ecu.get_all(self.parameter.data_id)
        columns = self.grid.columnCount()
        old_x = old_x[: used_axis_length(old_x)] if old_x else [0.0]
        old_y = old_y[: used_axis_length(old_y)] if old_y else [0.0]
        used_x = new_x[: used_axis_length(new_x)] if new_x else [0.0]
        used_y = new_y[: used_axis_length(new_y)] if new_y else [0.0]
        old_grid = [values[r * columns + c] for r in range(len(old_y)) for c in range(len(old_x))]
        cells = dict(enumerate(values))
        for row, y in enumerate(used_y):
            for column, x in enumerate(used_x):
                cells[row * columns + column] = tune.bilinear(old_x, old_y, old_grid, x, y)
        self.window_.worker.post("set_cells", self.parameter.data_id, cells)

    def _edit_axis(self, axis_id: int | None, index: int) -> None:
        ecu = self.window_.worker.ecu
        if self.read_only or axis_id is None or ecu is None or axis_id not in ecu.table:
            return
        current = ecu.get(axis_id, index)
        dialog = EnterValueDialog(f"={current:g}", self, 0, scopes=False, selected=True)
        if dialog.exec() and dialog.change() is not None and dialog.change().number != current:
            self.window_.write(axis_id, dialog.change().number, index)

    # ---- context menu -----------------------------------------------------

    def show_menu(self, at: QtCore.QPoint | None = None) -> None:
        if at is None:
            cell = self.grid.visualRect(self.grid.currentIndex()).center()
            at = self.grid.viewport().mapToGlobal(cell)
        self.build_menu().exec(at)

    def build_menu(self) -> QtWidgets.QMenu:
        menu = QtWidgets.QMenu(self)
        window = self.window_
        editing = not self.read_only

        def add(target, text, handler, shortcut="", enabled=True, checked=None):
            action = target.addAction(text)
            if shortcut:
                action.setText(f"{text}\t{shortcut}")
            if checked is not None:
                action.setCheckable(True)
                action.setChecked(checked)
            action.setEnabled(enabled)
            action.triggered.connect(lambda _checked=False: handler())
            return action

        add(menu, "What's this?", lambda: window.show_help(), "F1")
        menu.addSeparator()
        add(menu, "Axis Setup", self._axis_setup, "X", editing)
        add(menu, "Quick Trim", self._quick_trim, "M", editing and self.parameter.data_id in (tune.FUEL_TABLE_1, tune.FUEL_TABLE_2))
        transfer = menu.addMenu("Import/Export")
        add(transfer, "Export to Clipboard", self.export_clipboard)
        add(transfer, "Export to File", self.export_file)
        add(transfer, "Import from Clipboard", self.import_clipboard, "", editing)
        add(transfer, "Import from File", self.import_file, "", editing)
        markers = menu.addMenu("Cell Markers")
        first = self._targets()[:1]
        flags = window.marks.flag(self.parameter.data_id, *first[0]) if first else 0
        add(markers, "Clear Tuning Table Markers", self._clear_markers, "Shift+M")
        add(markers, "Clear All Table Markers", self._clear_all_markers, "F9")
        add(markers, "Runtime Marker", lambda: self._mark(tune.RUNTIME_MARKER), "N", checked=bool(flags & tune.RUNTIME_MARKER))
        add(markers, "Part Tuned", lambda: self._mark(tune.PART_TUNED), "Y", checked=bool(flags & tune.PART_TUNED))
        add(markers, "Tuned", lambda: self._mark(tune.TUNED), "Z", checked=bool(flags & tune.TUNED))
        add(menu, "Undo Short Term Changes", window.undo_changes)
        menu.addSeparator()
        for title, scope in (("Selection", ""), ("Row", "r"), ("Column", "c"), ("Table", "t")):
            sub = menu.addMenu(title)
            add(sub, "To Value", lambda s=scope: self._enter_value("=" + s, 1), "", editing)
            add(sub, "Offset by Value", lambda s=scope: self._enter_value("+" + s, 1), "", editing)
            add(sub, "Offset by Percentage", lambda s=scope: self._enter_value("+%" + s, 1), "", editing)
        menu.addSeparator()
        copy = menu.addMenu("Cell Copy")
        for name, key in (("Up", KEYS.Key_Up), ("Down", KEYS.Key_Down), ("Left", KEYS.Key_Left), ("Right", KEYS.Key_Right)):
            add(copy, f"Copy {name}", lambda k=key: self._copy_direction(k), f"Ctrl+{name}", editing)
        fill = menu.addMenu("Fill")
        for name, key in (("Up", KEYS.Key_Up), ("Down", KEYS.Key_Down), ("Left", KEYS.Key_Left), ("Right", KEYS.Key_Right)):
            add(fill, f"Fill {name}", lambda k=key: self._fill(k), "Ctrl+F" if name == "Right" else "", editing)
        interpolate = menu.addMenu("Interpolate")
        add(interpolate, "Interpolate Bi-Direction", lambda: self._interpolate_selection(True, True), "Ctrl+I", editing)
        add(interpolate, "Interpolate Vertical", lambda: self._interpolate_selection(True, False), "Shift+V", editing)
        add(interpolate, "Interpolate Horizontal", lambda: self._interpolate_selection(False, True), "Shift+H", editing)
        extrapolate = menu.addMenu("Extrapolate")
        for name, key in (("Up", KEYS.Key_Up), ("Down", KEYS.Key_Down), ("Left", KEYS.Key_Left), ("Right", KEYS.Key_Right)):
            add(extrapolate, f"Extrapolate {name}", lambda k=key: self._extrapolate(k), "", editing)
        add(menu, "Smooth Selection", self._smooth_selection, "O", editing)
        menu.addSeparator()
        add(menu, "Copy Selection", self._copy, "Ctrl+C")
        add(menu, "Paste Selection", self._paste, "Ctrl+V", editing)
        menu.addSeparator()
        snake = menu.addMenu("Snake Trail")
        add(snake, "Snake Visible", self._toggle_snake_visible, "Ctrl+,", checked=self.snake.view_enabled)
        add(snake, "Snake Paused", self._toggle_snake_paused, "Ctrl+.", checked=self.snake.view_paused)
        add(menu, "Colour Gradient Visible", lambda: self._toggle_attribute("ColourGradient"), "H", checked=self.flag("ColourGradient"))
        add(menu, "Logged Value Cross Hairs", self._toggle_cross_hairs, checked=self.cross_hairs)
        tracking = menu.addMenu("Active Cell Tracking")
        add(tracking, "Active Cell Tracking", self._toggle_tracking, "F5", checked=self.tracking)
        add(tracking, "Jump To Active Cell", self._goto_active, "Space")
        add(menu, "Reverse Load Axis", self._toggle_reverse, checked=self.grid.verticalHeader().visualIndex(0) != 0)
        menu.addSeparator()
        graph = self.stack.currentIndex() == 1
        add(menu, "Switch to table view" if graph else "Switch to graphic view", self.toggle_graph, "K", self.grid.rowCount() > 1)
        return menu

    def _toggle_cross_hairs(self) -> None:
        self.cross_hairs = not self.cross_hairs
        self.snake.update()

    def _toggle_snake_visible(self) -> None:
        self.snake.view_enabled = not self.snake.view_enabled
        self.set_flag("SnakeTrail", self.snake.view_enabled)
        if not self.snake.view_enabled:
            self.snake.points.clear()
        self.snake.update()

    def _toggle_snake_paused(self) -> None:
        self.snake.view_paused = not self.snake.view_paused

    def _toggle_tracking(self) -> None:
        self.tracking = not self.tracking
        if self.tracking:
            self._goto_active()

    def _toggle_reverse(self) -> None:
        header = self.grid.verticalHeader()
        reversed_now = header.visualIndex(0) != 0
        order = range(self.grid.rowCount()) if reversed_now else reversed(range(self.grid.rowCount()))
        for visual, logical in enumerate(order):
            header.moveSection(header.visualIndex(logical), visual)

    def _goto_active(self) -> None:
        if self.trace is not None:
            self.grid.setCurrentCell(*self.trace)

    # ---- keys -------------------------------------------------------------

    def eventFilter(self, watched: QtCore.QObject, event: QtCore.QEvent) -> bool:
        if event.type() != QtCore.QEvent.Type.KeyPress:
            return False
        if watched is self.grid and self.grid.state() == self.grid.State.EditingState:
            return False
        return self._key(event, watched is self.surface)

    def _key(self, event: QtGui.QKeyEvent, on_surface: bool) -> bool:
        key = event.key()
        modifiers = event.modifiers()
        control = bool(modifiers & MODS.ControlModifier)
        shift = bool(modifiers & MODS.ShiftModifier)
        alt = bool(modifiers & MODS.AltModifier)
        keypad = bool(modifiers & MODS.KeypadModifier)
        if key == KEYS.Key_CapsLock:
            self.caps_large = not self.caps_large
            return False
        if event.matches(QtGui.QKeySequence.StandardKey.Copy):
            self._copy()
            return True
        if control and shift and key == KEYS.Key_C:
            self.export_clipboard()
            return True
        if key == KEYS.Key_Backslash:
            self._toggle_attribute("KeyRotation")
            return True
        if key == KEYS.Key_Semicolon:
            self.surface.reset_view()
            return True
        if key == KEYS.Key_K:
            if self.grid.rowCount() > 1:
                self.toggle_graph()
            return True
        if key == KEYS.Key_Space:
            self._goto_active()
            return True
        if key == KEYS.Key_F5:
            self._toggle_tracking()
            return True
        if control and key in (KEYS.Key_Period, KEYS.Key_Comma):
            self._toggle_snake_paused() if key == KEYS.Key_Period else self._toggle_snake_visible()
            return True
        if on_surface and key in ARROWS and self.surface.key_rotation:
            self.surface.rotate_key(key, shift)
            return True
        if on_surface and key in ARROWS:
            QtWidgets.QApplication.sendEvent(self.grid, QtGui.QKeyEvent(event.type(), key, modifiers))
            return True
        if control and key == KEYS.Key_T:
            self.show_menu()
            return True
        if key in (KEYS.Key_Home, KEYS.Key_End):
            self._jump(key, control, shift)
            return True
        if self.read_only:
            return False
        return self._edit_key(event, key, control, shift, alt, keypad)

    def _jump(self, key: int, column_wise: bool, extend: bool) -> None:
        row, column = max(self.grid.currentRow(), 0), max(self.grid.currentColumn(), 0)
        used_columns = [c for c in range(self.grid.columnCount()) if not self.grid.isColumnHidden(c)]
        used_rows = [r for r in range(self.grid.rowCount()) if not self.grid.isRowHidden(r)]
        if column_wise:
            row = used_rows[0] if key == KEYS.Key_Home else used_rows[-1]
        else:
            column = used_columns[0] if key == KEYS.Key_Home else used_columns[-1]
        if extend:
            first = self.selected_cells()[0] if self.selected_cells() else (row, column)
            self.grid.setCurrentCell(row, column, QtCore.QItemSelectionModel.SelectionFlag.NoUpdate)
            self.grid.clearSelection()
            self.grid.setRangeSelected(QtWidgets.QTableWidgetSelectionRange(min(first[0], row), min(first[1], column), max(first[0], row), max(first[1], column)), True)
        else:
            self.grid.setCurrentCell(row, column)

    def _edit_key(self, event: QtGui.QKeyEvent, key: int, control: bool, shift: bool, alt: bool, keypad: bool) -> bool:
        if event.matches(QtGui.QKeySequence.StandardKey.Paste):
            self._paste()
            return True
        if control and shift and key == KEYS.Key_V:
            self.import_clipboard()
            return True
        if control and key == KEYS.Key_Z:
            self._undo()
            return True
        if control and key in ARROWS:
            self._copy_direction(key)
            return True
        letters = {
            KEYS.Key_N: lambda: self._mark(tune.RUNTIME_MARKER),
            KEYS.Key_Y: lambda: self._mark(tune.PART_TUNED),
            KEYS.Key_Z: lambda: self._mark(tune.TUNED),
            KEYS.Key_X: self._axis_setup,
            KEYS.Key_M: self._quick_trim,
            KEYS.Key_O: self._smooth_selection,
            KEYS.Key_H: lambda: self._toggle_attribute("ColourGradient"),
        }
        up = key in (KEYS.Key_Q, KEYS.Key_PageUp)
        down = key in (KEYS.Key_A, KEYS.Key_PageDown)
        if up or down:
            mode = "row" if control else "column" if alt else "cell"
            self._nudge(1 if up else -1, shift, mode)
            return True
        if control and key == KEYS.Key_I:
            self._interpolate_selection(True, True)
        elif control and key == KEYS.Key_F:
            self._fill_right()
        elif shift and key == KEYS.Key_H:
            self._interpolate_selection(False, True)
        elif shift and key == KEYS.Key_V:
            self._interpolate_selection(True, False)
        elif shift and key == KEYS.Key_M:
            self._clear_markers()
        elif control or alt:
            return False
        elif not shift and key in letters:
            letters[key]()
        elif key == KEYS.Key_F7:
            self.window_.open_table_entry(tune.FUEL_TABLE_1 if self.parameter.data_id != tune.FUEL_TABLE_1 else 2366)
        elif key == KEYS.Key_Plus and keypad:
            self._enter_value("+", 1)
        elif key == KEYS.Key_Minus and keypad:
            self._enter_value("-", 1)
        elif key == KEYS.Key_Asterisk:
            self._enter_value("*", 1)
        elif key == KEYS.Key_Slash:
            self._enter_value("/", 1)
        elif key == KEYS.Key_Percent:
            self._enter_value("+%", 1)
        elif key in (KEYS.Key_Plus, KEYS.Key_Equal):
            self._enter_value("=")
        elif key == KEYS.Key_Minus:
            self._enter_value("=-")
        elif KEYS.Key_0 <= key <= KEYS.Key_9 and not shift:
            self._enter_value("=" + chr(key))
        else:
            return False
        return True

    # ---- showing the ECU's values ----------------------------------------

    def line_is_selected(self, orientation: QtCore.Qt.Orientation, index: int) -> bool:
        cells = self.selected_cells()
        if orientation == QtCore.Qt.Orientation.Horizontal:
            return any(column == index for _, column in cells)
        return any(row == index for row, _ in cells)

    def _selection_moved(self) -> None:
        self.grid.horizontalHeader().viewport().update()
        self.grid.verticalHeader().viewport().update()
        self.snake.update()
        self._publish_cursor()

    def _surface_selects(self, row: int, column: int, row_end: int, column_end: int) -> None:
        self.grid.clearSelection()
        self.grid.setCurrentCell(row, column, QtCore.QItemSelectionModel.SelectionFlag.NoUpdate)
        self.grid.setRangeSelected(QtWidgets.QTableWidgetSelectionRange(min(row, row_end), min(column, column_end), max(row, row_end), max(column, column_end)), True)

    def _theme(self) -> None:
        theme = self.window_.theme
        if self._themed is theme:
            return
        self._themed = theme
        line = theme.colour("GridLines").darker(180).name()
        self.grid.setStyleSheet(
            f"QTableWidget {{ background:{theme.colour('DataAreaBackground').name()}; gridline-color:{line}; }}"
            f"QTableCornerButton::section {{ background:{theme.colour('GridLines').name()}; border:1px solid {theme.colour('DataAreaBackground').name()}; }}"
        )
        self.surface.set_theme(theme)

    def _cell_look(self, ecu, row: int, column: int, index: int, value: float, low: float, high: float) -> tuple[QtGui.QColor, QtGui.QColor]:
        theme = self.window_.theme
        marked = self.window_.cell_colour(self.parameter.data_id, row, column)
        base = theme.colour("DataAreaBackground")
        text = theme.colour("ViewText")
        if marked is not None:
            colour = marked
        elif self.trace == (row, column):
            colour = theme.colour("Active Cell")
        elif ecu.is_changed(self.parameter.data_id, index):
            colour = theme.colour("ValueChanged")
        elif self.flag("ColourGradient"):
            colour = _gradient(theme, value, low, high)
        else:
            return base, text
        return colour, QtGui.QColor(0, 0, 0) if colour.lightness() > 150 else QtGui.QColor(255, 255, 255)

    def refresh(self) -> None:
        ecu = self.window_.worker.ecu
        if ecu is None:
            return
        self._theme()
        kind = self._kind()
        units = self.window_.units
        values = [units.to_display(value, kind) for value in ecu.get_all(self.parameter.data_id)]
        self.grid.blockSignals(True)
        decimals = self.parameter.decimals
        decimals = None if decimals is None else units.decimals(decimals, kind)
        low, high = (min(values), max(values)) if values else (0.0, 0.0)
        for index, value in enumerate(values):
            row, column = divmod(index, self.grid.columnCount())
            text = f"{value:.{decimals}f}" if decimals is not None else f"{value:g}"
            cell = self.grid.item(row, column)
            if cell is None:
                cell = QtWidgets.QTableWidgetItem(text)
                cell.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
                self.grid.setItem(row, column, cell)
            elif cell.text() != text and not (self.grid.currentItem() is cell and self.grid.isPersistentEditorOpen(cell)):
                cell.setText(text)
            background, foreground = self._cell_look(ecu, row, column, index, value, low, high)
            cell.setBackground(background)
            cell.setForeground(foreground)
        for axis_id, size, horizontal in (
            (self.parameter.column_axis_id, self.grid.columnCount(), True),
            (self.parameter.row_axis_id, self.grid.rowCount(), False),
        ):
            if axis_id in ecu.table:
                breakpoints = ecu.get_all(axis_id)[:size]
                labels = [f"{value:g}" for value in breakpoints]
                used = used_axis_length(breakpoints)
                if horizontal:
                    self.grid.setHorizontalHeaderLabels(labels)
                    self.used_columns = used
                else:
                    self.grid.setVerticalHeaderLabels(labels)
                    self.used_rows = used
                for index in range(size):
                    (self.grid.setColumnHidden if horizontal else self.grid.setRowHidden)(index, index >= used)
        self.grid.blockSignals(False)
        self._titles(ecu)
        columns = self.grid.columnCount()
        rows_used, columns_used = self.used_rows or self.grid.rowCount(), self.used_columns or columns
        self.surface.set_values([values[r * columns + c] for r in range(rows_used) for c in range(columns_used)], columns_used)
        self._publish_cursor()
        self.update_trace()

    def _axis_title(self, ecu, source_id: int | None) -> str:
        window = self.window_
        if source_id is None or source_id not in ecu.table:
            return ""
        live = int(ecu.get(source_id))
        if not live or not window.database.known(live):
            return ""
        parameter = window.database.parameter(live)
        units = window.units_of(parameter)
        return f"{parameter.caption} ({units})" if units else parameter.caption

    def _titles(self, ecu) -> None:
        units = self.window_.units_of(self.parameter)
        self.caption.setText(f"{self.parameter.caption} ({units})" if units else self.parameter.caption)
        self.axis_caption.setText(self._axis_title(ecu, self.parameter.column_source_id))
        self.side.set_text(self._axis_title(ecu, self.parameter.row_source_id))

    def _publish_cursor(self) -> None:
        cells = self.selected_cells()
        self.surface.set_selection(cells)
        row, column = (self.grid.currentRow(), self.grid.currentColumn()) if self.grid.currentRow() >= 0 else (0, 0)
        ecu = self.window_.worker.ecu
        lines = []
        if ecu is not None and self.grid.item(row, column) is not None:
            for title, axis_id, at in (
                (self.side.text, self.parameter.row_axis_id, row),
                (self.axis_caption.text(), self.parameter.column_axis_id, column),
            ):
                if title and axis_id in ecu.table:
                    lines.append(f"{title}: {ecu.get(axis_id, at):g}")
            units = self.window_.units_of(self.parameter)
            lines.append(f"Value ({units}): {self.grid.item(row, column).text()}" if units else f"Value: {self.grid.item(row, column).text()}")
        self.surface.set_info(lines)

    def _live_fraction(self) -> tuple[float, float] | None:
        """Where the engine is between cells, for the cross hairs; the cell's centre if unknown."""
        ecu = self.window_.worker.ecu
        parameter = self.parameter
        if ecu is None or parameter.column_source_id is None:
            return None

        def along(source_id, axis_id, size):
            if source_id not in ecu.table or axis_id not in ecu.table:
                return None
            live = int(ecu.get(source_id))
            if live not in ecu.table or live in SECRET_IDS:
                return None
            axis = ecu.get_all(axis_id)[:size]
            axis = axis[: used_axis_length(axis)]
            value = ecu.get(live)
            if len(axis) < 2 or value <= axis[0]:
                return 0.0
            if value >= axis[-1]:
                return float(len(axis) - 1)
            at = bisect.bisect_right(axis, value) - 1
            return at + (value - axis[at]) / ((axis[at + 1] - axis[at]) or 1.0)

        try:
            column = along(parameter.column_source_id, parameter.column_axis_id, self.grid.columnCount())
            row = along(parameter.row_source_id, parameter.row_axis_id, self.grid.rowCount()) if self.grid.rowCount() > 1 else 0.0
        except (EcuProtected, KeyError, ValueError, IndexError):
            return None
        return (row, column) if row is not None and column is not None else None

    def update_trace(self) -> None:
        """Mark the cell the engine is using now."""
        position = self.window_.table_position(self.parameter, self.grid.rowCount(), self.grid.columnCount())
        if position == self.trace:
            return
        previous, self.trace = self.trace, position
        ecu = self.window_.worker.ecu
        if ecu is not None:
            self._recolour(ecu, previous)
            self._recolour(ecu, position)
        fraction = self._live_fraction() if position else None
        self.snake.cross = fraction if fraction is not None else (float(position[0]), float(position[1])) if position else None
        self.snake.update()
        self.surface.set_trace(self.snake.cross)
        if position and position != previous:
            self.snake.add(position)
            if self.tracking:
                self.grid.setCurrentCell(*position)

    def _recolour(self, ecu, spot: tuple[int, int] | None) -> None:
        # Changing a colour fires itemChanged, which must never become an ECU write.
        if not spot or self.grid.item(*spot) is None:
            return
        row, column = spot
        index = row * self.grid.columnCount() + column
        values = [self._cell_value(r, c) for r in range(self.grid.rowCount()) for c in range(self.grid.columnCount()) if self.grid.item(r, c)]
        self.grid.blockSignals(True)
        cell = self.grid.item(row, column)
        background, foreground = self._cell_look(ecu, row, column, index, float(cell.text()), min(values), max(values))
        cell.setBackground(background)
        cell.setForeground(foreground)
        self.grid.blockSignals(False)


def _gradient(theme, value: float, low: float, high: float) -> QtGui.QColor:
    """Gradient Low through Middle to High across a table's own range."""
    stops = [theme.colour("GradientLow"), theme.colour("GradientMiddle"), theme.colour("GradientHigh")]
    if high <= low:
        return stops[1]
    position = min(max((value - low) / (high - low), 0.0), 1.0) * 2
    index = min(int(position), 1)
    blend = position - index
    a, b = stops[index], stops[index + 1]
    return QtGui.QColor(*(round(getattr(a, name)() + (getattr(b, name)() - getattr(a, name)()) * blend) for name in ("red", "green", "blue")))
