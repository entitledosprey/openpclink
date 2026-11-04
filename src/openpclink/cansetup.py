"""CAN Setup: PCLink's five tabs (Mode, Streams, Test Calculator, CAN Devices, Custom CAN File), laid out as in the real dialog."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from . import can
from . import crypto
from . import protocol as p

BUTTONS = QtWidgets.QDialogButtonBox.StandardButton
Qt = QtCore.Qt
MAX_FILE_CHANNELS = 15
MAX_EXTENDED_ID = 0x1FFFFFFF
MAX_NORMAL_ID = 0x7FF
FILE_FILTER = "Link CAN Stream Files (*.lcf)"
STREAM_FILTER = "Link CAN Stream Files (*.lcs);;All Files (*)"
RATE_TEXTS = [f"{hertz} Hz" for hertz in can.RATES_HZ]
READ_ONLY_TEXT = "Read-only mode is on: nothing can be sent to the ECU or the CAN bus."


def read_only() -> bool:
    return bool(os.environ.get(p.READ_ONLY_ENV))


def _message(parent, text: str, warn: bool = True) -> None:
    box = QtWidgets.QMessageBox.warning if warn else QtWidgets.QMessageBox.information
    box(parent, "CAN Setup", text)


def _ask(parent, text: str) -> bool:
    return QtWidgets.QMessageBox.question(parent, "CAN Setup", text) == QtWidgets.QMessageBox.StandardButton.Yes


def _edit(width: int = 0, text: str = "") -> QtWidgets.QLineEdit:
    edit = QtWidgets.QLineEdit(text)
    if width:
        edit.setFixedWidth(width)
    return edit


def _number(text: str, default: float = 0.0) -> float:
    try:
        return float(text)
    except ValueError:
        return default


def option_texts(database, data_id: int, link_id: int) -> list[tuple[int, str]]:
    """The choices PCLink's combo offers for an option parameter: no NA entries, none hidden for this ECU."""
    if not database.known(data_id):
        return []
    return [(value, text) for value, text in database.visible_options(data_id, link_id) if text and text != "NA"]


@dataclass
class FileChannel:
    """One channel of a custom CAN file: its own id, direction and stream."""

    can_id: int = 0
    extended: bool = False
    transmit: bool = False
    rate: int = can.rate_value(1)
    stream: can.Stream = field(default_factory=can.Stream)


def channels_from_file(content: can.CustomCanFile) -> list[FileChannel]:
    out = []
    for raw_id, flags, stream in content.channels:
        extended = bool(raw_id & can.EXTENDED)
        transmit = bool(flags & 0x8000)
        can_id = raw_id & (MAX_EXTENDED_ID if extended else MAX_NORMAL_ID)
        out.append(FileChannel(can_id, extended, transmit, flags & 0x7FFF or can.rate_value(1), stream))
    return out


def file_from_channels(channels: list[FileChannel], lock_serial: int = 0, password: str = "") -> can.CustomCanFile:
    content = can.CustomCanFile(lock_serial=lock_serial, password=password)
    for channel in channels:
        raw_id = (channel.can_id & MAX_EXTENDED_ID) | can.EXTENDED if channel.extended else channel.can_id & MAX_NORMAL_ID
        flags = 0x8000 | (channel.rate & 0x7FFF) if channel.transmit else 0
        content.channels.append((raw_id, flags, channel.stream))
    return content


class BitBar(QtWidgets.QWidget):
    """The Visual Frame Display: 64 bit cells in eight bytes, with each parameter's bits coloured."""

    COLOURS = ("#4c9be8", "#e8a24c", "#6cc070", "#c070c0", "#d9d94c", "#4cd9d9", "#e86c6c", "#9a9a9a")

    def __init__(self) -> None:
        super().__init__()
        self.frame: can.StreamFrame | None = None
        self.setMinimumHeight(70)

    def set_frame(self, frame) -> None:
        self.frame = frame
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QtGui.QPainter(self)
        width = max(self.width() - 20, 64)
        cell = width / 64
        top = 14
        painter.setPen(QtGui.QPen(QtGui.QColor("#555555")))
        size = self.frame.length if self.frame else 8
        for bit in range(64):
            painter.setBrush(QtGui.QColor("#f2f2f2" if bit < size * 8 else "#cfcfcf"))
            painter.drawRect(QtCore.QRectF(10 + bit * cell, top, cell, 18))
            painter.drawText(QtCore.QRectF(10 + bit * cell, 0, cell, 14), Qt.AlignmentFlag.AlignCenter, str(bit))
        for byte in range(8):
            painter.drawText(QtCore.QRectF(10 + byte * 8 * cell, top + 22, 8 * cell, 16), Qt.AlignmentFlag.AlignCenter, str(byte))
        if not self.frame:
            return
        for index, item in enumerate(self.frame.parameters):
            painter.setBrush(QtGui.QColor(self.COLOURS[index % len(self.COLOURS)]))
            for bit in range(item.start, min(item.start + item.width, 64)):
                painter.drawRect(QtCore.QRectF(10 + bit * cell, top, cell, 18))
        if self.frame.id_position:
            painter.setBrush(QtGui.QColor("#202020"))
            first = (self.frame.id_position - 1) * 8
            for bit in range(first, min(first + 8, 64)):
                painter.drawRect(QtCore.QRectF(10 + bit * cell, top, cell, 18))


class ParameterPicker(QtWidgets.QDialog):
    """The parameter chooser of the Streams tab: groups, a search box and the Transmit / Receive filter."""

    def __init__(self, entries: list[tuple[int, str, str, bool]], parent=None, receive: bool = False) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add Parameter")
        self.resize(300, 420)
        self.entries = entries
        self.search = QtWidgets.QLineEdit()
        self.alphabetical = QtWidgets.QToolButton()
        self.alphabetical.setText("A-Z")
        self.alphabetical.setCheckable(True)
        self.transmit = QtWidgets.QRadioButton("Transmit")
        self.receive = QtWidgets.QRadioButton("Receive")
        (self.receive if receive else self.transmit).setChecked(True)
        group = QtWidgets.QGroupBox("Filter")
        row = QtWidgets.QHBoxLayout(group)
        row.addWidget(self.transmit)
        row.addWidget(self.receive)
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderHidden(True)
        top = QtWidgets.QHBoxLayout()
        top.addWidget(self.alphabetical)
        top.addWidget(self.search)
        top.addWidget(group)
        box = QtWidgets.QDialogButtonBox(BUTTONS.Ok | BUTTONS.Cancel)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        column = QtWidgets.QVBoxLayout(self)
        column.addLayout(top)
        column.addWidget(self.tree)
        column.addWidget(box)
        for control in (self.search,):
            control.textChanged.connect(self._fill)
        for control in (self.alphabetical, self.transmit, self.receive):
            control.toggled.connect(self._fill)
        self.tree.itemDoubleClicked.connect(lambda item, _column: self.accept() if item.data(0, Qt.ItemDataRole.UserRole) is not None else None)
        self._fill()

    def _fill(self) -> None:
        self.tree.clear()
        needle = self.search.text().strip().lower()
        groups: dict[str, QtWidgets.QTreeWidgetItem] = {}
        entries = sorted(self.entries, key=lambda entry: entry[1]) if self.alphabetical.isChecked() else self.entries
        for data_id, caption, group, writable in entries:
            if self.receive.isChecked() and not writable:
                continue
            if needle and needle not in caption.lower():
                continue
            parent = groups.get(group)
            if parent is None:
                parent = groups[group] = QtWidgets.QTreeWidgetItem(self.tree, [group])
            leaf = QtWidgets.QTreeWidgetItem(parent, [caption])
            leaf.setData(0, Qt.ItemDataRole.UserRole, data_id)
        if needle:
            self.tree.expandAll()

    def chosen(self) -> int | None:
        item = self.tree.currentItem()
        return item.data(0, Qt.ItemDataRole.UserRole) if item else None


class StreamPanel(QtWidgets.QWidget):
    """A list of streams with the frame and parameter editors, shared by the Streams and Custom CAN File tabs."""

    changed = QtCore.Signal()

    def __init__(self, dialog: "CanSetupDialog", streams: list[can.Stream], names=None) -> None:
        super().__init__()
        self.dialog = dialog
        self.streams = streams
        self.names = names or (lambda index: f"Stream {index + 1}")
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setMaximumWidth(125)
        self.add_frame = QtWidgets.QPushButton("Add Frame")
        self.load_stream = QtWidgets.QPushButton("Load Stream")
        self.delete_frame = QtWidgets.QPushButton("Delete Frame")
        self.save_stream = QtWidgets.QPushButton("Save Stream")
        stream_box = QtWidgets.QGroupBox("Stream")
        grid = QtWidgets.QGridLayout(stream_box)
        grid.addWidget(self.add_frame, 0, 0)
        grid.addWidget(self.load_stream, 0, 1)
        grid.addWidget(self.delete_frame, 1, 0)
        grid.addWidget(self.save_stream, 1, 1)
        self.frame_size = QtWidgets.QComboBox()
        self.frame_size.addItems([str(n) for n in range(1, 9)])
        self.frame_size.setCurrentIndex(7)
        self.id_position = QtWidgets.QComboBox()
        self.frame_id = _edit(70, "0")
        frame_box = QtWidgets.QGroupBox("Frame")
        form = QtWidgets.QGridLayout(frame_box)
        form.addWidget(QtWidgets.QLabel("Frame Size"), 0, 0)
        form.addWidget(QtWidgets.QLabel("ID Position"), 0, 1)
        form.addWidget(QtWidgets.QLabel("ID (decimal)"), 0, 2)
        form.addWidget(self.frame_size, 1, 0)
        form.addWidget(self.id_position, 1, 1)
        form.addWidget(self.frame_id, 1, 2)
        self.add_parameter = QtWidgets.QPushButton("Add")
        self.delete_parameter = QtWidgets.QPushButton("Delete")
        parameter_box = QtWidgets.QGroupBox("Parameters")
        column = QtWidgets.QVBoxLayout(parameter_box)
        column.addWidget(self.add_parameter)
        column.addWidget(self.delete_parameter)
        top = QtWidgets.QHBoxLayout()
        top.addWidget(stream_box)
        top.addWidget(frame_box)
        top.addWidget(parameter_box)
        self.table = QtWidgets.QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(["Parameter", "Start Position", "Width", "Byte Order", "Type", "Multiplier", "Divider", "Offset"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.bits = BitBar()
        right = QtWidgets.QVBoxLayout()
        right.addLayout(top)
        right.addWidget(self.table, 1)
        body = QtWidgets.QHBoxLayout()
        body.addWidget(self.tree)
        body.addLayout(right, 1)
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addLayout(body, 1)
        outer.addWidget(self.bits)
        self.tree.currentItemChanged.connect(self._selected)
        self.add_frame.clicked.connect(self._add_frame)
        self.delete_frame.clicked.connect(self._delete_frame)
        self.load_stream.clicked.connect(self._load)
        self.save_stream.clicked.connect(self._save)
        self.add_parameter.clicked.connect(self._add_parameter)
        self.delete_parameter.clicked.connect(self._delete_parameter)
        self.frame_size.currentIndexChanged.connect(self._frame_changed)
        self.id_position.currentIndexChanged.connect(self._frame_changed)
        self.frame_id.editingFinished.connect(self._frame_changed)
        self._busy = False
        self.refresh()

    # ---- structure ------------------------------------------------------------

    def refresh(self, keep: tuple[int, int] | None = None) -> None:
        self._busy = True
        self.tree.clear()
        for index, stream in enumerate(self.streams):
            node = QtWidgets.QTreeWidgetItem(self.tree, [self.names(index)])
            node.setData(0, Qt.ItemDataRole.UserRole, (index, -1))
            for number in range(len(stream.frames)):
                leaf = QtWidgets.QTreeWidgetItem(node, [f"Frame {number + 1}"])
                leaf.setData(0, Qt.ItemDataRole.UserRole, (index, number))
            node.setExpanded(keep is not None and keep[0] == index)
        self._busy = False
        self._apply_selection(keep)

    def _apply_selection(self, keep) -> None:
        target = keep or (0, -1)
        for top in range(self.tree.topLevelItemCount()):
            node = self.tree.topLevelItem(top)
            if node.data(0, Qt.ItemDataRole.UserRole) == target:
                self.tree.setCurrentItem(node)
                return
            for child in range(node.childCount()):
                if node.child(child).data(0, Qt.ItemDataRole.UserRole) == target:
                    self.tree.setCurrentItem(node.child(child))
                    return
        if self.tree.topLevelItemCount():
            self.tree.setCurrentItem(self.tree.topLevelItem(0))
        else:
            self._selected(None, None)

    def selection(self) -> tuple[int, int] | None:
        item = self.tree.currentItem()
        return item.data(0, Qt.ItemDataRole.UserRole) if item else None

    def _frame(self) -> can.StreamFrame | None:
        selection = self.selection()
        if selection is None or selection[1] < 0:
            return None
        return self.streams[selection[0]].frames[selection[1]]

    def _selected(self, _item, _previous) -> None:
        if self._busy:
            return
        selection = self.selection()
        frame = self._frame()
        has_stream = selection is not None
        self.add_frame.setEnabled(has_stream)
        self.load_stream.setEnabled(has_stream and selection[1] < 0)
        self.save_stream.setEnabled(has_stream and selection[1] < 0 and bool(self.streams[selection[0]].frames))
        self.delete_frame.setText("Delete Frame" if frame else "Delete Frames")
        self.delete_frame.setEnabled(has_stream and (frame is not None or bool(self.streams[selection[0]].frames)))
        for widget in (self.frame_size, self.id_position, self.frame_id, self.add_parameter, self.delete_parameter):
            widget.setEnabled(frame is not None)
        self._busy = True
        if frame:
            self.frame_size.setCurrentIndex(max(frame.length, 1) - 1)
            self._fill_positions(frame.length)
            self.id_position.setCurrentIndex(min(frame.id_position, self.id_position.count() - 1))
            self.frame_id.setText(str(frame.compound_id))
            self.frame_id.setEnabled(frame.id_position != 0)
        self._busy = False
        self._fill_table(frame)
        self.bits.set_frame(frame)

    def _fill_positions(self, length: int) -> None:
        self.id_position.clear()
        self.id_position.addItems(["None"] + [str(n) for n in range(1, max(length, 1) + 1)])

    def _add_frame(self) -> None:
        selection = self.selection()
        if selection is None:
            return
        stream = self.streams[selection[0]]
        stream.frames.append(can.StreamFrame())
        self.refresh((selection[0], len(stream.frames) - 1))
        self.changed.emit()

    def _delete_frame(self) -> None:
        selection = self.selection()
        if selection is None:
            return
        stream = self.streams[selection[0]]
        if selection[1] < 0:
            if stream.frames and _ask(self, "Delete All Frames"):
                stream.frames.clear()
        else:
            del stream.frames[selection[1]]
        self.refresh((selection[0], -1))
        self.changed.emit()

    def _frame_changed(self) -> None:
        frame = self._frame()
        if frame is None or self._busy:
            return
        frame.length = self.frame_size.currentIndex() + 1
        if self.id_position.count() != frame.length + 1:
            self._busy = True
            self._fill_positions(frame.length)
            self.id_position.setCurrentIndex(min(frame.id_position, frame.length))
            self._busy = False
        frame.id_position = self.id_position.currentIndex()
        frame.compound_id = int(_number(self.frame_id.text())) & 0xFF
        self.frame_id.setEnabled(frame.id_position != 0)
        self.bits.set_frame(frame)
        self.changed.emit()

    # ---- parameters -----------------------------------------------------------

    def _fill_table(self, frame) -> None:
        self._busy = True
        self.table.setRowCount(len(frame.parameters) if frame else 0)
        for row, item in enumerate(frame.parameters if frame else ()):
            name, _units = self.dialog.window_.describe(item.data_id)
            cell = QtWidgets.QTableWidgetItem(name)
            cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, 0, cell)
            for column, value in ((1, item.start), (2, item.width), (5, item.multiplier), (6, item.divider), (7, item.offset)):
                self.table.setItem(row, column, QtWidgets.QTableWidgetItem(str(value)))
            for column, texts, current in ((3, ("MS First", "LS First"), item.byte_order), (4, ("Unsigned", "Signed"), item.int_type)):
                combo = QtWidgets.QComboBox()
                combo.addItems(texts)
                combo.setCurrentIndex(current)
                combo.currentIndexChanged.connect(self._cell_changed)
                self.table.setCellWidget(row, column, combo)
        self._busy = False
        self.table.itemChanged.connect(self._cell_changed, Qt.ConnectionType.UniqueConnection)

    def _cell_changed(self, *_args) -> None:
        frame = self._frame()
        if frame is None or self._busy:
            return
        for row, item in enumerate(frame.parameters):
            def cell(column: int) -> int:
                widget = self.table.item(row, column)
                return int(_number(widget.text())) if widget else 0

            item.start = max(0, min(cell(1), 63))
            item.width = max(1, min(cell(2), 63)) if self.table.item(row, 2) else item.width
            item.multiplier = max(-32768, min(cell(5), 32767))
            item.divider = max(-32768, min(cell(6), 32767))
            item.offset = max(-32768, min(cell(7), 32767))
            item.byte_order = self.table.cellWidget(row, 3).currentIndex()
            item.int_type = self.table.cellWidget(row, 4).currentIndex()
        self.bits.set_frame(frame)
        self.changed.emit()

    def _add_parameter(self) -> None:
        frame = self._frame()
        if frame is None:
            return
        picker = ParameterPicker(self.dialog.parameter_entries(), self, receive=self.dialog.receive_filter(self))
        if not picker.exec() or picker.chosen() is None:
            return
        used = max((item.start + item.width for item in frame.parameters), default=0)
        frame.parameters.append(can.StreamParameter(picker.chosen(), min(used, 63), 16, 1))
        self._fill_table(frame)
        self.bits.set_frame(frame)
        self.changed.emit()

    def _delete_parameter(self) -> None:
        frame = self._frame()
        row = self.table.currentRow()
        if frame is None or not 0 <= row < len(frame.parameters):
            return
        del frame.parameters[row]
        self._fill_table(frame)
        self.bits.set_frame(frame)
        self.changed.emit()

    # ---- files ----------------------------------------------------------------

    def _load(self) -> None:
        selection = self.selection()
        if selection is None:
            return
        from .assets import asset

        folder = asset("CAN")
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Load Stream", str(folder or ""), STREAM_FILTER)
        if not path:
            return
        if self.streams[selection[0]].frames and not _ask(self, "Loading Stream from file will overwrite current stream.\nDo you wish to proceed?"):
            return
        try:
            stream = can.load_stream_file(Path(path), self.dialog.window_.database.stream_key)
        except (can.CanFileError, OSError, ValueError) as exc:
            _message(self, str(exc))
            return
        self.streams[selection[0]] = stream
        self.refresh((selection[0], -1))
        self.changed.emit()

    def _save(self) -> None:
        selection = self.selection()
        if selection is None:
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save Stream", "stream.lcs", STREAM_FILTER)
        if not path:
            return
        try:
            can.save_stream_file(self.streams[selection[0]], Path(path), self.dialog.window_.database.stream_key)
        except (can.CanFileError, OSError, ValueError, ZeroDivisionError) as exc:
            _message(self, str(exc))


class CanSetupDialog(QtWidgets.QDialog):
    """CAN Setup. Buttons Help, Apply, Ok, Cancel; changes reach the ECU only when applied."""

    def __init__(self, window) -> None:
        super().__init__(window)
        self.setWindowTitle("CAN Setup")
        self.resize(690, 440)
        self.window_ = window
        self.ecu = window.worker.ecu
        self.database = window.database
        info = getattr(self.ecu, "info", None)
        self.link_id = getattr(info, "link_id", 0)
        self.serial = getattr(info, "serial", 0)
        self.runtime_size = getattr(info, "runtime_size", 0)
        self.supported = can.CHANNEL_MODE_BASE[0] in self.ecu.table
        self.modules = [can.read_module(self.ecu, 0), can.read_module(self.ecu, 1)]
        self.obd = can.read_obd(self.ecu)
        self.streams = can.read_streams(self.ecu)
        self.file_channels = [FileChannel()]
        self.module = 0
        self._busy = False
        self.bridge_open = False
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(self._mode_tab(), "Mode")
        self.stream_panel = StreamPanel(self, self.streams)
        self.tabs.addTab(self.stream_panel, "Streams")
        self.tabs.addTab(self._calculator_tab(), "Test Calculator")
        self.tabs.addTab(self._devices_tab(), "CAN Devices")
        self.tabs.addTab(self._file_tab(), "Custom CAN File")
        self.tabs.currentChanged.connect(self._tab_changed)
        self.box = QtWidgets.QDialogButtonBox(BUTTONS.Help | BUTTONS.Apply | BUTTONS.Ok | BUTTONS.Cancel)
        self.box.accepted.connect(self._ok)
        self.box.rejected.connect(self.reject)
        self.box.helpRequested.connect(lambda: self.window_.show_topic("usercansetup"))
        self.apply_button = self.box.button(BUTTONS.Apply)
        self.apply_button.clicked.connect(self.apply)
        column = QtWidgets.QVBoxLayout(self)
        column.addWidget(self.tabs)
        column.addWidget(self.box)
        self._load_module(0)
        if not self.supported:
            for index in (0, 1, 2):
                self.tabs.setTabEnabled(index, False)
            self.tabs.setCurrentIndex(4)

    # ---- helpers --------------------------------------------------------------

    def is_writable(self, data_id: int) -> bool:
        loc = self.ecu.table.get(data_id)
        return loc is not None and not loc.read_only and loc.offset >= self.runtime_size

    def parameter_entries(self) -> list[tuple[int, str, str, bool]]:
        from .gui import SECRET_IDS

        out = []
        for data_id in self.ecu.table:
            if not self.database.known(data_id) or data_id in SECRET_IDS:
                continue
            parameter = self.database.parameter(data_id)
            if parameter.rows * parameter.columns > 1 or not parameter.caption:
                continue
            out.append((data_id, parameter.caption, parameter.path[0] if parameter.path else "Other", self.is_writable(data_id)))
        return out

    def receive_filter(self, panel) -> bool:
        return panel is getattr(self, "file_panel", None) and bool(self.file_channels) and not self._current_file_channel().transmit

    # ---- Mode tab -------------------------------------------------------------

    def _mode_tab(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        self.module_radios = [QtWidgets.QRadioButton("CAN 1"), QtWidgets.QRadioButton("CAN 2")]
        self.module_radios[0].setChecked(True)
        module_box = QtWidgets.QGroupBox("CAN Module")
        row = QtWidgets.QHBoxLayout(module_box)
        for radio in self.module_radios:
            row.addWidget(radio)
        self.module_mode = QtWidgets.QComboBox()
        self.bit_rate = QtWidgets.QComboBox()
        self.obd_radios = [QtWidgets.QRadioButton(text) for text in ("OFF", "ISO 15765 on CAN 1", "ISO 15765 on CAN 2")]
        obd_box = QtWidgets.QGroupBox("OBD")
        column = QtWidgets.QVBoxLayout(obd_box)
        for radio in self.obd_radios:
            column.addWidget(radio)
        self.obd_radios[self.obd if 0 <= self.obd <= 2 else 0].setChecked(True)
        config = QtWidgets.QGroupBox("CAN Configuration")
        grid = QtWidgets.QGridLayout(config)
        grid.addWidget(module_box, 0, 0, 2, 1)
        grid.addWidget(QtWidgets.QLabel("Mode"), 0, 1)
        grid.addWidget(self.module_mode, 1, 1)
        grid.addWidget(QtWidgets.QLabel("Bit Rate"), 0, 2)
        grid.addWidget(self.bit_rate, 1, 2)
        grid.addWidget(obd_box, 0, 3, 2, 1)
        grid.setColumnStretch(1, 2)
        self.channel_list = QtWidgets.QListWidget()
        self.channel_mode = QtWidgets.QComboBox()
        self.rate_label = QtWidgets.QLabel("Transmit Rate")
        self.rate = QtWidgets.QComboBox()
        self.rate.addItems(RATE_TEXTS)
        self.can_id = _edit(80)
        self.normal = QtWidgets.QRadioButton("Normal")
        self.extended = QtWidgets.QRadioButton("Extended")
        self.normal.setChecked(True)
        format_box = QtWidgets.QGroupBox("Format")
        stack = QtWidgets.QVBoxLayout(format_box)
        stack.addWidget(self.normal)
        stack.addWidget(self.extended)
        id_box = QtWidgets.QGroupBox("CAN ID")
        id_grid = QtWidgets.QGridLayout(id_box)
        id_grid.addWidget(QtWidgets.QLabel("ID (decimal)"), 0, 0)
        id_grid.addWidget(self.can_id, 1, 0)
        id_grid.addWidget(format_box, 0, 1, 3, 1)
        middle = QtWidgets.QGridLayout()
        middle.addWidget(QtWidgets.QLabel("Mode"), 0, 0)
        middle.addWidget(self.channel_mode, 1, 0)
        middle.addWidget(self.rate_label, 2, 0)
        middle.addWidget(self.rate, 3, 0)
        middle.setRowStretch(4, 1)
        data = QtWidgets.QGroupBox("Data")
        data_grid = QtWidgets.QGridLayout(data)
        data_grid.addWidget(QtWidgets.QLabel("Channel"), 0, 0)
        data_grid.addWidget(self.channel_list, 1, 0)
        data_grid.addLayout(middle, 1, 1)
        data_grid.addWidget(id_box, 1, 2)
        self.data_box = data
        outer = QtWidgets.QVBoxLayout(page)
        outer.addWidget(config)
        outer.addWidget(data, 1)
        for radio in self.module_radios:
            radio.toggled.connect(self._module_changed)
        self.module_mode.currentIndexChanged.connect(self._module_mode_changed)
        self.bit_rate.currentIndexChanged.connect(self._bit_rate_changed)
        for index, radio in enumerate(self.obd_radios):
            radio.toggled.connect(lambda on, index=index: on and setattr(self, "obd", index))
        self.channel_list.currentRowChanged.connect(self._channel_selected)
        self.channel_mode.currentIndexChanged.connect(self._channel_mode_changed)
        self.rate.currentIndexChanged.connect(self._rate_changed)
        self.can_id.textEdited.connect(self._id_edited)
        self.normal.toggled.connect(self._format_changed)
        return page

    def _fill_combo(self, combo: QtWidgets.QComboBox, items: list[tuple[int, str]], current: int) -> None:
        combo.blockSignals(True)
        combo.clear()
        for value, text in items:
            combo.addItem(text, value)
        index = combo.findData(current)
        combo.setCurrentIndex(index)
        combo.blockSignals(False)

    def _load_module(self, module: int) -> None:
        self._busy = True
        self.module = module
        settings = self.modules[module]
        self._fill_combo(self.module_mode, option_texts(self.database, can.MODE_IDS[module], self.link_id), settings.mode)
        self._fill_combo(self.bit_rate, option_texts(self.database, can.BIT_RATE_IDS[module], self.link_id), settings.bit_rate)
        self.channel_list.clear()
        for number in range(can.CHANNELS):
            self.channel_list.addItem(self._channel_text(number))
        self._busy = False
        self.data_box.setEnabled(settings.mode in can.USER_DEFINED_MODES)
        self.channel_list.setCurrentRow(0)
        self._channel_selected(0)
        enabled = self.supported
        for widget in (self.module_mode, self.bit_rate):
            widget.setEnabled(enabled)

    def _channel(self) -> can.CanChannel:
        return self.modules[self.module].channels[max(self.channel_list.currentRow(), 0)]

    def _channel_text(self, number: int) -> str:
        mode = self.modules[self.module].channels[number].mode
        base = can.CHANNEL_MODE_BASE[self.module]
        names = dict(option_texts(self.database, base, self.link_id))
        return f"{number + 1}: {names.get(mode, str(mode))}"

    def _module_changed(self, on: bool) -> None:
        if on and not self._busy:
            self._load_module(0 if self.module_radios[0].isChecked() else 1)

    def _module_mode_changed(self, _index: int) -> None:
        if self._busy or self.module_mode.currentData() is None:
            return
        self.modules[self.module].mode = self.module_mode.currentData()
        self.data_box.setEnabled(self.modules[self.module].mode in can.USER_DEFINED_MODES)

    def _bit_rate_changed(self, _index: int) -> None:
        if not self._busy and self.bit_rate.currentData() is not None:
            self.modules[self.module].bit_rate = self.bit_rate.currentData()

    def _channel_selected(self, row: int) -> None:
        if row < 0:
            return
        self._busy = True
        channel = self.modules[self.module].channels[row]
        base = (can.CHANNEL_MODE_BASE[0], can.CHANNEL_MODE_BASE[1])[self.module]
        self._fill_combo(self.channel_mode, option_texts(self.database, base, self.link_id), channel.mode)
        self.rate.setCurrentIndex(can.rate_index(channel.rate))
        self.can_id.setText(str(channel.can_id))
        (self.extended if channel.extended else self.normal).setChecked(True)
        self._busy = False
        self._channel_ui()

    def _channel_ui(self) -> None:
        channel = self._channel()
        active = channel.mode != 0
        transmit = channel.mode in can.TRANSMIT_MODES
        for widget in (self.can_id, self.normal, self.extended):
            widget.setEnabled(active)
        self.rate.setVisible(transmit)
        self.rate_label.setVisible(transmit)

    def _channel_mode_changed(self, _index: int) -> None:
        if self._busy or self.channel_mode.currentData() is None:
            return
        row = self.channel_list.currentRow()
        channel = self._channel()
        channel.mode = self.channel_mode.currentData()
        if channel.mode in can.TRANSMIT_MODES and can.rate_index(channel.rate) < 0:
            channel.rate = can.rate_value(1)
            self.rate.setCurrentIndex(0)
        self.channel_list.item(row).setText(self._channel_text(row))
        self._channel_ui()

    def _rate_changed(self, index: int) -> None:
        if not self._busy and index >= 0:
            self._channel().rate = can.rate_value(can.RATES_HZ[index])

    def _id_edited(self, text: str) -> None:
        channel = self._channel()
        digits = "".join(c for c in text if c.isdigit())
        value = int(digits) if digits else 0
        limit = MAX_EXTENDED_ID if self.extended.isChecked() else MAX_NORMAL_ID
        if value > limit:
            value = channel.can_id
        channel.can_id = value
        self.can_id.setText(str(value) if digits else "")

    def _format_changed(self, normal: bool) -> None:
        if self._busy:
            return
        channel = self._channel()
        channel.extended = not normal
        if normal and channel.can_id > MAX_NORMAL_ID:
            channel.can_id = 0
            self.can_id.setText("0")

    # ---- Test Calculator ------------------------------------------------------

    def _calculator_tab(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        self.calc_parameter = QtWidgets.QLineEdit()
        self.calc_parameter.setReadOnly(True)
        choose = QtWidgets.QPushButton("…")
        choose.setFixedWidth(26)
        choose.clicked.connect(self._choose_calculator_parameter)
        self.calc_info = QtWidgets.QPlainTextEdit()
        self.calc_info.setReadOnly(True)
        top = QtWidgets.QHBoxLayout()
        top.addWidget(QtWidgets.QLabel("Parameter"))
        top.addWidget(self.calc_parameter, 1)
        top.addWidget(choose)
        top.addStretch(1)
        self.calc_transmit = QtWidgets.QRadioButton("Transmit")
        self.calc_receive = QtWidgets.QRadioButton("Receive")
        self.calc_transmit.setChecked(True)
        direction = QtWidgets.QGroupBox("Direction")
        stack = QtWidgets.QVBoxLayout(direction)
        stack.addWidget(self.calc_transmit)
        stack.addWidget(self.calc_receive)
        self.bus_value = _edit(100, "0")
        self.display_value = QtWidgets.QComboBox()
        self.display_value.setEditable(True)
        self.display_value.setEditText("0")
        self.calc_multiplier, self.calc_divider, self.calc_offset = _edit(60, "1"), _edit(60, "1"), _edit(60, "0")
        test = QtWidgets.QGroupBox("Test Calculator")
        grid = QtWidgets.QGridLayout(test)
        grid.addWidget(direction, 0, 0, 3, 1)
        grid.addWidget(QtWidgets.QLabel("CAN Bus Data"), 0, 1)
        grid.addWidget(self.bus_value, 1, 1)
        grid.addWidget(QtWidgets.QLabel("="), 1, 2)
        grid.addWidget(QtWidgets.QLabel("PCLink Display Data"), 0, 3)
        grid.addWidget(self.display_value, 1, 3)
        grid.addWidget(QtWidgets.QLabel("x  Multiplier"), 0, 4)
        grid.addWidget(self.calc_multiplier, 1, 4)
        grid.addWidget(QtWidgets.QLabel("Divider"), 2, 3)
        grid.addWidget(self.calc_divider, 2, 4)
        grid.addWidget(QtWidgets.QLabel("+  Offset"), 0, 5)
        grid.addWidget(self.calc_offset, 1, 5)
        self.scale_transmit = QtWidgets.QRadioButton("Transmit")
        self.scale_receive = QtWidgets.QRadioButton("Receive")
        self.scale_transmit.setChecked(True)
        scale_direction = QtWidgets.QGroupBox("Direction")
        stack = QtWidgets.QVBoxLayout(scale_direction)
        stack.addWidget(self.scale_transmit)
        stack.addWidget(self.scale_receive)
        self.bus_min, self.bus_max = _edit(70, "0"), _edit(70, "1")
        self.shown_min, self.shown_max = _edit(70, "0"), _edit(70, "1")
        self.scale_multiplier, self.scale_divider, self.scale_offset = _edit(70, "1"), _edit(70, "1"), _edit(70, "0")
        for output in (self.scale_multiplier, self.scale_divider, self.scale_offset):
            output.setReadOnly(True)
        scale = QtWidgets.QGroupBox("Scale Generator")
        grid = QtWidgets.QGridLayout(scale)
        grid.addWidget(scale_direction, 0, 0, 4, 1)
        for column, (label, first, second) in enumerate((("CAN Bus Min", self.bus_min, self.bus_max), ("PCLink Min", self.shown_min, self.shown_max)), 1):
            grid.addWidget(QtWidgets.QLabel(label), 0, column)
            grid.addWidget(first, 1, column)
            grid.addWidget(QtWidgets.QLabel(label.replace("Min", "Max")), 2, column)
            grid.addWidget(second, 3, column)
        grid.addWidget(QtWidgets.QLabel("Multiplier"), 0, 3)
        grid.addWidget(self.scale_multiplier, 1, 3)
        grid.addWidget(QtWidgets.QLabel("Divider"), 2, 3)
        grid.addWidget(self.scale_divider, 3, 3)
        grid.addWidget(QtWidgets.QLabel("Offset"), 1, 4)
        grid.addWidget(self.scale_offset, 2, 4)
        outer = QtWidgets.QVBoxLayout(page)
        outer.addLayout(top)
        outer.addWidget(self.calc_info, 1)
        outer.addWidget(test)
        outer.addWidget(scale)
        for edit in (self.calc_multiplier, self.calc_divider, self.calc_offset):
            edit.textChanged.connect(self._calculate)
        self.bus_value.textEdited.connect(lambda _text: self._calculate(source="bus"))
        self.display_value.editTextChanged.connect(lambda _text: self._calculate(source="display"))
        self.calc_transmit.toggled.connect(lambda _on: self._calculate(source="display"))
        for edit in (self.bus_min, self.bus_max, self.shown_min, self.shown_max):
            edit.textChanged.connect(self._generate)
        self.scale_transmit.toggled.connect(self._generate)
        return page

    def _choose_calculator_parameter(self) -> None:
        picker = ParameterPicker(self.parameter_entries(), self)
        if not picker.exec() or picker.chosen() is None:
            return
        data_id = picker.chosen()
        parameter = self.database.parameter(data_id)
        self.calc_parameter.setText(parameter.caption)
        text = can.stored_in_ecu(self.ecu, data_id)
        if parameter.options:
            text += "\nStored in ECU with options:\n" + "\n".join(f"{i}: {o}" for i, o in enumerate(parameter.options) if o and o != "NA")
        elif parameter.minimum is not None and parameter.maximum is not None:
            text += f"\nMinimum {parameter.minimum:g}\nMaximum {parameter.maximum:g}"
            self.shown_min.setText(f"{parameter.minimum:g}")
            self.shown_max.setText(f"{parameter.maximum:g}")
        self.calc_info.setPlainText(text)

    def _calculate(self, *_args, source: str = "display") -> None:
        values = (_number(self.calc_multiplier.text(), 1), _number(self.calc_divider.text(), 1), _number(self.calc_offset.text()))
        transmit = self.calc_transmit.isChecked()
        if transmit and source != "bus":
            self.bus_value.setText(f"{can.calculate(_number(self.display_value.currentText()), *values):g}")
        elif not transmit and source != "display":
            self.display_value.blockSignals(True)
            self.display_value.setEditText(f"{can.calculate(_number(self.bus_value.text()), *values):g}")
            self.display_value.blockSignals(False)
        elif source == "display" and not transmit:
            self.display_value.blockSignals(True)
            self.display_value.setEditText(f"{can.calculate(_number(self.bus_value.text()), *values):g}")
            self.display_value.blockSignals(False)

    def _generate(self, *_args) -> None:
        multiplier, divider, offset = can.scale_factors(
            _number(self.bus_min.text()), _number(self.bus_max.text()), _number(self.shown_min.text()), _number(self.shown_max.text()),
            self.scale_transmit.isChecked(),
        )
        self.scale_multiplier.setText(str(multiplier))
        self.scale_divider.setText(str(divider))
        self.scale_offset.setText(f"{offset:g}")

    # ---- CAN Devices ----------------------------------------------------------

    def _devices_tab(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        self.find_button = QtWidgets.QPushButton("Find Devices")
        self.find_button.clicked.connect(self.find_devices)
        self.device_module = QtWidgets.QComboBox()
        self.device_module.addItems(["1", "2"])
        self.device_note = QtWidgets.QLabel("Note: 'Find Devices' will only find some specific devices, press F1 for more")
        self.configure_button = QtWidgets.QPushButton("Configure Device")
        self.configure_button.setEnabled(False)
        self.configure_button.clicked.connect(self.configure_device)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.find_button)
        row.addWidget(QtWidgets.QLabel("CAN Module:"))
        row.addWidget(self.device_module)
        row.addWidget(self.device_note, 1)
        row.addWidget(self.configure_button)
        self.devices = QtWidgets.QTableWidget(0, 2)
        self.devices.setHorizontalHeaderLabels(["CAN Device", ""])
        self.devices.horizontalHeader().setStretchLastSection(True)
        self.devices.verticalHeader().hide()
        self.devices.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.devices.itemSelectionChanged.connect(lambda: self.configure_button.setEnabled(self.devices.currentRow() >= 0))
        self.found: list[tuple[str, object]] = []
        column = QtWidgets.QVBoxLayout(page)
        column.addLayout(row)
        column.addWidget(self.devices, 1)
        return page

    def _live_ecu(self) -> bool:
        return hasattr(self.ecu, "link")

    def _tab_changed(self, index: int) -> None:
        """PCLink opens the CAN bridge with a zero word when this tab opens and closes it when it closes."""
        entering = index == 3
        if not self._live_ecu() or read_only() or entering == self.bridge_open:
            self.apply_button.setEnabled(index != 3)
            return
        self.apply_button.setEnabled(index != 3)
        self.bridge_open = entering
        self.window_.call(lambda ecu: ecu.link.request(p.CMD_CAN_CONTROL, bytes(4)), None)

    def find_devices(self) -> None:
        if not self._live_ecu():
            _message(self, "ECU not connected.")
            return
        if read_only():
            _message(self, READ_ONLY_TEXT)
            return
        if not _ask(self, "Searching for devices sends frames onto the vehicle's CAN bus. Continue?"):
            return
        module = self.device_module.currentIndex()
        self.device_note.setText("Searching for CAN devices")

        def work(ecu):
            bridge = can.CanBridge(ecu.link, module)
            bridge.start()
            try:
                links = bridge.find_link_devices()
                keypads = can.scan_keypads(bridge)
            finally:
                bridge.stop()
            return links, keypads

        self.window_.call(work, self._found)

    def _found(self, result) -> None:
        links, keypads = result
        self.found = [(f"Link CAN Device {code} (serial {serial})", ("link", device_id, code, serial)) for device_id, code, serial in links]
        self.found += [(f"CAN Keypad (node {k.node}, product {k.product:#05x})", ("keypad", k.node, k.product)) for k in keypads]
        self.devices.setRowCount(len(self.found))
        for row, (text, _item) in enumerate(self.found):
            self.devices.setItem(row, 0, QtWidgets.QTableWidgetItem(text))
        self.device_note.setText("Select a CAN Device to make changes" if self.found else "Could not find CAN Devices")

    def configure_device(self) -> None:
        row = self.devices.currentRow()
        if not 0 <= row < len(self.found) or not self._live_ecu():
            return
        if read_only():
            _message(self, READ_ONLY_TEXT)
            return
        kind = self.found[row][1]
        module = self.device_module.currentIndex()
        dialog = KeypadDialog(self, module, kind[1], kind[2]) if kind[0] == "keypad" else DeviceDialog(self, module, kind[1], kind[2], kind[3])
        dialog.exec()

    # ---- Custom CAN File ------------------------------------------------------

    def _file_tab(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        self.file_add = QtWidgets.QPushButton("Add")
        self.file_delete = QtWidgets.QPushButton("Delete")
        channels = QtWidgets.QGroupBox("Channels")
        column = QtWidgets.QVBoxLayout(channels)
        column.addWidget(self.file_add)
        column.addWidget(self.file_delete)
        self.file_receive = QtWidgets.QRadioButton("Receive")
        self.file_transmit = QtWidgets.QRadioButton("Transmit")
        self.file_receive.setChecked(True)
        direction = QtWidgets.QGroupBox("Transmit/Receive")
        stack = QtWidgets.QVBoxLayout(direction)
        stack.addWidget(self.file_receive)
        stack.addWidget(self.file_transmit)
        self.file_id = _edit(80, "0")
        self.file_rate = QtWidgets.QComboBox()
        self.file_rate.addItems(RATE_TEXTS)
        self.file_normal = QtWidgets.QRadioButton("Normal")
        self.file_extended = QtWidgets.QRadioButton("Extended")
        self.file_normal.setChecked(True)
        format_box = QtWidgets.QGroupBox("Format")
        stack = QtWidgets.QVBoxLayout(format_box)
        stack.addWidget(self.file_normal)
        stack.addWidget(self.file_extended)
        settings = QtWidgets.QGroupBox("Channel Settings")
        grid = QtWidgets.QGridLayout(settings)
        grid.addWidget(direction, 0, 0, 2, 1)
        grid.addWidget(QtWidgets.QLabel("ID (decimal)"), 0, 1)
        grid.addWidget(self.file_id, 1, 1)
        grid.addWidget(QtWidgets.QLabel("Transmit Rate"), 0, 2)
        grid.addWidget(self.file_rate, 1, 2)
        grid.addWidget(format_box, 0, 3, 2, 1)
        self.file_save = QtWidgets.QPushButton("Save File")
        self.file_load = QtWidgets.QPushButton("Load File")
        self.file_lock = QtWidgets.QCheckBox("Lock To ECU")
        self.file_serial = _edit(80, str(self.serial))
        self.file_serial.setReadOnly(True)
        self.file_into_ecu = QtWidgets.QPushButton("Load\nFile into\nECU")
        self.file_from_ecu = QtWidgets.QPushButton("Save\nFile from\nECU")
        options = QtWidgets.QGroupBox("File Options")
        grid = QtWidgets.QGridLayout(options)
        grid.addWidget(self.file_save, 0, 0)
        grid.addWidget(self.file_lock, 0, 1)
        grid.addWidget(self.file_load, 1, 0)
        grid.addWidget(self.file_serial, 1, 1)
        grid.addWidget(self.file_into_ecu, 2, 0)
        grid.addWidget(self.file_from_ecu, 2, 1)
        self.file_panel = StreamPanel(self, [c.stream for c in self.file_channels], names=lambda index: f"Stream {index + 1}")
        self.file_panel.add_frame.setText("Add Frame")
        top = QtWidgets.QHBoxLayout()
        top.addWidget(channels)
        top.addWidget(settings, 1)
        top.addWidget(options)
        outer = QtWidgets.QVBoxLayout(page)
        outer.addLayout(top)
        outer.addWidget(self.file_panel, 1)
        self.file_add.clicked.connect(self._file_add)
        self.file_delete.clicked.connect(self._file_delete)
        self.file_panel.tree.currentItemChanged.connect(lambda *_a: self._file_channel_selected())
        self.file_id.textEdited.connect(self._file_id_edited)
        self.file_rate.currentIndexChanged.connect(self._file_rate_changed)
        self.file_transmit.toggled.connect(self._file_direction_changed)
        self.file_normal.toggled.connect(self._file_format_changed)
        self.file_save.clicked.connect(self.save_can_file_from_editor)
        self.file_load.clicked.connect(self.load_can_file_into_editor)
        self.file_into_ecu.clicked.connect(self.load_can_file_into_ecu)
        self.file_from_ecu.clicked.connect(self.save_can_file_from_ecu)
        self._file_channel_selected()
        return page

    def _current_file_channel(self) -> FileChannel:
        selection = self.file_panel.selection()
        return self.file_channels[selection[0] if selection else 0]

    def _file_channel_selected(self) -> None:
        if not self.file_channels:
            return
        channel = self._current_file_channel()
        self._busy = True
        (self.file_transmit if channel.transmit else self.file_receive).setChecked(True)
        self.file_id.setText(str(channel.can_id))
        self.file_rate.setCurrentIndex(max(can.rate_index(channel.rate), 0))
        (self.file_extended if channel.extended else self.file_normal).setChecked(True)
        self._busy = False
        self.file_rate.setEnabled(channel.transmit)
        self.file_delete.setEnabled(len(self.file_channels) > 1)
        self.file_add.setEnabled(len(self.file_channels) < MAX_FILE_CHANNELS)

    def _refresh_file_panel(self, keep=None) -> None:
        self.file_panel.streams = [c.stream for c in self.file_channels]
        self.file_panel.refresh(keep)
        self._file_channel_selected()

    def _file_add(self) -> None:
        if len(self.file_channels) < MAX_FILE_CHANNELS:
            self.file_channels.append(FileChannel())
            self._refresh_file_panel((len(self.file_channels) - 1, -1))

    def _file_delete(self) -> None:
        if len(self.file_channels) > 1:
            selection = self.file_panel.selection()
            del self.file_channels[selection[0] if selection else -1]
            self._refresh_file_panel((0, -1))

    def _file_id_edited(self, text: str) -> None:
        channel = self._current_file_channel()
        digits = "".join(c for c in text if c.isdigit())
        value = int(digits) if digits else 0
        if value > (MAX_EXTENDED_ID if channel.extended else MAX_NORMAL_ID):
            value = channel.can_id
        channel.can_id = value
        self.file_id.setText(str(value) if digits else "")

    def _file_rate_changed(self, index: int) -> None:
        if not self._busy and index >= 0:
            self._current_file_channel().rate = can.rate_value(can.RATES_HZ[index])

    def _file_direction_changed(self, transmit: bool) -> None:
        if not self._busy:
            self._current_file_channel().transmit = transmit
            self.file_rate.setEnabled(transmit)

    def _file_format_changed(self, normal: bool) -> None:
        if self._busy:
            return
        channel = self._current_file_channel()
        channel.extended = not normal
        if normal and channel.can_id > MAX_NORMAL_ID:
            channel.can_id = 0
            self.file_id.setText("0")

    def _file_problems(self) -> list[str]:
        problems = []
        for number, channel in enumerate(self.file_channels, 1):
            if not channel.transmit and not can.stream_is_writable(channel.stream, self.is_writable):
                problems.append(f"Custom CAN File Channel {number} has a parameter that cannot be written by CAN.")
            if not channel.can_id:
                problems.append(f"Custom CAN File Channel {number} has an invalid ID.")
        return problems

    def _open_file_content(self, path: str) -> tuple[can.CustomCanFile, bytes] | None:
        """Decode a .lcf. One with a password stays closed unless the right one is typed."""
        key = self.window_.database.file_key
        try:
            content, plain = can.load_can_file(Path(path), key)
        except (can.CanFileError, crypto.BadContainer, crypto.ProtectedFile, ValueError, OSError) as exc:
            _message(self, str(exc) or "File load failed.")
            return None
        if content.has_password:
            entered, chosen = QtWidgets.QInputDialog.getText(self, "CAN Setup", "Password", QtWidgets.QLineEdit.EchoMode.Password)
            if not chosen or not content.check_password(entered):
                _message(self, "Incorrect Password")
                return None
        return content, plain

    def save_can_file_from_editor(self) -> None:
        problems = self._file_problems()
        if problems:
            _message(self, "\n".join(problems))
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save File", "CANFile.lcf", FILE_FILTER)
        if not path:
            return
        password = ""
        if _ask(self, "Do you want to password protect this file?"):
            password, chosen = QtWidgets.QInputDialog.getText(self, "CAN Setup", "Password", QtWidgets.QLineEdit.EchoMode.Password)
            if not chosen:
                return
        content = file_from_channels(self.file_channels, self.serial if self.file_lock.isChecked() else 0, password)
        if len(can.build_can_file(content)) > can.MAX_CAN_FILE:
            _message(self, "Too many parameters set in Streams")
            return
        try:
            can.save_can_file(content, Path(path), self.window_.database.file_key)
        except (OSError, ValueError, ZeroDivisionError) as exc:
            _message(self, str(exc))
            return
        if _ask(self, "Do you want to Load this file into the ECU?"):
            self._send_can_file(can.build_can_file(content), reset_streams=False)

    def load_can_file_into_editor(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Load File", "", FILE_FILTER)
        if not path:
            return
        opened = self._open_file_content(path)
        if opened is None:
            return
        self._show_file(opened[0])

    def _show_file(self, content: can.CustomCanFile) -> None:
        self.file_channels = channels_from_file(content) or [FileChannel()]
        self.file_lock.setChecked(bool(content.lock_serial))
        self._refresh_file_panel((0, -1))

    def load_can_file_into_ecu(self) -> None:
        if not self._live_ecu():
            _message(self, "ECU not connected.")
            return
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Load File into ECU", "", FILE_FILTER)
        if not path:
            return
        opened = self._open_file_content(path)
        if opened is None:
            return
        content, plain = opened
        if content.lock_serial and content.lock_serial != self.serial:
            _message(self, "This CAN File is locked to a different ECU.")
            return
        self._send_can_file(plain, reset_streams=True)

    def _send_can_file(self, plain: bytes, reset_streams: bool) -> None:
        if not self._live_ecu():
            return
        if read_only():
            _message(self, READ_ONLY_TEXT)
            return

        def work(ecu):
            ecu.logs.write_named(p.CMD_WRITE_CAN_FILE, plain)
            if reset_streams and can.RESET_STREAMS_ID in ecu.table:
                ecu.set(can.RESET_STREAMS_ID, 1)
                ecu.set(can.RESET_STREAMS_ID, 0)

        self.window_.call(work, lambda _r: _message(self, "File loaded Successfully.", warn=False))

    def save_can_file_from_ecu(self) -> None:
        if not self._live_ecu():
            _message(self, "ECU not connected.")
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save File from ECU", "CANFile.lcf", FILE_FILTER)
        if not path:
            return

        def done(raw: bytes) -> None:
            if not raw:
                _message(self, "No CAN File in ECU", warn=False)
                return
            Path(path).write_bytes(crypto.encode(raw, self.window_.database.file_key))
            if _ask(self, "Do you want to Load this file into the CAN File Editor?"):
                try:
                    content = can.parse_can_file(raw)
                except can.CanFileError as exc:
                    _message(self, str(exc))
                    return
                if content.has_password:
                    entered, chosen = QtWidgets.QInputDialog.getText(self, "CAN Setup", "Password", QtWidgets.QLineEdit.EchoMode.Password)
                    if not chosen or not content.check_password(entered):
                        _message(self, "Incorrect Password")
                        return
                self._show_file(content)

        self.window_.call(lambda ecu: ecu.logs.read_named("CANFile"), done)

    # ---- Apply ----------------------------------------------------------------

    def apply(self) -> bool:
        if not self.supported:
            return True
        problems = can.validate_modules(self.modules, self.streams, self.obd, self.is_writable)
        if problems:
            _message(self, "\n".join(problems))
            return False
        modules, obd, streams = [self._copy(m) for m in self.modules], self.obd, list(self.streams)

        def work(ecu):
            can.apply_modules(ecu, modules, obd, streams)

        self.window_.call(work, lambda _r: self.window_.statusBar().showMessage("Remember to store ECU settings", 3000))
        return True

    @staticmethod
    def _copy(module: can.ModuleSettings) -> can.ModuleSettings:
        return can.ModuleSettings(module.mode, module.bit_rate, [can.CanChannel(c.mode, c.rate, c.can_id, c.extended) for c in module.channels])

    def _ok(self) -> None:
        if self.apply() is not False:
            self.accept()

    def done(self, result: int) -> None:
        if self.bridge_open and self._live_ecu():
            self.bridge_open = False
            self.window_.call(lambda ecu: ecu.link.request(p.CMD_CAN_CONTROL, bytes(4)), None)
        super().done(result)


class DeviceDialog(QtWidgets.QDialog):
    """Configure Device for a Link CAN device: the registers PCLink knows, Visual ID, Store and CAN Lambda."""

    def __init__(self, owner: CanSetupDialog, module: int, device_id: int, code: int, serial: int) -> None:
        super().__init__(owner)
        self.setWindowTitle("Configure Device")
        self.owner, self.module, self.device_id = owner, module, device_id
        self.status = QtWidgets.QLabel(f"Device {code}, serial {serial}")
        self.terminator = QtWidgets.QCheckBox("Enable terminator")
        self.bit_rate = QtWidgets.QComboBox()
        self.bit_rate.addItems(can.BIT_RATES)
        self.visual = QtWidgets.QCheckBox("Visual identification")
        self.lambda_select = QtWidgets.QComboBox()
        self.lambda_select.addItems([str(n) for n in range(1, 6)])
        read = QtWidgets.QPushButton("Read")
        send = QtWidgets.QPushButton("Send")
        store = QtWidgets.QPushButton("Store in device")
        send_lambda = QtWidgets.QPushButton("Send CAN Lambda")
        form = QtWidgets.QFormLayout()
        form.addRow(self.terminator)
        form.addRow("Bit rate", self.bit_rate)
        form.addRow(self.visual)
        form.addRow("CAN Lambda", self.lambda_select)
        row = QtWidgets.QHBoxLayout()
        for button in (read, send, store, send_lambda):
            row.addWidget(button)
        column = QtWidgets.QVBoxLayout(self)
        column.addWidget(self.status)
        column.addLayout(form)
        column.addLayout(row)
        close = QtWidgets.QDialogButtonBox(BUTTONS.Close)
        close.rejected.connect(self.reject)
        column.addWidget(close)
        read.clicked.connect(self._read)
        send.clicked.connect(self._send)
        store.clicked.connect(self._store)
        send_lambda.clicked.connect(self._lambda)
        self.visual.toggled.connect(self._identify)

    def _run(self, work, done=None) -> None:
        def job(ecu):
            bridge = can.CanBridge(ecu.link, self.module)
            bridge.start()
            try:
                return work(can.LinkDevice(bridge, self.device_id))
            finally:
                bridge.stop()

        def finished(result) -> None:
            if done:
                done(result)

        self.status.setText("Sending changes please wait")
        self.owner.window_.call(job, finished)

    def _read(self) -> None:
        def work(device):
            return device.read_int(can.REGISTER_TERMINATOR), device.read_int(can.REGISTER_BIT_RATE)

        def done(result) -> None:
            self.terminator.setChecked(bool(result[0]))
            self.bit_rate.setCurrentIndex(max(0, min(result[1], len(can.BIT_RATES) - 1)))
            self.status.setText("Select a CAN Device to make changes")

        self._run(work, done)

    def _send(self) -> None:
        terminator, rate = int(self.terminator.isChecked()), self.bit_rate.currentIndex()

        def work(device):
            device.write_int(can.REGISTER_TERMINATOR, terminator)
            device.write_int(can.REGISTER_BIT_RATE, rate)

        self._run(work, lambda _r: self.status.setText("Changes were successful. Please cycle power to the CAN device."))

    def _store(self) -> None:
        self._run(lambda device: device.store(), lambda _r: self.status.setText("Stored in device"))

    def _identify(self, on: bool) -> None:
        self._run(lambda device: device.identify(on))

    def _lambda(self) -> None:
        index, rate = self.lambda_select.currentIndex() + 1, self.bit_rate.currentIndex()

        def work(device):
            can.set_lambda(device.bridge, self.device_id, index, rate)

        self._run(work, lambda _r: self.status.setText("Changes were successful. Please cycle power to the CAN device."))


class KeypadDialog(QtWidgets.QDialog):
    """CAN Keypad Setup: node id, bit rate, backlight, key brightness and start-up LED show."""

    def __init__(self, owner: CanSetupDialog, module: int, node: int, product: int) -> None:
        super().__init__(owner)
        self.setWindowTitle("CAN Keypad Setup")
        self.owner, self.module, self.node = owner, module, node
        self.variant = 1 if product == 0x3E2 else 0
        self.node_id = QtWidgets.QSpinBox()
        self.node_id.setRange(1, 0x7F)
        self.node_id.setValue(node)
        self.bit_rate = QtWidgets.QComboBox()
        self.bit_rate.addItems(can.KEYPAD_BIT_RATES)
        self.colour = QtWidgets.QComboBox()
        self.colour.addItems(("Off",) + can.KEYPAD_COLOURS)
        self.backlight = QtWidgets.QSpinBox()
        self.backlight.setRange(0, can.KEYPAD_MAX_LEVEL)
        self.keys = QtWidgets.QSpinBox()
        self.keys.setRange(0, can.KEYPAD_MAX_LEVEL)
        self.show_mode = QtWidgets.QComboBox()
        self.show_mode.addItems(can.KEYPAD_SHOWS)
        self.status = QtWidgets.QLabel(f"Node {node}, product {product:#05x}")
        form = QtWidgets.QFormLayout()
        for label, widget, handler in (
            ("NodeId:", self.node_id, self._send_node),
            ("Bit Rate", self.bit_rate, self._send_rate),
            ("Backlight Colour", self.colour, None),
            ("Backlight Colour and Brightness (0-63):", self.backlight, self._send_backlight),
            ("Key Brightness (0-63):", self.keys, self._send_keys),
            ("Startup LED Show:", self.show_mode, self._send_show),
        ):
            row = QtWidgets.QHBoxLayout()
            row.addWidget(widget, 1)
            if handler:
                send = QtWidgets.QPushButton("Send")
                send.clicked.connect(handler)
                row.addWidget(send)
            form.addRow(label, row)
        defaults = QtWidgets.QPushButton("Send Default Coms Setup")
        defaults.clicked.connect(self._send_defaults)
        close = QtWidgets.QDialogButtonBox(BUTTONS.Close)
        close.rejected.connect(self.reject)
        column = QtWidgets.QVBoxLayout(self)
        column.addWidget(self.status)
        column.addLayout(form)
        column.addWidget(defaults)
        column.addWidget(close)

    def _run(self, work, message: str) -> None:
        def job(ecu):
            bridge = can.CanBridge(ecu.link, self.module)
            bridge.start()
            try:
                work(bridge)
            finally:
                bridge.stop()

        def done(_result) -> None:
            self.status.setText(message)

        self.owner.window_.call(job, done)

    def _send_node(self) -> None:
        new = self.node_id.value()
        self._run(lambda bridge: can.keypad_node_id(bridge, self.node, new), "NodeId sent successfully")

    def _send_rate(self) -> None:
        index = self.bit_rate.currentIndex()
        self._run(lambda bridge: can.keypad_bit_rate(bridge, self.node, index), "Bit Rate sent successfully, please power cycle the device.")

    def _send_backlight(self) -> None:
        colour, level = self.colour.currentIndex(), self.backlight.value()
        self._run(lambda bridge: can.keypad_backlight(bridge, self.node, colour, level, self.variant), "Backlight Color and Brightness sent successfully")

    def _send_keys(self) -> None:
        level = self.keys.value()
        self._run(lambda bridge: can.keypad_key_brightness(bridge, self.node, level, self.variant), "Key Brightness sent successfully")

    def _send_show(self) -> None:
        show = self.show_mode.currentIndex()
        self._run(lambda bridge: can.keypad_startup_show(bridge, self.node, show), "Startup Mode sent successfully")

    def _send_defaults(self) -> None:
        self._run(lambda bridge: can.keypad_default_comms(bridge, self.node), "Default Communication Settings sent successfully")
