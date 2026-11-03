"""PCLink's dialogs and tool windows, each following the real program's fields and wording."""
from __future__ import annotations

import time
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from . import can, eculog, firmware, scope, theme as themes, tune
from .ecu import Ecu
from .gauges import GaugeSettings, tcolor, to_tcolor
from .layoutfile import Layout, LayoutError, LayoutView
from .logfile import DETAIL_FIELDS, READ_ONLY_DETAILS, ExportOptions, Log, build_link_log, export_csv, save_link_log
from .logviews import LogSession, TimePlot
from .transports import open_transport

BUTTONS = QtWidgets.QDialogButtonBox.StandardButton
ID_ROLE = QtCore.Qt.ItemDataRole.UserRole


def ok_cancel(dialog: QtWidgets.QDialog, ok=None) -> QtWidgets.QDialogButtonBox:
    box = QtWidgets.QDialogButtonBox(BUTTONS.Ok | BUTTONS.Cancel)
    box.accepted.connect(ok or dialog.accept)
    box.rejected.connect(dialog.reject)
    return box


class MemoDialog(QtWidgets.QDialog):
    def __init__(self, text: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Memo")
        self.resize(480, 340)
        self.editor = QtWidgets.QPlainTextEdit(text)
        column = QtWidgets.QVBoxLayout(self)
        column.addWidget(self.editor)
        column.addWidget(ok_cancel(self))


class TextDialog(QtWidgets.QDialog):
    """Read-only text, such as a trap dump."""

    def __init__(self, title: str, text: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(560, 420)
        view = QtWidgets.QPlainTextEdit(text)
        view.setReadOnly(True)
        box = QtWidgets.QDialogButtonBox(BUTTONS.Close)
        box.rejected.connect(self.reject)
        column = QtWidgets.QVBoxLayout(self)
        column.addWidget(view)
        column.addWidget(box)


class PasswordDialog(QtWidgets.QDialog):
    """Enter a new password, or the existing one to unlock."""

    def __init__(self, setting: bool, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Enter Password")
        self.setting = setting
        form = QtWidgets.QFormLayout(self)
        self.password = QtWidgets.QLineEdit()
        self.password.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.password.setMaxLength(20)
        form.addRow("Password", self.password)
        self.confirm = QtWidgets.QLineEdit()
        self.confirm.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.confirm.setMaxLength(20)
        self.temporary = QtWidgets.QCheckBox("Temporary Unlock")
        if setting:
            form.addRow("Confirm Password", self.confirm)
        else:
            form.addRow(self.temporary)
        form.addRow(ok_cancel(self, self._check))

    def _check(self) -> None:
        if self.setting:
            if len(self.password.text()) < 3:
                QtWidgets.QMessageBox.warning(self, "Enter Password", "Password must be at least 3 characters")
                return
            if self.password.text() != self.confirm.text():
                QtWidgets.QMessageBox.warning(self, "Enter Password", "New passwords do not match.")
                return
        self.accept()


class SerialDialog(QtWidgets.QDialog):
    """Save as Encrypted asks which ECU the file is for."""

    def __init__(self, current: int | None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Encrypt File")
        form = QtWidgets.QFormLayout(self)
        form.addRow(QtWidgets.QLabel("This locks the file to one ECU's serial number. It is not strong encryption."))
        self.serial = QtWidgets.QLineEdit()
        form.addRow("ECU serial number", self.serial)
        load = QtWidgets.QPushButton("Load From ECU")
        load.setEnabled(current is not None)
        load.clicked.connect(lambda: self.serial.setText(str(current)))
        form.addRow(load)
        form.addRow(ok_cancel(self, self._check))

    def value(self) -> int:
        text = self.serial.text().strip()
        return int(text) if text.isdigit() else 0

    def _check(self) -> None:
        if not self.value():
            QtWidgets.QMessageBox.warning(self, "Encrypt File", "Enter a valid ECU serial number")
            return
        self.accept()


class ColourThemeDialog(QtWidgets.QDialog):
    """Colour Theme Setup: pick a theme, then change any item's colour."""

    def __init__(self, window, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Colour Theme Setup")
        self.window_ = window
        self.theme = themes.theme_named(window.options)
        self.choice = QtWidgets.QComboBox()
        self.choice.addItems(themes.THEME_NAMES)
        self.choice.setCurrentText(self.theme.name if self.theme.name in themes.THEME_NAMES else "Black")
        self.choice.activated.connect(self._pick_theme)
        self.items = QtWidgets.QListWidget()
        self.parameters = QtWidgets.QListWidget()
        self.overlays = QtWidgets.QListWidget()
        for widget in (self.items, self.parameters, self.overlays):
            widget.itemDoubleClicked.connect(self._edit)
        edit = QtWidgets.QPushButton("Edit Colour")
        edit.clicked.connect(lambda: self._edit(self._selected()))
        form = QtWidgets.QFormLayout(self)
        form.addRow("Select Theme", self.choice)
        form.addRow("Theme Items", self.items)
        form.addRow("Parameters", self.parameters)
        form.addRow("Overlays", self.overlays)
        form.addRow(edit)
        box = QtWidgets.QDialogButtonBox(BUTTONS.Ok | BUTTONS.Apply | BUTTONS.Cancel)
        box.accepted.connect(self._ok)
        box.rejected.connect(self.reject)
        box.button(BUTTONS.Apply).clicked.connect(self._apply)
        form.addRow(box)
        self._fill()

    def _swatch(self, widget: QtWidgets.QListWidget, text: str, value: int, key) -> None:
        pixels = QtGui.QPixmap(16, 16)
        pixels.fill(tcolor(value))
        item = QtWidgets.QListWidgetItem(QtGui.QIcon(pixels), text)
        item.setData(ID_ROLE, key)
        widget.addItem(item)

    def _fill(self) -> None:
        for widget in (self.items, self.parameters, self.overlays):
            widget.clear()
        for key, title in themes.ITEMS:
            self._swatch(self.items, title, self.theme.items[key], ("item", key))
        for index, value in enumerate(self.theme.parameters):
            self._swatch(self.parameters, f"Parameter {index + 1}", value, ("parameter", index))
        for index, value in enumerate(self.theme.overlays):
            self._swatch(self.overlays, f"Overlay {index + 1}", value, ("overlay", index))

    def _pick_theme(self) -> None:
        self.theme = themes.Theme.built_in(self.choice.currentText())
        self._fill()

    def _selected(self):
        for widget in (self.items, self.parameters, self.overlays):
            if widget.currentItem():
                return widget.currentItem()
        return None

    def _edit(self, item) -> None:
        if item is None:
            return
        kind, key = item.data(ID_ROLE)
        store = {"item": self.theme.items, "parameter": self.theme.parameters, "overlay": self.theme.overlays}[kind]
        chosen = QtWidgets.QColorDialog.getColor(tcolor(store[key]), self, "Edit Colour")
        if chosen.isValid():
            store[key] = to_tcolor(chosen)
            self._fill()

    def _apply(self) -> None:
        self.window_.options.theme = self.theme.name
        self.window_.options.themes[self.theme.name] = self.theme.saved()
        self.window_.options_changed()

    def _ok(self) -> None:
        self._apply()
        self.accept()


class ParameterChooser(QtWidgets.QDialog):
    """Pick live or logged parameters for a view, in order."""

    def __init__(self, title: str, choices: list[tuple[int, str]], chosen: list[int], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(620, 480)
        self.available = QtWidgets.QListWidget()
        self.selected = QtWidgets.QListWidget()
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("Search")
        names = dict(choices)
        for data_id, name in sorted(choices, key=lambda pair: pair[1]):
            item = QtWidgets.QListWidgetItem(name)
            item.setData(ID_ROLE, data_id)
            self.available.addItem(item)
        for data_id in chosen:
            if data_id in names:
                item = QtWidgets.QListWidgetItem(names[data_id])
                item.setData(ID_ROLE, data_id)
                self.selected.addItem(item)
        self.search.textChanged.connect(self._filter)
        self.available.itemDoubleClicked.connect(self._add)
        self.selected.itemDoubleClicked.connect(lambda item: self.selected.takeItem(self.selected.row(item)))
        add = QtWidgets.QPushButton(">>")
        add.clicked.connect(lambda: self._add(self.available.currentItem()))
        remove = QtWidgets.QPushButton("<<")
        remove.clicked.connect(lambda: self.selected.takeItem(self.selected.currentRow()))
        middle = QtWidgets.QVBoxLayout()
        middle.addStretch()
        middle.addWidget(add)
        middle.addWidget(remove)
        middle.addStretch()
        lists = QtWidgets.QHBoxLayout()
        left = QtWidgets.QVBoxLayout()
        left.addWidget(self.search)
        left.addWidget(self.available)
        lists.addLayout(left)
        lists.addLayout(middle)
        lists.addWidget(self.selected)
        column = QtWidgets.QVBoxLayout(self)
        column.addLayout(lists)
        column.addWidget(ok_cancel(self))

    def _filter(self, text: str) -> None:
        for row in range(self.available.count()):
            item = self.available.item(row)
            item.setHidden(text.lower() not in item.text().lower())

    def _add(self, item) -> None:
        if item is None:
            return
        copy = QtWidgets.QListWidgetItem(item.text())
        copy.setData(ID_ROLE, item.data(ID_ROLE))
        self.selected.addItem(copy)

    def ids(self) -> list[int]:
        return [self.selected.item(row).data(ID_ROLE) for row in range(self.selected.count())]


class GaugePropertiesDialog(QtWidgets.QDialog):
    MULTIPLIERS = ("/ 100", "/ 10", "x 1", "x 10", "x 100", "x 1000")

    def __init__(self, settings: GaugeSettings, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Gauge Properties")
        self.settings = settings
        self.manual = QtWidgets.QCheckBox("Manual Configuration")
        self.manual.setChecked(settings.manual)
        self.caption = QtWidgets.QLineEdit(settings.caption)
        self.fields = {}
        form = QtWidgets.QFormLayout(self)
        form.addRow(self.manual)
        form.addRow("Caption", self.caption)
        for name, title in (("minimum", "Gauge Minimum"), ("maximum", "Gauge Maximum"), ("yellow", "Yellow Line"), ("red", "Red Line"), ("low_yellow", "Low Yellow Line"), ("low_red", "Low Red Line")):
            spin = QtWidgets.QDoubleSpinBox()
            spin.setRange(-1e6, 1e6)
            spin.setDecimals(3)
            spin.setValue(getattr(settings, name))
            self.fields[name] = spin
            form.addRow(title, spin)
        self.small = QtWidgets.QSpinBox()
        self.small.setRange(1, 10)
        self.small.setValue(int(settings.small_ticks))
        self.large = QtWidgets.QSpinBox()
        self.large.setRange(1, 20)
        self.large.setValue(int(settings.large_ticks))
        self.places = QtWidgets.QSpinBox()
        self.places.setRange(0, 3)
        self.places.setValue(settings.decimals)
        self.multiplier = QtWidgets.QComboBox()
        self.multiplier.addItems(self.MULTIPLIERS)
        self.multiplier.setCurrentIndex(settings.multiplier)
        self.quality = QtWidgets.QCheckBox("High Quality")
        self.quality.setChecked(settings.high_quality)
        self.colour = settings.colour
        colour = QtWidgets.QPushButton("Colour…")
        colour.clicked.connect(self._choose_colour)
        form.addRow("Small Ticks", self.small)
        form.addRow("Large Ticks", self.large)
        form.addRow("Decimal Places", self.places)
        form.addRow("Multiplier", self.multiplier)
        form.addRow(self.quality)
        form.addRow(colour)
        form.addRow(ok_cancel(self, self._ok))

    def _choose_colour(self) -> None:
        chosen = QtWidgets.QColorDialog.getColor(tcolor(self.colour), self, "Gauge Colour")
        if chosen.isValid():
            self.colour = to_tcolor(chosen)

    def _ok(self) -> None:
        s = self.settings
        s.manual, s.caption = self.manual.isChecked(), self.caption.text()
        for name, spin in self.fields.items():
            setattr(s, name, spin.value())
        s.small_ticks, s.large_ticks, s.decimals = self.small.value(), self.large.value(), self.places.value()
        s.multiplier, s.high_quality, s.colour = self.multiplier.currentIndex(), self.quality.isChecked(), self.colour
        self.accept()


class ParameterSetupDialog(QtWidgets.QDialog):
    """Parameter Properties: display range, warnings and colour for one live value."""

    def __init__(self, caption: str, units: str, display, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Parameter Properties")
        self.display = display
        form = QtWidgets.QFormLayout(self)
        form.addRow("Name", QtWidgets.QLabel(caption))
        form.addRow("Units", QtWidgets.QLabel(units))
        self.colour = QtWidgets.QSpinBox()
        self.colour.setRange(0, 13)
        self.colour.setValue(display.colour)
        form.addRow("Color", self.colour)
        self.fields = {}
        for name, title in (("minimum", "Display Minimum"), ("maximum", "Display Maximum"), ("high_warning", "High Warning"), ("high_error", "High Error"), ("low_warning", "Low Warning"), ("low_error", "Low Error")):
            edit = QtWidgets.QLineEdit("" if getattr(display, name) is None else f"{getattr(display, name):g}")
            self.fields[name] = edit
            form.addRow(title, edit)
        self.auto = QtWidgets.QCheckBox("Automatic Log Scaling")
        self.auto.setChecked(display.auto_scale)
        form.addRow(self.auto)
        form.addRow(ok_cancel(self))

    def result_display(self):
        from .assets import ParameterDisplay

        def number(name: str) -> float | None:
            try:
                return float(self.fields[name].text())
            except ValueError:
                return None

        return ParameterDisplay(self.colour.value(), number("minimum"), number("maximum"), number("high_error"), number("high_warning"), number("low_warning"), number("low_error"), self.auto.isChecked())


class LogDetailsDialog(QtWidgets.QDialog):
    def __init__(self, log: Log, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Log File Details")
        self.log = log
        self.edits = {}
        form = QtWidgets.QFormLayout(self)
        for name, _offset, size in DETAIL_FIELDS:
            if size > 200:
                edit = QtWidgets.QPlainTextEdit(log.details.get(name, ""))
                edit.setMaximumHeight(70)
                edit.setReadOnly(name in READ_ONLY_DETAILS)
            else:
                edit = QtWidgets.QLineEdit(log.details.get(name, ""))
                edit.setReadOnly(name in READ_ONLY_DETAILS)
            self.edits[name] = edit
            form.addRow(name, edit)
        form.addRow(ok_cancel(self, self._ok))

    def _ok(self) -> None:
        for name, edit in self.edits.items():
            text = edit.toPlainText() if isinstance(edit, QtWidgets.QPlainTextEdit) else edit.text()
            if text != self.log.details.get(name, ""):
                self.log.details[name] = text
                self.log.modified = True
        self.accept()


class ExportDialog(QtWidgets.QDialog):
    """PCLink's log export options."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Export")
        self.range = QtWidgets.QComboBox()
        self.range.addItems(["Full Log", "Current Section", "Current Zoom"])
        self.format = QtWidgets.QComboBox()
        self.format.addItems(["CSV", "Link Log File"])
        self.file_time = QtWidgets.QCheckBox("File Time")
        self.file_time.setChecked(True)
        self.section_time = QtWidgets.QCheckBox("Section Time")
        self.device = QtWidgets.QCheckBox("Device Information")
        self.file_info = QtWidgets.QCheckBox("File Information")
        self.driver = QtWidgets.QCheckBox("Driver/Vehicle/Venue")
        form = QtWidgets.QFormLayout(self)
        form.addRow("Range", self.range)
        form.addRow("Format", self.format)
        for box in (self.file_time, self.section_time, self.device, self.file_info, self.driver):
            form.addRow(box)
        form.addRow(ok_cancel(self))


def export_log(session: LogSession, dialog: ExportDialog, path: Path) -> None:
    log = session.log
    start, end = log.time_range()
    if dialog.range.currentIndex() == 1:
        section = next((s for s in log.sections() if s.start <= session.cursor <= s.end), None)
        if section:
            start, end = section.start, section.end
    elif dialog.range.currentIndex() == 2:
        start, end = session.start, session.end
    if dialog.format.currentIndex() == 1:
        Path(path).write_bytes(build_link_log(log, start, end))
        return
    export_csv(log, path, ExportOptions(
        start, end, dialog.file_time.isChecked(), dialog.section_time.isChecked(),
        dialog.device.isChecked(), dialog.file_info.isChecked(), dialog.driver.isChecked(),
    ))


PC_RATES = ("40 Hz", "20 Hz", "15 Hz", "10 Hz", "5 Hz", "2 Hz", "1 Hz", "0.5 Hz", "0.1 Hz")
ECU_SETTINGS_G5 = {"parameters": 10175, "rates": 10176, "io_on": 10170, "io_off": 10171, "label": 10177, "rpm": 10167, "map": 10169, "tps": 10168, "off_delay": 10166, "mode": 10173}
ECU_SETTINGS = {"parameters": 1717, "rates": 1718, "io_on": 1726, "io_off": 1721, "label": 392, "rpm": 1723, "map": 1725, "tps": 1724, "off_delay": 1727, "mode": 3544}
AUTO_RECORD_IDS = {"Manual": 0, "RPM": 1000, "MAP": 1001, "TPS": 1078}


def ecu_log_settings(ecu) -> dict[str, int]:
    return ECU_SETTINGS_G5 if 10173 in ecu.table else ECU_SETTINGS


def hz_of(divider: int, tick_rate: int) -> float:
    return tick_rate / divider if divider else 0.0


def divider_of(hz: float, tick_rate: int) -> int:
    return round(tick_rate / hz) if hz else 0


class LoggingSetupDialog(QtWidgets.QDialog):
    """Setup Logging: PC Logging and ECU Logging tabs."""

    ECU_RATES = (1000, 500, 200, 100, 50, 25, 20, 10, 5, 2, 1)

    def __init__(self, window, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Logging Setup")
        self.resize(760, 560)
        self.window_ = window
        self.options = window.options.log_settings
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(self._pc_tab(), "PC Logging")
        self.has_ecu_tab = isinstance(window.worker.ecu, Ecu)
        if self.has_ecu_tab:
            self.tabs.addTab(self._ecu_tab(), "ECU Logging")
        column = QtWidgets.QVBoxLayout(self)
        column.addWidget(self.tabs)
        column.addWidget(ok_cancel(self, self._ok))

    def _parameter_lists(self, chosen: list[int]) -> tuple[QtWidgets.QWidget, ParameterChooser]:
        chooser = ParameterChooser("", self.window_.live_choices(), chosen, self)
        chooser.setWindowFlags(QtCore.Qt.WindowType.Widget)
        for box in chooser.findChildren(QtWidgets.QDialogButtonBox):
            box.hide()
        return chooser, chooser

    def _pc_tab(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(page)
        self.auto_source = QtWidgets.QComboBox()
        self.auto_source.addItems(list(AUTO_RECORD_IDS))
        wanted = int(self.options.get("auto_id", 0))
        self.auto_source.setCurrentText(next((name for name, data_id in AUTO_RECORD_IDS.items() if data_id == wanted), "Manual"))
        self.auto_value = QtWidgets.QDoubleSpinBox()
        self.auto_value.setRange(-1e6, 1e6)
        self.auto_value.setValue(float(self.options.get("auto_value", 50)))
        self.rate = QtWidgets.QComboBox()
        self.rate.addItems(PC_RATES)
        self.rate.setCurrentText(self.options.get("rate", "20 Hz"))
        self.keep_all = QtWidgets.QCheckBox("Save All Data Points")
        self.keep_all.setChecked(bool(self.options.get("keep_all", False)))
        self.pc_list, self.pc_chooser = self._parameter_lists(self.window_.recorded_ids())
        form.addRow("Auto Record", self.auto_source)
        form.addRow("Above", self.auto_value)
        form.addRow("Default Frequency", self.rate)
        form.addRow(self.keep_all)
        form.addRow(self.pc_list)
        return page

    def _ecu_tab(self) -> QtWidgets.QWidget:
        ecu = self.window_.worker.ecu
        self.ids = ecu_log_settings(ecu)
        page = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(page)

        def read(name: str, default: float = 0.0) -> float:
            return ecu.get(self.ids[name]) if self.ids[name] in ecu.table else default

        self.ecu_enabled = QtWidgets.QCheckBox("Enabled")
        self.ecu_enabled.setChecked(read("mode") != 0)
        self.fields = {}
        for name, title in (("rpm", "RPM Above"), ("tps", "TPS Above"), ("map", "MAP Above"), ("off_delay", "Off Delay")):
            spin = QtWidgets.QDoubleSpinBox()
            spin.setRange(-1e6, 1e6)
            spin.setValue(read(name))
            self.fields[name] = spin
        self.label_edit = QtWidgets.QLineEdit(ecu.get_string(self.ids["label"]) if self.ids["label"] in ecu.table else "")
        self.ecu_rate = QtWidgets.QComboBox()
        self.ecu_rate.addItems([f"{rate} Hz" for rate in self.ECU_RATES])
        self.ecu_rate.setCurrentText("50 Hz")
        chosen = []
        if self.ids["parameters"] in ecu.table:
            chosen = [int(value) for value in ecu.get_all(self.ids["parameters"]) if value]
        self.ecu_list, self.ecu_chooser = self._parameter_lists(chosen)
        form.addRow(self.ecu_enabled)
        for name, spin in self.fields.items():
            form.addRow({"rpm": "RPM Above", "tps": "TPS Above", "map": "MAP Above", "off_delay": "Off Delay"}[name], spin)
        form.addRow("Log File Label", self.label_edit)
        form.addRow("Default Frequency", self.ecu_rate)
        form.addRow(self.ecu_list)
        return page

    def _ok(self) -> None:
        self.options.update(
            auto_id=AUTO_RECORD_IDS[self.auto_source.currentText()], auto_value=self.auto_value.value(),
            rate=self.rate.currentText(), keep_all=self.keep_all.isChecked(), record_ids=self.pc_chooser.ids(),
        )
        self.window_.options.save(self.window_.options_path)
        if self.has_ecu_tab and self.tabs.currentIndex() == 1:
            ids = self.ecu_chooser.ids()
            rate = float(self.ecu_rate.currentText().split()[0])
            values = {name: spin.value() for name, spin in self.fields.items()}
            enabled, label = self.ecu_enabled.isChecked(), self.label_edit.text()
            settings = self.ids
            self.window_.call(lambda ecu: apply_ecu_logging(ecu, settings, ids, rate, values, label, enabled), lambda _r: self.window_.statusBar().showMessage("ECU logging set. Store to keep it.", 6000))
        self.accept()


def apply_ecu_logging(ecu, ids: dict, parameters: list[int], rate: float, values: dict, label: str, enabled: bool) -> None:
    """PCLink's Apply: pause logging, write the list and triggers, then start it again if enabled."""
    info = ecu.logs.info()
    ecu.logs.pause()
    capacity = ecu.table[ids["parameters"]].count if ids["parameters"] in ecu.table else 0
    for index in range(capacity):
        data_id = parameters[index] if index < len(parameters) else 0
        divider = divider_of(rate, info.tick_rate) if data_id else 0
        ecu.set(ids["parameters"], data_id, index)
        ecu.set(ids["rates"], divider, index)
    for name, value in values.items():
        if ids[name] in ecu.table:
            ecu.set(ids[name], value)
    if ids["label"] in ecu.table:
        ecu.set_string(ids["label"], label)
    if ids["mode"] in ecu.table:
        ecu.set(ids["mode"], 1 if enabled else 0)
    if enabled:
        ecu.logs.resume()


class LogDownloadDialog(QtWidgets.QDialog):
    """ECU Log File Download. Logging pauses while it is open, as in PCLink."""

    def __init__(self, window, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("ECU Log File Download")
        self.resize(720, 460)
        self.window_ = window
        self.entries: list[eculog.EcuLogEntry] = []
        self.info: eculog.LogInfo | None = None
        self.grid = QtWidgets.QTableWidget(0, 4)
        self.grid.setHorizontalHeaderLabels(["Log File", "Size", "Duration", "Date"])
        self.grid.horizontalHeader().setStretchLastSection(True)
        self.grid.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.grid.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.grid.itemSelectionChanged.connect(self._show_entry)
        self.parameters = QtWidgets.QTableWidget(0, 2)
        self.parameters.setHorizontalHeaderLabels(["Parameter", "Rate"])
        self.parameters.horizontalHeader().setStretchLastSection(True)
        self.progress = QtWidgets.QProgressBar()
        self.message = QtWidgets.QLabel("Reading the ECU's log files…")
        download = QtWidgets.QPushButton("Download")
        download.clicked.connect(self.download)
        delete = QtWidgets.QPushButton("Delete")
        delete.clicked.connect(self.delete)
        delete_all = QtWidgets.QPushButton("Delete All")
        delete_all.clicked.connect(self.delete_all)
        close = QtWidgets.QPushButton("Close")
        close.clicked.connect(self.accept)
        row = QtWidgets.QHBoxLayout()
        for button in (download, delete, delete_all):
            row.addWidget(button)
        row.addStretch()
        row.addWidget(close)
        column = QtWidgets.QVBoxLayout(self)
        column.addWidget(self.message)
        column.addWidget(self.grid, 2)
        column.addWidget(self.parameters, 1)
        column.addWidget(self.progress)
        column.addLayout(row)
        self.enable_id = None
        self.was_enabled = False
        window.call(self._open, self._listed)

    def _open(self, ecu) -> tuple:
        ids = ecu_log_settings(ecu)
        self.enable_id = ids["mode"]
        self.was_enabled = self.enable_id in ecu.table and ecu.get(self.enable_id) == 1
        import os

        from .protocol import READ_ONLY_ENV

        if not os.environ.get(READ_ONLY_ENV):
            ecu.logs.pause()
        return self._list(ecu)

    @staticmethod
    def _list(ecu) -> tuple:
        info, entries = ecu.logs.list_files()
        for entry in entries:
            entry.header = eculog.parse_header(ecu.logs.read_log_file(entry.name, info.header_size, header_only=True))
        return info, entries

    def _listed(self, result) -> None:
        self.info, self.entries = result
        self.grid.setRowCount(len(self.entries))
        for row, entry in enumerate(self.entries):
            size = f"{round(entry.size / 100) / 10:g} kB" if entry.size < 1000001 else f"{round(entry.size / 100000) / 10:g} MB"
            seconds = entry.header.duration(entry.size, self.info.tick_rate)
            duration = time.strftime("%H:%M:%S", time.gmtime(seconds))
            for column, text in enumerate((entry.name, size, duration, entry.header.date_text())):
                self.grid.setItem(row, column, QtWidgets.QTableWidgetItem(text))
        self.message.setText("" if self.entries else "No log files found in ECU")

    def _show_entry(self) -> None:
        rows = {index.row() for index in self.grid.selectedIndexes()}
        if not rows:
            return
        entry = self.entries[min(rows)]
        self.parameters.setRowCount(len(entry.header.channels))
        for row, (data_id, divider) in enumerate(entry.header.channels):
            name, _units = self.window_.describe(data_id)
            self.parameters.setItem(row, 0, QtWidgets.QTableWidgetItem(name))
            self.parameters.setItem(row, 1, QtWidgets.QTableWidgetItem(f"{hz_of(divider, self.info.tick_rate):g} Hz"))

    def _selected(self) -> list[eculog.EcuLogEntry]:
        return [self.entries[row] for row in sorted({index.row() for index in self.grid.selectedIndexes()})]

    def download(self) -> None:
        chosen = self._selected()
        if not chosen:
            return
        total = sum(entry.size for entry in chosen) or 1
        self.progress.setRange(0, total)
        self.message.setText("Downloading…")
        info = self.info

        def work(ecu):
            files, base = [], 0
            for entry in chosen:
                data = ecu.logs.read_log_file(entry.name, entry.size, lambda sent, at=base: self.window_.report_progress.emit(at + sent))
                files.append((entry.name, data))
                base += entry.size
            return eculog.build_logs(files, info, self.window_.describe)

        self.window_.call(work, self._downloaded)

    def _downloaded(self, logs: list[Log]) -> None:
        self.message.setText(f"Downloaded {len(logs)} log file(s).")
        self.window_.open_downloaded_logs(logs)

    def delete(self) -> None:
        names = [entry.name for entry in self._selected()]
        if names:
            self.window_.call(lambda ecu: self._delete(ecu, names), self._listed)

    def delete_all(self) -> None:
        if QtWidgets.QMessageBox.question(self, "Delete All", "Are you sure you want to delete all log files in the ECU?") == BUTTONS.Yes:
            names = [entry.name for entry in self.entries]
            self.window_.call(lambda ecu: self._delete(ecu, names), self._listed)

    def _delete(self, ecu, names: list[str]) -> tuple:
        for name in names:
            ecu.logs.delete(name)
        return self._list(ecu)

    def done(self, result: int) -> None:
        if self.was_enabled:
            self.window_.call(lambda ecu: ecu.logs.resume(), lambda _r: None)
        super().done(result)


class ScopeWindow(QtWidgets.QWidget):
    """Trigger Scope. Stops ECU logging while open and restores it afterwards, as PCLink does."""

    LOGGING_MODE_ID = 3544

    def __init__(self, window) -> None:
        super().__init__()
        self.setWindowTitle("Trigger Scope")
        self.resize(883, 478)
        self.window_ = window
        self.session = LogSession()
        self.log: Log | None = None
        self.resume_logging = False
        capture = QtWidgets.QPushButton("Capture")
        capture.clicked.connect(self.capture)
        self.save = QtWidgets.QPushButton("Save")
        self.save.setEnabled(False)
        self.save.clicked.connect(self.save_log)
        help_button = QtWidgets.QPushButton("Help")
        help_button.clicked.connect(lambda: window.show_topic("trigger_scope"))
        side = QtWidgets.QVBoxLayout()
        for button in (capture, self.save, help_button):
            side.addWidget(button)
        side.addStretch()
        self.tabs = QtWidgets.QTabWidget()
        self.plots = {}
        for name, ids in scope.TABS.items():
            view = LayoutView("Time Plot", log_parameters=[(data_id, order) for order, data_id in enumerate(ids)])
            plot = TimePlot(self.session, view)
            self.plots[name] = plot
            self.tabs.addTab(plot, name)
        row = QtWidgets.QHBoxLayout(self)
        row.addLayout(side)
        row.addWidget(self.tabs, 1)
        window.call(self._pause_logging, lambda _r: None)

    def _pause_logging(self, ecu) -> None:
        if self.LOGGING_MODE_ID in ecu.table and ecu.get(self.LOGGING_MODE_ID) == 1:
            ecu.set(self.LOGGING_MODE_ID, 0)
            self.resume_logging = True

    def capture(self) -> None:
        self.save.setEnabled(True)
        self.window_.call(scope.capture, self._captured)

    def _captured(self, log: Log) -> None:
        self.log = log
        self.session.logs = []
        self.session.add(log)

    def save_log(self) -> None:
        if self.log is None:
            return
        folder = self.window_.options.folder("log")
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save Trigger Scope", str(Path(folder) / "Trigger_Scope.llg5"), "Link Log Files (*.llg5 *.llgx)")
        if path:
            save_link_log(self.log, Path(path))

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        if self.resume_logging:
            self.window_.call(lambda ecu: ecu.set(self.LOGGING_MODE_ID, 1), lambda _r: None)
        super().closeEvent(event)


TPS_CAL_STATUS = 1663
THROTTLE = {
    0: {"mode": 1692, "state": 1670, "sources": (734, 735), "title": "E-Throttle 1"},
    1: {"mode": 901, "state": 918, "sources": (919, 920), "title": "E-Throttle 2"},
}
APS_OPEN, APS_CLOSED = 1691, 1690
APS_SOURCES = (736, 737)
APS_MAIN, APS_SUB = 1650, 1654
ETHROTTLE_SETUP_MODE = 2


class CalibrationWindow(QtWidgets.QDialog):
    """E-throttle and pedal calibration. The ECU does the work; these only start and follow it."""

    def __init__(self, window, title: str) -> None:
        super().__init__(window)
        self.setWindowTitle(title)
        self.window_ = window
        self.label = QtWidgets.QLabel("")
        self.label.setWordWrap(True)
        self.button = QtWidgets.QPushButton("Close")
        self.button.clicked.connect(self.next_step)
        column = QtWidgets.QVBoxLayout(self)
        column.addWidget(self.label)
        column.addWidget(self.button)
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.refresh)

    def _write(self, *pairs: tuple[int, float]) -> None:
        def work(ecu):
            for data_id, value in pairs:
                if data_id in ecu.table:
                    ecu.set(data_id, value)

        self.window_.call(work, lambda _r: None)

    def refresh(self) -> None:
        pass

    def next_step(self) -> None:
        self.accept()

    def done(self, result: int) -> None:
        self.timer.stop()
        self._write((APS_OPEN, 0), (APS_CLOSED, 0), (THROTTLE[0]["state"], 0), (THROTTLE[1]["state"], 0))
        super().done(result)


class ThrottleCalibration(CalibrationWindow):
    def __init__(self, window, throttle: int) -> None:
        super().__init__(window, "Calibration")
        self.throttle = throttle
        self._write((THROTTLE[throttle]["state"], 1))
        self.timer.start()

    def refresh(self) -> None:
        reading = self.window_.reading(TPS_CAL_STATUS)
        self.label.setText(reading.text if reading else "")


class PedalCalibration(CalibrationWindow):
    STEPS = (("Press pedal down", "Next"), ("Release pedal", "Next"), ("APS cal complete", "Done"))

    def __init__(self, window) -> None:
        super().__init__(window, "Calibration")
        self.step = 0
        self._show()
        self._write((APS_OPEN, 1))

    def _show(self) -> None:
        text, button = self.STEPS[self.step]
        self.label.setText(text)
        self.button.setText(button)

    def next_step(self) -> None:
        if self.step == 0:
            self._write((APS_OPEN, 0), (APS_CLOSED, 1))
        elif self.step == 1:
            self._write((APS_CLOSED, 0))
        else:
            self.accept()
            return
        self.step += 1
        self._show()


CRANK_TIMING, LOCK_CRANK, REF_TIMING, LOCK_REF, TRIGGER_OFFSET, IGNITION_DELAY, TRIGGER_MODE = 3540, 3541, 3538, 3537, 3526, 1466, 3501


class BaseTimingDialog(QtWidgets.QDialog):
    """Set Base Timing: locks the timing while it is checked with a light, then undoes the lock."""

    SAVED = (TRIGGER_OFFSET, REF_TIMING, CRANK_TIMING, IGNITION_DELAY)

    def __init__(self, window) -> None:
        super().__init__(window)
        self.setWindowTitle("Set Base Timing")
        self.window_ = window
        ecu = window.worker.ecu
        self.saved = {data_id: ecu.get(data_id) for data_id in self.SAVED if data_id in ecu.table}
        self.mode = ecu.get(TRIGGER_MODE) if TRIGGER_MODE in ecu.table else 1
        form = QtWidgets.QFormLayout(self)
        form.addRow(QtWidgets.QLabel("Press 'Enter' after changing a value for it to take effect."))
        rows = [(CRANK_TIMING, "Crank Timing"), (None, "2) Adjust CAS until timing matches the number above.")] if self.mode == 0 else [
            (None, "1) Lock Ignition Timing to:"), (REF_TIMING, "Ref. Timing"),
            (None, "2) Adjust Offset until Ignition Timing matches the number above:"), (TRIGGER_OFFSET, "Trigger Offset"),
        ]
        rows += [(None, "3) Adjust Delay so Ignition Timing is constant over entire RPM range:"), (IGNITION_DELAY, "Ignition Delay")]
        self.spins = {}
        for data_id, title in rows:
            if data_id is None:
                form.addRow(QtWidgets.QLabel(title))
            elif data_id in ecu.table:
                spin = self._spin(data_id)
                self.spins[data_id] = spin
                form.addRow(title, spin)
        box = QtWidgets.QDialogButtonBox()
        box.addButton("Done", QtWidgets.QDialogButtonBox.ButtonRole.AcceptRole)
        box.addButton(BUTTONS.Cancel)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        form.addRow(box)
        self.lock = LOCK_CRANK if self.mode == 0 else LOCK_REF
        self.window_.write(self.lock, 1, 0)

    def _spin(self, data_id: int) -> QtWidgets.QDoubleSpinBox:
        parameter = self.window_.database.parameter(data_id)
        spin = QtWidgets.QDoubleSpinBox()
        spin.setDecimals(parameter.decimals if parameter.decimals is not None else 2)
        spin.setRange(parameter.minimum if parameter.minimum is not None else -1e6, parameter.maximum if parameter.maximum is not None else 1e6)
        spin.setValue(self.window_.worker.ecu.get(data_id))
        spin.setKeyboardTracking(False)
        spin.editingFinished.connect(lambda: self.window_.write(data_id, spin.value(), 0))
        return spin

    def done(self, result: int) -> None:
        if result != QtWidgets.QDialog.DialogCode.Accepted:
            for data_id, value in self.saved.items():
                self.window_.write(data_id, value, 0)
        for data_id in (LOCK_REF, LOCK_CRANK):
            if data_id in self.window_.worker.ecu.table:
                self.window_.write(data_id, 0, 0)
        super().done(result)


class FeatureUnlockDialog(QtWidgets.QDialog):
    def __init__(self, serial: int, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("ECU Unlock")
        form = QtWidgets.QFormLayout(self)
        form.addRow("Serial Number", QtWidgets.QLabel(str(serial)))
        self.code = QtWidgets.QLineEdit()
        self.code.setMaxLength(32)
        form.addRow("Unlock Code", self.code)
        form.addRow(ok_cancel(self))


class FirmwareWorker(QtCore.QThread):
    progress = QtCore.Signal(int, int)
    finished_with = QtCore.Signal(object)

    def __init__(self, uri: str, images: list[firmware.FirmwareImage]) -> None:
        super().__init__()
        self.uri, self.images = uri, images

    def run(self) -> None:
        from . import protocol as p

        transport = open_transport(self.uri)
        error = None
        try:
            link = p.Link(transport)
            for image in self.images:
                firmware.program_image(link, image, None, self.progress.emit)
        except (firmware.FirmwareError, p.ProtocolError, p.ForbiddenCommand, OSError) as exc:
            error = exc
        finally:
            transport.close()
        self.finished_with.emit(error)


class FirmwareDialog(QtWidgets.QDialog):
    """Update Firmware. Checks everything before the ECU is touched and refuses anything doubtful."""

    def __init__(self, window, uri: str, current: tuple[int, int, int] | None, product: int) -> None:
        super().__init__(window)
        self.setWindowTitle("Update Firmware")
        self.resize(560, 420)
        self.window_, self.uri, self.current, self.product = window, uri, current, product
        self.package: firmware.FirmwarePackage | None = None
        self.notes_read = False
        self.files = QtWidgets.QListWidget()
        self.files.currentRowChanged.connect(self._selected)
        browse = QtWidgets.QPushButton("Browse…")
        browse.clicked.connect(self.browse)
        self.message = QtWidgets.QLabel("")
        self.message.setWordWrap(True)
        self.progress = QtWidgets.QProgressBar()
        self.start = QtWidgets.QPushButton("Read Notes First")
        self.start.setEnabled(False)
        self.start.clicked.connect(self.clicked)
        close = QtWidgets.QPushButton("Close")
        close.clicked.connect(self.reject)
        column = QtWidgets.QVBoxLayout(self)
        column.addWidget(QtWidgets.QLabel(f"ECU version: {'.'.join(map(str, current)) if current else 'not available'}"))
        column.addWidget(self.files)
        column.addWidget(browse)
        column.addWidget(self.message)
        column.addWidget(self.progress)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.start)
        row.addWidget(close)
        column.addLayout(row)
        from .assets import asset

        folder = asset("Firmware")
        for path in sorted(folder.glob("*.lfp")) if folder else []:
            self._add_file(path)

    def _add_file(self, path: Path) -> None:
        item = QtWidgets.QListWidgetItem(path.name)
        item.setData(ID_ROLE, str(path))
        self.files.addItem(item)
        self.files.setCurrentItem(item)

    def browse(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Firmware File", "", "Firmware File (*.img *.lfp)")
        if path:
            self._add_file(Path(path))

    def images(self) -> list[firmware.FirmwareImage]:
        if self.package is None:
            return []
        if self.package.path and self.package.path.suffix.lower() == ".img":
            return list(self.package.images.values())
        return self.package.images_for(self.product)

    def _selected(self, row: int) -> None:
        self.package = None
        self.start.setEnabled(False)
        if row < 0:
            return
        try:
            self.package = firmware.load(Path(self.files.item(row).data(ID_ROLE)), self.window_.database.firmware_key)
        except (firmware.FirmwareError, OSError) as exc:
            self.message.setText(f"This file cannot be used: {exc}")
            return
        images = self.images()
        if not images:
            self.message.setText("No Firmware images in this package for the connected device.")
            return
        main = next((image for image in images if not image.is_comms), images[-1])
        verdict = firmware.verdict(main.version, self.current)
        text = f"{verdict}: {'.'.join(map(str, main.version))}"
        if verdict == "Incompatible":
            self.message.setText(f"{text}. This firmware does not fit this ECU.")
            return
        if self.current and firmware.needs_base_timing_check(self.current, main.version):
            text += "\nThe base timing must be verified after this firmware change!"
        self.message.setText(text)
        self.verdict = verdict
        self.start.setText(verdict if self.notes_read else "Read Notes First")
        self.start.setEnabled(True)

    def clicked(self) -> None:
        if not self.notes_read:
            self.notes_read = True
            self.window_.show_topic("ecu_firmware_version_information")
            self.start.setText(self.verdict)
            return
        warnings = (
            "Have you read the firmware version notes?",
            "This will overwrite the firmware in the ECU, though tuning information will be retained. "
            "Save a copy of your tune first. The ignition must stay on and the engine stopped until it finishes. Continue?",
        )
        for text in warnings:
            if QtWidgets.QMessageBox.warning(self, "Update Firmware", text, BUTTONS.Yes | BUTTONS.No) != BUTTONS.Yes:
                return
        self.start.setEnabled(False)
        self.files.setEnabled(False)
        self.message.setText("PROGRAMMING HAS STARTED......please wait.")
        self.worker = FirmwareWorker(self.uri, self.images())
        self.worker.progress.connect(self._progress)
        self.worker.finished_with.connect(self._finished)
        self.worker.start()

    def _progress(self, sent: int, total: int) -> None:
        self.progress.setRange(0, total)
        self.progress.setValue(sent)

    def _finished(self, error) -> None:
        self.files.setEnabled(True)
        if error is None:
            self.message.setText("PROGRAMMING COMPLETE. If the ECU reports Safe Mode, perform a Store and cycle its power.")
        else:
            self.message.setText(str(error) if isinstance(error, firmware.EngineRunning) else f"{firmware.FAILED_TEXT}\n\n{error}")


class LogFileManager(QtWidgets.QWidget):
    """Open logs and their sections, with the same actions as the Logging menu."""

    def __init__(self, window) -> None:
        super().__init__()
        self.window_ = window
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.itemClicked.connect(self._clicked)
        row = QtWidgets.QHBoxLayout()
        for title, action in (("Open", lambda: window.open_log()), ("Save", lambda: window.save_log_file()), ("Close", lambda: window.close_log_file()), ("Details", lambda: window.log_details())):
            button = QtWidgets.QPushButton(title)
            button.clicked.connect(lambda _checked=False, run=action: run())
            row.addWidget(button)
        column = QtWidgets.QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.addLayout(row)
        column.addWidget(self.tree)
        window.log_session.changed.connect(self.refresh)

    def refresh(self) -> None:
        session = self.window_.log_session
        self.tree.clear()
        for index, log in enumerate(session.logs):
            name = log.details.get("Name") or (log.path.name if log.path else "Unsaved log")
            item = QtWidgets.QTreeWidgetItem(self.tree, [("* " if log.modified else "") + name])
            item.setData(0, ID_ROLE, (index, None))
            font = item.font(0)
            font.setBold(index == session.active)
            item.setFont(0, font)
            for section in log.sections():
                child = QtWidgets.QTreeWidgetItem(item, [f"{section.name}  ({section.end - section.start:.1f} s)"])
                child.setData(0, ID_ROLE, (index, section))
            item.setExpanded(True)

    def _clicked(self, item: QtWidgets.QTreeWidgetItem, _column: int) -> None:
        index, section = item.data(0, ID_ROLE)
        session = self.window_.log_session
        if index != session.active:
            session.select(index)
        elif section:
            session.set_range(section.start, section.end)


class EventLogView(QtWidgets.QWidget):
    """Messages since starting, with the buttons PCLink puts here."""

    def __init__(self, clear_faults=None) -> None:
        super().__init__()
        self.list = QtWidgets.QListWidget()
        clear = QtWidgets.QPushButton("Clr Event Log")
        clear.clicked.connect(self.list.clear)
        self.clear_faults = QtWidgets.QPushButton("Clr ECU Fault Codes")
        row = QtWidgets.QHBoxLayout()
        row.addWidget(clear)
        row.addWidget(self.clear_faults)
        column = QtWidgets.QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.addLayout(row)
        column.addWidget(self.list)

    def add(self, text: str, important: bool = False) -> None:
        item = QtWidgets.QListWidgetItem(f"{time.strftime('%H:%M:%S')}  {text}")
        if important:
            item.setForeground(QtGui.QColor(220, 40, 40))
        self.list.addItem(item)
        self.list.scrollToBottom()


class QuickTuneWindow(QtWidgets.QWidget):
    """Quick Tune: corrects fuel table cells from the wideband while the engine holds a cell."""

    WIDEBAND_IDS = (3040, 3041, tune.LAMBDA_AVERAGE)

    def __init__(self, window) -> None:
        super().__init__(window, QtCore.Qt.WindowType.Tool | QtCore.Qt.WindowType.WindowStaysOnTopHint)
        self.window_ = window
        self.engine = tune.QuickTune()
        self.setWindowTitle("Quick Tune")
        self.status = QtWidgets.QLabel("OFF")
        font = self.status.font()
        font.setBold(True)
        font.setPointSize(font.pointSize() + 6)
        self.status.setFont(font)
        self.status.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.reading = QtWidgets.QLabel("")
        self.mode = QtWidgets.QButtonGroup(self)
        modes = QtWidgets.QHBoxLayout()
        for index, title in enumerate(("Off", "Manual", "Auto")):
            button = QtWidgets.QRadioButton(title)
            self.mode.addButton(button, index)
            modes.addWidget(button)
        self.mode.button(0).setChecked(True)
        self.mode.idClicked.connect(self._mode_changed)
        self.tune_button = QtWidgets.QPushButton("Tune Cell (F10)")
        self.tune_button.setEnabled(False)
        self.tune_button.clicked.connect(self.press_tune)
        reset = QtWidgets.QPushButton("Reset Cell (F11)")
        reset.clicked.connect(self.reset_cell)
        self.show_settings = QtWidgets.QCheckBox("Show Settings")
        self.settings_box = self._settings_panel()
        self.settings_box.hide()
        self.show_settings.toggled.connect(self.settings_box.setVisible)
        row = QtWidgets.QHBoxLayout()
        row.addLayout(modes)
        row.addWidget(self.show_settings)
        buttons = QtWidgets.QHBoxLayout()
        buttons.addWidget(self.tune_button)
        buttons.addWidget(reset)
        column = QtWidgets.QVBoxLayout(self)
        column.addWidget(self.status)
        column.addWidget(self.reading)
        column.addLayout(row)
        column.addLayout(buttons)
        column.addWidget(self.settings_box)
        self.last = None

    def _settings_panel(self) -> QtWidgets.QWidget:
        s = self.engine.settings
        box = QtWidgets.QGroupBox("Settings")
        form = QtWidgets.QFormLayout(box)
        self.table_choice = QtWidgets.QComboBox()
        self.table_choice.addItems(["Fuel Table 1", "Fuel Table 2"])
        form.addRow("Fuel Table", self.table_choice)
        self.source_choice = QtWidgets.QComboBox()
        self.source_choice.addItems(["Lambda 1", "Lambda 2", "Lambda Average"])
        form.addRow("Measured WB Oxy Source", self.source_choice)
        self.ahead_choice = QtWidgets.QComboBox()
        self.ahead_choice.addItems(tune.TUNE_AHEAD_OPTIONS)
        self.ahead_choice.currentIndexChanged.connect(lambda index: setattr(s, "tune_ahead", index))
        form.addRow("Tune Ahead", self.ahead_choice)
        for name, title, low, high, step in (
            ("filter", "Filter", 1, 100, 1), ("precision", "Precision", 0, 10, 0.1), ("time_in_centre", "Time In Cell Centre", 0, 5, 0.1),
            ("max_attempts", "Max Auto Attempts", 1, 10, 1), ("load_tolerance", "Load Centre Tolerance", 1, 50, 1),
            ("rpm_tolerance", "RPM Centre Tolerance", 1, 50, 1), ("rpm_lower", "RPM Lockout Lower", 0, 15000, 100),
            ("rpm_upper", "RPM Lockout Upper", 0, 15000, 100), ("load_lower", "Load Lockout Lower", -100, 600, 10),
            ("load_upper", "Load Lockout Upper", -100, 600, 10), ("ect_lockout", "ECT Lockout", 0, 300, 1),
            ("gauge_minimum", "Gauge Minimum", 0, 30, 0.1), ("gauge_maximum", "Gauge Maximum", 0, 30, 0.1),
            ("tune_ahead_trim", "Tune Ahead Trim", -20, 20, 0.1),
        ):
            spin = QtWidgets.QDoubleSpinBox()
            spin.setRange(low, high)
            spin.setSingleStep(step)
            spin.setDecimals(2)
            spin.setValue(getattr(s, name))
            integer = isinstance(getattr(s, name), int)
            spin.valueChanged.connect(lambda value, key=name, whole=integer: setattr(s, key, int(value) if whole else value))
            form.addRow(title, spin)
        return box

    def table_id(self) -> int:
        return tune.FUEL_TABLE_2 if self.table_choice.currentIndex() == 1 else tune.FUEL_TABLE_1

    def wideband_id(self) -> int:
        return self.WIDEBAND_IDS[self.source_choice.currentIndex()]

    def _mode_changed(self, mode: int) -> None:
        self.engine.mode = mode
        self.engine.running = False
        self.tune_button.setCheckable(mode == tune.AUTOMATIC)
        self.tune_button.setChecked(False)
        self.tune_button.setText("Start Tune (F10)" if mode == tune.AUTOMATIC else "Tune Cell (F10)")

    def press_tune(self) -> None:
        if self.engine.mode == tune.AUTOMATIC:
            self.engine.running = self.tune_button.isChecked()
            self.tune_button.setText("Stop Tune (F10)" if self.engine.running else "Start Tune (F10)")
            if self.engine.running:
                self.window_.marks.attempts.clear()
            return
        status = self.last
        if status is None or not status.ready:
            QtWidgets.QMessageBox.information(self, "Information", "Cell not ready to tune.")
            self.mode.button(tune.MANUAL).setChecked(True)
            return
        self.tune_step()

    def reset_cell(self) -> None:
        self.engine.reset_cell()
        cell = self.engine.cell
        if cell:
            key = (self.table_id(), *cell)
            self.window_.marks.attempts.pop(key, None)
            self.window_.marks.set_flag(self.table_id(), *cell, tune.TUNED, False)

    def inputs(self) -> tune.TuneInputs | None:
        window = self.window_
        ecu = window.worker.ecu
        table_id = self.table_id()
        if ecu is None:
            return None
        has_table = table_id in ecu.table
        parameter = window.database.parameter(table_id) if window.database.known(table_id) else None
        cell = rpm_cell = load_cell = None
        load = 0.0
        if has_table and parameter is not None:
            columns = parameter.columns if parameter.columns > 1 else ecu.table[table_id].count
            rows = max(ecu.table[table_id].count // max(columns, 1), 1)
            cell = window.table_position(parameter, rows, columns)
            if cell:
                if parameter.column_axis_id in ecu.table:
                    rpm_cell = tune.cell_bounds(_used(ecu.get_all(parameter.column_axis_id)), cell[1])
                if rows > 1 and parameter.row_axis_id in ecu.table:
                    load_cell = tune.cell_bounds(_used(ecu.get_all(parameter.row_axis_id)), cell[0])
                if parameter.row_source_id in ecu.table:
                    source = int(ecu.get(parameter.row_source_id))
                    load = ecu.get(source) if source in ecu.table else 0.0
        active_id = {tune.FUEL_TABLE_1: tune.ACTIVE_FUEL_1, tune.FUEL_TABLE_2: tune.ACTIVE_FUEL_2}.get(table_id)
        active = active_id not in ecu.table or ecu.get(active_id) == 1
        wideband = ecu.get(self.wideband_id()) if self.wideband_id() in ecu.table else None
        target = ecu.get(tune.LAMBDA_TARGET_RUNTIME) if tune.LAMBDA_TARGET_RUNTIME in ecu.table else None
        return tune.TuneInputs(
            wideband, target, has_table, active, ecu.get(tune.ENGINE_SPEED) if tune.ENGINE_SPEED in ecu.table else 0.0, load,
            ecu.get(tune.ECT) if tune.ECT in ecu.table else 0.0, rpm_cell, load_cell, cell, time.monotonic(),
        )

    def refresh(self) -> None:
        inputs = self.inputs()
        if inputs is None:
            return
        marks = self.window_.marks
        table_id = self.table_id()
        attempts = marks.attempts.get((table_id, *inputs.cell), 0) if inputs.cell else 0
        tuned = bool(inputs.cell and marks.flag(table_id, *inputs.cell) & tune.TUNED)
        status = self.engine.update(inputs, attempts, tuned)
        self.last = status
        self.status.setText(status.text)
        self.status.setToolTip(status.hint)
        self.status.setStyleSheet(f"color:{tcolor(status.colour).name()}")
        self.reading.setText(f"Target {self.engine.target:.3f}   Measured {self.engine.measured:.3f}")
        if self.engine.mode == tune.OFF:
            self.tune_button.setEnabled(False)
        else:
            self.tune_button.setEnabled(self.engine.mode == tune.AUTOMATIC or status.ready)
        if inputs.cell and status.may_tune and abs(self.engine.target - self.engine.measured) < self.engine.settings.precision and self.engine.mode == tune.AUTOMATIC:
            marks.set_flag(table_id, *inputs.cell, tune.TUNED)
        if self.engine.mode == tune.AUTOMATIC and self.engine.running and status.ready:
            self.tune_step()

    def tune_step(self) -> None:
        cell, measured, target = self.engine.cell, self.engine.measured, self.engine.target
        if cell is None or target <= 0 or measured <= 0:
            QtWidgets.QMessageBox.information(self, "Information", "Unable to perform tune operation.")
            self.mode.button(tune.MANUAL).setChecked(True)
            self._mode_changed(tune.MANUAL)
            return
        window, table_id = self.window_, self.table_id()
        ecu = window.worker.ecu
        parameter = window.database.parameter(table_id)
        columns = parameter.columns if parameter.columns > 1 else ecu.table[table_id].count
        rows = max(ecu.table[table_id].count // max(columns, 1), 1)
        row, column = cell
        dual = ecu.get(tune.DUAL_FUEL_MODE) if tune.DUAL_FUEL_MODE in ecu.table else 0.0
        old = ecu.get(table_id, row * columns + column)
        new = tune.tune_value(old, measured, target, tune.is_additive(table_id, dual))
        window.write(table_id, new, row * columns + column)
        marks = window.marks
        marks.set_flag(table_id, row, column, tune.PART_TUNED)
        key = (table_id, row, column)
        marks.attempts[key] = marks.attempts.get(key, 0) + 1
        for r, c, value in self.engine.tune_ahead(row, column, new, rows, columns, lambda r, c: marks.flag(table_id, r, c) & tune.TUNED):
            window.write(table_id, value, r * columns + c)
        self.engine.started = None
        window.statusBar().showMessage(f"Tuned cell {row + 1},{column + 1}: {old:g} to {new:g}", 4000)


def _used(axis: list[float]) -> list[float]:
    from .views import used_axis_length

    return axis[: used_axis_length(axis)]


class QuickTrimDialog(QtWidgets.QDialog):
    """Quick Trim: corrects the selected cells once, from a target, a reading and any closed loop trim."""

    def __init__(self, target: float, measured: float, correction: float, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Quick Trim")
        form = QtWidgets.QFormLayout(self)
        self.target = self._spin(target, 0.1, 30)
        self.measured = self._spin(measured, 0.1, 30)
        self.correction = self._spin(correction, -50, 50)
        form.addRow("Target Lambda", self.target)
        form.addRow("Measured Lambda", self.measured)
        form.addRow("CL Lambda Fuel Corr.", self.correction)
        form.addRow(ok_cancel(self))

    @staticmethod
    def _spin(value: float, low: float, high: float) -> QtWidgets.QDoubleSpinBox:
        spin = QtWidgets.QDoubleSpinBox()
        spin.setRange(low, high)
        spin.setDecimals(3)
        spin.setValue(value)
        return spin


class AxisSetupDialog(QtWidgets.QDialog):
    """Table Axis Setup: edit, insert and remove breakpoints, optionally reshaping the table."""

    def __init__(self, x_axis: list[float], y_axis: list[float], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Table Axis Setup")
        self.x_grid = self._grid(x_axis)
        self.y_grid = self._grid(y_axis)
        self.interpolate = QtWidgets.QCheckBox("Interpolate table values for altered or new rows and columns")
        form = QtWidgets.QFormLayout(self)
        form.addRow("X Axis", self.x_grid)
        form.addRow("Y Axis", self.y_grid)
        form.addRow(self.interpolate)
        form.addRow(ok_cancel(self, self._check))

    @staticmethod
    def _grid(values: list[float]) -> QtWidgets.QTableWidget:
        grid = QtWidgets.QTableWidget(1, len(values))
        grid.setFixedHeight(64)
        for column, value in enumerate(values):
            grid.setItem(0, column, QtWidgets.QTableWidgetItem(f"{value:g}"))
        return grid

    @staticmethod
    def _values(grid: QtWidgets.QTableWidget) -> list[float]:
        return [float(grid.item(0, column).text()) for column in range(grid.columnCount())]

    def axes(self) -> tuple[list[float], list[float]]:
        return self._values(self.x_grid), self._values(self.y_grid)

    def _check(self) -> None:
        try:
            for name, values in zip("XY", self.axes()):
                if any(b <= a for a, b in zip(values, values[1:])):
                    QtWidgets.QMessageBox.warning(self, "Table Axis Setup", f"{name} Axis values must rise from left to right.")
                    return
        except ValueError:
            QtWidgets.QMessageBox.warning(self, "Table Axis Setup", "The axes must hold numbers.")
            return
        self.accept()


class ItemPicker(QtWidgets.QDialog):
    """Select Item: a searchable list for settings with very many options."""

    def __init__(self, items: list[tuple[int, str]], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Select Item")
        self.resize(320, 480)
        self.items = items
        self.alphabetical = QtWidgets.QCheckBox("Alphabetical")
        self.list = QtWidgets.QListWidget()
        self.alphabetical.toggled.connect(self._fill)
        self.list.itemDoubleClicked.connect(lambda _item: self.accept())
        column = QtWidgets.QVBoxLayout(self)
        column.addWidget(self.alphabetical)
        column.addWidget(self.list)
        column.addWidget(ok_cancel(self))
        self._fill()

    def _fill(self) -> None:
        self.list.clear()
        shown = sorted(self.items, key=lambda pair: pair[1]) if self.alphabetical.isChecked() else self.items
        for value, text in shown:
            if text == "NA":
                continue
            item = QtWidgets.QListWidgetItem(text)
            item.setData(ID_ROLE, value)
            self.list.addItem(item)

    def value(self) -> int | None:
        item = self.list.currentItem()
        return item.data(ID_ROLE) if item else None


class CanSetupDialog(QtWidgets.QDialog):
    """CAN Setup: Mode, Streams, Test Calculator, CAN Devices and Custom CAN File."""

    def __init__(self, window) -> None:
        super().__init__(window)
        self.setWindowTitle("CAN Setup")
        self.resize(820, 560)
        self.window_ = window
        ecu = window.worker.ecu
        self.streams = can.read_streams(ecu)
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(self._mode_tab(ecu), "Mode")
        self.tabs.addTab(self._streams_tab(), "Streams")
        self.tabs.addTab(self._calculator_tab(), "Test Calculator")
        self.tabs.addTab(self._devices_tab(), "CAN Devices")
        self.tabs.addTab(self._file_tab(), "Custom CAN File")
        box = QtWidgets.QDialogButtonBox(BUTTONS.Ok | BUTTONS.Apply | BUTTONS.Close)
        box.accepted.connect(self._ok)
        box.rejected.connect(self.reject)
        box.button(BUTTONS.Apply).clicked.connect(self.apply)
        column = QtWidgets.QVBoxLayout(self)
        column.addWidget(self.tabs)
        column.addWidget(box)

    def _mode_tab(self, ecu) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        column = QtWidgets.QVBoxLayout(page)
        self.module = QtWidgets.QComboBox()
        self.module.addItems(["CAN 1", "CAN 2"])
        self.module.currentIndexChanged.connect(self._load_channels)
        self.bit_rate = QtWidgets.QComboBox()
        self.bit_rate.addItems(can.BIT_RATES)
        self.channels = QtWidgets.QTableWidget(can.CHANNELS, 4)
        self.channels.setHorizontalHeaderLabels(["Channel Mode", "Rate (ms)", "CAN ID", "Extended"])
        self.channels.horizontalHeader().setStretchLastSection(True)
        form = QtWidgets.QFormLayout()
        form.addRow("CAN Module", self.module)
        form.addRow("Bit Rate", self.bit_rate)
        column.addLayout(form)
        column.addWidget(self.channels)
        self.edited: dict[int, list[can.CanChannel]] = {}
        self._loaded_module = 0
        if can.CHANNEL_MODE_BASE[0] in ecu.table:
            self._fill_channels(0)
        else:
            page.setEnabled(False)
        return page

    def _channels_of(self, module: int) -> list[can.CanChannel]:
        if module not in self.edited:
            self.edited[module] = can.read_channels(self.window_.worker.ecu, module)
        return self.edited[module]

    def _fill_channels(self, module: int) -> None:
        ecu = self.window_.worker.ecu
        self._loaded_module = module
        if can.BIT_RATE_IDS[module] in ecu.table:
            self.bit_rate.setCurrentIndex(int(ecu.get(can.BIT_RATE_IDS[module])))
        for row, channel in enumerate(self._channels_of(module)):
            for column, text in enumerate((str(channel.mode), str(channel.rate), f"{channel.can_id:X}", "Yes" if channel.extended else "No")):
                self.channels.setItem(row, column, QtWidgets.QTableWidgetItem(text))

    def _read_grid(self) -> None:
        channels = self._channels_of(self._loaded_module)
        for row, channel in enumerate(channels):
            channel.mode = int(self.channels.item(row, 0).text())
            channel.rate = int(self.channels.item(row, 1).text())
            channel.can_id = int(self.channels.item(row, 2).text(), 16)
            channel.extended = self.channels.item(row, 3).text().lower().startswith("y")

    def _load_channels(self, module: int) -> None:
        self._read_grid()
        self._fill_channels(module)

    def _streams_tab(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        column = QtWidgets.QVBoxLayout(page)
        self.stream_choice = QtWidgets.QComboBox()
        self.stream_choice.addItems([f"Stream {i + 1}" for i in range(can.STREAMS)])
        self.stream_choice.currentIndexChanged.connect(self._show_stream)
        load = QtWidgets.QPushButton("Load Stream…")
        load.clicked.connect(self.load_stream)
        save = QtWidgets.QPushButton("Save Stream…")
        save.clicked.connect(self.save_stream)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.stream_choice)
        row.addWidget(load)
        row.addWidget(save)
        row.addStretch()
        self.stream_table = QtWidgets.QTableWidget(0, 9)
        self.stream_table.setHorizontalHeaderLabels(["Frame", "Parameter", "Start", "Width", "Byte Order", "Type", "Multiplier", "Divider", "Offset"])
        self.stream_table.horizontalHeader().setStretchLastSection(True)
        column.addLayout(row)
        column.addWidget(self.stream_table)
        self._show_stream(0)
        return page

    def _show_stream(self, index: int) -> None:
        rows = [(f, item) for f, frame in enumerate(self.streams[index].frames) for item in frame.parameters]
        self.stream_table.setRowCount(len(rows))
        for row, (f, item) in enumerate(rows):
            name, _units = self.window_.describe(item.data_id)
            cells = (f + 1, name, item.start, item.width, "LS First" if item.byte_order else "MS First", "Signed" if item.int_type else "Unsigned", item.multiplier, item.divider, item.offset)
            for column, value in enumerate(cells):
                self.stream_table.setItem(row, column, QtWidgets.QTableWidgetItem(str(value)))

    def load_stream(self) -> None:
        from .assets import asset

        folder = asset("CAN")
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Load CAN Stream", str(folder or ""), "CAN Stream Files (*.lcs)")
        if not path:
            return
        try:
            stream = can.load_stream_file(Path(path), self.window_.database.stream_key)
        except (can.CanFileError, OSError) as exc:
            QtWidgets.QMessageBox.warning(self, "CAN Setup", str(exc))
            return
        self.streams[self.stream_choice.currentIndex()] = stream
        self._show_stream(self.stream_choice.currentIndex())

    def save_stream(self) -> None:
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save CAN Stream", "stream.lcs", "CAN Stream Files (*.lcs)")
        if path:
            can.save_stream_file(self.streams[self.stream_choice.currentIndex()], Path(path), self.window_.database.stream_key)

    def _calculator_tab(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(page)
        self.bus_value = QtWidgets.QDoubleSpinBox()
        self.multiplier = QtWidgets.QDoubleSpinBox()
        self.divider = QtWidgets.QDoubleSpinBox()
        self.offset = QtWidgets.QDoubleSpinBox()
        for spin, value in ((self.bus_value, 0), (self.multiplier, 1), (self.divider, 1), (self.offset, 0)):
            spin.setRange(-1e7, 1e7)
            spin.setValue(value)
            spin.valueChanged.connect(self._calculate)
            form.addRow({id(self.bus_value): "Bus Value", id(self.multiplier): "Multiplier", id(self.divider): "Divider", id(self.offset): "Offset"}[id(spin)], spin)
        self.display_result = QtWidgets.QLabel("0")
        form.addRow("Display Value", self.display_result)
        return page

    def _calculate(self) -> None:
        divider = self.divider.value() or 1
        self.display_result.setText(f"{self.bus_value.value() * self.multiplier.value() / divider + self.offset.value():g}")

    def _devices_tab(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        column = QtWidgets.QVBoxLayout(page)
        column.addWidget(QtWidgets.QLabel("Searching sends frames onto the vehicle's CAN bus."))
        find = QtWidgets.QPushButton("Find Link CAN Devices")
        find.clicked.connect(self.find_devices)
        self.devices = QtWidgets.QTableWidget(0, 3)
        self.devices.setHorizontalHeaderLabels(["Device ID", "Device Code", "Serial"])
        self.devices.horizontalHeader().setStretchLastSection(True)
        column.addWidget(find)
        column.addWidget(self.devices)
        return page

    def find_devices(self) -> None:
        if not isinstance(self.window_.worker.ecu, Ecu):
            return
        if QtWidgets.QMessageBox.question(self, "CAN Devices", "This sends frames onto the vehicle's CAN bus. Continue?") != BUTTONS.Yes:
            return
        module = self.module.currentIndex()

        def work(ecu):
            bridge = can.CanBridge(ecu.link, module)
            bridge.start()
            try:
                return bridge.find_link_devices()
            finally:
                bridge.stop()

        self.window_.call(work, self._found)

    def _found(self, devices) -> None:
        self.devices.setRowCount(len(devices))
        for row, (device_id, code, serial) in enumerate(devices):
            for column, value in enumerate((f"{device_id:X}", code, serial)):
                self.devices.setItem(row, column, QtWidgets.QTableWidgetItem(str(value)))

    def _file_tab(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        column = QtWidgets.QVBoxLayout(page)
        load = QtWidgets.QPushButton("Load File into ECU…")
        load.clicked.connect(self.load_can_file)
        save = QtWidgets.QPushButton("Save File from ECU…")
        save.clicked.connect(self.save_can_file)
        column.addWidget(load)
        column.addWidget(save)
        column.addStretch()
        return page

    def load_can_file(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Load CAN File", "", "Link Vehicle CAN files (*.lcf)")
        if not path:
            return
        window = self.window_
        try:
            content, plain = can.load_can_file(Path(path), window.database.file_key)
        except (can.CanFileError, ValueError, OSError) as exc:
            QtWidgets.QMessageBox.warning(self, "CAN Setup", str(exc))
            return
        if content.password:
            entered, chosen = QtWidgets.QInputDialog.getText(self, "CAN File", "Password", QtWidgets.QLineEdit.EchoMode.Password)
            if not chosen or entered != content.password:
                QtWidgets.QMessageBox.warning(self, "CAN Setup", "Incorrect Password")
                return
        ecu = window.worker.ecu
        if content.lock_serial and content.lock_serial != ecu.info.serial:
            QtWidgets.QMessageBox.warning(self, "CAN Setup", "This CAN File is locked to a different ECU.")
            return

        def work(live):
            from . import protocol as p

            live.logs.write_named(p.CMD_WRITE_CAN_FILE, plain)
            if can.RESET_STREAMS_ID in live.table:
                live.set(can.RESET_STREAMS_ID, 1)
                live.set(can.RESET_STREAMS_ID, 0)

        window.call(work, lambda _r: QtWidgets.QMessageBox.information(self, "CAN Setup", "File loaded Successfully."))

    def save_can_file(self) -> None:
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save CAN File", "CANFile.lcf", "Link Vehicle CAN files (*.lcf)")
        if not path:
            return
        window = self.window_

        def done(raw: bytes) -> None:
            if not raw:
                QtWidgets.QMessageBox.information(self, "CAN Setup", "No CAN File in ECU")
                return
            from . import crypto

            Path(path).write_bytes(crypto.encode(raw, window.database.file_key))

        window.call(lambda ecu: ecu.logs.read_named("CANFile"), done)

    def apply(self) -> None:
        self._read_grid()
        problems = [text for module, channels in self.edited.items() for text in can.validate_channels(module, channels)]
        if problems:
            QtWidgets.QMessageBox.warning(self, "CAN Setup", "\n".join(problems))
            return False
        module, rate, edited, streams = self._loaded_module, self.bit_rate.currentIndex(), dict(self.edited), list(self.streams)

        def work(ecu):
            if can.BIT_RATE_IDS[module] in ecu.table:
                ecu.set(can.BIT_RATE_IDS[module], rate)
            for which, channels in edited.items():
                can.apply_channels(ecu, which, channels)
            if can.STREAM_PARAMETER_ID in ecu.table:
                can.write_streams(ecu, streams)

        self.window_.call(work, lambda _r: self.window_.statusBar().showMessage("Remember to store ECU settings", 3000))
        return True

    def _ok(self) -> None:
        if self.apply() is not False:
            self.accept()


class KnockAudioWindow(QtWidgets.QWidget):
    """Knock Audio: the computer's sound input as a trace, spectrum or waterfall. No ECU traffic."""

    RATE, BLOCK = 44100, 1024
    MODES = ("Sampled", "FFT", "Waterfall")
    COLUMNS = 400

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Knock Audio")
        self.resize(760, 420)
        self.samples: list[float] = [0.0] * self.BLOCK
        self.spectrum: list[float] = [0.0] * (self.BLOCK // 2)
        self.waterfall = QtGui.QImage(self.COLUMNS, self.BLOCK // 2, QtGui.QImage.Format.Format_RGB32)
        self.waterfall.fill(0)
        self.column = 0
        self.source = None
        self.device = None
        self.buffer = bytearray()
        self.mode = QtWidgets.QComboBox()
        self.mode.addItems(self.MODES)
        self.mode.setCurrentIndex(2)
        self.inputs = QtWidgets.QComboBox()
        self.contrast = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.contrast.setRange(1, 100)
        self.contrast.setValue(30)
        self.volume = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(100)
        self.canvas = _AudioCanvas(self)
        row = QtWidgets.QFormLayout()
        row.addRow("Audio Input", self.inputs)
        row.addRow("View", self.mode)
        row.addRow("Contrast", self.contrast)
        row.addRow("Volume", self.volume)
        column = QtWidgets.QVBoxLayout(self)
        column.addLayout(row)
        column.addWidget(self.canvas, 1)
        self.mode.currentIndexChanged.connect(self.canvas.update)
        self._list_devices()
        self.inputs.currentIndexChanged.connect(self.start)

    def _list_devices(self) -> None:
        try:
            from PySide6.QtMultimedia import QMediaDevices
        except ImportError:
            self.inputs.addItem("Sound input is not available in this build")
            return
        for device in QMediaDevices.audioInputs():
            self.inputs.addItem(device.description(), device)

    def start(self) -> None:
        try:
            from PySide6.QtMultimedia import QAudioFormat, QAudioSource
        except ImportError:
            return
        self.stop()
        device = self.inputs.currentData()
        if device is None:
            return
        wanted = QAudioFormat()
        wanted.setSampleRate(self.RATE)
        wanted.setChannelCount(1)
        wanted.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        self.source = QAudioSource(device, wanted, self)
        self.device = self.source.start()
        if self.device is not None:
            self.device.readyRead.connect(self._read)

    def stop(self) -> None:
        if self.source is not None:
            self.source.stop()
            self.source = None
            self.device = None

    def showEvent(self, event: QtGui.QShowEvent) -> None:
        self.start()
        super().showEvent(event)

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        self.stop()
        super().closeEvent(event)

    def _read(self) -> None:
        self.buffer += bytes(self.device.readAll())
        size = self.BLOCK * 2
        while len(self.buffer) >= size:
            import struct

            block, self.buffer = bytes(self.buffer[:size]), self.buffer[size:]
            if len(self.buffer) > size:
                continue  # A slow computer shows every other block rather than falling behind.
            self.add_block(struct.unpack(f"<{self.BLOCK}h", block))

    def add_block(self, block) -> None:
        gain = self.volume.value() / 100
        self.samples = [value * gain / 32768 for value in block]
        self.spectrum = tune.fft_magnitudes(self.samples)
        scale = self.contrast.value() * 200
        for y, value in enumerate(self.spectrum):
            level = int(value * scale)
            self.waterfall.setPixel(self.column, len(self.spectrum) - 1 - y, 0xFFFF00FF if level > 250 else 0xFF000000 | min(level, 255) << 8)
        self.column = (self.column + 1) % self.COLUMNS
        self.canvas.update()


class _AudioCanvas(QtWidgets.QWidget):
    def __init__(self, owner: KnockAudioWindow) -> None:
        super().__init__()
        self.owner = owner

    def paintEvent(self, _event: QtGui.QPaintEvent) -> None:
        painter = QtGui.QPainter(self)
        painter.fillRect(self.rect(), QtGui.QColor(0, 0, 0))
        owner = self.owner
        width, height = self.width(), self.height()
        mode = owner.mode.currentIndex()
        if mode == 2:
            painter.drawImage(self.rect(), owner.waterfall)
            return
        data = owner.samples if mode == 0 else owner.spectrum
        painter.setPen(QtGui.QColor(0, 0, 255) if mode == 0 else QtGui.QColor(80, 160, 255))
        points = QtGui.QPolygonF()
        tallest = max(max(data), 1e-9) if mode == 1 else 1.0
        for i in range(0, len(data), max(len(data) // max(width, 1), 1)):
            y = height / 2 - data[i] * height / 2 if mode == 0 else height - data[i] / tallest * height
            points.append(QtCore.QPointF(i / len(data) * width, y))
        painter.drawPolyline(points)
        if mode == 1:
            painter.setPen(QtGui.QColor(128, 128, 128))
            for hertz in range(5000, owner.RATE // 2, 5000):
                x = hertz / (owner.RATE / 2) * width
                painter.drawLine(QtCore.QPointF(x, 0), QtCore.QPointF(x, height))
                painter.setPen(QtGui.QColor(255, 255, 255))
                painter.drawText(QtCore.QPointF(x + 2, 12), f"{hertz // 1000} kHz")
                painter.setPen(QtGui.QColor(128, 128, 128))


def load_runtime_layout(name: str) -> Layout | None:
    from .assets import asset

    path = asset(name)
    if path is None:
        return None
    try:
        return Layout.load(path)
    except LayoutError:
        return None


class MixtureSetupDialog(QtWidgets.QDialog):
    """Mixture Map Setup, with PCLink's fields."""

    def __init__(self, settings: tune.MixtureSettings, tables: list[tuple[int, str]], choices: list[tuple[int, str]], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Mixture Map Setup")
        self.settings = settings
        self.table = QtWidgets.QComboBox()
        for data_id, name in tables:
            self.table.addItem(name, data_id)
        self.table.setCurrentIndex(max(self.table.findData(settings.table), 0))
        self.highlight = QtWidgets.QDoubleSpinBox()
        self.highlight.setRange(0.01, 10000)
        self.highlight.setValue(settings.absolute_error)
        form = QtWidgets.QFormLayout(self)
        form.addRow("Fuel Table", self.table)
        form.addRow("Highlight Range", self.highlight)
        self.sources = {}
        for name, title in (("rpm_id", "RPM Source"), ("mixture_id", "Wideband Source"), ("load_id", "Fuel Load"), ("load2_id", "Target Load")):
            self.sources[name] = self._source(choices, getattr(settings, name))
            form.addRow(title, self.sources[name])
        self.views = {}
        for name, title in (("show_actual", "Actual"), ("show_target", "Target"), ("show_samples", "Samples"), ("show_error", "Error")):
            box = QtWidgets.QCheckBox(title)
            box.setChecked(getattr(settings, name))
            self.views[name] = box
            form.addRow(box)
        self.min_enabled = QtWidgets.QCheckBox("Minimum Samples")
        self.min_enabled.setChecked(settings.min_samples_enabled)
        self.min_samples = QtWidgets.QSpinBox()
        self.min_samples.setRange(0, 1000)
        self.min_samples.setValue(settings.min_samples)
        form.addRow(self.min_enabled, self.min_samples)
        self.zone_enabled = QtWidgets.QCheckBox("Active Zone Area (% of Zone)")
        self.zone_enabled.setChecked(settings.zone_enabled)
        self.zone = QtWidgets.QDoubleSpinBox()
        self.zone.setRange(0, 100)
        self.zone.setValue(settings.zone_area)
        form.addRow(self.zone_enabled, self.zone)
        self.filters = []
        existing = settings.filters or [(0, False, 0.0, index >= 3, False) for index in range(6)]
        for index, (data_id, greater, value, transient, enabled) in enumerate(existing):
            use = QtWidgets.QCheckBox(f"{'Transient' if transient else 'Conditional'} Filter {index % 3 + 1}")
            use.setChecked(enabled)
            source = self._source([(0, ""), *choices], data_id)
            comparison = QtWidgets.QComboBox()
            comparison.addItems([">", "<"])
            comparison.setCurrentIndex(0 if greater else 1)
            amount = QtWidgets.QDoubleSpinBox()
            amount.setRange(-1000, 10000)
            amount.setDecimals(1)
            amount.setValue(value)
            row = QtWidgets.QHBoxLayout()
            for widget in (source, comparison, amount):
                row.addWidget(widget)
            form.addRow(use, row)
            self.filters.append((use, source, comparison, amount, transient))
        form.addRow(ok_cancel(self, self._ok))

    @staticmethod
    def _source(choices: list[tuple[int, str]], current: int) -> QtWidgets.QComboBox:
        box = QtWidgets.QComboBox()
        for data_id, name in choices:
            box.addItem(name, data_id)
        box.setCurrentIndex(max(box.findData(current), 0))
        return box

    def _ok(self) -> None:
        s = self.settings
        s.table = self.table.currentData()
        s.absolute_error = self.highlight.value()
        for name, box in self.sources.items():
            setattr(s, name, box.currentData())
        for name, box in self.views.items():
            setattr(s, name, box.isChecked())
        s.min_samples_enabled, s.min_samples = self.min_enabled.isChecked(), self.min_samples.value()
        s.zone_enabled, s.zone_area = self.zone_enabled.isChecked(), self.zone.value()
        s.filters = [(source.currentData(), comparison.currentIndex() == 0, amount.value(), transient, use.isChecked()) for use, source, comparison, amount, transient in self.filters]
        self.accept()
