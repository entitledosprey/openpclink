"""Logging Setup: log set files (.lls5, .llsx), and the PC and ECU logging tabs."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from . import crypto


class LogSetError(Exception):
    pass


def read_log_set(path: Path, key: tuple[int, ...]) -> list[tuple[int, str]]:
    """The parameter ids in a log set file, each with its rate text ("" when the file has none)."""
    try:
        plain = crypto.decode(Path(path).read_bytes(), key)
        root = ET.fromstring(plain)
    except (OSError, crypto.BadContainer, ET.ParseError) as exc:
        raise LogSetError(str(exc)) from exc
    rows = []
    for node in root.iter("Parameter"):
        name = node.get("Name", "")
        if not name.isdigit():
            continue
        rate = next((prop.get("Value", "") for prop in node.iter("Property") if prop.get("Name") == "Rate"), "")
        rows.append((int(name), rate))
    return rows


def log_set_xml(rows: list[tuple[int, str]]) -> bytes:
    root = ET.Element("LinkStorage", {"Version": "101", "Type": ".xml"})
    parameters = ET.SubElement(root, "Parameters")
    for index, (data_id, rate) in enumerate(rows, 1):
        node = ET.SubElement(parameters, "Parameter", {"DataId": str(index), "Name": str(data_id)})
        properties = ET.SubElement(node, "Properties")
        if rate:
            ET.SubElement(properties, "Property", {"Name": "Rate", "Type": "3", "Value": rate})
    ET.indent(root, "\t")
    return b'<?xml version="1.0"?>\n' + ET.tostring(root, encoding="utf-8").replace(b" />", b"/>") + b"\n"


def write_log_set(path: Path, rows: list[tuple[int, str]], key: tuple[int, ...]) -> None:
    Path(path).write_bytes(crypto.encode(log_set_xml(rows), key))


# ---- settings ---------------------------------------------------------------

import weakref

from PySide6 import QtCore, QtGui, QtWidgets

PC_RATES = ("40 Hz", "20 Hz", "15 Hz", "10 Hz", "5 Hz", "2 Hz", "1 Hz", "0.5 Hz", "0.1 Hz")
ECU_RATES = (100, 50, 20, 10, 5, 2, 1, 0.5)
AUTO_RECORD = (("Manual", 0), ("RPM", 1000), ("MAP", 1001), ("TPS", 1078))
ID_ROLE = QtCore.Qt.ItemDataRole.UserRole
STATUS_KIND = 5
DEFAULT_PC = {
    "rate": "40 Hz", "keep_all": False, "auto_id": 0, "auto_value": 50.0, "display_auto": False,
    "display_range": 40, "open_zoom": 0, "history": True, "history_size": 100,
}
_infos: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def rate_text(hz: float) -> str:
    return f"{round(hz, 1):g} Hz"


def rate_value(text: str) -> float:
    try:
        return float(text.strip().split()[0])
    except (IndexError, ValueError):
        return 0.0


def pc_settings(stored: dict) -> dict:
    """The PC logging options with LoggingOptions.xml's defaults behind them."""
    merged = dict(DEFAULT_PC)
    merged.update({key: value for key, value in stored.items() if key in DEFAULT_PC})
    return merged


def playback_interval(stored: dict) -> float:
    """Seconds between playback steps: PlayBackInterval follows the PC rate."""
    hz = rate_value(str(stored.get("rate", DEFAULT_PC["rate"])))
    return 1.0 / hz if hz else 0.025


def log_info(ecu):
    """The 0x1B reply, asked for once per connection as PCLink does."""
    info = _infos.get(ecu)
    if info is None:
        info = _infos[ecu] = ecu.logs.info()
    return info


class UsageLabels:
    """Logged Parameters n, % of the maximum combined rate, and the data rate."""

    @staticmethod
    def texts(rates: list[float], max_rate: float) -> tuple[str, str, str]:
        combined = sum(rates)
        percent = int(combined * 100 / max_rate) if max_rate else 0
        return f"{len(rates)} Logged Parameters", f"{percent}% of maximum combined rate", f"Data Rate {int(combined * 4 / 1000)}kB/s"


# ---- widgets ----------------------------------------------------------------

class AvailableTree(QtWidgets.QWidget):
    """Available Parameters: grouped like the menu, or a sorted list, or the matches of a search."""

    chosen = QtCore.Signal(int)

    def __init__(self, window, parent=None) -> None:
        super().__init__(parent)
        self.window_ = window
        self.groups = window._groups(True) if window.worker.ecu is not None else {}
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.itemDoubleClicked.connect(self._double_clicked)
        self.search = QtWidgets.QLineEdit()
        self.grouped = QtWidgets.QToolButton()
        self.grouped.setText("Tree")
        self.grouped.setCheckable(True)
        self.grouped.setChecked(True)
        self.alphabetic = QtWidgets.QToolButton()
        self.alphabetic.setText("A-Z")
        self.alphabetic.setCheckable(True)
        group = QtWidgets.QButtonGroup(self)
        group.addButton(self.grouped)
        group.addButton(self.alphabetic)
        group.buttonClicked.connect(lambda _b: self.fill())
        self.search.textChanged.connect(lambda _t: self.fill())
        bar = QtWidgets.QHBoxLayout()
        bar.addWidget(self.grouped)
        bar.addWidget(self.alphabetic)
        bar.addWidget(self.search)
        column = QtWidgets.QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.addLayout(bar)
        column.addWidget(self.tree)
        self.fill()

    def caption(self, data_id: int) -> str:
        database = self.window_.database
        if not database.known(data_id):
            return str(data_id)
        parameter = database.parameter(data_id)
        return parameter.caption + (" (Status)" if parameter.kind == STATUS_KIND else "")

    def _leaf(self, data_id: int) -> QtWidgets.QTreeWidgetItem:
        item = QtWidgets.QTreeWidgetItem([self.caption(data_id)])
        item.setData(0, ID_ROLE, data_id)
        return item

    def fill(self) -> None:
        self.tree.clear()
        text = self.search.text().strip().lower()
        everything = sorted({i for ids in self.groups.values() for i in ids if i}, key=lambda i: self.caption(i).lower())
        if text or self.alphabetic.isChecked():
            for data_id in everything:
                if text in self.caption(data_id).lower():
                    self.tree.addTopLevelItem(self._leaf(data_id))
            return
        nodes: dict[tuple[str, ...], QtWidgets.QTreeWidgetItem] = {}
        tree = getattr(self.window_, "tree", None)
        order = {tree.topLevelItem(i).text(0): i for i in range(tree.topLevelItemCount())} if tree is not None else {}
        for path in sorted(self.groups, key=lambda p: (order.get(p[0], len(order)), p)):
            for depth in range(1, len(path) + 1):
                prefix = path[:depth]
                if prefix not in nodes:
                    parent = nodes.get(prefix[:-1])
                    nodes[prefix] = QtWidgets.QTreeWidgetItem(parent or self.tree, [prefix[-1]])
            for data_id in sorted((i for i in self.groups[path] if i), key=lambda i: self.caption(i).lower()):
                nodes[path].addChild(self._leaf(data_id))

    def selected_ids(self) -> list[int]:
        return [item.data(0, ID_ROLE) for item in self.tree.selectedItems() if item.data(0, ID_ROLE) is not None]

    def all_ids(self) -> list[int]:
        return sorted({i for ids in self.groups.values() for i in ids if i}, key=lambda i: self.caption(i).lower())

    def _double_clicked(self, item, _column) -> None:
        data_id = item.data(0, ID_ROLE)
        if data_id is not None:
            self.chosen.emit(data_id)


def arrow_buttons(add_text: str = ">>", remove_text: str = "<<") -> tuple[QtWidgets.QPushButton, QtWidgets.QPushButton, QtWidgets.QVBoxLayout]:
    add, remove = QtWidgets.QPushButton(add_text), QtWidgets.QPushButton(remove_text)
    add.setFixedWidth(40)
    remove.setFixedWidth(40)
    column = QtWidgets.QVBoxLayout()
    column.addStretch()
    column.addWidget(add)
    column.addWidget(remove)
    column.addStretch()
    return add, remove, column


class LoggingSetupDialog(QtWidgets.QDialog):
    """Logging Setup: PC Logging and ECU Logging tabs, as PCLink's form."""

    def __init__(self, window, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Logging Setup")
        self.resize(560, 720)
        self.window_ = window
        self.options = window.options.log_settings
        self.settings = pc_settings(self.options)
        from .ecu import Ecu

        self.has_ecu_tab = isinstance(window.worker.ecu, Ecu)
        self.info = None
        self.ecu_dirty = False
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(self._pc_tab(), "PC Logging")
        self.tabs.addTab(self._ecu_tab(), "ECU Logging")
        if self.has_ecu_tab:
            self.tabs.setCurrentIndex(1)
            window.call(lambda ecu: log_info(ecu), self._got_info)
        buttons = QtWidgets.QHBoxLayout()
        self.help_button, self.ok_button, self.cancel_button = (QtWidgets.QPushButton(t) for t in ("Help", "Ok", "Cancel"))
        self.help_button.clicked.connect(self._help)
        self.ok_button.clicked.connect(self._ok)
        self.cancel_button.clicked.connect(self.reject)
        self.ok_button.setDefault(True)
        buttons.addStretch()
        for button in (self.help_button, self.ok_button, self.cancel_button):
            buttons.addWidget(button)
        column = QtWidgets.QVBoxLayout(self)
        column.addWidget(self.tabs)
        column.addLayout(buttons)

    # ---- PC tab -------------------------------------------------------------

    def _pc_tab(self) -> QtWidgets.QWidget:
        settings = self.settings
        page = QtWidgets.QWidget()
        self.rate = QtWidgets.QComboBox()
        self.rate.addItems(PC_RATES)
        self.rate.setCurrentText(settings["rate"] if settings["rate"] in PC_RATES else "40 Hz")
        self.keep_all = QtWidgets.QCheckBox("Save unchanged points")
        self.keep_all.setChecked(bool(settings["keep_all"]))
        top = QtWidgets.QVBoxLayout()
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.rate)
        row.addWidget(self.keep_all)
        top.addWidget(QtWidgets.QLabel("Rate"))
        top.addLayout(row)

        self.display_auto = QtWidgets.QCheckBox("Auto")
        self.display_auto.setChecked(bool(settings["display_auto"]))
        self.display_range = QtWidgets.QSpinBox()
        self.display_range.setRange(1, 100000)
        self.display_range.setValue(int(settings["display_range"]))
        self.display_range.setEnabled(not self.display_auto.isChecked())
        self.display_auto.toggled.connect(lambda on: self.display_range.setEnabled(not on))
        display = QtWidgets.QGroupBox("Display time Range")
        form = QtWidgets.QGridLayout(display)
        form.addWidget(self.display_auto, 0, 0, 1, 2)
        form.addWidget(self.display_range, 1, 0)
        form.addWidget(QtWidgets.QLabel("Seconds"), 1, 1)

        self.zoom_all = QtWidgets.QRadioButton("Zoom All")
        self.zoom_shortest = QtWidgets.QRadioButton("Zoom Shortest Section (Fastest Lap)")
        (self.zoom_shortest if settings["open_zoom"] == 1 else self.zoom_all).setChecked(True)
        opening = QtWidgets.QGroupBox("When Opening Log File")
        column = QtWidgets.QVBoxLayout(opening)
        column.addWidget(self.zoom_all)
        column.addWidget(self.zoom_shortest)

        control = QtWidgets.QGroupBox("Control")
        grid = QtWidgets.QGridLayout(control)
        self.auto_buttons = {}
        wanted = int(settings["auto_id"])
        known = [data_id for _name, data_id in AUTO_RECORD]
        for row_number, (name, data_id) in enumerate(AUTO_RECORD):
            button = QtWidgets.QRadioButton(name)
            button.setChecked(data_id == wanted or (data_id == 0 and wanted not in known))
            button.toggled.connect(lambda _on: self.auto_value.setEnabled(not self.auto_buttons["Manual"].isChecked()))
            self.auto_buttons[name] = button
            grid.addWidget(button, row_number, 0)
        self.auto_value = QtWidgets.QDoubleSpinBox()
        self.auto_value.setRange(-1e6, 1e6)
        self.auto_value.setDecimals(1)
        self.auto_value.setValue(float(settings["auto_value"]))
        self.auto_value.setEnabled(not self.auto_buttons["Manual"].isChecked())
        grid.addWidget(QtWidgets.QLabel("Greater Than:"), 0, 1)
        grid.addWidget(self.auto_value, 1, 1)

        self.history = QtWidgets.QCheckBox("Add to Start of PC Log")
        self.history.setChecked(bool(settings["history"]))
        self.history_size = QtWidgets.QSpinBox()
        self.history_size.setRange(1, 100000)
        self.history_size.setValue(int(settings["history_size"]))
        sample = QtWidgets.QGroupBox("Online Sample History")
        grid = QtWidgets.QGridLayout(sample)
        grid.addWidget(self.history, 0, 0, 1, 2)
        grid.addWidget(self.history_size, 1, 0)
        grid.addWidget(QtWidgets.QLabel("Samples"), 1, 1)

        left = QtWidgets.QVBoxLayout()
        left.addLayout(top)
        left.addWidget(display)
        left.addWidget(opening)
        right = QtWidgets.QVBoxLayout()
        right.addWidget(control)
        right.addWidget(sample)
        head = QtWidgets.QHBoxLayout()
        head.addLayout(left, 1)
        head.addLayout(right, 1)

        self.pc_available = AvailableTree(self.window_)
        self.pc_logged = QtWidgets.QListWidget()
        self.pc_logged.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        for data_id in self.window_.recorded_ids():
            if data_id:
                self._pc_add(data_id)
        add, remove, arrows = arrow_buttons(">>", "<<")
        remove.hide()
        add.clicked.connect(lambda: [self._pc_add(i) for i in self.pc_available.selected_ids()])
        self.pc_available.chosen.connect(self._pc_add)
        lists = QtWidgets.QHBoxLayout()
        left_list = QtWidgets.QVBoxLayout()
        left_list.addWidget(QtWidgets.QLabel("Available Parameters"))
        left_list.addWidget(self.pc_available)
        right_list = QtWidgets.QVBoxLayout()
        right_list.addWidget(QtWidgets.QLabel("Logged Parameters"))
        right_list.addWidget(self.pc_logged)
        lists.addLayout(left_list, 1)
        lists.addLayout(arrows)
        lists.addLayout(right_list, 1)

        add_all, load, save, remove_items = (QtWidgets.QPushButton(t) for t in ("Add All >>", "Load", "Save", "Remove Item(s)"))
        add_all.clicked.connect(lambda: [self._pc_add(i) for i in self.pc_available.all_ids()])
        remove_items.clicked.connect(self._pc_remove)
        load.clicked.connect(lambda: self._load(False))
        save.clicked.connect(lambda: self._save(False))
        foot = QtWidgets.QHBoxLayout()
        for button in (add_all, load, save, remove_items):
            foot.addWidget(button)
        layout = QtWidgets.QVBoxLayout(page)
        layout.addLayout(head)
        layout.addLayout(lists, 1)
        layout.addLayout(foot)
        return page

    def _pc_ids(self) -> list[int]:
        return [self.pc_logged.item(row).data(ID_ROLE) for row in range(self.pc_logged.count())]

    def _pc_add(self, data_id: int) -> bool:
        if data_id in self._pc_ids():
            return False
        item = QtWidgets.QListWidgetItem(self.pc_available.caption(data_id))
        item.setData(ID_ROLE, data_id)
        self.pc_logged.addItem(item)
        return True

    def _pc_remove(self) -> None:
        for item in self.pc_logged.selectedItems():
            self.pc_logged.takeItem(self.pc_logged.row(item))

    # ---- ECU tab ------------------------------------------------------------

    def _ecu_tab(self) -> QtWidgets.QWidget:
        window = self.window_
        ecu = window.worker.ecu if self.has_ecu_tab else None
        from . import tools

        self.ids = tools.ecu_log_settings(ecu) if ecu is not None else dict(tools.ECU_SETTINGS)
        page = QtWidgets.QWidget()
        self.tick_rate = 0
        self.max_rate = 0
        self.max_parameters = ecu.table[self.ids["parameters"]].count if ecu is not None and self.ids["parameters"] in ecu.table else 0

        def read(name: str) -> float:
            return ecu.get(self.ids[name]) if ecu is not None and self.ids[name] in ecu.table else 0.0

        self.ecu_enabled = QtWidgets.QCheckBox("Enabled")
        self.ecu_enabled.setChecked(read("mode") != 0)
        self.default_rate = QtWidgets.QComboBox()
        self.default_rate.addItems([rate_text(rate) for rate in ECU_RATES])
        self.default_rate.setCurrentText("5 Hz")
        self.ecu_enabled.toggled.connect(self._ecu_changed)
        self.default_rate.currentIndexChanged.connect(self._ecu_changed)
        head = QtWidgets.QVBoxLayout()
        head.addWidget(self.ecu_enabled)
        head.addWidget(QtWidgets.QLabel("Default Frequency"))
        head.addWidget(self.default_rate)
        head.addStretch()

        self.io_on, self.io_off = QtWidgets.QComboBox(), QtWidgets.QComboBox()
        for combo, key in ((self.io_on, "io_on"), (self.io_off, "io_off")):
            if ecu is not None and window.database.known(self.ids[key]):
                for value, text in window.database.visible_options(self.ids[key], ecu.info.link_id):
                    combo.addItem(text, value)
                at = combo.findData(int(read(key)))
                combo.setCurrentIndex(max(at, 0))
            combo.currentIndexChanged.connect(self._ecu_changed)
        self.fields: dict[str, QtWidgets.QLineEdit] = {}
        self.limits: dict[str, tuple[float, float]] = {}
        for name in ("rpm", "tps", "map", "off_delay"):
            edit = QtWidgets.QLineEdit(f"{read(name):g}")
            edit.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight)
            edit.textEdited.connect(self._ecu_changed)
            self.fields[name] = edit
            low, high = 0.0, 1e9
            if window.database.known(self.ids[name]):
                parameter = window.database.parameter(self.ids[name])
                low = parameter.minimum if parameter.minimum is not None else 0.0
                high = parameter.maximum if parameter.maximum is not None else 1e9
            self.limits[name] = (low, high)
        conditions = QtWidgets.QGroupBox("Control Conditions")
        grid = QtWidgets.QGridLayout(conditions)
        grid.addWidget(QtWidgets.QLabel("IO On"), 0, 0)
        grid.addWidget(self.io_on, 1, 0)
        grid.addWidget(QtWidgets.QLabel("Off Condition"), 2, 0)
        grid.addWidget(self.io_off, 3, 0)
        for column, (title, name) in enumerate((("RPM Above (RPM)", "rpm"), ("TPS Above (%)", "tps"), ("MAP Above (kPa)", "map"), ("Off delay (s)", "off_delay"))):
            row, offset = divmod(column, 2)
            grid.addWidget(QtWidgets.QLabel(title), row * 2, 1 + offset)
            grid.addWidget(self.fields[name], row * 2 + 1, 1 + offset)
        self.label_text = ecu.get_string(self.ids["label"]) if ecu is not None and self.ids["label"] in ecu.table else ""
        top = QtWidgets.QHBoxLayout()
        top.addLayout(head)
        top.addWidget(conditions, 1)

        self.ecu_available = AvailableTree(window)
        self.ecu_logged = QtWidgets.QTreeWidget()
        self.ecu_logged.setColumnCount(2)
        self.ecu_logged.setHeaderLabels(["Item", "Frequency"])
        self.ecu_logged.setRootIsDecorated(False)
        self.ecu_logged.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.usage = [QtWidgets.QLabel() for _ in range(3)]
        self.messages = QtWidgets.QLabel("ECU must be connected to setup ECU logging.")
        self.messages.setVisible(not self.has_ecu_tab)
        if ecu is not None and self.ids["parameters"] in ecu.table:
            parameters, rates = ecu.get_all(self.ids["parameters"]), ecu.get_all(self.ids["rates"])
            self._pending = [(int(i), int(r)) for i, r in zip(parameters, rates) if i and r]
        else:
            self._pending = []
        add, remove, arrows = arrow_buttons()
        add.clicked.connect(lambda: [self._ecu_add(i, rate_value(self.default_rate.currentText()), True) for i in self.ecu_available.selected_ids()])
        remove.clicked.connect(self._ecu_remove)
        self.ecu_available.chosen.connect(lambda i: self._ecu_add(i, rate_value(self.default_rate.currentText()), True))
        lists = QtWidgets.QHBoxLayout()
        left = QtWidgets.QVBoxLayout()
        left.addWidget(QtWidgets.QLabel("Available Parameters"))
        left.addWidget(self.ecu_available)
        right = QtWidgets.QVBoxLayout()
        right.addWidget(QtWidgets.QLabel("Logged Parameters"))
        for label in self.usage:
            right.addWidget(label)
        right.addWidget(self.ecu_logged)
        lists.addLayout(left, 1)
        lists.addLayout(arrows)
        lists.addLayout(right, 1)
        load, save = QtWidgets.QPushButton("Load"), QtWidgets.QPushButton("Save")
        load.clicked.connect(lambda: self._load(True))
        save.clicked.connect(lambda: self._save(True))
        foot = QtWidgets.QHBoxLayout()
        foot.addStretch()
        foot.addWidget(load)
        foot.addWidget(save)
        layout = QtWidgets.QVBoxLayout(page)
        layout.addWidget(self.messages)
        layout.addLayout(top)
        layout.addLayout(lists, 1)
        layout.addLayout(foot)
        page.setEnabled(self.has_ecu_tab)
        self._ecu_enable_controls()
        self._usage()
        return page

    def _got_info(self, info) -> None:
        """The first reply of 0x1B gives the tick rate every divider is relative to."""
        self.info = info
        self.tick_rate, self.max_rate = info.tick_rate, info.max_rate
        if info.max_channels:
            self.max_parameters = info.max_channels
        for data_id, divider in self._pending:
            self._ecu_add(data_id, hz_of_divider(divider, self.tick_rate), False, loading=True)
        self._pending = []
        self.ecu_dirty = False
        self._usage()

    def _ecu_changed(self, *_args) -> None:
        self.ecu_dirty = True
        self._ecu_enable_controls()

    def _ecu_enable_controls(self) -> None:
        on = self.ecu_enabled.isChecked()
        for widget in (self.default_rate, self.io_on, self.io_off, *self.fields.values()):
            widget.setEnabled(on)

    def ecu_rows(self) -> list[tuple[int, float]]:
        rows = []
        for row in range(self.ecu_logged.topLevelItemCount()):
            item = self.ecu_logged.topLevelItem(row)
            rows.append((item.data(0, ID_ROLE), rate_value(self.ecu_logged.itemWidget(item, 1).currentText())))
        return rows

    def _usage(self) -> None:
        rates = [hz for _id, hz in self.ecu_rows()]
        for label, text in zip(self.usage, UsageLabels.texts(rates, self.max_rate)):
            label.setText(text)

    def _warn(self, text: str) -> None:
        QtWidgets.QMessageBox.warning(self, "Logging Setup", text)

    def _ecu_add(self, data_id: int, hz: float, announce: bool, loading: bool = False) -> bool:
        """PCLink's checks in order: room, already listed, an allowed rate, the combined rate."""
        def refuse(text: str) -> bool:
            if announce:
                self._warn(text)
            return False

        if self.ecu_logged.topLevelItemCount() >= (self.max_parameters or 250):
            return refuse("Allowed number of parameters exceeded.")
        if data_id in [i for i, _hz in self.ecu_rows()]:
            return refuse("Parameter(s) already in list.")
        text = rate_text(hz)
        if text not in [rate_text(rate) for rate in ECU_RATES]:
            return refuse("Frequency is not an allowed value.")
        if self.max_rate and sum(h for _i, h in self.ecu_rows()) + hz > self.max_rate:
            return refuse("Maximum combined logging rate exceeded. Select lower rates or remove some parameters.")
        item = QtWidgets.QTreeWidgetItem([self.ecu_available.caption(data_id)])
        item.setData(0, ID_ROLE, data_id)
        self.ecu_logged.addTopLevelItem(item)
        combo = QtWidgets.QComboBox()
        combo.addItems([rate_text(rate) for rate in ECU_RATES])
        combo.setCurrentText(text)
        combo.currentIndexChanged.connect(self._rate_edited)
        self.ecu_logged.setItemWidget(item, 1, combo)
        if not loading:
            self._ecu_changed()
        self._usage()
        return True

    def _rate_edited(self, _index) -> None:
        self._ecu_changed()
        self._usage()

    def _ecu_remove(self) -> None:
        for item in self.ecu_logged.selectedItems():
            self.ecu_logged.takeTopLevelItem(self.ecu_logged.indexOfTopLevelItem(item))
        self._ecu_changed()
        self._usage()

    # ---- log set files -----------------------------------------------------

    def _load(self, ecu_tab: bool) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Load", self.window_.options.folder("log"), "Log Sets (*.lls5 *.llsx)")
        if not path:
            return
        try:
            rows = read_log_set(Path(path), self.window_.database.map_key)
        except LogSetError as exc:
            self._warn(f"Failed to load file! {exc}")
            return
        known = set(self.pc_available.all_ids())
        for data_id, rate in rows:
            if data_id not in known:
                continue
            if ecu_tab:
                self._ecu_add(data_id, rate_value(rate) if rate else 50.0, False)
            else:
                self._pc_add(data_id)

    def _save(self, ecu_tab: bool) -> None:
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save", str(Path(self.window_.options.folder("log")) / "LogSet.lls5"), "Log Sets (*.lls5)")
        if not path:
            return
        rows = [(data_id, rate_text(hz)) for data_id, hz in self.ecu_rows()] if ecu_tab else [(i, self.default_rate.currentText()) for i in self._pc_ids()]
        try:
            write_log_set(Path(path), rows, self.window_.database.map_key)
        except OSError as exc:
            self._warn(f"Failed to save file! {exc}")

    # ---- closing ------------------------------------------------------------

    def _help(self) -> None:
        show = getattr(self.window_, "show_topic", None)
        if show:
            show("ECU_Logging" if self.tabs.currentIndex() == 1 else "PC_Logging")

    def _ecu_values(self) -> dict[str, float] | None:
        values = {}
        for name, edit in self.fields.items():
            low, high = self.limits[name]
            try:
                value = float(edit.text())
            except ValueError:
                value = low - 1
            if not low <= value <= high:
                self._warn(f"Please enter a value between {low:f} and {high:f}")
                edit.setFocus()
                return None
            values[name] = value
        return values

    def _ok(self) -> None:
        apply_ecu = self.has_ecu_tab and self.info is not None and (self.tabs.currentIndex() == 1 or self.ecu_dirty)
        values = self._ecu_values() if apply_ecu else {}
        if values is None:
            return
        zoom = 1 if self.zoom_shortest.isChecked() else 0
        auto_id = next(data_id for name, data_id in AUTO_RECORD if self.auto_buttons[name].isChecked())
        self.options.update(
            rate=self.rate.currentText(), keep_all=self.keep_all.isChecked(), auto_id=auto_id, auto_value=self.auto_value.value(),
            display_auto=self.display_auto.isChecked(), display_range=self.display_range.value(), open_zoom=zoom,
            history=self.history.isChecked(), history_size=self.history_size.value(), record_ids=self._pc_ids(),
        )
        self.window_.options.save(self.window_.options_path)
        refresh = getattr(self.window_, "log_settings_changed", None)
        if refresh:
            refresh()
        if apply_ecu:
            from . import tools

            rows, enabled = self.ecu_rows(), self.ecu_enabled.isChecked()
            ids, tick, label = self.ids, self.tick_rate, self.label_text
            io_on, io_off = self.io_on.currentData() or 0, self.io_off.currentData() or 0
            self.window_.call(
                lambda ecu: tools.apply_ecu_logging(ecu, ids, rows, values, label, enabled, io_on, io_off, tick),
                lambda _r: self.window_.statusBar().showMessage("ECU logging set. Store to keep it.", 6000),
            )
        self.accept()


def hz_of_divider(divider: int, tick_rate: int) -> float:
    return tick_rate / divider if divider else 0.0
