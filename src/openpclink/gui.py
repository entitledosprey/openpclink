"""Desktop tuning interface. All ECU traffic runs on one worker thread."""
from __future__ import annotations

import queue
import sys
from collections import defaultdict
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from . import __version__
from .database import Database, DatabaseMissing, Parameter
from .ecu import SECRET_IDS, Ecu, EcuProtected, StoreUnconfirmed, WriteMismatch, WriteRefused
from .diagnostics import enable_log_file
from .importer import ImportFailed, import_pclink
from .protocol import ProtocolError
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

    def __init__(self, database: Database) -> None:
        super().__init__()
        self.database = database
        self.ecu: Ecu | None = None
        self.jobs: queue.Queue = queue.Queue()

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
            if self.ecu:
                self.ecu.poll_runtime()
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
            self.connected.emit(info, ecu.is_protected)
        elif name == "disconnect":
            self._close()
            self.disconnected.emit("")
        elif self.ecu is None:
            self.failed.emit("Not connected to an ECU.")
        elif name == "set":
            self._guarded(lambda: self.ecu.set(args[0], args[1], args[2]), args[0], args[2])
        elif name == "set_string":
            self._guarded(lambda: self.ecu.set_string(args[0], args[1]), args[0], 0)
        elif name == "store":
            try:
                self.ecu.store()
            except StoreUnconfirmed as exc:
                self.failed.emit(str(exc))
            else:
                self.stored.emit()

    def _guarded(self, action, data_id: int, index: int) -> None:
        try:
            action()
        except (WriteRefused, WriteMismatch, EcuProtected, ValueError) as exc:
            self.failed.emit(str(exc))
        self.wrote.emit(data_id, index)

    def _close(self) -> None:
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
        self._wheel_guard = _IgnoreWheel(self)

    def show_parameters(self, parameters: list[Parameter]) -> None:
        while self.layout_.rowCount():
            self.layout_.removeRow(0)
        self.editors.clear()
        for parameter in parameters:
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
            for index, option in enumerate(parameter.options[:limit]):
                combo.addItem(option, index)
            combo.setEnabled(not read_only)
            combo.activated.connect(lambda _: self.window_.write(data_id, combo.currentData(), 0))
            combo.installEventFilter(self._wheel_guard)
            return combo
        if loc.count > 1 and not loc.is_bitfield:
            return self._build_table(parameter, loc.count, read_only)
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
        spin.setSuffix(f" {parameter.units}" if parameter.units else "")
        spin.setReadOnly(read_only)
        spin.installEventFilter(self._wheel_guard)
        spin.editingFinished.connect(lambda: self._commit_number(data_id, spin))
        return spin

    def _build_table(self, parameter: Parameter, count: int, read_only: bool) -> QtWidgets.QWidget:
        columns = parameter.columns if parameter.columns > 1 else count
        rows = max(count // columns, 1)
        grid = QtWidgets.QTableWidget(rows, columns)
        grid.setMinimumHeight(min(90 + rows * 30, 420))
        if parameter.column_labels:
            grid.setHorizontalHeaderLabels(list(parameter.column_labels)[:columns])
        if parameter.row_labels:
            grid.setVerticalHeaderLabels(list(parameter.row_labels)[:rows])
        if read_only:
            grid.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        data_id = parameter.data_id
        grid.itemChanged.connect(lambda item: self._commit_cell(data_id, grid, item))
        return grid

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

    def _commit_cell(self, data_id: int, grid: QtWidgets.QTableWidget, item: QtWidgets.QTableWidgetItem) -> None:
        if grid.signalsBlocked():
            return
        try:
            value = float(item.text())
        except ValueError:
            self.window_.show_error(f"'{item.text()}' is not a number.")
            self.refresh(data_id)
            return
        index = item.row() * grid.columnCount() + item.column()
        self.window_.write(data_id, value, index)

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

    @staticmethod
    def _label_axes(ecu: Ecu, parameter: Parameter, grid: QtWidgets.QTableWidget) -> None:
        """Label a table with its live axis breakpoints, as tuners expect to see."""
        for axis_id, size, setter in (
            (parameter.column_axis_id, grid.columnCount(), grid.setHorizontalHeaderLabels),
            (parameter.row_axis_id, grid.rowCount(), grid.setVerticalHeaderLabels),
        ):
            if axis_id in ecu.table:
                setter([f"{value:g}" for value in ecu.get_all(axis_id)[:size]])

    def _refresh_editor(self, ecu: Ecu, data_id: int, editor: QtWidgets.QWidget) -> None:
        if isinstance(editor, QtWidgets.QLineEdit):
            if not editor.hasFocus():
                editor.setText(ecu.get_string(data_id))
        elif isinstance(editor, QtWidgets.QComboBox):
            editor.setCurrentIndex(editor.findData(int(ecu.get(data_id))))
        elif isinstance(editor, QtWidgets.QTableWidget):
            editor.blockSignals(True)
            parameter = self.window_.database.parameter(data_id)
            decimals = parameter.decimals
            self._label_axes(ecu, parameter, editor)
            for index, value in enumerate(ecu.get_all(data_id)):
                row, column = divmod(index, editor.columnCount())
                text = f"{value:.{decimals}f}" if decimals is not None else f"{value:g}"
                cell = editor.item(row, column)
                if cell is None:
                    editor.setItem(row, column, QtWidgets.QTableWidgetItem(text))
                elif cell.text() != text:
                    cell.setText(text)
            editor.blockSignals(False)
        elif isinstance(editor, QtWidgets.QDoubleSpinBox) and not editor.hasFocus():
            editor.blockSignals(True)
            editor.setValue(ecu.get(data_id))
            editor.setProperty("shown", editor.value())
            editor.blockSignals(False)


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
        left = QtWidgets.QWidget()
        left_layout = QtWidgets.QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(self.search)
        left_layout.addWidget(self.tree)

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
        splitter.addWidget(self.gauges)
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
        self.worker.start()

    def _build_actions(self) -> None:
        self.connect_action = QtGui.QAction("Connect", self, shortcut="F3", triggered=self.toggle_connection)
        self.store_action = QtGui.QAction("Store to ECU", self, shortcut="F4", triggered=self.store)
        backup = QtGui.QAction("Back Up Settings…", self, triggered=self.backup)
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
        self.status_label.setText("ONLINE" if online else "OFFLINE")
        self.status_label.setStyleSheet(
            f"background:{'#1b7f3b' if online else '#a12626'};color:white;padding:2px 8px;font-weight:bold"
        )
        self.connect_action.setText("Disconnect" if online else "Connect")
        self.store_action.setEnabled(online and not self.protected)
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

    def _on_disconnected(self, reason: str) -> None:
        self.protected = False
        self._set_online(False)
        self.placeholder.setText("Press F3 to connect to an ECU.")
        self.tree.clear()
        self.unstored_label.clear()
        if reason:
            self.show_error(f"Connection failed: {reason}")

    # ---- menu tree --------------------------------------------------------

    def _build_tree(self) -> None:
        self.tree.clear()
        ecu = self.worker.ecu
        groups: dict[tuple[str, ...], list[int]] = defaultdict(list)
        for data_id in ecu.table:
            if data_id in SECRET_IDS or not self.database.known(data_id):
                continue
            parameter = self.database.parameter(data_id)
            is_live = ecu.table[data_id].offset < ecu.info.runtime_size
            if parameter.visible and parameter.path and not is_live:
                groups[parameter.path].append(data_id)
        nodes: dict[tuple[str, ...], QtWidgets.QTreeWidgetItem] = {}
        for path in sorted(groups):
            for depth in range(1, len(path) + 1):
                prefix = path[:depth]
                if prefix not in nodes:
                    parent = nodes.get(prefix[:-1])
                    item = QtWidgets.QTreeWidgetItem(parent or self.tree, [prefix[-1]])
                    nodes[prefix] = item
            nodes[path].setData(0, ID_ROLE, groups[path])

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

    def _tree_selected(self, item: QtWidgets.QTreeWidgetItem | None, _previous) -> None:
        ids = (item.data(0, ID_ROLE) or []) if item else []
        self.form.show_parameters([self.database.parameter(i) for i in ids])

    # ---- live values ------------------------------------------------------

    def _build_gauges(self) -> None:
        ecu = self.worker.ecu
        by_caption = {}
        for data_id, loc in ecu.table.items():
            if loc.offset < ecu.info.runtime_size and self.database.known(data_id):
                by_caption.setdefault(self.database.parameter(data_id).caption, data_id)
        self.gauge_ids = [by_caption[c] for c in GAUGE_CAPTIONS if c in by_caption]
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
            self.gauges.item(row, 1).setText(f"{ecu.get(data_id):.{decimals}f} {parameter.units}")

    # ---- writing ----------------------------------------------------------

    def write(self, data_id: int, value: float, index: int) -> None:
        self.worker.post("set", data_id, value, index)

    def write_text(self, data_id: int, text: str) -> None:
        self.worker.post("set_string", data_id, text)

    def _on_wrote(self, data_id: int, _index: int) -> None:
        self.form.refresh(data_id)
        if self.worker.ecu and self.worker.ecu.has_unstored_changes:
            self.unstored_label.setText("Unstored changes (F4 to store)")

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
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
