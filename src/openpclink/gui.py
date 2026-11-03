"""Desktop tuning interface. All ECU traffic runs on one worker thread."""
from __future__ import annotations

import os
import queue
import sys
from collections import defaultdict
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from . import __version__
from .crypto import BadContainer, ProtectedFile
from .database import MENU_ORDER, Database, DatabaseMissing, HelpManual, Parameter
from .diagnostics import enable_log_file
from .ecu import ENGINE_SPEED_ID, SECRET_IDS, Ecu, EcuProtected, StoreUnconfirmed, WriteMismatch, WriteRefused
from .importer import ImportFailed, import_pclink, rules_table_path
from .logfile import PcLogger, read_log
from .mapfile import MapError, MapFile, OfflineEcu, compare
from .protocol import READ_ONLY_ENV, ForbiddenCommand, ProtocolError
from .native_rules import NativeRules, RulesTable
from .views import GaugePanel, LogViewer, SurfaceView, axis_cell, heat_colour, used_axis_length
from .transports import TransportError, list_ports, open_transport

POLL_INTERVAL_S = 0.1
ID_ROLE = QtCore.Qt.ItemDataRole.UserRole
GAUGE_CAPTIONS = ("Engine Speed", "MAP", "ECT", "IAT", "Batt Voltage", "TPS (Main)", "Lambda 1", "Ignition Angle")


class Worker(QtCore.QThread):
    connected = QtCore.Signal(object, bool)
    disconnected = QtCore.Signal(str)
    runtime = QtCore.Signal()
    wrote = QtCore.Signal(int, int)
    failed = QtCore.Signal(str)
    stored = QtCore.Signal()
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
        elif name == "store":
            try:
                self.ecu.store()
            except (StoreUnconfirmed, ForbiddenCommand) as exc:
                self.failed.emit(str(exc))
            else:
                self.stored.emit()

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


class ParameterForm(QtWidgets.QWidget):
    """Edits the parameters of one menu entry."""

    def __init__(self, window: "MainWindow") -> None:
        super().__init__()
        self.window_ = window
        self.layout_ = QtWidgets.QFormLayout(self)
        self.editors: dict[int, QtWidgets.QWidget] = {}
        self.parameters: list[Parameter] = []
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
            return combo
        if loc.count > 1 and not loc.is_bitfield:
            if len(self.parameters) > 1 and parameter.is_table:
                button = QtWidgets.QPushButton("Open table…")
                button.clicked.connect(lambda: self.window_.open_table_entry(data_id))
                return button
            return TableEditor(self.window_, parameter, loc.count, read_only)
        spin = QtWidgets.QDoubleSpinBox()
        spin.setKeyboardTracking(False)
        if loc.is_bitfield:
            decimals = 0
        else:
            decimals = parameter.decimals if parameter.decimals is not None else (0 if loc.scale == 1 else 3)
        spin.setDecimals(decimals)
        spin.setRange(
            parameter.minimum if parameter.minimum is not None else -1e9,
            parameter.maximum if parameter.maximum is not None else 1e9,
        )
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
            self.window_.write(data_id, spin.value(), 0)

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
        elif isinstance(editor, QtWidgets.QComboBox):
            editor.setCurrentIndex(editor.findData(int(ecu.get(data_id))))
        elif isinstance(editor, TableEditor):
            editor.refresh()
        elif isinstance(editor, QtWidgets.QDoubleSpinBox) and not editor.hasFocus():
            editor.blockSignals(True)
            editor.setValue(ecu.get(data_id))
            editor.setProperty("shown", editor.value())
            editor.blockSignals(False)


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
        toggle.clicked.connect(lambda: self.stack.setCurrentIndex(1 - self.stack.currentIndex()))
        toggle.setEnabled(self.grid.rowCount() > 1)
        hint = QtWidgets.QLabel("+ / − nudge selection,  = set,  * scale %,  Ctrl+C / Ctrl+V copy and paste")
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
        shown = None if held is None else (f"{held:.{decimals}f}" if decimals is not None else f"{held:g}")
        if item.text() != shown:
            self.window_.write(self.parameter.data_id, value, self._index(item))

    def _apply(self, change) -> None:
        """Apply a function to each selected cell."""
        for item in self.grid.selectedItems():
            self.window_.write(self.parameter.data_id, change(float(item.text())), self._index(item))

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
                    self.window_.write(self.parameter.data_id, value, row * self.grid.columnCount() + column)

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
        values = ecu.get_all(self.parameter.data_id)
        self.grid.blockSignals(True)
        decimals = self.parameter.decimals
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
            cell.setBackground(QtGui.QColor(*heat_colour(value, low, high)))
            cell.setForeground(QtGui.QColor(20, 20, 20))
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


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, database: Database) -> None:
        super().__init__()
        self.database = database
        self.worker = Worker(database)
        self.setWindowTitle("OpenPCLink (unofficial)")
        self.resize(1200, 760)
        self.protected = False
        self._uri = ""

        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("Search parameters")
        self.search.textChanged.connect(self._filter_tree)
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.currentItemChanged.connect(self._tree_selected)
        self.runtime_tree = QtWidgets.QTreeWidget()
        self.runtime_tree.setHeaderHidden(True)
        self.runtime_tree.itemDoubleClicked.connect(self._toggle_gauge)
        self.runtime_tree.setToolTip("Double-click a value to add or remove it from the live list")
        self.left_tabs = QtWidgets.QTabWidget()
        self.left_tabs.addTab(self.tree, "ECU Settings")
        self.left_tabs.addTab(self.runtime_tree, "Runtime Values")
        left = QtWidgets.QWidget()
        left_layout = QtWidgets.QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(self.search)
        left_layout.addWidget(self.left_tabs)

        self.form = ParameterForm(self)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.form)
        self.placeholder = QtWidgets.QLabel("Press F3 to connect to an ECU.")
        self.placeholder.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.centre = QtWidgets.QStackedWidget()
        self.centre.addWidget(self.placeholder)
        self.centre.addWidget(scroll)

        self.gauges = QtWidgets.QTableWidget(0, 2)
        self.gauges.setHorizontalHeaderLabels(["Live value", ""])
        self.gauges.horizontalHeader().setStretchLastSection(True)
        self.gauges.verticalHeader().hide()
        self.gauges.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.gauge_ids: list[int] = []

        splitter = QtWidgets.QSplitter()
        splitter.addWidget(left)
        splitter.addWidget(self.centre)
        self.help_view = QtWidgets.QTextBrowser()
        self.help_view.setOpenLinks(False)
        self.help_view.anchorClicked.connect(self._help_link)
        self.manual = HelpManual()
        self.right_tabs = QtWidgets.QTabWidget()
        self.right_tabs.addTab(self.gauges, "Live")
        self.gauge_panel = GaugePanel()
        self.right_tabs.addTab(self.gauge_panel, "Gauges")
        self.right_tabs.addTab(self.help_view, "Help")
        splitter.addWidget(self.right_tabs)
        splitter.setSizes([280, 620, 300])
        self.setCentralWidget(splitter)

        self.status_label = QtWidgets.QLabel()
        self.unstored_label = QtWidgets.QLabel()
        self.statusBar().addPermanentWidget(self.unstored_label)
        self.statusBar().addPermanentWidget(self.status_label)
        self._build_actions()
        self._set_online(False)

        self.worker.connected.connect(self._on_connected)
        self.worker.disconnected.connect(self._on_disconnected)
        self.worker.runtime.connect(self._on_runtime)
        self.worker.wrote.connect(self._on_wrote)
        self.worker.failed.connect(self.show_error)
        self.worker.stored.connect(self._on_stored)
        self.worker.rules_status.connect(self.statusBar().showMessage)
        self.worker.rules_messages.connect(lambda messages: self.show_error("\n\n".join(messages)))
        self.worker.visibility_changed.connect(self._on_visibility_changed)
        self.worker.start()

    def _build_actions(self) -> None:
        self.connect_action = QtGui.QAction("Connect", self, shortcut="F3", triggered=self.toggle_connection)
        self.store_action = QtGui.QAction("Store to ECU", self, shortcut="F4", triggered=self.store)
        backup = QtGui.QAction("Back Up Settings…", self, triggered=self.backup)
        file_menu = self.menuBar().addMenu("File")
        file_menu.addAction(QtGui.QAction("Open Map (Offline)…", self, shortcut="Ctrl+O", triggered=self.open_map))
        file_menu.addAction(QtGui.QAction("Save Map As…", self, shortcut="Ctrl+Shift+S", triggered=self.save_map))
        file_menu.addAction(QtGui.QAction("Compare With Map…", self, triggered=self.compare_map))
        log_menu = self.menuBar().addMenu("Logging")
        self.log_action = QtGui.QAction("Start PC Logging…", self, shortcut="F8", triggered=self.toggle_logging)
        log_menu.addAction(self.log_action)
        log_menu.addAction(QtGui.QAction("Open Log…", self, triggered=self.open_log))
        help_menu = self.menuBar().addMenu("Help")
        help_menu.addAction(QtGui.QAction("Help for Selection", self, shortcut="F1", triggered=self.show_help))
        ecu_menu = self.menuBar().addMenu("ECU")
        ecu_menu.addAction(self.connect_action)
        ecu_menu.addAction(self.store_action)
        ecu_menu.addAction(backup)
        toolbar = self.addToolBar("ECU")
        toolbar.setMovable(False)
        toolbar.addAction(self.connect_action)
        toolbar.addAction(self.store_action)

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
        self.centre.setCurrentIndex(1 if online and not self.protected else 0)

    def _on_connected(self, info, protected: bool) -> None:
        self.protected = protected
        self._set_online(True)
        self.statusBar().showMessage(f"Connected: firmware {info.firmware}, serial {info.serial}", 8000)
        self._build_gauges()
        if protected:
            self.placeholder.setText(
                "Password protection activated.\n\n"
                "This ECU was locked by its tuner, so its settings are hidden and cannot be changed.\n"
                "Unlock it with that password in official PCLink."
            )
            self.tree.clear()
        else:
            self._build_tree()
        self._build_runtime_tree()
        self._set_online(True)

    def _on_disconnected(self, reason: str) -> None:
        self.protected = False
        self._set_online(False)
        self.placeholder.setText("Press F3 to connect to an ECU.")
        self.tree.clear()
        self.runtime_tree.clear()
        self.unstored_label.clear()
        self.log_action.setText("Start PC Logging…")
        if reason:
            self.show_error(f"Connection failed: {reason}")

    # ---- menu tree --------------------------------------------------------

    def menu_path(self, data_id: int) -> tuple[str, ...]:
        """Where a setting sits now: PCLink's rules can move it to another menu."""
        rules = self.worker.rules
        moved = rules.parents.get(data_id) if rules else None
        return self.database.menu_path_from(moved) if moved else self.database.parameter(data_id).path

    def units_of(self, parameter: Parameter) -> str:
        rules = self.worker.rules
        return (rules.units.get(parameter.data_id) if rules else None) or parameter.units

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

    def _filter_tree(self, text: str) -> None:
        needle = text.lower()

        def visit(item: QtWidgets.QTreeWidgetItem) -> bool:
            show = needle in item.text(0).lower()
            ids = item.data(0, ID_ROLE) or []
            show = show or any(needle in self.database.parameter(i).caption.lower() for i in ids)
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
        self.form.show_parameters([self.database.parameter(i) for i in ids])
        if self.right_tabs.currentWidget() is self.help_view:
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

    def _gauge_spec(self, data_id: int) -> tuple[int, str, str, float, float]:
        parameter = self.database.parameter(data_id)
        low = parameter.minimum if parameter.minimum is not None else 0.0
        high = parameter.maximum if parameter.maximum is not None else low + 100.0
        return (data_id, parameter.caption, parameter.units, low, high)

    def _lay_out_gauges(self) -> None:
        self.gauge_panel.rebuild([self._gauge_spec(i) for i in self.gauge_ids])
        self.gauges.setRowCount(len(self.gauge_ids))
        for row, data_id in enumerate(self.gauge_ids):
            self.gauges.setItem(row, 0, QtWidgets.QTableWidgetItem(self.database.parameter(data_id).caption))
            self.gauges.setItem(row, 1, QtWidgets.QTableWidgetItem(""))
        self.gauges.resizeColumnToContents(0)

    def _on_runtime(self) -> None:
        ecu = self.worker.ecu
        if ecu is None:
            return
        for row, data_id in enumerate(self.gauge_ids):
            parameter = self.database.parameter(data_id)
            decimals = parameter.decimals if parameter.decimals is not None else 1
            text = self._format_live(parameter, ecu.get(data_id), decimals, self.units_of(parameter))
            self.gauges.item(row, 1).setText(text)
            if data_id in self.gauge_panel.gauges:
                self.gauge_panel.gauges[data_id].set_value(ecu.get(data_id), text)
        for data_id, editor in self.form.editors.items():
            if self._is_live(data_id):
                self.form.refresh(data_id)
            elif isinstance(editor, TableEditor):
                editor.update_trace()

    @staticmethod
    def _format_live(parameter: Parameter, value: float, decimals: int, units: str = "") -> str:
        if parameter.options and 0 <= int(value) < len(parameter.options):
            return parameter.options[int(value)]
        return f"{value:.{decimals}f} {units}".strip()

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
        if ecu and ecu.has_unstored_changes:
            self.unstored_label.setText(
                "Unsaved map changes" if isinstance(ecu, OfflineEcu) else "Unstored changes (F4 to store)"
            )

    def store(self) -> None:
        choice = QtWidgets.QMessageBox.question(
            self, "Store to ECU", "Permanently save the current settings to the ECU? This cannot be undone."
        )
        if choice == QtWidgets.QMessageBox.StandardButton.Yes:
            self.worker.post("store")

    def _on_stored(self) -> None:
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
        if self.worker.ecu:
            self.show_error("Disconnect from the ECU before opening a map to edit offline.")
            return
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Open map", "", self._map_dialog_filter())
        if path:
            self.load_map(Path(path))

    def load_map(self, path: Path) -> None:
        loaded = self._read_map(path)
        if loaded:
            self.worker.post("offline", OfflineEcu(loaded, path))

    def _read_map(self, path: Path) -> MapFile | None:
        try:
            return MapFile.load(path, self.database.map_key)
        except ProtectedFile as exc:
            self.show_error(str(exc))
        except (BadContainer, MapError, OSError, ValueError) as exc:
            self.show_error(f"Could not open {path.name}: {exc}")
        return None

    def save_map(self) -> None:
        ecu = self.worker.ecu
        if ecu is None:
            return
        default = "tune.pcl5" if ecu.info.link_id >= 50000 else "tune.pclx"
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save map", default, self._map_dialog_filter())
        if path:
            self.save_map_to(Path(path))

    def save_map_to(self, path: Path) -> None:
        ecu = self.worker.ecu
        try:
            if isinstance(ecu, OfflineEcu):
                ecu.save(path, self.database.map_key)
                self.unstored_label.clear()
            else:
                MapFile.from_ecu(ecu, self.database).save(path, self.database.map_key)
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

    def toggle_logging(self) -> None:
        if self.worker.logger:
            self.stop_logging()
            return
        if not isinstance(self.worker.ecu, Ecu):
            self.show_error("Connect to an ECU to start logging.")
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Log to", "pclink-log.csv", "CSV (*.csv)")
        if path:
            self.start_logging(Path(path))

    def start_logging(self, path: Path) -> None:
        channels = [(i, self.database.parameter(i).caption) for i in self.gauge_ids]
        try:
            self.worker.logger = PcLogger(path, channels)
        except OSError as exc:
            self.show_error(str(exc))
            return
        self.log_action.setText("Stop PC Logging")
        self.statusBar().showMessage(f"Logging {len(channels)} values to {path}")

    def stop_logging(self) -> None:
        logger, self.worker.logger = self.worker.logger, None
        if logger:
            logger.close()
            self.statusBar().showMessage(f"Saved {logger.samples} samples to {logger.path}", 8000)
        self.log_action.setText("Start PC Logging…")

    def open_log(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Open log", "", "CSV (*.csv)")
        if path:
            try:
                self.log_viewer = LogViewer(read_log(Path(path)), Path(path).name)
                self.log_viewer.show()
            except (OSError, ValueError, StopIteration) as exc:
                self.show_error(f"Could not open that log: {exc}")

    # ---- help -------------------------------------------------------------

    def show_help(self) -> None:
        self.right_tabs.setCurrentWidget(self.help_view)
        if not self.manual.pages:
            self.help_view.setPlainText("The manual was not imported with this build.")
            return
        focus = self.form.parameters[0] if self.form.parameters else None
        for parameter in sorted(self.form.parameters, key=lambda item: -item.priority):
            if parameter.help_url:
                focus = parameter
                break
        page = self.manual.page_for(focus.help_url) if focus else None
        self.help_view.setHtml(page or "<p>No manual page for this selection.</p>")

    def _help_link(self, url: QtCore.QUrl) -> None:
        name = Path(url.path()).stem or url.toString()
        page = self.manual.page_for(name)
        if page:
            self.help_view.setHtml(page)

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
        self.worker.post("quit")
        self.worker.wait(3000)
        event.accept()


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
    if os.environ.get(READ_ONLY_ENV):
        window.setWindowTitle(f"{window.windowTitle()} — READ ONLY")
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
