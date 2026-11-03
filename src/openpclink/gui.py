"""Desktop tuning interface. All ECU traffic runs on one worker thread."""
from __future__ import annotations

import os
import queue
import sys
import time
from collections import defaultdict
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from . import __version__
from .crypto import BadContainer, ProtectedFile
from .database import MENU_ORDER, Database, DatabaseMissing, HelpManual, Parameter
from . import assets, gauges, tools, tune
from .assets import RuntimeConfig
from .diagnostics import enable_log_file
from .ecu import ENGINE_SPEED_ID, SECRET_IDS, TPS_CLOSED_ID, TPS_OPEN_ID, Ecu, EcuProtected, StoreUnconfirmed, WriteMismatch, WriteRefused
from .eculog import EcuLogError
from .gauges import GAUGE_TYPES, Gauge, Reading
from .importer import ImportFailed, import_pclink, rules_table_path
from .layoutfile import GROUP_ALL, Layout, LayoutError, LayoutPage, LayoutView
from .logfile import LogFormatError, PcLogger, read_log, save_log
from .logviews import LOG_VIEW_TYPES, LogSession, MixtureMapView, VehiclePlan
from .options import Options, OptionsDialog
from .theme import theme_named
from .units import UnitSystem
from .workspace import LiveView, ViewFrame, Workspace
from .mapfile import MapError, MapFile, OfflineEcu, compare
from .protocol import READ_ONLY_ENV, EcuError, ForbiddenCommand, ProtocolError
from .native_rules import NativeRules, RulesTable
from .views import SurfaceView, axis_cell, heat_colour, used_axis_length
from .transports import TransportError, list_ports, open_transport

POLL_INTERVAL_S = 0.1
ID_ROLE = QtCore.Qt.ItemDataRole.UserRole
MANY_OPTIONS = 12
SNAKE_LENGTH = 64
PLAYBACK_INTERVAL_MS = 25
IGNITION_TABLE_ID = 2366
GAUGE_CAPTIONS = ("Engine Speed", "MAP", "ECT", "IAT", "Batt Voltage", "TPS (Main)", "Lambda 1", "Ignition Angle")


def used_axis(ecu, axis_id: int | None) -> list[float]:
    if axis_id not in ecu.table:
        return []
    axis = ecu.get_all(axis_id)
    return axis[: used_axis_length(axis)]


class Worker(QtCore.QThread):
    connected = QtCore.Signal(object, bool)
    disconnected = QtCore.Signal(str)
    runtime = QtCore.Signal()
    wrote = QtCore.Signal(int, int)
    failed = QtCore.Signal(str)
    stored = QtCore.Signal()
    controlled = QtCore.Signal(str)
    called = QtCore.Signal(object, object, object)
    rules_status = QtCore.Signal(str)
    rules_messages = QtCore.Signal(list)
    visibility_changed = QtCore.Signal()

    def __init__(self, database: Database) -> None:
        super().__init__()
        self.rules: NativeRules | None = None
        self.rules_table: RulesTable | None = None
        self.database = database
        self.ecu: Ecu | OfflineEcu | None = None
        self.jobs: queue.Queue = queue.Queue()
        self.logger: PcLogger | None = None

    def post(self, *job) -> None:
        self.jobs.put(job)

    def run(self) -> None:
        while True:
            try:
                job = self.jobs.get(timeout=POLL_INTERVAL_S if self.ecu else None)
            except queue.Empty:
                job = None
            if job == ("quit",):
                self._close()
                return
            try:
                self._step(job)
            except (ProtocolError, TransportError, OSError) as exc:
                self._close()
                self.disconnected.emit(str(exc))

    def _step(self, job) -> None:
        if job is None:
            if isinstance(self.ecu, Ecu):
                self.ecu.poll_runtime()
                if self.logger:
                    self.logger.sample(self.ecu.get)
                self.runtime.emit()
            return
        name, *args = job
        if name == "connect":
            self._close()
            ecu = Ecu(open_transport(args[0]), self.database)
            try:
                info = ecu.connect()
            except Exception:
                ecu.close()
                raise
            self.ecu = ecu
            if not ecu.is_protected:
                self._start_rules()
            self.connected.emit(info, ecu.is_protected)
        elif name == "offline":
            self._close()
            self.ecu = args[0]
            self._start_rules()
            self.connected.emit(self.ecu.info, False)
        elif name == "disconnect":
            self._close()
            self.disconnected.emit("")
        elif self.ecu is None:
            self.failed.emit("Not connected to an ECU.")
        elif name == "set":
            self._set_value(*args)
        elif name == "set_string":
            self._guarded(lambda: self.ecu.set_string(args[0], args[1]), args[0], 0)
        elif name == "control":
            self._control(*args)
        elif name == "call":
            self._call(*args)
        elif name == "store":
            try:
                self.ecu.store()
            except (StoreUnconfirmed, ForbiddenCommand) as exc:
                self.failed.emit(str(exc))
            else:
                self.stored.emit()

    def _call(self, work, callback) -> None:
        """Run ECU work here, and hand its result or error back to the window."""
        try:
            result = work(self.ecu)
        except (WriteRefused, WriteMismatch, EcuProtected, ForbiddenCommand, EcuError, EcuLogError, ValueError, KeyError) as exc:
            self.called.emit(callback, None, exc)
        except ProtocolError as exc:
            self.called.emit(callback, None, exc)
            raise
        else:
            self.called.emit(callback, result, None)

    def _control(self, action, done: str) -> None:
        try:
            action(self.ecu)
        except (WriteRefused, WriteMismatch, ForbiddenCommand, EcuError) as exc:
            self.failed.emit(str(exc))
        else:
            self.controlled.emit(done)

    def _rules_read(self, data_id: int, index: int = 0) -> float:
        try:
            return self.ecu.get(data_id, index)
        except (EcuProtected, IndexError, KeyError, TypeError, ValueError):
            return 0.0  # The rules also ask about text and reserved parameters.

    def _start_rules(self) -> None:
        """Apply PCLink's dependent-setting rules to the current tune, as PCLink does on connecting."""
        self.rules = None
        path = rules_table_path()
        if path is None:
            self.rules_status.emit("This build has no rules table, so dependent settings are not hidden.")
            return
        database = self.database

        def pinned(data_id: int) -> bool:
            return database.known(data_id) and database.parameter(data_id).path[:1] == ("Table Allocation",)

        try:
            if self.rules_table is None:
                self.rules_table = RulesTable.load(path, pinned)
        except (OSError, ValueError, KeyError) as exc:
            self.rules_status.emit(f"The rules table could not be read, so dependent settings are not hidden: {exc}")
            return
        engine = NativeRules(
            self.rules_table,
            self._rules_read,
            lambda data_id: self.ecu is not None and data_id in self.ecu.table,
            lambda data_id: database.known(data_id) and database.parameter(data_id).visible,
        )
        for data_id, loc in sorted(self.ecu.table.items()):
            if data_id in SECRET_IDS or loc.is_string or not database.known(data_id) or data_id == 0:
                continue
            if loc.count > 1 and not loc.is_bitfield:
                continue
            engine.value_changed(data_id, self._rules_read(data_id))
        engine.take_messages()  # PCLink's checks speak up on a change, not on connecting.
        self.rules = engine

    def _set_value(self, data_id: int, value: float, index: int) -> None:
        old = self._rules_read(data_id, index)
        self._guarded(lambda: self.ecu.set(data_id, value, index), data_id, index)
        new = self._rules_read(data_id, index)
        if self.rules is None or new == old or index != 0:
            return
        self.rules.visibility_changed = False
        self.rules.value_changed(data_id, new)
        messages = list(dict.fromkeys(self.rules.take_messages()))
        if messages:
            self.rules_messages.emit(messages)
        if self.rules.visibility_changed:
            self.visibility_changed.emit()

    def _guarded(self, action, data_id: int, index: int) -> None:
        try:
            action()
        except (WriteRefused, WriteMismatch, EcuProtected, ForbiddenCommand, ValueError) as exc:
            self.failed.emit(str(exc))
        self.wrote.emit(data_id, index)

    def _close(self) -> None:
        if self.logger:
            self.logger.close()
            self.logger = None
        self.rules = None
        if self.ecu:
            self.ecu.close()
            self.ecu = None


class ConnectDialog(QtWidgets.QDialog):
    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Connect to ECU")
        self.resize(480, 300)
        self.ports = QtWidgets.QListWidget()
        self.address = QtWidgets.QLineEdit()
        self.address.setPlaceholderText("Manual: serial:/dev/x, ftdi:7068 or udp:192.168.4.1")
        refresh = QtWidgets.QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        self.show_all = QtWidgets.QCheckBox("Show all serial ports")
        self.show_all.toggled.connect(self.refresh)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.ports.itemDoubleClicked.connect(lambda _: self.accept())
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.show_all)
        row.addStretch()
        row.addWidget(refresh)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(QtWidgets.QLabel("Power the ECU on and connect its USB cable:"))
        layout.addWidget(self.ports)
        layout.addLayout(row)
        layout.addWidget(self.address)
        layout.addWidget(buttons)
        self.refresh()

    def refresh(self) -> None:
        self.ports.clear()
        for port in list_ports(all_serial=self.show_all.isChecked()):
            item = QtWidgets.QListWidgetItem(port.description)
            item.setData(ID_ROLE, port.uri)
            self.ports.addItem(item)
        if self.ports.count():
            self.ports.setCurrentRow(0)

    def uri(self) -> str:
        manual = self.address.text().strip()
        if manual:
            return manual
        item = self.ports.currentItem()
        return item.data(ID_ROLE) if item else ""


class _IgnoreWheel(QtCore.QObject):
    """Stops the scroll wheel changing a setting while scrolling past it."""

    def eventFilter(self, _watched: QtCore.QObject, event: QtCore.QEvent) -> bool:
        return event.type() == QtCore.QEvent.Type.Wheel


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
        dialog = tools.ItemPicker(items, self)
        if dialog.exec() and dialog.value() is not None:
            self.window_.choose_option(self.data_id, dialog.value())


class ParameterForm(QtWidgets.QWidget):
    """Edits the parameters of one menu entry."""

    def __init__(self, window: "MainWindow") -> None:
        super().__init__()
        self.window_ = window
        self.layout_ = QtWidgets.QFormLayout(self)
        self.editors: dict[int, QtWidgets.QWidget] = {}
        self.parameters: list[Parameter] = []
        self.prefers_graph = False
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
                self.layout_.addRow(parameter.caption, editor)
                self.editors[parameter.data_id] = editor
        self.refresh()

    def _build_editor(self, parameter: Parameter) -> QtWidgets.QWidget | None:
        ecu = self.window_.worker.ecu
        if parameter.data_id in self.window_.ACTION_PARAMETERS:
            button = QtWidgets.QPushButton(parameter.caption)
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
            table = TableEditor(self.window_, parameter, loc.count, read_only)
            if self.prefers_graph and table.grid.rowCount() > 1:
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


class StatusWindow(QtWidgets.QWidget):
    """The ECU's status, led by its fault code. Read-only, and it updates live."""

    FAULT_CODE_ID = 1162
    FAULT_COUNT_ID = 1186

    def __init__(self, window: "MainWindow") -> None:
        super().__init__()
        self.window_ = window
        self.setWindowTitle("ECU Status")
        self.resize(560, 620)
        self.fault = QtWidgets.QLabel()
        self.fault.setWordWrap(True)
        self.table = QtWidgets.QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Status", "Value"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self.fault)
        layout.addWidget(self.table)
        database, ecu = window.database, window.worker.ecu
        self.ids = [
            data_id
            for data_id in database.ids
            if data_id in ecu.table
            and data_id not in SECRET_IDS
            and database.parameter(data_id).path[:1] == ("ECU Status",)
            and window.parameter_visible(data_id)
        ]
        self.table.setRowCount(len(self.ids))
        for row, data_id in enumerate(self.ids):
            self.table.setItem(row, 0, QtWidgets.QTableWidgetItem(database.parameter(data_id).caption))
            self.table.setItem(row, 1, QtWidgets.QTableWidgetItem(""))
        self.table.resizeColumnToContents(0)
        self.refresh()

    def _text(self, data_id: int) -> str:
        window = self.window_
        parameter = window.database.parameter(data_id)
        decimals = parameter.decimals if parameter.decimals is not None else 0
        return window._format_live(parameter, window.worker.ecu.get(data_id), decimals, window.units_of(parameter))

    def refresh(self) -> None:
        ecu = self.window_.worker.ecu
        if ecu is None:
            self.fault.setText("Not connected.")
            return
        for row, data_id in enumerate(self.ids):
            self.table.item(row, 1).setText(self._text(data_id))
        self._show_fault(ecu)

    def _show_fault(self, ecu) -> None:
        if self.FAULT_CODE_ID not in ecu.table:
            self.fault.setText("This ECU does not report fault codes here.")
            return
        code = int(ecu.get(self.FAULT_CODE_ID))
        count = int(ecu.get(self.FAULT_COUNT_ID)) if self.FAULT_COUNT_ID in ecu.table else None
        counted = f" ({count} active)" if count is not None else ""
        self.fault.setText(f"Fault code: {self._text(self.FAULT_CODE_ID)}{counted}" if code else "No fault codes.")
        self.fault.setStyleSheet("color:#a12626;font-weight:bold" if code else "color:#1b7f3b;font-weight:bold")


class _SnakeOverlay(QtWidgets.QWidget):
    """The trail of cells the engine has just been through."""

    enabled_trail = True
    paused = False

    def __init__(self, grid: QtWidgets.QTableWidget) -> None:
        super().__init__(grid.viewport())
        self.grid = grid
        self.points: list[tuple[int, int]] = []
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        grid.viewport().installEventFilter(self)

    def eventFilter(self, _watched: QtCore.QObject, event: QtCore.QEvent) -> bool:
        if event.type() == QtCore.QEvent.Type.Resize:
            self.resize(self.grid.viewport().size())
        return False

    def add(self, position: tuple[int, int]) -> None:
        if _SnakeOverlay.paused or not _SnakeOverlay.enabled_trail:
            if not _SnakeOverlay.enabled_trail and self.points:
                self.points.clear()
                self.update()
            return
        self.points.append(position)
        del self.points[: max(len(self.points) - SNAKE_LENGTH, 0)]
        self.update()

    def paintEvent(self, _event: QtGui.QPaintEvent) -> None:
        if len(self.points) < 2 or not _SnakeOverlay.enabled_trail:
            return
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setPen(QtGui.QPen(QtGui.QColor(0, 0, 0), 2))
        line = QtGui.QPolygonF()
        for row, column in self.points:
            item = self.grid.item(row, column)
            if item is not None:
                line.append(QtCore.QPointF(self.grid.visualItemRect(item).center()))
        painter.drawPolyline(line)


class TableEditor(QtWidgets.QWidget):
    """A tuning table: heat map, live position, bulk edits, axes and 3D view."""

    def __init__(self, window: "MainWindow", parameter: Parameter, count: int, read_only: bool) -> None:
        super().__init__()
        self.window_ = window
        self.parameter = parameter
        self.read_only = read_only
        columns = parameter.columns if parameter.columns > 1 else count
        self.grid = QtWidgets.QTableWidget(max(count // columns, 1), columns)
        self.grid.setMinimumHeight(min(90 + self.grid.rowCount() * 30, 480))
        self.grid.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.grid.horizontalHeader().setDefaultSectionSize(58)
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
        self.surface = SurfaceView()
        self.stack = QtWidgets.QStackedWidget()
        self.stack.addWidget(self.grid)
        self.stack.addWidget(self.surface)
        toggle = QtWidgets.QPushButton("Grid / 3D")
        toggle.clicked.connect(self.toggle_graph)
        self.snake = _SnakeOverlay(self.grid)
        toggle.setEnabled(self.grid.rowCount() > 1)
        hint = QtWidgets.QLabel("+ / − nudge,  = set,  * scale %,  N Y Z markers,  X axes,  M Quick Trim,  Ctrl+I interpolate,  K graph")
        hint.setEnabled(False)
        controls = QtWidgets.QHBoxLayout()
        controls.addWidget(hint)
        controls.addStretch()
        controls.addWidget(toggle)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(controls)
        layout.addWidget(self.stack)
        self.trace: tuple[int, int] | None = None

    def toggle_graph(self) -> None:
        self.stack.setCurrentIndex(1 - self.stack.currentIndex())

    def _selected_cells(self) -> list[tuple[int, int]]:
        return sorted({(index.row(), index.column()) for index in self.grid.selectedIndexes()})

    def _kind(self) -> str:
        return self.window_.kind_of(self.parameter)

    def _write_display(self, value: float, index: int) -> None:
        self.window_.write(self.parameter.data_id, self.window_.units.to_base(value, self._kind()), index)

    def _mark(self, bit: int) -> None:
        self.window_.marks.toggle(self.parameter.data_id, self._selected_cells(), bit)
        self.refresh()

    def _interpolate_selection(self, by_row: bool, by_column: bool) -> None:
        """Straight lines between the corners of the selection."""
        cells = self._selected_cells()
        if len(cells) < 2:
            return
        rows = sorted({row for row, _ in cells})
        columns = sorted({column for _, column in cells})
        def value(row: int, column: int) -> float:
            return float(self.grid.item(row, column).text())
        top, bottom, left, right = rows[0], rows[-1], columns[0], columns[-1]
        for row in rows:
            for column in columns:
                fx = (column - left) / ((right - left) or 1) if by_column else 0.0
                fy = (row - top) / ((bottom - top) or 1) if by_row else 0.0
                base_row, base_column = (row if not by_row else top), (column if not by_column else left)
                upper = value(base_row, left if by_column else base_column) + ((value(base_row, right) - value(base_row, left)) * fx if by_column else 0.0)
                lower = value(bottom if by_row else base_row, left if by_column else base_column) + ((value(bottom if by_row else base_row, right) - value(bottom if by_row else base_row, left)) * fx if by_column else 0.0)
                new = upper + (lower - upper) * fy
                self._write_display(new, row * self.grid.columnCount() + column)

    def _smooth_selection(self) -> None:
        cells = set(self._selected_cells())
        for row, column in cells:
            around = [
                float(self.grid.item(r, c).text())
                for r in range(row - 1, row + 2) for c in range(column - 1, column + 2)
                if self.grid.item(r, c) is not None and not self.grid.isRowHidden(r) and not self.grid.isColumnHidden(c)
            ]
            self._write_display(sum(around) / len(around), row * self.grid.columnCount() + column)

    def _fill_right(self) -> None:
        for row, column in self._selected_cells():
            for c in range(column + 1, self.grid.columnCount()):
                self._write_display(float(self.grid.item(row, column).text()), row * self.grid.columnCount() + c)

    def _quick_trim(self) -> None:
        window, ecu = self.window_, self.window_.worker.ecu
        if self.parameter.data_id not in (tune.FUEL_TABLE_1, tune.FUEL_TABLE_2) or ecu is None:
            return

        def live(data_id: int) -> float:
            return ecu.get(data_id) if data_id in ecu.table else 0.0

        correction = live(tune.CL_CORRECTION) if live(tune.CL_MODE) else 0.0
        dialog = tools.QuickTrimDialog(live(tune.LAMBDA_TARGET_RUNTIME), live(tune.LAMBDA_AVERAGE), correction, self)
        if dialog.exec():
            self._apply(lambda value: tune.quick_trim_value(value, dialog.measured.value(), dialog.target.value(), dialog.correction.value()))
            window.statusBar().showMessage("Quick Trim applied.", 4000)

    def _axis_setup(self) -> None:
        ecu = self.window_.worker.ecu
        ids = (self.parameter.column_axis_id, self.parameter.row_axis_id)
        if ecu is None or self.read_only or not any(i in ecu.table for i in ids):
            return
        x_axis, y_axis = (ecu.get_all(i) if i in ecu.table else [] for i in ids)
        dialog = tools.AxisSetupDialog(x_axis, y_axis, self)
        if not dialog.exec():
            return
        for axis_id, old, new in zip(ids, (x_axis, y_axis), dialog.axes()):
            for index, (before, after) in enumerate(zip(old, new)):
                if before != after:
                    self.window_.write(axis_id, after, index)
        if dialog.interpolate.isChecked():
            self._reinterpolate(x_axis, y_axis, *dialog.axes())

    def _reinterpolate(self, old_x: list[float], old_y: list[float], new_x: list[float], new_y: list[float]) -> None:
        """Keep the table's shape when its axes move."""
        ecu = self.window_.worker.ecu
        values = ecu.get_all(self.parameter.data_id)
        columns = self.grid.columnCount()
        for row in range(self.grid.rowCount()):
            for column in range(columns):
                x = new_x[column] if column < len(new_x) else 0.0
                y = new_y[row] if row < len(new_y) else 0.0
                new = tune.bilinear(old_x or [0.0], old_y or [0.0], values, x, y)
                self.window_.write(self.parameter.data_id, new, row * columns + column)

    def _step(self) -> float:
        ecu = self.window_.worker.ecu
        scale = abs(ecu.table[self.parameter.data_id].scale) if ecu else 1
        if self.parameter.decimals is not None:
            return max(10.0 ** -self.parameter.decimals, scale)
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

    def _apply(self, change) -> None:
        """Apply a function to each selected cell."""
        for item in self.grid.selectedItems():
            self._write_display(change(float(item.text())), self._index(item))

    def eventFilter(self, _watched: QtCore.QObject, event: QtCore.QEvent) -> bool:
        if event.type() != QtCore.QEvent.Type.KeyPress or self.grid.state() == self.grid.State.EditingState:
            return False
        key = event.key()
        if event.matches(QtGui.QKeySequence.StandardKey.Copy):
            self._copy()
            return True
        if self.read_only:
            return False
        if event.matches(QtGui.QKeySequence.StandardKey.Paste):
            self._paste()
            return True
        control = event.modifiers() & QtCore.Qt.KeyboardModifier.ControlModifier
        shift = event.modifiers() & QtCore.Qt.KeyboardModifier.ShiftModifier
        letters = {
            QtCore.Qt.Key.Key_N: lambda: self._mark(tune.RUNTIME_MARKER),
            QtCore.Qt.Key.Key_Y: lambda: self._mark(tune.PART_TUNED),
            QtCore.Qt.Key.Key_Z: lambda: self._mark(tune.TUNED),
            QtCore.Qt.Key.Key_X: self._axis_setup,
            QtCore.Qt.Key.Key_M: self._quick_trim,
            QtCore.Qt.Key.Key_K: self.toggle_graph,
            QtCore.Qt.Key.Key_O: self._smooth_selection,
        }
        if control and key == QtCore.Qt.Key.Key_I:
            self._interpolate_selection(True, True)
            return True
        if control and key == QtCore.Qt.Key.Key_F:
            self._fill_right()
            return True
        if shift and key == QtCore.Qt.Key.Key_H:
            self._interpolate_selection(False, True)
            return True
        if shift and key == QtCore.Qt.Key.Key_V:
            self._interpolate_selection(True, False)
            return True
        if shift and key == QtCore.Qt.Key.Key_M:
            self.window_.marks.clear_table(self.parameter.data_id)
            self.refresh()
            return True
        if not control and not shift and key in letters:
            letters[key]()
            return True
        if key in (QtCore.Qt.Key.Key_Plus, QtCore.Qt.Key.Key_Greater):
            self._apply(lambda value: value + self._step())
        elif key in (QtCore.Qt.Key.Key_Minus, QtCore.Qt.Key.Key_Less):
            self._apply(lambda value: value - self._step())
        elif key == QtCore.Qt.Key.Key_Equal:
            number, chosen = QtWidgets.QInputDialog.getDouble(self, "Set cells", "Value:", 0, -1e9, 1e9, 3)
            if chosen:
                self._apply(lambda _value: number)
        elif key == QtCore.Qt.Key.Key_Asterisk:
            percent, chosen = QtWidgets.QInputDialog.getDouble(self, "Scale cells", "Percent:", 100, 0, 1000, 2)
            if chosen:
                self._apply(lambda value: value * percent / 100)
        else:
            return False
        return True

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
        for r, values in enumerate(rows):
            for c, value in enumerate(values):
                row, column = current.row() + r, current.column() + c
                if row < self.grid.rowCount() and column < self.grid.columnCount():
                    self._write_display(value, row * self.grid.columnCount() + column)

    def _edit_axis(self, axis_id: int | None, index: int) -> None:
        ecu = self.window_.worker.ecu
        if self.read_only or axis_id is None or ecu is None or axis_id not in ecu.table:
            return
        current = ecu.get(axis_id, index)
        number, chosen = QtWidgets.QInputDialog.getDouble(self, "Axis breakpoint", "Value:", current, -1e9, 1e9, 3)
        if chosen and number != current:
            self.window_.write(axis_id, number, index)

    def refresh(self) -> None:
        ecu = self.window_.worker.ecu
        if ecu is None:
            return
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
                self.grid.setItem(row, column, cell)
            elif cell.text() != text and not (self.grid.currentItem() is cell and self.grid.isPersistentEditorOpen(cell)):
                cell.setText(text)
            cell.setBackground(self.window_.cell_colour(self.parameter.data_id, row, column) or QtGui.QColor(*heat_colour(value, low, high)))
            changed = ecu.is_changed(self.parameter.data_id, index)
            cell.setForeground(self.window_.theme.colour("ValueChanged") if changed else QtGui.QColor(20, 20, 20))
        for axis_id, size, setter in (
            (self.parameter.column_axis_id, self.grid.columnCount(), self.grid.setHorizontalHeaderLabels),
            (self.parameter.row_axis_id, self.grid.rowCount(), self.grid.setVerticalHeaderLabels),
        ):
            if axis_id in ecu.table:
                breakpoints = ecu.get_all(axis_id)[:size]
                setter([f"{value:g}" for value in breakpoints])
                hide = self.grid.setColumnHidden if size == self.grid.columnCount() and setter == self.grid.setHorizontalHeaderLabels else self.grid.setRowHidden
                for index in range(size):
                    hide(index, index >= used_axis_length(breakpoints))
        self.grid.blockSignals(False)
        self.surface.set_values(values, self.grid.columnCount())
        self.update_trace()

    def update_trace(self) -> None:
        """Outline the cell the engine is using now."""
        position = self.window_.table_position(self.parameter, self.grid.rowCount(), self.grid.columnCount())
        if position == self.trace:
            return
        # Changing a font fires itemChanged, which must never become an ECU write.
        self.grid.blockSignals(True)
        for spot, bold in ((self.trace, False), (position, True)):
            if spot and self.grid.item(*spot):
                font = self.grid.item(*spot).font()
                font.setBold(bold)
                font.setUnderline(bold)
                self.grid.item(*spot).setFont(font)
        self.grid.blockSignals(False)
        self.trace = position
        if position:
            self.snake.add(position)


class SettingsView(QtWidgets.QScrollArea):
    """PCLink's Auto Settings view: shows what is chosen in the settings tree."""

    def __init__(self, window: "MainWindow", view: LayoutView) -> None:
        super().__init__()
        self.view = view
        self.form = ParameterForm(window)
        self.form.prefers_graph = view.attributes.get("GraphDisplay") == "true"
        self.filled = False
        self.setWidgetResizable(True)
        self.setWidget(self.form)


class MainWindow(QtWidgets.QMainWindow):
    report_progress = QtCore.Signal(int)
    ACTION_PARAMETERS = (1682, 1679, 1661, 3550, 2035, 1123, 1647)

    def __init__(self, database: Database, options: Options | None = None) -> None:
        super().__init__()
        self.database = database
        self.options = options if options is not None else Options.load()
        self.worker = Worker(database)
        self.units = UnitSystem(self.options.unit_choice())
        self.runtime_config = RuntimeConfig.load()
        self.setWindowTitle("OpenPCLink (unofficial)")
        self.resize(1200, 760)
        self.protected = False
        self._uri = ""
        self._map_path: Path | None = None
        self.status_window: StatusWindow | None = None
        self.options_path: Path | None = None
        self.theme = theme_named(self.options)
        self.marks = tune.CellMarks()
        self.snake_state = 0
        self.log_session = LogSession()
        self.log_session.describe = lambda data_id: self.describe(data_id)[0]
        self.log_session.colour_of = lambda data_id: self.theme.parameter_colour(self.runtime_config.get(data_id).colour) if data_id in self.runtime_config.entries else None
        self.current_table_id: int | None = None
        self.quick_tune_window: tools.QuickTuneWindow | None = None
        self.runtime_window: Workspace | None = None
        self.help_window: QtWidgets.QTextBrowser | None = None
        self.windows: dict[str, QtWidgets.QWidget] = {}
        self.log_timer = QtCore.QTimer(self)
        self.log_timer.setInterval(PLAYBACK_INTERVAL_MS)
        self.log_timer.timeout.connect(self._play_step)
        self.report_progress_target = None
        self._selected_parameters: list[Parameter] = []

        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("Search parameters")
        self.search.textChanged.connect(self._search_typed)
        self.search.returnPressed.connect(lambda: self._filter_tree(self.search.text()))
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.currentItemChanged.connect(self._tree_selected)
        self.runtime_tree = QtWidgets.QTreeWidget()
        self.runtime_tree.setHeaderHidden(True)
        self.runtime_tree.itemDoubleClicked.connect(self._toggle_gauge)
        self.runtime_tree.setToolTip("Double-click a value to add or remove it from the live list")
        self.gauge_ids: list[int] = []
        self.left_tabs = QtWidgets.QTabWidget()
        self.left_tabs.setTabPosition(QtWidgets.QTabWidget.TabPosition.West)
        settings_page = QtWidgets.QWidget()
        settings_layout = QtWidgets.QVBoxLayout(settings_page)
        settings_layout.setContentsMargins(0, 0, 0, 0)
        settings_layout.addWidget(self.search)
        settings_layout.addWidget(self.tree)
        self.left_tabs.addTab(settings_page, "ECU Settings")
        self.left_tabs.addTab(self.runtime_tree, "Parameters")
        self.log_manager = tools.LogFileManager(self)
        self.left_tabs.addTab(self.log_manager, "Log File Manager")
        self.event_log = tools.EventLogView()
        self.left_tabs.addTab(self.event_log, "Event Log")
        self.event_log.clear_faults.clicked.connect(self.clear_faults)

        self.fallback_form = ParameterForm(self)
        self.fallback_help = QtWidgets.QTextBrowser()
        self.manual = HelpManual()
        self.placeholder = QtWidgets.QLabel("Press F3 to connect to an ECU.")
        self.placeholder.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.workspace = Workspace(self)
        self.log_workspace = Workspace(self)
        self.display_stack = QtWidgets.QStackedWidget()
        self.display_stack.addWidget(self.workspace)
        self.display_stack.addWidget(self.log_workspace)
        self.centre = QtWidgets.QStackedWidget()
        self.centre.addWidget(self.placeholder)
        self.centre.addWidget(self.display_stack)
        self.workspace.page_changed.connect(self._page_changed)
        self.log_workspace.page_changed.connect(self._page_changed)

        self.splitter = QtWidgets.QSplitter()
        self.splitter.addWidget(self.left_tabs)
        self.splitter.addWidget(self.centre)
        self.splitter.setSizes([280, 920])
        self.setCentralWidget(self.splitter)

        self.status_label = QtWidgets.QLabel()
        self.unstored_label = QtWidgets.QLabel()
        self.fault_label = QtWidgets.QLabel()
        self.fault_label.setStyleSheet("color:#a12626;font-weight:bold")
        self.log_state_label = QtWidgets.QLabel("PC Log = Off")
        self.rate_label = QtWidgets.QLabel()
        self.statusBar().addPermanentWidget(self.fault_label)
        self.statusBar().addPermanentWidget(self.unstored_label)
        self.statusBar().addPermanentWidget(self.log_state_label)
        self.statusBar().addPermanentWidget(self.status_label)
        self.statusBar().addPermanentWidget(self.rate_label)
        self._load_default_layouts()
        self.apply_appearance()
        self._build_actions()
        self._set_online(False)

        self.worker.connected.connect(self._on_connected)
        self.worker.disconnected.connect(self._on_disconnected)
        self.worker.runtime.connect(self._on_runtime)
        self.worker.wrote.connect(self._on_wrote)
        self.worker.failed.connect(self.show_error)
        self.worker.stored.connect(self._on_stored)
        self.worker.controlled.connect(self._on_controlled)
        self.worker.called.connect(self._on_called)
        self.worker.rules_status.connect(self.statusBar().showMessage)
        self.worker.rules_messages.connect(lambda messages: self.show_error("\n\n".join(messages)))
        self.worker.visibility_changed.connect(self._on_visibility_changed)
        self.worker.start()

    # ---- workspace --------------------------------------------------------

    @property
    def form(self) -> ParameterForm:
        """Where the settings tree's choice shows: the current page's Auto Settings view."""
        page = self.workspace.current()
        views = page.bodies(SettingsView) if page else []
        if not views:
            return self.fallback_form
        wanted = page.page.current_settings_view
        return next((view.form for view in views if view.view.guid == wanted), views[0].form)

    @property
    def help_view(self) -> QtWidgets.QTextBrowser:
        page = self.workspace.current()
        views = page.bodies(QtWidgets.QTextBrowser) if page else []
        return views[0] if views else self.fallback_help

    def _load_default_layouts(self) -> None:
        self.workspace.load(self._layout_named(self.options.recent_layout, "DefaultLayout", _fallback_layout))
        self.log_workspace.load(self._layout_named("", "DefaultLoggingLayout", _fallback_log_layout))

    def _layout_named(self, recent: str, name: str, fallback) -> Layout:
        for path in (Path(recent) if recent else None, assets.layout_path(name)):
            if path and path.exists():
                try:
                    return Layout.load(path)
                except LayoutError as exc:
                    self.statusBar().showMessage(f"Could not read layout {path.name}: {exc}")
        return fallback()

    def reading(self, data_id: int) -> Reading | None:
        ecu = self.worker.ecu
        if ecu is None or data_id in SECRET_IDS or data_id not in ecu.table or not self.database.known(data_id):
            return None
        parameter = self.database.parameter(data_id)
        try:
            raw = ecu.get(data_id)
        except (EcuProtected, IndexError, KeyError, TypeError, ValueError):
            return None
        decimals = parameter.decimals if parameter.decimals is not None else 1
        config = self.runtime_config.get(data_id)
        kind = self.kind_of(parameter)
        show = lambda value: None if value is None or abs(value) >= gauges.NO_WARNING else self.units.to_display(value, kind)  # noqa: E731
        units = self.display_units(parameter)
        decimals = self.units.decimals(decimals, kind)
        shown = raw if parameter.options else self.units.to_display(raw, kind)
        return Reading(
            data_id, parameter.caption, units, shown, self._format_live(parameter, shown, decimals, units),
            minimum=show(config.minimum if config.minimum is not None else parameter.minimum if parameter.minimum is not None else 0.0) or 0.0,
            maximum=show(config.maximum if config.maximum is not None else parameter.maximum if parameter.maximum is not None else 100.0) or 100.0,
            high_warning=show(config.high_warning), high_error=show(config.high_error),
            low_warning=show(config.low_warning), low_error=show(config.low_error), colour=config.colour,
            is_option=bool(parameter.options),
        )

    def caption_of_view(self, view: LayoutView) -> str:
        reference = view.attributes.get("CaptionStrId") or view.attributes.get("BaseCaption")
        text = self.database.translate(reference) if reference else ""
        text = text or view.attributes.get("Caption") or view.attributes.get("ViewCaption")
        return text or ("" if view.type in GAUGE_TYPES else view.type)

    def _fill_settings_views(self) -> None:
        """Show what each Auto Settings view on this page was saved showing."""
        page = self.workspace.current()
        if page is None or self.worker.ecu is None or self.protected:
            return
        for view in page.bodies(SettingsView):
            if view.filled:
                continue
            view.filled = True
            base = view.view.attributes.get("BaseCaption")
            ids = self._ids_for_caption(self.database.translate(base)) if base else []
            if ids:
                view.form.show_parameters([self.database.parameter(i) for i in ids])

    def _ids_for_caption(self, caption: str) -> list[int]:
        ecu = self.worker.ecu
        for data_id in ecu.table:
            if self.database.known(data_id) and self.database.parameter(data_id).caption == caption and self.database.parameter(data_id).is_table:
                return [data_id]
        stack = [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]
        while stack:
            item = stack.pop(0)
            if item.text(0) == caption:
                return self._form_ids(item)
            stack.extend(item.child(i) for i in range(item.childCount()))
        return []

    def build_view(self, view: LayoutView) -> QtWidgets.QWidget | None:
        if view.type == "Auto Settings":
            return SettingsView(self, view)
        if view.type == "Help Browser":
            browser = QtWidgets.QTextBrowser()
            browser.setOpenLinks(False)
            browser.anchorClicked.connect(self._help_link)
            return browser
        if view.type == "Mixture Map":
            return MixtureMapView(self.log_session, view, self)
        if view.type == "Vehicle Plan":
            return VehiclePlan(self.log_session, view, self._live_value)
        kind = LOG_VIEW_TYPES.get(view.type)
        return kind(self.log_session, view) if kind else None

    def _live_value(self, data_id: int) -> float | None:
        reading = self.reading(data_id)
        return reading.value if reading else None

    def view_context_menu(self, frame: ViewFrame, menu: QtWidgets.QMenu) -> None:
        if isinstance(frame.body, LiveView):
            menu.addAction("Choose Parameters…", lambda: self.choose_live_parameters(frame))
            if isinstance(frame.body.body, Gauge):
                menu.addAction("Properties…", lambda: self.gauge_properties(frame))
        elif hasattr(frame.body, "set_parameters"):
            menu.addAction("Choose Parameters…", lambda: self.choose_log_parameters(frame))

    def current_table(self) -> int | None:
        return self.current_table_id

    def layout_changed(self) -> None:
        if self.options.auto_save_layout and self.options.recent_layout:
            try:
                self.workspace.layout_.save(Path(self.options.recent_layout))
            except OSError as exc:
                self.statusBar().showMessage(f"Could not save layout: {exc}")

    def _page_changed(self) -> None:
        self._fill_settings_views()
        self.workspace.refresh_live()
        self.log_workspace.refresh_live()

    def apply_appearance(self) -> None:
        self.theme = theme = theme_named(self.options)
        sheet = theme.style_sheet()
        gauges.PARAMETER_COLOURS[:] = [theme.parameter_colour(i) for i in range(len(theme.parameters))]
        gauges.set_theme(theme.colour("DataAreaBackground"), theme.colour("ViewText"), theme.colour("View Title Bar"))
        for workspace in (self.workspace, self.log_workspace, self.runtime_window):
            if workspace is not None:
                workspace.setStyleSheet(sheet)
        app = QtWidgets.QApplication.instance()
        if self.options.windows_font:
            font = QtGui.QFont()
            font.fromString(self.options.windows_font)
            app.setFont(font)
        if self.options.view_font:
            font = QtGui.QFont()
            font.fromString(self.options.view_font)
            self.workspace.setFont(font)
            self.log_workspace.setFont(font)

    def options_changed(self) -> None:
        self.options.save(self.options_path)
        self._update_units()
        self.apply_appearance()
        self.form.show_parameters(self.form.parameters)
        self.workspace.refresh_live()

    def _update_units(self) -> None:
        ecu = self.worker.ecu

        def setting(data_id: int, default: float) -> float:
            try:
                return ecu.get(data_id) if ecu and data_id in ecu.table else default
            except EcuProtected:
                return default

        self.units = UnitSystem(self.options.unit_choice(), setting(tune.STOICH_RATIO, 14.7), setting(tune.FUEL_EQUATION_MODE, 0) == 2.0)

    def kind_of(self, parameter: Parameter) -> str:
        return parameter.value_type or parameter.units

    def display_units(self, parameter: Parameter) -> str:
        rules = self.worker.rules
        moved = rules.units.get(parameter.data_id) if rules else None
        return moved or (self.units.label(parameter.value_type) if parameter.value_type else self.units.label(parameter.units))

    def live_choices(self) -> list[tuple[int, str]]:
        ecu = self.worker.ecu
        if ecu is None:
            return []
        return [
            (data_id, self.database.parameter(data_id).caption)
            for data_id in ecu.table
            if self.database.known(data_id) and data_id not in SECRET_IDS and self._is_live(data_id)
        ]

    def describe(self, data_id: int) -> tuple[str, str]:
        if not self.database.known(data_id):
            return str(data_id), ""
        parameter = self.database.parameter(data_id)
        return parameter.caption, parameter.units

    def cell_colour(self, table_id: int, row: int, column: int) -> QtGui.QColor | None:
        """PCLink's priority: runtime marker, then tuned, then part tuned."""
        flags = self.marks.flag(table_id, row, column)
        if flags & tune.RUNTIME_MARKER:
            return self.theme.colour("Markers")
        if flags & tune.TUNED:
            return self.theme.colour("OK")
        if flags & tune.PART_TUNED:
            return self.theme.colour("WarningLow")
        return None

    def call(self, work, on_result) -> None:
        """Run `work(ecu)` on the worker thread; `on_result` then runs here with what it returned."""
        if self.worker.ecu is None:
            self.show_error("Connect to an ECU first.")
            return
        self.worker.post("call", work, on_result)

    def _on_called(self, callback, result, error) -> None:
        if error is not None:
            self.show_error(str(error))
        elif callback:
            callback(result)
        self.form.refresh()
        self._show_unstored()

    def _build_actions(self) -> None:
        """PCLink's menu bar, item for item."""
        menus = self.menuBar()

        def item(menu: QtWidgets.QMenu, text: str, handler, shortcut: str | None = None) -> QtGui.QAction:
            action = QtGui.QAction(text, self, triggered=lambda _checked=False: handler())
            if shortcut:
                action.setShortcut(shortcut)
            menu.addAction(action)
            return action

        file_menu = menus.addMenu("File")
        item(file_menu, "Open", self.open_map, "Ctrl+O")
        item(file_menu, "Save", self.save_map_quick, "F2")
        item(file_menu, "Save as…", self.save_map, "Ctrl+Shift+S")
        item(file_menu, "Save as Encrypted", self.save_map_encrypted)
        file_menu.addSeparator()
        item(file_menu, "Compare", self.compare_map)
        self.compare_action_close = item(file_menu, "Compare Close", self.compare_close)
        self.compare_action_close.setEnabled(False)
        file_menu.addSeparator()
        item(file_menu, "Show Memo", self.show_memo, "Ctrl+M")
        file_menu.addSeparator()
        item(file_menu, "Exit", self.close)

        options = menus.addMenu("Options")
        item(options, "Connection…", lambda: self.show_options("Connection"))
        item(options, "Preferences…", lambda: self.show_options("Preferences"))
        item(options, "Directories", lambda: self.show_options("Directories"))
        item(options, "Colour Theme", self.colour_theme)
        units = options.addMenu("Units")
        item(units, "Metric", lambda: self.set_unit_system("Metric"))
        item(units, "Imperial", lambda: self.set_unit_system("Imperial"))
        item(units, "Options", lambda: self.show_options("Units"))
        language = options.addMenu("Language")
        english = QtGui.QAction(self.options.language, self, checkable=True, checked=True)
        language.addAction(english)
        options.addSeparator()
        item(options, "Setup ECU Parameters", lambda: self.left_tabs.setCurrentWidget(self.runtime_tree), "Ctrl+E")

        ecu_menu = menus.addMenu("ECU Controls")
        self.connect_action = item(ecu_menu, "Connect", self.toggle_connection, "F3")
        item(ecu_menu, "Search for ECU", self.search_for_ecu, "Shift+F3")
        self.firmware_action = item(ecu_menu, "Update Firmware", self.update_firmware)
        item(ecu_menu, "Clear ECU Fault Codes", self.clear_faults)
        ecu_menu.addSeparator()
        item(ecu_menu, "ECU Log File Setup", self.setup_logging)
        self.log_download_action = item(ecu_menu, "ECU Log File Download", self.download_ecu_logs, "D")
        item(ecu_menu, "CAN Setup", self.can_setup)
        ecu_menu.addSeparator()
        item(ecu_menu, "Show ECU Statistics", self.show_statistics, "Ctrl+D")
        item(ecu_menu, "Update Statistics", self.update_statistics)
        item(ecu_menu, "Clear Statistics", self.clear_statistics)
        item(ecu_menu, "ECU Status and Fault Codes", self.show_status)
        ecu_menu.addSeparator()
        self.store_action = item(ecu_menu, "Store", self.store, "F4")
        self.feature_action = item(ecu_menu, "ECU Unlock", self.feature_unlock)
        self.factory_action = item(ecu_menu, "Restore to Factory Settings", self.restore_factory)
        ecu_menu.addSeparator()
        self.setup_password_action = item(ecu_menu, "Setup Password Protection", self.setup_password)
        self.unlock_password_action = item(ecu_menu, "Unlock Password Protection", self.unlock_password)
        ecu_menu.addSeparator()
        item(ecu_menu, "TPS Setup", self.tps_setup)
        item(ecu_menu, "MAP Sensor Calibration", self.calibrate_map)
        item(ecu_menu, "Baro Calibration", self.calibrate_baro)
        item(ecu_menu, "APS Calibration", self.calibrate_pedal)
        item(ecu_menu, "E-Throttle 1 Calibration", lambda: self.calibrate_throttle(0))
        item(ecu_menu, "E-Throttle 2 Calibration", lambda: self.calibrate_throttle(1))
        item(ecu_menu, "Set Base Timing", self.set_base_timing)
        item(ecu_menu, "Trigger Scope", self.trigger_scope)
        backup = item(ecu_menu, "Back Up Settings…", self.backup)
        self.ecu_controls_menu = ecu_menu
        self.backup_action = backup

        display = menus.addMenu("Display Mode")
        item(display, "ECU", lambda: self.display_stack.setCurrentIndex(0))
        item(display, "Log Analysis", lambda: self.display_stack.setCurrentIndex(1))
        display.addSeparator()
        item(display, "Full Screen with Menus (Ctrl + F4)", lambda: self.set_full_screen(1))
        item(display, "Full Screen (Ctrl + F4)", lambda: self.set_full_screen(2))
        QtGui.QShortcut("Ctrl+F4", self, activated=lambda: self.set_full_screen(0 if self.isFullScreen() else 2))

        self.layout_menu = menus.addMenu("Layout")
        self.layout_menu.aboutToShow.connect(self._fill_layout_menu)

        view = menus.addMenu("View")
        item(view, "ECU Settings", lambda: self.left_tabs.setCurrentIndex(0))
        item(view, "Log File Manager", lambda: self.left_tabs.setCurrentWidget(self.log_manager))
        item(view, "Parameters", lambda: self.left_tabs.setCurrentWidget(self.runtime_tree))
        item(view, "Event Log", lambda: self.left_tabs.setCurrentWidget(self.event_log))
        view.addSeparator()
        item(view, "Digital Gauge", lambda: self.add_live_view("Digital Gauge"), "Ctrl+F5")
        item(view, "Bar Gauge", lambda: self.add_live_view("Bar Gauge"), "Ctrl+F6")
        item(view, "Curve Gauge", lambda: self.add_live_view("Curved Bar"), "Ctrl+F7")
        item(view, "Dial Gauge", lambda: self.add_live_view("Dial Gauge"), "Ctrl+F8")
        item(view, "Target Gauge", lambda: self.add_live_view("Target Gauge"), "Ctrl+F9")
        item(view, "Runtime Value List", lambda: self.add_live_view("Runtime Value List"), "Ctrl+F10")
        item(view, "Help Browser", lambda: self.add_view("Help Browser"))
        item(view, "Vehicle Plan", lambda: self.add_view("Vehicle Plan"))

        logging_menu = menus.addMenu("Logging")
        item(logging_menu, "Open Log File", self.open_log)
        item(logging_menu, "Save Log File", self.save_log_file)
        item(logging_menu, "Save Log File As…", self.save_log_file_as)
        item(logging_menu, "Save All Log Files", self.save_all_logs)
        item(logging_menu, "Close log File", self.close_log_file)
        item(logging_menu, "Close All Log Files", self.close_all_logs)
        item(logging_menu, "Log File Details", self.log_details)
        item(logging_menu, "Export", self.export_log)
        logging_menu.addSeparator()
        self.play_action = item(logging_menu, "Play", self.toggle_play)
        self.log_action = item(logging_menu, "Record", self.toggle_logging, "F8")
        item(logging_menu, "Stop", self.stop_recording_or_play)
        item(logging_menu, "Setup Logging", self.setup_logging)
        item(logging_menu, "ECU Log File Download", self.download_ecu_logs)
        logging_menu.addSeparator()
        for title, view_type in (
            ("Navigator", "Navigator"), ("Time Plot", "Time Plot"), ("Logged Values List", "Value List"), ("XY Plot", "XY Plot"),
            ("Vehicle Plan", "Vehicle Plan"), ("Parameter List", "Parameter List"), ("Histogram", "Histogram"),
            ("Statistics", "Statistics"), ("GPS Plot", "GPS Plot"),
        ):
            item(logging_menu, title, lambda kind=view_type: self.add_view(kind))

        tuning = menus.addMenu("Tuning")
        item(tuning, "Runtime Values", self.show_runtime_values, "F12")
        item(tuning, "Quick Tune (F10)", self.quick_tune, "F10")
        item(tuning, "Mixture Map", self.mixture_map)
        item(tuning, "Knock Audio", self.knock_audio)
        tuning.addSeparator()
        item(tuning, "Reset All Tuning Markers", self.reset_markers, "F9")
        item(tuning, "Undo Short Term Changes", self.undo_changes)
        QtGui.QShortcut("F11", self, activated=lambda: self.quick_tune_window and self.quick_tune_window.reset_cell())
        QtGui.QShortcut("F6", self, activated=self.cycle_snake)

        help_menu = menus.addMenu("Help")
        item(help_menu, "Contents", lambda: self.show_topic("welcome"))
        item(help_menu, "Help for Selection", self.show_help, "F1")
        item(help_menu, "Keyboard shortcuts", lambda: self.show_topic("keyboard_shortcuts"))
        item(help_menu, "Wiring Information", lambda: self.show_topic("wiring_information"))
        item(help_menu, "ECU Fault Codes", lambda: self.show_topic("ecu_fault_codes"))
        help_menu.addSeparator()
        item(help_menu, "ECU Information", self.ecu_information)
        item(help_menu, "About…", self.about)

        toolbar = self.addToolBar("ECU")
        toolbar.setMovable(False)
        toolbar.addAction(self.connect_action)
        toolbar.addAction(self.store_action)
        self._apply_lock_state()

    # Allowed on a locked ECU, as in PCLink's menu handling.
    def _apply_lock_state(self) -> None:
        locked = self.protected
        for action in (self.feature_action, self.firmware_action):
            action.setEnabled(not locked)
        self.setup_password_action.setEnabled(not locked)
        self.unlock_password_action.setEnabled(locked)

    def _fill_layout_menu(self) -> None:
        menu = self.layout_menu
        menu.clear()
        menu.addAction("New Layout", self.new_layout)
        menu.addAction("Load Layout", self.load_layout)
        menu.addAction("Save Layout As…", self.save_layout)
        menu.addSeparator()
        menu.addAction("New Page", self.new_page)
        menu.addAction("Delete Page", self.delete_page)
        menu.addAction("Rename Page", self.rename_page)
        menu.addSeparator()
        for page in self.current_workspace().layout_.pages:
            menu.addAction(page.name, lambda name=page.name: self.current_workspace().show_page(name))

    def current_workspace(self) -> Workspace:
        return self.display_stack.currentWidget()

    # ---- files and options ------------------------------------------------

    def compare_close(self) -> None:
        if getattr(self, "compare_dialog", None) is not None:
            self.compare_dialog.close()
        self.compare_action_close.setEnabled(False)

    def show_memo(self) -> None:
        ecu = self.worker.ecu
        if ecu is None:
            self.show_error("Connect to an ECU or open a map first.")
            return
        if isinstance(ecu, OfflineEcu):
            self._edit_memo(ecu.map.memory.get("Memo", ("3", ""))[1])
        else:
            self.call(Ecu.read_memo, self._edit_memo)

    def _edit_memo(self, text: str) -> None:
        dialog = tools.MemoDialog(text, self)
        if not dialog.exec() or dialog.editor.toPlainText() == text:
            return
        new = dialog.editor.toPlainText()
        ecu = self.worker.ecu
        if isinstance(ecu, OfflineEcu):
            ecu.map.memory["Memo"] = ("3", new)
            ecu._dirty = True
            self._show_unstored()
        else:
            self.call(lambda live: live.set_memo(new), lambda _r: self.statusBar().showMessage("Remember to perform a store to save memo to ECU", 3000))

    def show_options(self, tab: str) -> None:
        dialog = OptionsDialog(self.options, self)
        dialog.show_tab(tab)
        if dialog.exec():
            self.options_changed()

    def colour_theme(self) -> None:
        tools.ColourThemeDialog(self, self).exec()

    def set_unit_system(self, name: str) -> None:
        self.options.system = name
        self.options_changed()
        self.statusBar().showMessage(f"{name} units", 3000)

    def set_full_screen(self, mode: int) -> None:
        """0 normal, 1 full screen with menus, 2 full screen without menus or the side panel."""
        self.menuBar().setVisible(mode != 2)
        self.left_tabs.setVisible(mode != 2)
        self.showFullScreen() if mode else self.showNormal()

    def ecu_information(self) -> None:
        ecu = self.worker.ecu
        if ecu is None:
            self.show_error("ECU information is not available.")
            return
        info = ecu.info
        enabled = "Enabled"
        lines = [
            f"MainCode Firmware Version: {info.firmware}", f"BootCode Firmware Version: {info.boot}",
            f"ECU Serial Number: {info.serial}", f"LinkID: {info.link_id}", f"ECU Enabled: {enabled}",
        ]
        QtWidgets.QMessageBox.information(self, "ECU Information", "\n".join(lines))

    def about(self) -> None:
        QtWidgets.QMessageBox.about(
            self, "Version Information",
            f"OpenPCLink {__version__}\nPython {sys.version.split()[0]}\nQt {QtCore.qVersion()}\n\nUnofficial. Not made or supported by Link.",
        )

    # ---- layouts ----------------------------------------------------------

    def new_layout(self) -> None:
        self.current_workspace().load(Layout([LayoutPage("Page 1")], "Page 1"))

    def load_layout(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Load Layout", self.options.folder("layout"), "Layout Files (*.llf5 *.llfx)")
        if path:
            self.load_layout_file(Path(path))

    def load_layout_file(self, path: Path) -> None:
        try:
            self.current_workspace().load(Layout.load(path))
        except LayoutError as exc:
            self.show_error(f"Could not load layout: {exc}")
            return
        if self.current_workspace() is self.workspace:
            self.options.recent_layout = str(path)
            self.options.save(self.options_path)

    def save_layout(self) -> None:
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save Layout As", self.options.folder("layout"), "Layout Files (*.llf5)")
        if path:
            target = Path(path).with_suffix(".llf5")
            self.current_workspace().layout_.save(target)
            if self.current_workspace() is self.workspace:
                self.options.recent_layout = str(target)
                self.options.save(self.options_path)

    def new_page(self) -> None:
        name, chosen = QtWidgets.QInputDialog.getText(self, "New Page", "Name:")
        if chosen and name:
            self.current_workspace().add_page(name)

    def delete_page(self) -> None:
        self.current_workspace().delete_page()

    def rename_page(self) -> None:
        name, chosen = QtWidgets.QInputDialog.getText(self, "Rename Page", "Name:")
        if chosen and name:
            self.current_workspace().rename_page(name)

    def add_view(self, view_type: str) -> None:
        self.current_workspace().add_view(view_type)

    def add_live_view(self, view_type: str) -> None:
        if not self.worker.ecu:
            self.show_error("Connect to an ECU or open a map first.")
            return
        wanted = "Choose Parameters" if view_type in ("Digital Gauge", "Runtime Value List") else "Choose Parameter" if view_type != "Target Gauge" else "Choose Target, then Actual"
        dialog = tools.ParameterChooser(wanted, self.live_choices(), [], self)
        if not dialog.exec() or not dialog.ids():
            return
        ids = dialog.ids()

        def prepare(view: LayoutView) -> None:
            view.groups[GROUP_ALL] = ids
            if view_type == "Runtime Value List":
                view.attributes.update(ColumnsCount="1", Caption="Runtime Values")

        frame = self.current_workspace().add_view(view_type, prepare)
        if frame is not None:
            self.workspace.refresh_live()

    def choose_live_parameters(self, frame: ViewFrame) -> None:
        dialog = tools.ParameterChooser("Choose Parameters", self.live_choices(), frame.view.groups.get(GROUP_ALL, []), self)
        if dialog.exec():
            frame.view.groups[GROUP_ALL] = dialog.ids()
            frame.body.refresh()
            self.layout_changed()

    def choose_log_parameters(self, frame: ViewFrame) -> None:
        log = self.log_session.log
        if log is None:
            return
        choices = [(series.data_id, series.name) for series in log.series if series.data_id]
        dialog = tools.ParameterChooser("Choose Parameters", choices, [data_id for data_id, _ in frame.view.log_parameters], self)
        if dialog.exec():
            frame.body.set_parameters(dialog.ids())
            self.layout_changed()

    def gauge_properties(self, frame: ViewFrame) -> None:
        settings = frame.body.body.settings
        if tools.GaugePropertiesDialog(settings, self).exec():
            frame.view.attributes.update(settings.attributes())
            frame.body.body.update()
            self.layout_changed()

    def show_runtime_values(self) -> None:
        if self.runtime_window is None:
            layout = tools.load_runtime_layout("RuntimeViewLayout")
            self.runtime_window = Workspace(self)
            self.runtime_window.setWindowTitle("Runtime Values")
            self.runtime_window.resize(900, 620)
            self.runtime_window.setStyleSheet(self.theme.style_sheet())
            self.runtime_window.load(layout or Layout([LayoutPage("Runtime Values", [LayoutView("Runtime Value List", 0, 0, 900, 600, attributes={"ColumnsCount": "3"}, groups={GROUP_ALL: [i for i, _ in self.live_choices()]})])]))
        self.runtime_window.show()
        self.runtime_window.raise_()

    def run_action_parameter(self, data_id: int) -> None:
        """Settings that are really buttons, which PCLink places in its settings tree."""
        {
            1682: self.calibrate_pedal, 1679: lambda: self.calibrate_throttle(0), 1661: lambda: self.calibrate_throttle(1),
            3550: self.set_base_timing, 2035: self.calibrate_map, 1123: self.show_memo, 1647: self.can_setup,
        }[data_id]()

    # ---- ECU controls -----------------------------------------------------

    def search_for_ecu(self) -> None:
        """Connect to the first ECU found, without asking which."""
        if self.worker.ecu:
            self._confirm_disconnect()
            return
        ports = list_ports()
        if ports:
            self.connect_to(ports[0].uri)
        else:
            self.show_error("No ECU found. Power it on and connect its USB cable.")

    def update_statistics(self) -> None:
        if self._live_ecu():
            self.call(Ecu.update_statistics, lambda _r: self.statusBar().showMessage("Statistics updated.", 3000))

    def show_statistics(self) -> None:
        if self.protected:
            self.show_error("Unable show ECU statistics, password protection is activated.")
            return
        stack = [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]
        for entry in stack:
            if entry.text(0) == "Statistics":
                self.tree.setCurrentItem(entry)
                self.update_statistics() if isinstance(self.worker.ecu, Ecu) else None
                return
        self.show_error("Connect to an ECU or open a map first.")

    def calibrate_baro(self) -> None:
        if not self._live_ecu():
            return
        value, chosen = QtWidgets.QInputDialog.getDouble(self, "Enter New Value", "Barometric pressure (kPa) equals", 101.0, -10000, 10000, 1)
        if chosen:
            self.worker.post("control", lambda ecu: ecu.calibrate_baro(value), "Baro sensor calibrated.")

    def _engine_stopped(self, message: str) -> bool:
        reading = self.reading(ENGINE_SPEED_ID)
        if reading is not None and reading.value and reading.value > 0:
            QtWidgets.QMessageBox.warning(self, "Engine Running!", message)
            return False
        return True

    def calibrate_throttle(self, throttle: int) -> None:
        if not self._live_ecu():
            return
        if not self._engine_stopped("TPS can not be calibrated while the engine is running."):
            return
        spec = tools.THROTTLE[throttle]
        ecu = self.worker.ecu
        mode = ecu.get(spec["mode"]) if spec["mode"] in ecu.table else None
        if mode == 0 and throttle == 0:
            self.tps_setup()
            return
        if mode != tools.ETHROTTLE_SETUP_MODE:
            self.show_error("Ethrottle TPS can only be Calibrated in Setup Mode.")
            return
        buttons = QtWidgets.QMessageBox.StandardButton
        answer = QtWidgets.QMessageBox.warning(
            self, "Warning",
            "This will automatically calibrate the Throttle Plate Position Sensor.\n * Ensure you have one analog channel set to TPS (Main) one set to TPS (Sub).\n * Ensure E-Throttle PID settings are sensible.\n\nThe throttle will move.",
            buttons.Ok | buttons.Cancel,
        )
        if answer == buttons.Ok:
            tools.ThrottleCalibration(self, throttle).exec()

    def calibrate_pedal(self) -> None:
        if not self._live_ecu() or not self._engine_stopped("APS can not be calibrated while the engine is running."):
            return
        buttons = QtWidgets.QMessageBox.StandardButton
        answer = QtWidgets.QMessageBox.warning(
            self, "Warning",
            "This will calibrate the Accelerator Position Sensor.\n\nMake sure you have one analog channel set to APS (Main)\n one set to APS (Sub) and Ethrottle is disabled.",
            buttons.Ok | buttons.Cancel,
        )
        if answer == buttons.Ok:
            tools.PedalCalibration(self).exec()

    def set_base_timing(self) -> None:
        if self._live_ecu():
            tools.BaseTimingDialog(self).exec()

    def trigger_scope(self) -> None:
        if not isinstance(self.worker.ecu, Ecu):
            self.show_error("ECU must be connected to use trigger scope function.")
            return
        self.windows["scope"] = tools.ScopeWindow(self)
        self.windows["scope"].show()

    def can_setup(self) -> None:
        if not self.worker.ecu:
            self.show_error("ECU must be connected or base map open to use this function")
            return
        tools.CanSetupDialog(self).exec()

    def feature_unlock(self) -> None:
        if not isinstance(self.worker.ecu, Ecu):
            self.show_error("ECU must be connected to unlock functions.")
            return
        dialog = tools.FeatureUnlockDialog(self.worker.ecu.info.serial, self)
        if dialog.exec():
            code = dialog.code.text()
            self.call(lambda ecu: ecu.feature_unlock(code), self._feature_unlocked)

    def _feature_unlocked(self, _result) -> None:
        QtWidgets.QMessageBox.information(self, "ECU Unlock", "Unlock code accepted.\n\nPerform a Store. The ECU's power may need cycling.")
        self.connect_to(self._uri)

    def restore_factory(self) -> None:
        if not isinstance(self.worker.ecu, Ecu):
            self.show_error("Reload function only available when ECU is online.")
            return
        if not self._engine_stopped("Operation cannot be completed while engine is running"):
            return
        buttons = QtWidgets.QMessageBox.StandardButton
        answer = QtWidgets.QMessageBox.warning(
            self, "Warning",
            "Do you want to restore all settings to factory defaults?\n\n WARNING, this will erase all current tuning settings and can not be undone!\n\n"
            "Be careful doing this while the ECU is connected to a vehicle as inputs and outputs may change state.",
            buttons.Yes | buttons.No, buttons.No,
        )
        if answer == buttons.Yes:
            self.call(Ecu.restore_factory, self._factory_restored)

    def _factory_restored(self, _result) -> None:
        self._on_connected(self.worker.ecu.info, self.worker.ecu.is_protected)
        if self._confirm("Message", "Reload command completed successfully. Do you want to store?"):
            self.worker.post("store")

    def setup_password(self) -> None:
        if not isinstance(self.worker.ecu, Ecu):
            self.show_error("Connect to an ECU first.")
            return
        if self.worker.ecu.is_protected:
            self.show_error("Before performing this operation, password protection must be deactivated.")
            return
        dialog = tools.PasswordDialog(True, self)
        if dialog.exec() and self._confirm("Setup Password Protection", "This locks the ECU and stores to it straight away. Without this password, its settings can only be recovered by a factory reset, which erases the tune. Continue?"):
            password = dialog.password.text()
            self.call(lambda ecu: ecu.set_password(password), lambda _r: self._lock_changed())

    def unlock_password(self) -> None:
        if not isinstance(self.worker.ecu, Ecu):
            self.show_error("Connect to an ECU first.")
            return
        dialog = tools.PasswordDialog(False, self)
        if dialog.exec():
            password, temporary = dialog.password.text(), dialog.temporary.isChecked()
            self.call(lambda ecu: ecu.unlock(password, temporary), self._unlock_result)

    def _unlock_result(self, unlocked: bool) -> None:
        if not unlocked:
            self.show_error("Unlock password protection failed, check password.")
        self._lock_changed()

    def _lock_changed(self) -> None:
        ecu = self.worker.ecu
        self._on_connected(ecu.info, ecu.is_protected)

    def update_firmware(self) -> None:
        ecu = self.worker.ecu
        if isinstance(ecu, Ecu):
            uri, info = self._uri, ecu.info
            current = tuple(int(part) for part in info.firmware.split(".")[:3])
            product = info.hardware_family
        else:
            dialog = ConnectDialog(self)
            if not dialog.exec() or not dialog.uri():
                return
            uri, current, product = dialog.uri(), None, 0
        if uri.startswith("udp:"):
            self.show_error("Can't update Firmware over Wifi")
            return
        reconnect = isinstance(ecu, Ecu)
        if ecu is not None:
            self.worker.post("disconnect")
        if tools.FirmwareDialog(self, uri, current, product).exec() or reconnect:
            if reconnect:
                self.connect_to(uri)

    # ---- tuning -----------------------------------------------------------

    def quick_tune(self) -> None:
        if not isinstance(self.worker.ecu, Ecu):
            QtWidgets.QMessageBox.information(self, "Information", "Quick tune function only available when ECU is online.")
            return
        if self.quick_tune_window is None:
            self.quick_tune_window = tools.QuickTuneWindow(self)
        if self.quick_tune_window.isVisible() and self.quick_tune_window.engine.mode != tune.OFF:
            self.quick_tune_window.tune_button.click()
            return
        self.quick_tune_window.show()
        self.quick_tune_window.raise_()

    def mixture_map(self) -> None:
        pages = self.workspace.layout_.pages
        if not any(page.name == "Mixture Map" for page in pages):
            self.display_stack.setCurrentIndex(0)
            self.add_view("Mixture Map")
        else:
            self.display_stack.setCurrentIndex(0)
            self.workspace.show_page("Mixture Map")
        page = self.workspace.current()
        views = page.bodies(MixtureMapView) if page else []
        if views:
            self.mixture_setup(views[0])

    def mixture_setup(self, view: MixtureMapView) -> None:
        names = [(data_id, self.describe(data_id)[0]) for data_id in (tune.FUEL_TABLE_1, tune.FUEL_TABLE_2) if self.database.known(data_id)]
        log = self.log_session.log
        choices = [(series.data_id, series.name) for series in log.series if series.data_id] if log else self.live_choices()
        if tools.MixtureSetupDialog(view.settings, names, sorted(choices, key=lambda pair: pair[1]), self).exec():
            view.view.attributes.update(view.settings.attributes())
            view.rebuild()
            self.layout_changed()

    def build_mixture(self, settings: tune.MixtureSettings, log) -> tuple[list, str]:
        ecu = self.worker.ecu
        if ecu is None or settings.table not in ecu.table or not self.database.known(settings.table):
            return [], "Fuel table parameter missing."
        parameter = self.database.parameter(settings.table)
        xs = used_axis(ecu, parameter.column_axis_id)
        ys = used_axis(ecu, parameter.row_axis_id)
        target = None
        target_parameter = self.database.parameter(tune.LAMBDA_TARGET_TABLE) if self.database.known(tune.LAMBDA_TARGET_TABLE) else None
        if target_parameter and tune.LAMBDA_TARGET_TABLE in ecu.table:
            t_xs, t_ys = used_axis(ecu, target_parameter.column_axis_id), used_axis(ecu, target_parameter.row_axis_id)
            columns = target_parameter.columns if target_parameter.columns > 1 else max(len(t_xs), 1)
            raw = ecu.get_all(tune.LAMBDA_TARGET_TABLE)
            values = [raw[r * columns + c] for r in range(max(len(t_ys), 1)) for c in range(len(t_xs))]
            target = lambda x, y: tune.bilinear(t_xs, t_ys, values, x, y)  # noqa: E731
        try:
            return tune.build_mixture_map(log, settings, xs, ys, target), ""
        except tune.MixtureError as exc:
            return [], str(exc)

    def correct_mixture_cell(self, table_id: int, row: int, column: int, actual: float, target: float) -> None:
        ecu = self.worker.ecu
        parameter = self.database.parameter(table_id)
        columns = parameter.columns if parameter.columns > 1 else ecu.table[table_id].count
        index = row * columns + column
        dual = ecu.get(tune.DUAL_FUEL_MODE) if tune.DUAL_FUEL_MODE in ecu.table else 0.0
        self.write(table_id, tune.tune_value(ecu.get(table_id, index), actual, target, tune.is_additive(table_id, dual)), index)
        self.marks.set_flag(table_id, row, column, tune.PART_TUNED)

    def knock_audio(self) -> None:
        self.windows.setdefault("knock", tools.KnockAudioWindow())
        self.windows["knock"].show()
        self.windows["knock"].raise_()

    def reset_markers(self) -> None:
        self.marks.clear_all()
        if self.quick_tune_window:
            self.quick_tune_window.engine.started = None
        self.form.refresh()
        self.statusBar().showMessage("All tuning markers reset.", 3000)

    def undo_changes(self) -> None:
        if not isinstance(self.worker.ecu, Ecu):
            return
        answer = QtWidgets.QMessageBox.warning(
            self, "Warning",
            "This function will restore all short term changes, marked in blue, back to their original values. Do you want to continue?",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
        )
        if answer == QtWidgets.QMessageBox.StandardButton.Yes:
            self.call(Ecu.undo_short_term_changes, self._undone)

    def _undone(self, restored: int) -> None:
        self.unstored_label.clear()
        self.statusBar().showMessage(f"Short term changes restored ({restored} bytes).", 5000)

    def cycle_snake(self) -> None:
        """Enabled, paused, off."""
        self.snake_state = (self.snake_state + 1) % 3
        _SnakeOverlay.enabled_trail = self.snake_state != 2
        _SnakeOverlay.paused = self.snake_state == 1
        self.statusBar().showMessage(("Snake Trails Enabled", "Snake Trails Paused", "Snake Trails Off")[self.snake_state], 3000)

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:
        """PCLink's single-key commands. Fields being typed in keep their keys."""
        key, shift = event.key(), event.modifiers() & QtCore.Qt.KeyboardModifier.ShiftModifier
        pages = {QtCore.Qt.Key.Key_BracketLeft: -1, QtCore.Qt.Key.Key_BracketRight: 1}
        if key in pages:
            tabs = self.current_workspace().tabs
            tabs.setCurrentIndex((tabs.currentIndex() + pages[key]) % max(tabs.count(), 1))
        elif key == QtCore.Qt.Key.Key_R:
            self.show_runtime_values()
        elif key == QtCore.Qt.Key.Key_E:
            self.left_tabs.setCurrentWidget(self.event_log)
        elif key == QtCore.Qt.Key.Key_P:
            self.left_tabs.setCurrentWidget(self.runtime_tree)
        elif key == QtCore.Qt.Key.Key_S or key == QtCore.Qt.Key.Key_Escape:
            self.left_tabs.setCurrentIndex(0)
        elif key == QtCore.Qt.Key.Key_U:
            self.set_unit_system("Metric" if self.options.system == "Imperial" else "Imperial")
        elif key == QtCore.Qt.Key.Key_B:
            self.mark_active_cells()
        elif key == QtCore.Qt.Key.Key_F:
            self.open_table_entry(tune.FUEL_TABLE_1)
        elif key == QtCore.Qt.Key.Key_I:
            self.open_table_entry(IGNITION_TABLE_ID)
        elif key == QtCore.Qt.Key.Key_T and shift:
            self.open_table_entry(tune.LAMBDA_TARGET_TABLE)
        else:
            super().keyPressEvent(event)

    def mark_active_cells(self) -> None:
        """B: mark where the engine is on every table, and drop a marker in the running log."""
        ecu = self.worker.ecu
        if ecu is None:
            return
        for data_id in ecu.table:
            if not self.database.known(data_id):
                continue
            parameter = self.database.parameter(data_id)
            if parameter.is_table and parameter.column_source_id is not None:
                columns = parameter.columns if parameter.columns > 1 else ecu.table[data_id].count
                rows = max(ecu.table[data_id].count // max(columns, 1), 1)
                position = self.table_position(parameter, rows, columns)
                if position:
                    self.marks.set_flag(data_id, *position, tune.RUNTIME_MARKER)
        logger = self.worker.logger
        if logger is not None:
            logger.add_marker("Key Press")
        self.form.refresh()

    # ---- connection -------------------------------------------------------

    def toggle_connection(self) -> None:
        if self.worker.ecu:
            self._confirm_disconnect()
            return
        dialog = ConnectDialog(self)
        if dialog.exec() and dialog.uri():
            self.connect_to(dialog.uri())

    def connect_to(self, uri: str) -> None:
        self._uri = uri
        self.statusBar().showMessage(f"Connecting to {uri}…")
        self.worker.post("connect", uri)

    def _confirm_disconnect(self) -> None:
        if self.worker.ecu.has_unstored_changes:
            choice = QtWidgets.QMessageBox.question(
                self, "Unstored changes",
                "Changes that are not stored are lost when the ECU powers off. Disconnect anyway?",
            )
            if choice != QtWidgets.QMessageBox.StandardButton.Yes:
                return
        self.worker.post("disconnect")

    def _set_online(self, online: bool) -> None:
        offline_map = isinstance(self.worker.ecu, OfflineEcu)
        self.status_label.setText("MAP FILE" if offline_map else "ONLINE" if online else "OFFLINE")
        self.status_label.setStyleSheet(
            f"background:{'#1b7f3b' if online else '#a12626'};color:white;padding:2px 8px;font-weight:bold"
        )
        self.connect_action.setText("Disconnect" if online else "Connect")
        self.store_action.setEnabled(online and not self.protected and not offline_map)
        self.centre.setCurrentIndex(1 if not self.protected else 0)

    def _on_connected(self, info, protected: bool) -> None:
        self.protected = protected
        self._update_units()
        self._set_online(True)
        self._apply_lock_state()
        self.event_log.add(f"Connected: firmware {info.firmware}, serial {info.serial}")
        self.statusBar().showMessage(f"Connected: firmware {info.firmware}, serial {info.serial}", 8000)
        self._build_gauges()
        if protected:
            self.placeholder.setText(
                "Password protection activated.\n\n"
                "This ECU was locked by its tuner, so its settings are hidden and cannot be changed.\n"
                "Unlock it with its password from ECU Controls > Unlock Password Protection."
            )
            self.tree.clear()
        else:
            self._build_tree()
        self._build_runtime_tree()
        self._set_online(True)
        for widget in self.workspace.findChildren(SettingsView):
            widget.filled = False
        self._fill_settings_views()
        self.workspace.refresh_live()

    def _on_disconnected(self, reason: str) -> None:
        self.protected = False
        self._set_online(False)
        self.placeholder.setText("Press F3 to connect to an ECU.")
        self.tree.clear()
        self.runtime_tree.clear()
        self.unstored_label.clear()
        self.log_state_label.setText("PC Log = Off")
        self._apply_lock_state()
        self.workspace.refresh_live()
        self.event_log.add(f"Disconnected: {reason}" if reason else "Disconnected", bool(reason))
        if reason:
            self.show_error(f"Connection failed: {reason}")

    # ---- menu tree --------------------------------------------------------

    def menu_path(self, data_id: int) -> tuple[str, ...]:
        """Where a setting sits now: PCLink's rules can move it to another menu."""
        rules = self.worker.rules
        moved = rules.parents.get(data_id) if rules else None
        return self.database.menu_path_from(moved) if moved else self.database.parameter(data_id).path

    def units_of(self, parameter: Parameter) -> str:
        return self.display_units(parameter)

    def parameter_visible(self, data_id: int) -> bool:
        ecu = self.worker.ecu
        if ecu is None or not self.database.is_visible(data_id, ecu.info.link_id, self._option_of):
            return False
        rules = self.worker.rules
        return rules is None or rules.visible(data_id)

    def _option_of(self, data_id: int) -> int | None:
        ecu = self.worker.ecu
        if ecu is None or data_id not in ecu.table or data_id in SECRET_IDS:
            return None
        try:
            return int(ecu.get(data_id))
        except (EcuProtected, ValueError):
            return None

    def _is_live(self, data_id: int) -> bool:
        ecu = self.worker.ecu
        return ecu.table[data_id].offset < ecu.info.runtime_size

    def _groups(self, live: bool) -> dict[tuple[str, ...], list[int]]:
        """Menu entries with something to show, each keeping all its parameters.

        Hidden ones are kept so the form can show them when the rules reveal them.
        """
        ecu = self.worker.ecu
        groups: dict[tuple[str, ...], list[int]] = defaultdict(list)
        for data_id in ecu.table:
            if data_id in SECRET_IDS or not self.database.known(data_id):
                continue
            parameter = self.database.parameter(data_id)
            # PCLink lists statistics among the settings, though they are live values.
            statistics = parameter.path[:1] == ("Statistics",)
            if self._is_live(data_id) != live and not (statistics and not live):
                continue
            path = self.menu_path(data_id)
            if path and (live or statistics or not parameter.is_runtime_value):
                groups[path].append(data_id)
        return {
            path: ids for path, ids in groups.items() if any(self.parameter_visible(i) for i in ids)
        }

    @staticmethod
    def _fill_tree(tree: QtWidgets.QTreeWidget, groups, sort_key) -> None:
        tree.clear()
        nodes: dict[tuple[str, ...], QtWidgets.QTreeWidgetItem] = {}
        for path in sorted(groups, key=sort_key):
            for depth in range(1, len(path) + 1):
                prefix = path[:depth]
                if prefix not in nodes:
                    parent = nodes.get(prefix[:-1])
                    nodes[prefix] = QtWidgets.QTreeWidgetItem(parent or tree, [prefix[-1]])
            nodes[path].setData(0, ID_ROLE, groups[path])

    def _add_table_entries(self, groups: dict) -> None:
        """PCLink gives each table its own menu entry under its form."""
        nodes = {}

        def index(item: QtWidgets.QTreeWidgetItem, path: tuple[str, ...]) -> None:
            nodes[path] = item
            for child in range(item.childCount()):
                index(item.child(child), path + (item.child(child).text(0),))

        for top in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(top)
            index(item, (item.text(0),))
        ecu = self.worker.ecu
        for path, ids in groups.items():
            tables = [i for i in ids if ecu.table[i].count > 1 and not ecu.table[i].is_bitfield and self.database.parameter(i).is_table]
            for data_id in sorted(tables, key=lambda i: -self.database.parameter(i).priority):
                if self.parameter_visible(data_id):
                    entry = QtWidgets.QTreeWidgetItem(nodes[path], [self.database.parameter(data_id).caption])
                    entry.setData(0, ID_ROLE, [data_id])

    def _build_tree(self) -> None:
        groups = self._groups(live=False)
        priority = {path: max(self.database.parameter(i).priority for i in ids) for path, ids in groups.items()}

        def sort_key(path: tuple[str, ...]):
            top = MENU_ORDER.index(path[0]) if path[0] in MENU_ORDER else len(MENU_ORDER)
            return (top, path[0], -priority[path] if len(path) > 1 else 0, path)

        self._fill_tree(self.tree, groups, sort_key)
        self._add_table_entries(groups)

    def _build_runtime_tree(self) -> None:
        tree = self.runtime_tree
        tree.clear()
        nodes: dict[tuple[str, ...], QtWidgets.QTreeWidgetItem] = {}
        groups = self._groups(live=True)
        for path in sorted(groups):
            for depth in range(1, len(path) + 1):
                prefix = path[:depth]
                if prefix not in nodes:
                    nodes[prefix] = QtWidgets.QTreeWidgetItem(nodes.get(prefix[:-1]) or tree, [prefix[-1]])
            visible = [i for i in groups[path] if self.parameter_visible(i)]
            for data_id in sorted(visible, key=lambda i: self.database.parameter(i).caption):
                item = QtWidgets.QTreeWidgetItem(nodes[path], [self.database.parameter(data_id).caption])
                item.setData(0, ID_ROLE, data_id)

    def _toggle_gauge(self, item: QtWidgets.QTreeWidgetItem, _column: int) -> None:
        data_id = item.data(0, ID_ROLE)
        if not isinstance(data_id, int):
            return
        if data_id in self.gauge_ids:
            self.gauge_ids.remove(data_id)
        else:
            self.gauge_ids.append(data_id)
        self._lay_out_gauges()

    def table_position(self, parameter: Parameter, rows: int, columns: int) -> tuple[int, int] | None:
        """Where the engine is now on a table, from its axis sources and breakpoints."""
        ecu = self.worker.ecu
        if isinstance(ecu, OfflineEcu) or ecu is None or parameter.column_source_id is None:
            return None
        column = self._axis_position(parameter.column_source_id, parameter.column_axis_id, columns)
        row = self._axis_position(parameter.row_source_id, parameter.row_axis_id, rows) if rows > 1 else 0
        return (row, column) if row is not None and column is not None else None

    def _axis_position(self, source_id: int | None, axis_id: int | None, size: int) -> int | None:
        ecu = self.worker.ecu
        if source_id not in ecu.table or axis_id not in ecu.table:
            return None
        live_id = int(ecu.get(source_id))
        if live_id not in ecu.table or live_id in SECRET_IDS:
            return None
        return axis_cell(ecu.get(live_id), ecu.get_all(axis_id)[:size])

    def _search_typed(self, text: str) -> None:
        if not self.options.search_on_enter:
            self._filter_tree(text)

    def _filter_tree(self, text: str) -> None:
        needle = text.lower()

        def visit(item: QtWidgets.QTreeWidgetItem) -> bool:
            show = needle in item.text(0).lower()
            ids = item.data(0, ID_ROLE) or []
            show = show or any(needle in self.database.parameter(i).caption.lower() or needle in self.database.parameter(i).keywords.lower() for i in ids)
            for index in range(item.childCount()):
                show = visit(item.child(index)) or show
            item.setHidden(not show)
            return show

        for index in range(self.tree.topLevelItemCount()):
            visit(self.tree.topLevelItem(index))

    def _form_ids(self, item: QtWidgets.QTreeWidgetItem) -> list[int]:
        """A folder with no settings of its own shows its children's, as in PCLink."""
        ids = list(item.data(0, ID_ROLE) or [])
        if not ids:
            for child in range(item.childCount()):
                for data_id in self._form_ids(item.child(child)):
                    if data_id not in ids:
                        ids.append(data_id)
        return ids

    def open_table_entry(self, data_id: int) -> None:
        stack = [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]
        while stack:
            item = stack.pop()
            if item.data(0, ID_ROLE) == [data_id]:
                self.tree.setCurrentItem(item)
                return
            stack.extend(item.child(i) for i in range(item.childCount()))

    def _tree_selected(self, item: QtWidgets.QTreeWidgetItem | None, _previous) -> None:
        ids = self._form_ids(item) if item else []
        self._selected_parameters = [self.database.parameter(i) for i in ids]
        self.form.show_parameters(self._selected_parameters)
        tables = [p.data_id for p in self._selected_parameters if p.is_table]
        if tables:
            self.current_table_id = tables[0]
        self.show_help()

    # ---- live values ------------------------------------------------------

    def _build_gauges(self) -> None:
        ecu = self.worker.ecu
        by_caption = {}
        for data_id, loc in ecu.table.items():
            if loc.offset < ecu.info.runtime_size and self.database.known(data_id):
                by_caption.setdefault(self.database.parameter(data_id).caption, data_id)
        self.gauge_ids = [by_caption[c] for c in GAUGE_CAPTIONS if c in by_caption]
        self._lay_out_gauges()

    def _lay_out_gauges(self) -> None:
        self.workspace.refresh_live()

    def _show_fault(self, ecu) -> None:
        if StatusWindow.FAULT_CODE_ID not in ecu.table or not ecu.get(StatusWindow.FAULT_CODE_ID):
            self.fault_label.clear()
            return
        parameter = self.database.parameter(StatusWindow.FAULT_CODE_ID)
        text = self._format_live(parameter, ecu.get(StatusWindow.FAULT_CODE_ID), 0, "")
        if text != self.fault_label.text():
            self.event_log.add(f"ECU Fault Code: {text}", True)
        self.fault_label.setText(text)

    def _live_ecu(self) -> bool:
        if isinstance(self.worker.ecu, Ecu):
            return True
        self.show_error("Connect to an ECU first.")
        return False

    def _confirm(self, title: str, message: str) -> bool:
        choice = QtWidgets.QMessageBox.question(self, title, message)
        return choice == QtWidgets.QMessageBox.StandardButton.Yes

    def clear_faults(self) -> None:
        if self._live_ecu() and self._confirm("Clear Fault Codes", "This will clear all fault codes stored in the ECU, are you sure?"):
            self.worker.post("control", Ecu.clear_faults, "Fault codes cleared.")

    def clear_statistics(self) -> None:
        if self._live_ecu() and self._confirm("Clear Statistics", "Are you sure you want to clear ECU statistics?"):
            self.worker.post("control", Ecu.clear_statistics, "Statistics cleared.")

    def calibrate_map(self) -> None:
        if self._live_ecu() and self._confirm("MAP Sensor Calibration", "Do you want to perform a MAP Sensor Calibration? Key on, engine stopped."):
            self.worker.post("control", Ecu.calibrate_map, "MAP sensor calibrated. Check MAP matches BAP, then Store.")

    def tps_setup(self) -> None:
        if not self._live_ecu():
            return
        buttons = QtWidgets.QMessageBox.StandardButton
        choice = QtWidgets.QMessageBox.information(
            self, "TPS Span Adjustment", "Depress THROTTLE to fully OPEN position.", buttons.Ok | buttons.Cancel
        )
        if choice != buttons.Ok:
            return
        self.worker.post("control", lambda ecu: ecu.set_tps_span(TPS_OPEN_ID), "TPS open point saved.")
        QtWidgets.QMessageBox.information(self, "TPS Span Adjustment", "Release THROTTLE to CLOSED position.")
        self.worker.post("control", lambda ecu: ecu.set_tps_span(TPS_CLOSED_ID), "TPS closed point saved. Store to keep it.")

    def _on_controlled(self, message: str) -> None:
        self.statusBar().showMessage(message, 6000)
        self._show_unstored()
        self.form.refresh()

    def show_status(self) -> None:
        if not self.worker.ecu:
            self.show_error("Connect to an ECU or open a map first.")
            return
        self.status_window = StatusWindow(self)
        self.status_window.show()

    def _on_runtime(self) -> None:
        ecu = self.worker.ecu
        if ecu is None:
            return
        if self.status_window is not None and self.status_window.isVisible():
            self.status_window.refresh()
        self._show_fault(ecu)
        self.workspace.refresh_live()
        self.rate_label.setText(f"{ecu.link.rate():.1f}Hz" if hasattr(ecu.link, "rate") else "")
        if self.runtime_window is not None and self.runtime_window.isVisible():
            self.runtime_window.refresh_live()
        if self.quick_tune_window is not None and self.quick_tune_window.isVisible():
            self.quick_tune_window.refresh()
        self._auto_record(ecu)
        for data_id, editor in self.form.editors.items():
            if self._is_live(data_id):
                self.form.refresh(data_id)
            elif isinstance(editor, TableEditor):
                editor.update_trace()

    @staticmethod
    def _format_live(parameter: Parameter, value: float, decimals: int, units: str = "") -> str:
        if parameter.options and 0 <= int(value) < len(parameter.options):
            return parameter.options[int(value)]
        return f"{value:.{max(decimals, 0)}f} {units}".strip()

    # ---- writing ----------------------------------------------------------

    def _on_visibility_changed(self) -> None:
        current = self.tree.currentItem()
        path = []
        while current is not None:
            path.insert(0, current.text(0))
            current = current.parent()
        parameters = self.form.parameters
        self._build_tree()
        self.form.show_parameters(parameters)
        self._reselect(path)

    def _reselect(self, path: list[str]) -> None:
        items = [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]
        found = None
        for name in path:
            found = next((item for item in items if item.text(0) == name), None)
            if found is None:
                return
            found.setExpanded(True)
            items = [found.child(i) for i in range(found.childCount())]
        if found is not None:
            self.tree.blockSignals(True)
            self.tree.setCurrentItem(found)
            self.tree.blockSignals(False)

    def choose_option(self, data_id: int, option: int) -> None:
        warning = self.database.warning_for(data_id, option)
        if warning and self.worker.ecu and not isinstance(self.worker.ecu, OfflineEcu):
            answer = QtWidgets.QMessageBox.warning(
                self, "Check before changing", warning,
                QtWidgets.QMessageBox.StandardButton.Ok | QtWidgets.QMessageBox.StandardButton.Cancel,
            )
            if answer != QtWidgets.QMessageBox.StandardButton.Ok:
                self.form.refresh(data_id)
                return
        self.write(data_id, option, 0)

    def write(self, data_id: int, value: float, index: int) -> None:
        self.worker.post("set", data_id, value, index)

    def write_text(self, data_id: int, text: str) -> None:
        self.worker.post("set_string", data_id, text)

    def _on_wrote(self, data_id: int, _index: int) -> None:
        ecu = self.worker.ecu
        if ecu and self.database.known(data_id) and self.database.parameter(data_id).children_by_option:
            self.form.show_parameters(self.form.parameters)  # Its option can show or hide others.
        self.form.refresh()
        self._show_unstored()

    def _show_unstored(self) -> None:
        ecu = self.worker.ecu
        if ecu and ecu.has_unstored_changes:
            self.unstored_label.setText(
                "Unsaved map changes" if isinstance(ecu, OfflineEcu) else "Unstored changes (F4 to store)"
            )
        elif ecu:
            self.unstored_label.clear()

    def store(self) -> None:
        choice = QtWidgets.QMessageBox.question(
            self, "Store to ECU", "Permanently save the current settings to the ECU? This cannot be undone."
        )
        if choice == QtWidgets.QMessageBox.StandardButton.Yes:
            self.worker.post("store")

    def _on_stored(self) -> None:
        self.event_log.add("Store completed successfully")
        self.unstored_label.clear()
        self.form.refresh()
        self.statusBar().showMessage("Stored to ECU.", 6000)

    def backup(self) -> None:
        if not self.worker.ecu:
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Back up settings", "ecu-backup.json", "JSON (*.json)")
        if path:
            try:
                self.worker.ecu.backup(Path(path))
                self.statusBar().showMessage(f"Saved {path}", 6000)
            except (EcuProtected, OSError) as exc:
                self.show_error(str(exc))

    # ---- map files --------------------------------------------------------

    def _map_dialog_filter(self) -> str:
        return "PCLink maps (*.pclx *.pcl5)"

    def open_map(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Open map", self.options.folder("map"), self._map_dialog_filter())
        if path:
            self.open_map_file(Path(path))

    def open_map_file(self, path: Path) -> None:
        if not isinstance(self.worker.ecu, Ecu):
            self.load_map(path)
            return
        buttons = QtWidgets.QMessageBox.StandardButton
        choice = QtWidgets.QMessageBox.warning(
            self, "Warning",
            "Select YES to open the file and download the settings to the ECU, or NO to disconnect the ECU before opening the file.",
            buttons.Yes | buttons.No | buttons.Cancel,
        )
        if choice == buttons.No:
            self.worker.post("disconnect")
            self.load_map(path)
        elif choice == buttons.Yes:
            self.download_map(path)

    def download_map(self, path: Path) -> None:
        loaded = self._read_map(path, self.worker.ecu.info.serial)
        if loaded is None:
            return
        self.statusBar().showMessage("Downloading data to ECU ...")
        self.call(lambda ecu: ecu.load_map(loaded.entries.values()), self._map_downloaded)

    def _map_downloaded(self, cells: int) -> None:
        self.form.show_parameters(self.form.parameters)
        self.statusBar().showMessage(f"{cells} values changed.", 6000)
        if self._confirm("Store", "File opened and downloaded successfully.\nDo you wish to carry out a store command on the ECU?"):
            self.worker.post("store")

    def load_map(self, path: Path) -> None:
        loaded = self._read_map(path)
        if loaded:
            self.worker.post("offline", OfflineEcu(loaded, path))

    def _read_map(self, path: Path, serial: int | None = None) -> MapFile | None:
        try:
            return MapFile.load(path, self.database.map_key, serial)
        except ProtectedFile as exc:
            self.show_error(str(exc))
        except (BadContainer, MapError, OSError, ValueError) as exc:
            self.show_error(f"Could not open {path.name}: {exc}")
        return None

    def save_map(self) -> None:
        ecu = self.worker.ecu
        if ecu is None:
            return
        default = str(Path(self.options.folder("map")) / ("tune.pcl5" if ecu.info.link_id >= 50000 else "tune.pclx"))
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save map", default, self._map_dialog_filter())
        if path:
            self.save_map_to(Path(path))

    def save_map_quick(self) -> None:
        """Save over the open file. A session with no file yet asks for a name."""
        if self._map_path is None and isinstance(self.worker.ecu, OfflineEcu):
            self._map_path = self.worker.ecu.path
        if self._map_path is None:
            self.save_map()
        else:
            self.save_map_to(self._map_path)

    def save_map_encrypted(self) -> None:
        ecu = self.worker.ecu
        if ecu is None:
            return
        dialog = tools.SerialDialog(ecu.info.serial if isinstance(ecu, Ecu) else None, self)
        if not dialog.exec():
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save as Encrypted", self.options.folder("map"), self._map_dialog_filter())
        if not path:
            return
        try:
            content = ecu.map if isinstance(ecu, OfflineEcu) else MapFile.from_ecu(ecu, self.database, keep_lock=True)
            content.save(Path(path), self.database.map_key, dialog.value())
            self.statusBar().showMessage("File saved successfully.", 6000)
        except (EcuProtected, OSError) as exc:
            self.show_error(str(exc))

    def save_map_to(self, path: Path) -> None:
        ecu = self.worker.ecu
        try:
            if isinstance(ecu, OfflineEcu):
                ecu.save(path, self.database.map_key)
                self.unstored_label.clear()
            else:
                MapFile.from_ecu(ecu, self.database).save(path, self.database.map_key)
            self._map_path = Path(path)
            self.statusBar().showMessage(f"Saved {path}", 6000)
        except (EcuProtected, OSError) as exc:
            self.show_error(str(exc))

    def compare_map(self) -> None:
        ecu = self.worker.ecu
        if ecu is None:
            return
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Compare with map", "", self._map_dialog_filter())
        if not path:
            return
        other = self._read_map(Path(path))
        if other is None:
            return
        try:
            current = ecu.map if isinstance(ecu, OfflineEcu) else MapFile.from_ecu(ecu, self.database)
        except EcuProtected as exc:
            self.show_error(str(exc))
            return
        self.compare_action_close.setEnabled(True)
        self._show_differences(compare(current, other))

    def _show_differences(self, differences) -> None:
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(f"{len(differences)} settings differ")
        dialog.resize(760, 480)
        table = QtWidgets.QTableWidget(len(differences), 4)
        table.setHorizontalHeaderLabels(["Setting", "Cells", "Current", "Map file"])
        table.horizontalHeader().setStretchLastSection(True)
        table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        for row, difference in enumerate(differences):
            known = self.database.known(difference.data_id)
            caption = self.database.parameter(difference.data_id).caption if known else str(difference.data_id)
            for column, value in enumerate((caption, difference.cells, difference.first_a, difference.first_b)):
                table.setItem(row, column, QtWidgets.QTableWidgetItem(f"{value:g}" if isinstance(value, float) else str(value)))
        table.resizeColumnToContents(0)
        QtWidgets.QVBoxLayout(dialog).addWidget(table)
        dialog.show()
        self.compare_dialog = dialog

    # ---- logging ----------------------------------------------------------

    def recorded_ids(self) -> list[int]:
        saved = self.options.log_settings.get("record_ids")
        if saved:
            return list(saved)
        path = assets.asset("LoggingOptions.xml")
        if path:
            import xml.etree.ElementTree as ET

            try:
                node = ET.parse(path).getroot().find("LogFileManager")
                return [int(item) for item in node.get("RecordIDs", "").split(",") if item.strip().isdigit() and int(item)]
            except (OSError, ET.ParseError, AttributeError):
                pass
        return list(self.gauge_ids)

    def toggle_logging(self) -> None:
        if self.worker.logger:
            self.stop_logging()
            return
        if not isinstance(self.worker.ecu, Ecu):
            self.show_error("Connect to an ECU to start logging.")
            return
        self.start_logging(None)

    def start_logging(self, path: Path | None) -> None:
        ecu = self.worker.ecu
        ids = [data_id for data_id in (self.gauge_ids if path else self.recorded_ids()) if data_id in ecu.table and data_id not in SECRET_IDS and self.database.known(data_id)]
        channels = [(i, self.database.parameter(i).caption) for i in ids]
        units = [self.database.parameter(i).units for i in ids]
        settings = self.options.log_settings
        rate = float(settings.get("rate", "20 Hz").split()[0])
        logger = PcLogger(path, channels, units, 1.0 / rate if rate else 0.0, bool(settings.get("keep_all", False)))
        logger.log.details["ECU"] = ""
        logger.log.details["Firmware Version"] = ecu.info.firmware
        logger.log.details["Serial Number"] = str(ecu.info.serial)
        logger.log.details["Software Version"] = f"OpenPCLink {__version__}"
        try:
            if path:
                Path(path).touch()
        except OSError as exc:
            self.show_error(str(exc))
            return
        self.worker.logger = logger
        self.log_action.setText("Stop Recording")
        self.log_state_label.setText("PC Log = On")
        self.statusBar().showMessage(f"Recording {len(channels)} values")

    def stop_logging(self) -> None:
        logger, self.worker.logger = self.worker.logger, None
        if logger:
            try:
                logger.close()
            except OSError as exc:
                self.show_error(str(exc))
            self.log_session.add(logger.log)
            self.statusBar().showMessage(f"Recorded {logger.samples} samples", 8000)
        self.log_action.setText("Record")
        self.log_state_label.setText("PC Log = Off")

    def _auto_record(self, ecu) -> None:
        settings = self.options.log_settings
        source = int(settings.get("auto_id", 0))
        if not source or source not in ecu.table:
            return
        above = ecu.get(source) > float(settings.get("auto_value", 50))
        if above and not self.worker.logger:
            self.start_logging(None)
        elif not above and self.worker.logger and self.worker.logger.path is None:
            self.stop_logging()

    def stop_recording_or_play(self) -> None:
        if self.worker.logger:
            self.stop_logging()
        self.log_timer.stop()
        self.play_action.setText("Play")

    def toggle_play(self) -> None:
        if self.log_timer.isActive():
            self.log_timer.stop()
            self.play_action.setText("Play")
        elif self.log_session.log is not None:
            self.log_timer.start()
            self.play_action.setText("Pause")

    def _play_step(self) -> None:
        session = self.log_session
        log = session.log
        if log is None or not log.time or session.cursor >= log.time[-1]:
            self.log_timer.stop()
            self.play_action.setText("Play")
            return
        session.set_cursor(session.cursor + PLAYBACK_INTERVAL_MS / 1000)

    def open_log(self) -> None:
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(self, "Open Log File", self.options.folder("log"), "Log Files (*.llg5 *.llgx *.csv)")
        for path in paths:
            self.open_log_path(Path(path))

    def open_log_path(self, path: Path) -> None:
        try:
            self.log_session.add(read_log(path))
        except (OSError, ValueError, StopIteration, LogFormatError) as exc:
            self.show_error(f"Could not open that log: {exc}")
            return
        self.display_stack.setCurrentIndex(1)

    def open_downloaded_logs(self, logs: list) -> None:
        for log in logs:
            log.details.setdefault("Name", "ECU Log " + time.strftime("%Y-%m-%d %H;%M;%S"))
            self.log_session.add(log)
        self.display_stack.setCurrentIndex(1)

    def _save_log(self, log, path: Path) -> bool:
        try:
            save_log(log, path)
            return True
        except OSError as exc:
            self.show_error(f"Failed to save file! {exc}")
            return False

    def save_log_file(self) -> None:
        log = self.log_session.log
        if log is None:
            return
        if log.path and log.path.suffix.lower() != ".csv":
            self._save_log(log, log.path)
            self.log_manager.refresh()
        else:
            self.save_log_file_as()

    def save_log_file_as(self) -> None:
        log = self.log_session.log
        if log is None:
            return
        name = log.details.get("Name") or "PC Datalog"
        extension = ".llg5" if str(log.details.get("Firmware Version", "7")).startswith("7") else ".llgx"
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save Log File", str(Path(self.options.folder("log")) / f"{name}{extension}"), "Link Log Files (*.llg5 *.llgx)")
        if path and self._save_log(log, Path(path)):
            self.log_manager.refresh()

    def save_all_logs(self) -> None:
        for index in range(len(self.log_session.logs)):
            self.log_session.select(index)
            if self.log_session.log.modified:
                self.save_log_file()

    def _logs_may_close(self, logs: list) -> bool:
        unsaved = [log for log in logs if log.modified]
        if not unsaved:
            return True
        return self._confirm("Unsaved logs", f"{len(unsaved)} log file(s) have not been saved. Close anyway?")

    def close_log_file(self) -> None:
        log = self.log_session.log
        if log is not None and self._logs_may_close([log]):
            self.log_session.close_log(log)

    def close_all_logs(self) -> None:
        if self._logs_may_close(self.log_session.logs):
            self.log_session.close_all()

    def log_details(self) -> None:
        if self.log_session.log is not None and tools.LogDetailsDialog(self.log_session.log, self).exec():
            self.log_manager.refresh()

    def export_log(self) -> None:
        if self.log_session.log is None:
            return
        dialog = tools.ExportDialog(self)
        if not dialog.exec():
            return
        extension, label = (".llg5", "Link Log Files (*.llg5)") if dialog.format.currentIndex() else (".csv", "CSV (*.csv)")
        name = self.log_session.log.details.get("Name") or "log"
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Export", str(Path(self.options.folder("log")) / f"{name} - Export{extension}"), label)
        if path:
            try:
                tools.export_log(self.log_session, dialog, Path(path))
            except OSError as exc:
                self.show_error(str(exc))

    def setup_logging(self) -> None:
        tools.LoggingSetupDialog(self, self).exec()

    def download_ecu_logs(self) -> None:
        if not isinstance(self.worker.ecu, Ecu):
            self.show_error("Connect to an ECU first.")
            return
        dialog = tools.LogDownloadDialog(self, self)
        self.report_progress.connect(dialog.progress.setValue)
        dialog.exec()
        self.report_progress.disconnect(dialog.progress.setValue)


    # ---- help -------------------------------------------------------------

    def show_help(self) -> None:
        if not self.manual.pages:
            self.help_view.setPlainText("The manual was not imported with this build.")
            return
        focus = self.form.parameters[0] if self.form.parameters else None
        for parameter in sorted(self.form.parameters, key=lambda item: -item.priority):
            if parameter.help_url:
                focus = parameter
                break
        page = self.manual.page_for(focus.help_url) if focus else None
        self._show_page(self.help_view, page or "<p>No manual page for this selection.</p>")

    def _show_page(self, browser: QtWidgets.QTextBrowser, html: str) -> None:
        browser.setSearchPaths(self.manual.image_folders())
        browser.setHtml(html)

    def show_topic(self, name: str) -> None:
        """A manual page in its own window, as PCLink opens its help file."""
        if self.help_window is None:
            self.help_window = QtWidgets.QTextBrowser()
            self.help_window.setWindowTitle("Link ECU Online Manual")
            self.help_window.setOpenLinks(False)
            self.help_window.anchorClicked.connect(lambda url: self._help_link(url, self.help_window))
            self.help_window.resize(860, 640)
        self._show_page(self.help_window, self.manual.page_for(name) or f"<p>The manual has no page called {name}.</p>")
        self.help_window.show()
        self.help_window.raise_()

    def _help_link(self, url: QtCore.QUrl, browser: QtWidgets.QTextBrowser | None = None) -> None:
        name = Path(url.path()).stem or url.toString()
        page = self.manual.page_for(name)
        if page:
            self._show_page(browser or self.help_view, page)

    def show_error(self, message: str) -> None:
        QtWidgets.QMessageBox.warning(self, "OpenPCLink", message)

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        ecu = self.worker.ecu
        if ecu and ecu.has_unstored_changes:
            choice = QtWidgets.QMessageBox.question(
                self, "Unstored changes", "Changes that are not stored are lost when the ECU powers off. Quit anyway?"
            )
            if choice != QtWidgets.QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        if not self._logs_may_close(self.log_session.logs):
            event.ignore()
            return
        for extra in self.windows.values():
            extra.close()
        if self.quick_tune_window is not None:
            self.quick_tune_window.close()
        if self.runtime_window is not None:
            self.runtime_window.close()
        self.layout_changed()
        self.worker.post("quit")
        self.worker.wait(3000)
        event.accept()


def _fallback_layout() -> Layout:
    """For an install imported before layouts were read, until it is imported again."""
    settings = LayoutView("Auto Settings", 0, 0, 620, 630, guid="{00000000-0000-0000-0000-000000000001}", attributes={"BaseCaption": ""})
    help_view = LayoutView("Help Browser", 620, 0, 350, 370)
    live = LayoutView("Runtime Value List", 620, 370, 350, 260, attributes={"ColumnsCount": "1", "Caption": "Live"}, groups={GROUP_ALL: [1000, 1001, 1004, 1011, 1078, 3040, 1012]})
    return Layout([LayoutPage("Configuration", [settings, help_view, live], current_settings_view=settings.guid)], "Configuration")


def _fallback_log_layout() -> Layout:
    plot = LayoutView("Time Plot", 0, 0, 970, 400)
    navigator = LayoutView("Navigator", 0, 400, 970, 110)
    return Layout([LayoutPage("Time Plot", [plot, navigator])], "Time Plot")


def _load_database(parent: QtWidgets.QWidget | None = None) -> Database | None:
    while True:
        try:
            return Database.load()
        except DatabaseMissing:
            answer = QtWidgets.QMessageBox.information(
                parent, "PCLink data needed",
                "OpenPCLink reads parameter names and limits from your own copy of PCLink.\n\n"
                "Choose your PCLink installer (.exe), or an installed PCLink folder's ECU.cfgr.",
                QtWidgets.QMessageBox.StandardButton.Open | QtWidgets.QMessageBox.StandardButton.Cancel,
            )
            if answer != QtWidgets.QMessageBox.StandardButton.Open:
                return None
            path, _ = QtWidgets.QFileDialog.getOpenFileName(
                parent, "Select PCLink", str(Path.home()), "PCLink (*.exe ECU.cfgr)"
            )
            if not path:
                return None
            source = Path(path)
            try:
                import_pclink(source.parent if source.name == "ECU.cfgr" else source)
            except (ImportFailed, OSError, ValueError) as exc:
                QtWidgets.QMessageBox.critical(parent, "Import failed", str(exc))


def main() -> int:
    enable_log_file()
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("OpenPCLink")
    app.setApplicationVersion(__version__)
    database = _load_database()
    if database is None:
        return 1
    window = MainWindow(database)
    if window.options.start_full_screen:
        window.set_full_screen(2)
    if os.environ.get(READ_ONLY_ENV):
        window.setWindowTitle(f"{window.windowTitle()} — READ ONLY")
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
