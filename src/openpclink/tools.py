"""PCLink's dialogs and tool windows, each following the real program's fields and wording."""
from __future__ import annotations

import time
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from . import __version__, eculog, firmware, scope, theme as themes
from .cansetup import CanSetupDialog  # noqa: F401  (kept here for gui.py)
from .ecu import Ecu
from .gauges import GaugeSettings, tcolor, to_tcolor
from .layoutfile import Layout, LayoutError, LayoutView
from .logfile import DETAIL_FIELDS, READ_ONLY_DETAILS, ExportOptions, Log, build_link_log, export_csv, save_link_log
from .logviews import LogSession, TimePlot, hms
from .transports import TransportError, open_transport

BUTTONS = QtWidgets.QDialogButtonBox.StandardButton
MESSAGE_BUTTONS = QtWidgets.QMessageBox.StandardButton
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
        self.temporary.setChecked(True)
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
        self.serial = QtWidgets.QLineEdit("" if current is None else str(current))
        form.addRow("ECU Serial Number", self.serial)
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
    """Colour Theme Setup: pick a theme, then change any item's colour. Edited themes are kept as Custom."""

    def __init__(self, window, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Colour Theme Setup")
        self.window_ = window
        self.theme = themes.theme_named(window.options)
        self.choice = QtWidgets.QComboBox()
        self._fill_choices()
        self.items = QtWidgets.QListWidget()
        self.parameters = QtWidgets.QListWidget()
        self.overlays = QtWidgets.QListWidget()
        for widget in (self.items, self.parameters, self.overlays):
            widget.setFixedHeight(112)
            widget.itemDoubleClicked.connect(self._edit)
            widget.itemSelectionChanged.connect(lambda widget=widget: self._focus(widget))
        self.choice.activated.connect(self._pick_theme)
        self.selected_list = self.items
        edit = QtWidgets.QPushButton("Edit Colour")
        edit.clicked.connect(lambda: self._edit(self._selected()))
        self.swatches = []
        palette = QtWidgets.QGridLayout()
        palette.setSpacing(4)
        for index, colour in enumerate(themes.PALETTE):
            swatch = QtWidgets.QPushButton()
            swatch.setFixedSize(30, 24)
            swatch.setStyleSheet(f"background:#{colour}; border:1px solid #606060")
            swatch.clicked.connect(lambda _checked=False, value=int(colour, 16): self._paint(value))
            palette.addWidget(swatch, index % 9, index // 9)
            self.swatches.append(swatch)
        left = QtWidgets.QGridLayout()
        left.addWidget(QtWidgets.QLabel("Select Theme"), 0, 0)
        left.addWidget(self.choice, 1, 0)
        left.addWidget(QtWidgets.QLabel("Theme Items"), 2, 0)
        left.addWidget(self.items, 3, 0)
        left.addWidget(QtWidgets.QLabel("Parameters"), 4, 0)
        left.addWidget(self.parameters, 5, 0)
        left.addLayout(self._arrows(self.parameters, "parameter"), 5, 1)
        left.addWidget(QtWidgets.QLabel("Overlays"), 6, 0)
        left.addWidget(self.overlays, 7, 0)
        left.addLayout(self._arrows(self.overlays, "overlay"), 7, 1)
        right = QtWidgets.QVBoxLayout()
        right.addWidget(QtWidgets.QLabel("Item Colour"))
        right.addLayout(palette)
        right.addStretch()
        right.addWidget(edit)
        top = QtWidgets.QHBoxLayout()
        top.addLayout(left)
        top.addLayout(right)
        box = QtWidgets.QDialogButtonBox(BUTTONS.Help | BUTTONS.Apply | BUTTONS.Ok | BUTTONS.Cancel)
        box.accepted.connect(self._ok)
        box.rejected.connect(self.reject)
        box.helpRequested.connect(lambda: getattr(window, "show_topic", lambda _name: None)("colour_themes"))
        box.button(BUTTONS.Apply).clicked.connect(self._apply)
        column = QtWidgets.QVBoxLayout(self)
        column.addLayout(top)
        column.addWidget(box)
        self._fill()

    def _fill_choices(self) -> None:
        self.choice.clear()
        names = list(themes.THEME_NAMES)
        if self.theme.name not in names:
            names.append(self.theme.name)
        self.choice.addItems(names)
        self.choice.setCurrentText(self.theme.name)

    def _arrows(self, widget: QtWidgets.QListWidget, kind: str) -> QtWidgets.QVBoxLayout:
        add, remove = QtWidgets.QPushButton("<<"), QtWidgets.QPushButton(">>")
        for button in (add, remove):
            button.setFixedWidth(34)
        add.clicked.connect(lambda: self._grow(kind))
        remove.clicked.connect(lambda: self._shrink(kind, widget))
        column = QtWidgets.QVBoxLayout()
        column.addWidget(add)
        column.addWidget(remove)
        column.addStretch()
        return column

    def _list_of(self, kind: str) -> list[int]:
        return self.theme.parameters if kind == "parameter" else self.theme.overlays

    def _grow(self, kind: str) -> None:
        self._list_of(kind).append(0)
        self._modified()

    def _shrink(self, kind: str, widget: QtWidgets.QListWidget) -> None:
        values = self._list_of(kind)
        if len(values) > 1:
            row = widget.currentRow()
            del values[row if 0 <= row < len(values) else -1]
            self._modified()

    def _focus(self, widget: QtWidgets.QListWidget) -> None:
        if widget.currentItem():
            self.selected_list = widget

    def _swatch(self, widget: QtWidgets.QListWidget, text: str, value: int, key) -> None:
        pixels = QtGui.QPixmap(16, 16)
        pixels.fill(tcolor(value))
        item = QtWidgets.QListWidgetItem(QtGui.QIcon(pixels), text)
        item.setData(ID_ROLE, key)
        widget.addItem(item)

    def _fill(self) -> None:
        rows = {widget: widget.currentRow() for widget in (self.items, self.parameters, self.overlays)}
        for widget in rows:
            widget.clear()
        for key, title in themes.list_entries():
            self._swatch(self.items, title, self.theme.items[key], ("item", key))
        for index, value in enumerate(self.theme.parameters):
            self._swatch(self.parameters, f"Parameter {index + 1}", value, ("parameter", index))
        for index, value in enumerate(self.theme.overlays):
            self._swatch(self.overlays, f"Overlay {index + 1}", value, ("overlay", index))
        for widget, row in rows.items():
            if row >= 0:
                widget.setCurrentRow(min(row, widget.count() - 1))

    def _pick_theme(self) -> None:
        name = self.choice.currentText()
        saved = self.window_.options.themes.get(name)
        self.theme = themes.Theme.built_in(name) if name in themes.THEME_NAMES else themes.Theme.from_saved(saved) if saved else self.theme
        self._fill()

    def _selected(self):
        widget = self.selected_list
        return widget.currentItem() or next((w.currentItem() for w in (self.items, self.parameters, self.overlays) if w.currentItem()), None)

    def _store(self, kind: str, key) -> tuple:
        if kind == "item":
            return self.theme.items, (("Background Centre", "Background Edge") if key == "Background Centre" else (key,))
        return self._list_of(kind), (key,)

    def _set(self, item, value: int) -> None:
        kind, key = item.data(ID_ROLE)
        store, keys = self._store(kind, key)
        for each in keys:
            store[each] = value
        self._modified()

    def _modified(self) -> None:
        if self.theme.name in themes.THEME_NAMES and self._differs():
            self.theme.name = themes.CUSTOM_NAME
            self._fill_choices()
        self._fill()

    def _differs(self) -> bool:
        built = themes.Theme.built_in(self.theme.base)
        return (built.items, built.overlays, built.parameters) != (self.theme.items, self.theme.overlays, self.theme.parameters)

    def _paint(self, value: int) -> None:
        item = self._selected()
        if item is not None:
            self._set(item, to_tcolor(QtGui.QColor.fromRgb(value)))

    def _edit(self, item) -> None:
        if item is None:
            return
        kind, key = item.data(ID_ROLE)
        store, _keys = self._store(kind, key)
        chosen = QtWidgets.QColorDialog.getColor(tcolor(store[key]), self, "Edit Colour")
        if chosen.isValid():
            self._set(item, to_tcolor(chosen))

    def _apply(self) -> None:
        options = self.window_.options
        options.theme = self.theme.name
        if self.theme.name not in themes.THEME_NAMES:
            options.themes[self.theme.name] = self.theme.saved()
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
    """Parameter Properties: display range, warnings and colour for one live value, as PCLink lays them out."""

    def __init__(self, caption: str, units: str, display, parent=None, colours=(), decimals: int = 1, log_info: tuple | None = None, convert=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Parameter Properties")
        self.display = display
        self.to_display, self.to_base = convert or (lambda value: value, lambda value: value)
        name = QtWidgets.QLineEdit(caption)
        name.setEnabled(False)
        unit = QtWidgets.QLineEdit(units)
        unit.setEnabled(False)
        self.colour = QtWidgets.QComboBox()
        for index, colour in enumerate(colours or [QtGui.QColor("white")] * 14):
            pixels = QtGui.QPixmap(48, 12)
            pixels.fill(colour)
            self.colour.addItem(QtGui.QIcon(pixels), "", index)
        self.colour.setIconSize(QtCore.QSize(48, 12))
        self.colour.setCurrentIndex(min(display.colour, self.colour.count() - 1))
        general = QtWidgets.QGroupBox("General")
        form = QtWidgets.QFormLayout(general)
        form.setLabelAlignment(QtCore.Qt.AlignmentFlag.AlignLeft)
        for title, widget in (("Name", name), ("Units", unit), ("Colour", self.colour)):
            form.addRow(QtWidgets.QLabel(title))
            form.addRow(widget)
        self.fields = {}
        shown = QtWidgets.QGroupBox("Display")
        grid = QtWidgets.QGridLayout(shown)
        for at, (field, title) in enumerate((
            ("minimum", "Display Minimum"), ("maximum", "Display Maximum"), ("low_warning", "Low Soft Warning Value"),
            ("high_warning", "High Soft Warning Value"), ("low_error", "Low Hard Warning Value"), ("high_error", "High Hard Warning Value"),
        )):
            value = getattr(display, field)
            edit = QtWidgets.QLineEdit("" if value is None or abs(value) >= 1e8 else f"{self.to_display(value):.{decimals}f}")
            edit.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight)
            self.fields[field] = edit
            edit.setProperty("initial", edit.text())
            grid.addWidget(QtWidgets.QLabel(title), at // 2 * 2, at % 2)
            grid.addWidget(edit, at // 2 * 2 + 1, at % 2)
        logging = QtWidgets.QGroupBox("Logging Display")
        box = QtWidgets.QVBoxLayout(logging)
        self.auto = QtWidgets.QRadioButton("Automatic")
        self.fixed = QtWidgets.QRadioButton("Display Min/Max")
        (self.auto if display.auto_scale else self.fixed).setChecked(True)
        box.addWidget(QtWidgets.QLabel("Scaling"))
        box.addWidget(self.auto)
        box.addWidget(self.fixed)
        info = QtWidgets.QGroupBox("Current Log File Info")
        rows = QtWidgets.QGridLayout(info)
        rate, samples = QtWidgets.QLineEdit(), QtWidgets.QLineEdit()
        for edit in (rate, samples):
            edit.setReadOnly(True)
        if log_info:
            rate.setText(f"{log_info[0]:g}")
            samples.setText(str(log_info[1]))
        rows.addWidget(QtWidgets.QLabel("Sample Rate"), 0, 0)
        rows.addWidget(rate, 1, 0)
        rows.addWidget(QtWidgets.QLabel("Hz"), 1, 1)
        rows.addWidget(QtWidgets.QLabel("Samples"), 2, 0)
        rows.addWidget(samples, 3, 0)
        column = QtWidgets.QVBoxLayout(self)
        for group in (general, shown, logging, info):
            column.addWidget(group)
        column.addWidget(ok_cancel(self))

    def result_display(self):
        from .assets import ParameterDisplay

        def number(name: str) -> float | None:
            if self.fields[name].text() == self.fields[name].property("initial"):
                return getattr(self.display, name)
            try:
                return self.to_base(float(self.fields[name].text()))
            except ValueError:
                return None

        return ParameterDisplay(self.colour.currentData(), number("minimum"), number("maximum"), number("high_error"), number("high_warning"), number("low_warning"), number("low_error"), self.auto.isChecked())


class LogDetailsDialog(QtWidgets.QDialog):
    """Log File Details: Description, General, Environment and Other boxes, as PCLink lays them out."""

    def __init__(self, log: Log, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Log File Details")
        self.log = log
        self.edits = {}

        def field(name: str, memo: bool = False) -> QtWidgets.QWidget:
            if memo:
                edit = QtWidgets.QPlainTextEdit(log.details.get(name, ""))
                edit.setMinimumHeight(70)
            else:
                edit = QtWidgets.QLineEdit(log.details.get(name, ""))
            edit.setReadOnly(name in READ_ONLY_DETAILS)
            if name in READ_ONLY_DETAILS:
                edit.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
            self.edits[name] = edit
            return edit

        def box(title: str, rows) -> QtWidgets.QGroupBox:
            group = QtWidgets.QGroupBox(title)
            form = QtWidgets.QFormLayout(group)
            form.setLabelAlignment(QtCore.Qt.AlignmentFlag.AlignLeft)
            form.setRowWrapPolicy(QtWidgets.QFormLayout.RowWrapPolicy.WrapAllRows)
            for label, name, memo in rows:
                form.addRow(label, field(name, memo))
            return group

        description = box("Description", [("Name", "Name", False)])
        general = box("General", [("Driver", "Driver", False), ("Vehicle", "Vehicle", False), ("Engine", "Engine", False), ("ECU", "ECU", False), ("Notes", "Notes", True)])
        environment = box("Environment", [("Location", "Location", False), ("Event", "Event", False), ("Log Date", "Log Date", False), ("Log Time", "Log Time", False), ("Weather", "Weather", False), ("Notes", "Environment", True)])
        other = QtWidgets.QGroupBox("Other")
        grid = QtWidgets.QGridLayout(other)
        for column, rows in enumerate((
            (("Download Date", "Download Date"), ("Download Time", "Download Time")),
            (("Firmware Version", "Firmware Version"), ("Software Version", "Software Version")),
            (("Data Source", "Data Source"), ("ECU Serial Number", "Serial Number")),
        )):
            for row, (label, name) in enumerate(rows):
                cell = QtWidgets.QVBoxLayout()
                cell.addWidget(QtWidgets.QLabel(label))
                cell.addWidget(field(name))
                grid.addLayout(cell, row, column)
        middle = QtWidgets.QHBoxLayout()
        middle.addWidget(general)
        middle.addWidget(environment)
        column = QtWidgets.QVBoxLayout(self)
        column.addWidget(description)
        column.addLayout(middle)
        column.addWidget(other)
        column.addWidget(ok_cancel(self, self._ok))

    def _ok(self) -> None:
        for name, edit in self.edits.items():
            text = edit.toPlainText() if isinstance(edit, QtWidgets.QPlainTextEdit) else edit.text()
            if text != self.log.details.get(name, ""):
                self.log.details[name] = text
                self.log.modified = True
        self.accept()


class _Choice:
    """A radio group that answers like a combo box, which is what the callers ask."""

    def __init__(self, group: QtWidgets.QButtonGroup) -> None:
        self.group = group

    def currentIndex(self) -> int:
        return self.group.checkedId()

    def setCurrentIndex(self, index: int) -> None:
        self.group.button(index).setChecked(True)


class ExportDialog(QtWidgets.QDialog):
    """PCLink's log export options: Export Data, Output, Time Stamps and Details."""

    FORMATS = ("Comma Separated Value (.CSV)", "Link Log Files (.llgx/.llg5)")

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Export")
        group = QtWidgets.QButtonGroup(self)
        data = QtWidgets.QGroupBox("Export Data")
        column = QtWidgets.QVBoxLayout(data)
        for number, title in enumerate(("Full Log", "Current Section", "Current Zoom")):
            button = QtWidgets.QRadioButton(title)
            group.addButton(button, number)
            column.addWidget(button)
        group.button(2).setChecked(True)
        self.range = _Choice(group)
        self.format = QtWidgets.QComboBox()
        self.format.addItems(self.FORMATS)
        output = QtWidgets.QGroupBox("Output")
        form = QtWidgets.QFormLayout(output)
        form.setRowWrapPolicy(QtWidgets.QFormLayout.RowWrapPolicy.WrapAllRows)
        form.addRow("File Format", self.format)
        self.file_time = QtWidgets.QCheckBox("File Time")
        self.section_time = QtWidgets.QCheckBox("Section Time")
        self.section_time.setChecked(True)
        stamps = QtWidgets.QGroupBox("Time Stamps")
        stamp_column = QtWidgets.QVBoxLayout(stamps)
        stamp_column.addWidget(self.file_time)
        stamp_column.addWidget(self.section_time)
        self.device = QtWidgets.QCheckBox("Device Information")
        self.file_info = QtWidgets.QCheckBox("File Information")
        self.driver = QtWidgets.QCheckBox("Driver / Vehicle / Venue Information")
        details = QtWidgets.QGroupBox("Details")
        detail_column = QtWidgets.QVBoxLayout(details)
        for box in (self.device, self.file_info, self.driver):
            detail_column.addWidget(box)
        self.format.currentIndexChanged.connect(lambda index: stamps.setEnabled(index == 0) or details.setEnabled(index == 0))
        buttons = QtWidgets.QDialogButtonBox(BUTTONS.Help | BUTTONS.Ok | BUTTONS.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        buttons.helpRequested.connect(lambda: QtWidgets.QMessageBox.information(self, "Export", "Exports the log, a section or the zoomed range as CSV or as a Link log file."))
        outer = QtWidgets.QVBoxLayout(self)
        for part in (data, output, stamps, details, buttons):
            outer.addWidget(part)


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


def apply_ecu_logging(ecu, ids: dict, rows: list[tuple[int, float]], values: dict, label: str, enabled: bool, io_on: int = 0, io_off: int = 0, tick_rate: int = 0) -> None:
    """PCLink's Apply: pause logging, write the list, the triggers and the label, then start it again if enabled.

    `rows` are (data id, Hz). The list and label are always written; the rest only when they changed, as PCLink does.
    """
    from .protocol import ProtocolError

    try:
        ecu.logs.pause()
    except ProtocolError:
        if ids["mode"] in ecu.table and ecu.get(ids["mode"]) != 0:
            ecu.set(ids["mode"], 0)
    capacity = ecu.table[ids["parameters"]].count if ids["parameters"] in ecu.table else 0
    for index in range(capacity):
        data_id, hz = rows[index] if index < len(rows) else (0, 0.0)
        divider = divider_of(hz, tick_rate) if data_id else 0
        if index < len(rows) and not (data_id and divider):
            continue
        ecu.set(ids["parameters"], data_id, index)
        ecu.set(ids["rates"], divider, index)

    def change(name: str, value: float) -> bool:
        if ids[name] not in ecu.table or abs(ecu.get(ids[name]) - value) < 1e-6:
            return False
        ecu.set(ids[name], value)
        return True

    for name in ("rpm", "map", "tps", "off_delay"):
        change(name, values.get(name, 0.0))
    if ids["label"] in ecu.table:
        ecu.set_string(ids["label"], label)
    change("io_on", io_on)
    change("io_off", io_off)
    if enabled:
        if change("mode", 1):
            ecu.set_clock(comms=bool(ecu.comms))
        ecu.logs.resume()
    else:
        change("mode", 0)


from .logsetup import LoggingSetupDialog  # noqa: E402,F401 (the dialog lives in its own module)


class LogDownloadDialog(QtWidgets.QDialog):
    """ECU Log File Download. Logging pauses while it is open, as in PCLink."""

    MARKER_LABEL_IDS = (497, 10174)

    def __init__(self, window, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("ECU Log File Download")
        self.resize(907, 563)
        self.window_ = window
        self.entries: list[eculog.EcuLogEntry] = []
        self.info: eculog.LogInfo | None = None
        self.busy = False
        self.cancelled = False
        self.grid = self._table(["Log File", "Size", "Duration", "Log Date"])
        self.grid.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.grid.itemSelectionChanged.connect(self._show_entry)
        self.details = self._table(["Log Date", "Engine Starts", "Start Time", "Parameters", "Rate"])
        self.details.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.NoSelection)
        self.details.setMaximumHeight(64)
        self.details.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.parameters = self._table(["Parameter", "Name", "Rate"])
        self.parameters.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.NoSelection)
        self.progress = QtWidgets.QProgressBar()
        self.progress.setTextVisible(False)
        self.message = QtWidgets.QLabel("")
        self.delete_button = QtWidgets.QPushButton("Delete Log")
        self.delete_button.clicked.connect(self.delete)
        self.delete_all_button = QtWidgets.QPushButton("Delete All")
        self.delete_all_button.clicked.connect(self.delete_all)
        self.download_button = QtWidgets.QPushButton("Download")
        self.download_button.clicked.connect(self.download)
        self.cancel_button = QtWidgets.QPushButton("Cancel")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel)
        close = QtWidgets.QPushButton("Close")
        close.clicked.connect(self.reject)
        files = QtWidgets.QGroupBox("Log Files")
        left = QtWidgets.QVBoxLayout(files)
        left.addWidget(self.grid, 1)
        left.addWidget(self.progress)
        left.addWidget(self.message)
        info = QtWidgets.QGroupBox("Log File Info")
        QtWidgets.QVBoxLayout(info).addWidget(self.details)
        shown = QtWidgets.QGroupBox("Log File Parameters")
        QtWidgets.QVBoxLayout(shown).addWidget(self.parameters)
        row = QtWidgets.QHBoxLayout()
        for button in (self.delete_button, self.delete_all_button, self.download_button, self.cancel_button, close):
            row.addWidget(button)
        right = QtWidgets.QVBoxLayout()
        right.addWidget(info)
        right.addWidget(shown, 1)
        right.addLayout(row)
        columns = QtWidgets.QHBoxLayout()
        columns.addWidget(files, 1)
        columns.addLayout(right, 1)
        column = QtWidgets.QVBoxLayout(self)
        column.addLayout(columns, 1)
        column.addWidget(QtWidgets.QLabel("Please do not power cycle the ECU"))
        self.enable_id = None
        self.was_enabled = False
        window.call(self._open, self._listed)

    @staticmethod
    def _table(labels: list[str]) -> QtWidgets.QTableWidget:
        table = QtWidgets.QTableWidget(0, len(labels))
        table.setHorizontalHeaderLabels(labels)
        table.verticalHeader().hide()
        table.horizontalHeader().setStretchLastSection(True)
        table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        return table

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

    @staticmethod
    def size_text(size: int) -> str:
        return f"{size // 100000 / 10:.1f}MB" if size > 1000000 else f"{size // 100 / 10:.1f}kB"

    @staticmethod
    def rate_text(divider: int) -> str:
        return f"{eculog.BASE_RATE / divider:g}Hz" if divider else "0Hz"

    def _listed(self, result) -> None:
        self.info, self.entries = result
        self.grid.setRowCount(len(self.entries))
        for row, entry in enumerate(self.entries):
            seconds = int(entry.header.duration(entry.size))
            cells = (str(entry.kind), self.size_text(entry.size), f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}", entry.header.date_text())
            for column, text in enumerate(cells):
                self.grid.setItem(row, column, QtWidgets.QTableWidgetItem(text))
        self.details.setRowCount(0)
        self.parameters.setRowCount(0)
        self.message.setText("" if self.entries else "No log files found in ECU")
        self._enable()

    def _enable(self) -> None:
        self.delete_button.setEnabled(not self.busy)
        self.delete_all_button.setEnabled(not self.busy)
        self.download_button.setEnabled(not self.busy)
        self.cancel_button.setEnabled(self.busy)

    def _show_entry(self) -> None:
        chosen = self._selected()
        if not chosen:
            return
        entry, header = chosen[0], chosen[0].header
        self.details.setRowCount(1)
        rate = header.bytes_per_second() / 1000
        cells = (header.date_text(), str(header.engine_starts), f"{header.start_time}s", str(len(header.channels)), f"{rate:.1f}kB/s")
        for column, text in enumerate(cells):
            self.details.setItem(0, column, QtWidgets.QTableWidgetItem(text))
        self.parameters.setRowCount(len(header.channels))
        for row, (data_id, divider) in enumerate(header.channels):
            name, _units = self.window_.describe(data_id)
            for column, text in enumerate((str(row), name, self.rate_text(divider))):
                self.parameters.setItem(row, column, QtWidgets.QTableWidgetItem(text))

    def _selected(self) -> list[eculog.EcuLogEntry]:
        return [self.entries[row] for row in sorted({index.row() for index in self.grid.selectedIndexes()})]

    def download(self) -> None:
        chosen = self._selected()
        if not chosen or self.busy:
            return
        total = sum(entry.size for entry in chosen) or 1
        self.progress.setRange(0, total)
        self.progress.setValue(0)
        self.message.setText(f"Download Log '{chosen[0].kind}'")
        self.busy, self.cancelled = True, False
        self._enable()
        info = self.info

        def work(ecu):
            files, base = [], 0
            for entry in chosen:
                data = ecu.logs.download(entry, ecu.info.firmware, lambda sent, at=base: self._report(at + sent))
                files.append((entry.name, data))
                base += entry.size
                if self.cancelled:
                    break
            label = next((ecu.get_string(i) for i in self.MARKER_LABEL_IDS if i in ecu.table), "") or eculog.RECORD_NAMES[4]
            return files, label, ecu.info.firmware, str(ecu.info.serial)

        self.window_.call(work, self._downloaded)

    def _report(self, done: int) -> bool:
        self.window_.report_progress.emit(done)
        return not self.cancelled

    def cancel(self) -> None:
        self.cancelled = True

    def _downloaded(self, result) -> None:
        files, label, firmware, serial = result
        self.busy = False
        self._enable()
        if self.cancelled:
            if not files or QtWidgets.QMessageBox.question(self, "warning", "Do you want to continue with the partial log file?") != QtWidgets.QMessageBox.StandardButton.Yes:
                self.message.setText("Download log file cancelled")
                return
        try:
            logs = eculog.build_logs(files, self.info, self.window_.describe, label)
        except eculog.EcuLogError as exc:
            QtWidgets.QMessageBox.warning(self, "warning", f"Log download failed with error:\r\n{exc}")
            return
        self.message.setText("Download log file completed.")
        stamp = time.strftime("%Y-%m-%d %H;%M;%S")
        for log in logs:
            log.details["Name"] = f"ECU Internal Datalog - {stamp}"
            log.details["Firmware Version"] = firmware
            log.details["Serial Number"] = serial
            log.details["Software Version"] = f"OpenPCLink {__version__}"
            path = self._save(log, stamp)
            if path and QtWidgets.QMessageBox.question(self, "Open", f"Open downloaded log file for viewing now?\r\n{path}") == QtWidgets.QMessageBox.StandardButton.Yes:
                self.window_.open_log_path(path, show=False)

    def _save(self, log: Log, stamp: str) -> Path | None:
        folder = self.window_.options.folder("log")
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save Log File", str(Path(folder) / f"ECU Log {stamp}.llg5"), "Link Log Files (*.llg5)")
        while path:
            try:
                save_link_log(log, Path(path))
                self.window_.log_manager.refresh()
                return Path(path)
            except OSError as exc:
                if QtWidgets.QMessageBox.question(self, "warning", f"Unable save log file to:\r\n{path}\r\n{exc}\r\nTry again?") != QtWidgets.QMessageBox.StandardButton.Yes:
                    break
        return None

    def delete(self) -> None:
        names = [entry.name for entry in self._selected()]
        if names and not self.busy and QtWidgets.QMessageBox.question(self, "Delete Log", "Are you sure you want to delete this log file?") == QtWidgets.QMessageBox.StandardButton.Yes:
            self.window_.call(lambda ecu: self._delete(ecu, names), self._listed)

    def delete_all(self) -> None:
        if not self.busy and QtWidgets.QMessageBox.question(self, "Delete All", "Are you sure you want to delete all of the log files?") == QtWidgets.QMessageBox.StandardButton.Yes:
            names = [entry.name for entry in self.entries]
            self.window_.call(lambda ecu: self._delete(ecu, names), self._listed)

    def _delete(self, ecu, names: list[str]) -> tuple:
        for name in names:
            ecu.logs.delete(name)
        return self._list(ecu)

    def reject(self) -> None:
        if self.busy:
            if QtWidgets.QMessageBox.question(self, "Close Log Downloader", "Download progress will be lost, do you want to close?") != QtWidgets.QMessageBox.StandardButton.Yes:
                return
            self.cancelled = True
        super().reject()

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
        for name, parameters in self._pages():
            view = LayoutView("Time Plot", log_parameters=parameters)
            plot = TimePlot(self.session, view)
            self.plots[name] = plot
            self.tabs.addTab(plot, name)
        row = QtWidgets.QHBoxLayout(self)
        row.addLayout(side)
        row.addWidget(self.tabs, 1)
        window.call(self._pause_logging, lambda _r: None)

    def _pages(self) -> list[tuple[str, list[tuple[int, int]]]]:
        ecu = self.window_.worker.ecu
        layout = load_runtime_layout(scope.layout_name(ecu)) if ecu is not None else None
        pages = [(page.name, page.views[0].log_parameters) for page in layout.pages if page.views] if layout else []
        return pages or [(name, [(data_id, order) for order, data_id in enumerate(ids)]) for name, ids in scope.TABS.items()]

    def _pause_logging(self, ecu) -> None:
        if self.LOGGING_MODE_ID in ecu.table and ecu.get(self.LOGGING_MODE_ID) == 1:
            ecu.set(self.LOGGING_MODE_ID, 0)
            self.resume_logging = True

    def capture(self) -> None:
        self.save.setEnabled(True)
        database = self.window_.database
        caption = lambda data_id, name: database.parameter(data_id).caption if database.known(data_id) else name
        self.window_.call(lambda ecu: scope.capture(ecu, caption=caption), self._captured)

    def _captured(self, log: Log) -> None:
        ecu = self.window_.worker.ecu
        if ecu is not None:
            log.details.update({"Firmware Version": ecu.info.firmware, "Serial Number": str(ecu.info.serial), "Software Version": f"OpenPCLink {__version__}"})
        self.log = log
        self.session.logs = []
        self.session.add(log)

    def save_log(self) -> None:
        if self.log is None:
            return
        folder = self.window_.options.folder("log")
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save Trigger Scope", str(Path(folder) / f"Trigger Scope - {time.strftime('%Y-%m-%d %H:%M:%S')}.llg5"), "Link Log Files (*.llg5)")
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
# Analog source choice -> the volts value it reads, from PCLink's own table.
SOURCE_VOLTS = (
    0, 0, 1334, 1335, 1336, 1337, 1338, 1339, 1355, 1356, 1357, 1358, 1334, 1360, 1361, 1362, 1363, 1364, 4710, 4711, 4712, 4713,
    1052, 1053, 1054, 1055, 0, 0, 0, 0, 0, 0, 0, 1421, 1422, 1423, 1424, 1425, 1426, 1427, 1428, 1979, 1980, 1981, 1982, 1983,
    1984, 1985, 1986, 0, 416, 425, 434, 443, 455, 464, 473, 482, 4632, 4633, 4634, 4635, 4636, 4637, 4735, 4736, 0, 0, 0, 0, 0,
    0, 0, 0, 4714, 4715, 4716,
)


def source_millivolts(ecu, source_id: int) -> int:
    """What the analog input chosen as a sensor's source reads, in millivolts."""
    if source_id not in ecu.table:
        return 0
    choice = int(ecu.get(source_id))
    volts_id = SOURCE_VOLTS[choice] if 0 <= choice < len(SOURCE_VOLTS) else 0
    return round(ecu.get(volts_id) * 1000) if volts_id in ecu.table else 0


class CalibrationWindow(QtWidgets.QDialog):
    """E-throttle and pedal calibration. The ECU does the work; these only start and follow it."""

    def __init__(self, window, title: str) -> None:
        super().__init__(window)
        self.setWindowTitle(title)
        self.window_ = window
        self.resize(292, 146)
        self.setFixedSize(292, 146)
        self.label = QtWidgets.QLabel("", self)
        self.label.setWordWrap(True)
        self.label.setGeometry(82, 35, 200, 60)
        self.button = QtWidgets.QPushButton("Close", self)
        self.button.setGeometry(107, 108, 101, 23)
        self.button.clicked.connect(self.next_step)
        self.bars = []
        for at, name in ((10, "Main"), (46, "Sub")):
            bar = QtWidgets.QSlider(QtCore.Qt.Orientation.Vertical, self)
            bar.setRange(0, 5000)
            bar.setInvertedAppearance(True)
            bar.setEnabled(False)
            bar.setGeometry(at, 28, 28, 108)
            QtWidgets.QLabel(name, self).move(at + 2, 8)
            self.bars.append(bar)
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.refresh)

    def _write(self, *pairs: tuple[int, float]) -> None:
        def work(ecu):
            for data_id, value in pairs:
                if data_id in ecu.table and ecu.get(data_id) != value:
                    ecu.set(data_id, value)

        self.window_.call(work, lambda _r: None)

    def refresh(self) -> None:
        ecu = self.window_.worker.ecu
        for bar, millivolts in zip(self.bars, self.millivolts(ecu)):
            bar.setValue(max(0, min(5000, 5000 - millivolts)))

    def millivolts(self, ecu) -> tuple[int, int]:
        return 0, 0

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

    def millivolts(self, ecu) -> tuple[int, int]:
        return tuple(source_millivolts(ecu, source) for source in THROTTLE[self.throttle]["sources"])

    def refresh(self) -> None:
        reading = self.window_.reading(TPS_CAL_STATUS)
        self.label.setText(reading.text if reading else "")
        super().refresh()


class PedalCalibration(CalibrationWindow):
    STEPS = (("Press pedal down", "Next"), ("Release pedal", "Next"), ("APS cal complete", "Done"))

    def __init__(self, window) -> None:
        super().__init__(window, "Calibration")
        self.step = 0
        self._show()
        self._write((APS_OPEN, 1))
        self.timer.start()

    def millivolts(self, ecu) -> tuple[int, int]:
        if self.step < 2:
            return tuple(source_millivolts(ecu, source) for source in APS_SOURCES)
        return tuple(round(ecu.get(data_id)) if data_id in ecu.table else 0 for data_id in (APS_MAIN, APS_SUB))

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
        column = QtWidgets.QVBoxLayout(self)
        note = QtWidgets.QLabel("Press 'Enter' after changing a value for it to take effect.")
        note.setStyleSheet("color: red; font-weight: bold")
        column.addWidget(note)
        rows = [(None, "2) Adjust CAS until timing matches"), (None, "the number above."), (CRANK_TIMING, "Degrees BTDC")] if self.mode == 0 else [
            (None, "1) Lock Ignition Timing to:"), (REF_TIMING, "Degrees BTDC"),
            (None, "2) Adjust Offset until Ignition Timing"), (None, "matches the number above:"), (TRIGGER_OFFSET, "Degrees BTDC"),
        ]
        rows += [(None, "3) Adjust Delay so Ignition Timing is"), (None, "constant over entire RPM range:"), (IGNITION_DELAY, "Microseconds")]
        self.spins = {}
        for data_id, text in rows:
            if data_id is None:
                column.addWidget(QtWidgets.QLabel(text))
            elif data_id in ecu.table:
                edit = self._edit(data_id)
                self.spins[data_id] = edit
                row = QtWidgets.QHBoxLayout()
                row.addSpacing(20)
                row.addWidget(edit)
                row.addWidget(QtWidgets.QLabel(text))
                row.addStretch()
                column.addLayout(row)
        box = QtWidgets.QDialogButtonBox()
        box.addButton("Done", QtWidgets.QDialogButtonBox.ButtonRole.AcceptRole)
        box.addButton(BUTTONS.Cancel)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        column.addWidget(box)
        self.lock = LOCK_CRANK if self.mode == 0 else LOCK_REF
        self._write(self.lock, 1)

    def _write(self, data_id: int, value: float) -> None:
        ecu = self.window_.worker.ecu
        if data_id in ecu.table and ecu.get(data_id) != value:
            self.window_.write(data_id, value, 0)

    def _places(self, data_id: int) -> int:
        decimals = self.window_.database.parameter(data_id).decimals
        return decimals if decimals is not None else 2

    def _edit(self, data_id: int) -> QtWidgets.QLineEdit:
        edit = QtWidgets.QLineEdit(f"{self.window_.worker.ecu.get(data_id):.{self._places(data_id)}f}")
        edit.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight)
        edit.setFixedWidth(72)
        edit.returnPressed.connect(lambda: self._commit(data_id, edit))
        return edit

    def _commit(self, data_id: int, edit: QtWidgets.QLineEdit) -> None:
        parameter = self.window_.database.parameter(data_id)
        low = parameter.minimum if parameter.minimum is not None else -1e6
        high = parameter.maximum if parameter.maximum is not None else 1e6
        try:
            value = float(edit.text())
        except ValueError:
            QtWidgets.QMessageBox.warning(self, "Error", f"{edit.text()} is not a number. Please enter a value between {low:f} and {high:f}")
            value = self.window_.worker.ecu.get(data_id)
        else:
            if not low <= value <= high:
                QtWidgets.QMessageBox.warning(self, "Error", f"Please enter a value between {low:f} and {high:f}")
                value = min(max(value, low), high)
        edit.setText(f"{value:.{self._places(data_id)}f}")
        self._write(data_id, round(value, self._places(data_id)))

    def done(self, result: int) -> None:
        if result != QtWidgets.QDialog.DialogCode.Accepted:
            for data_id, value in self.saved.items():
                self._write(data_id, value)
        for data_id in (LOCK_REF, LOCK_CRANK):
            self._write(data_id, 0)
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
    log = QtCore.Signal(str)
    finished_with = QtCore.Signal(object)

    def __init__(self, uri: str, steps: list[tuple[firmware.FirmwareImage, str]]) -> None:
        super().__init__()
        self.uri, self.steps = uri, steps
        self.transport = None

    def _reopen(self, link, wait: float) -> None:
        self.transport.close()
        time.sleep(wait)
        self.transport = open_transport(self.uri)
        link.transport = self.transport

    def run(self) -> None:
        from . import protocol as p

        error = None
        try:
            self.transport = open_transport(self.uri)
            link = p.Link(self.transport)
            for image, verb in self.steps:
                firmware.program_image(link, image, lambda wait: self._reopen(link, wait), self.progress.emit, log=self.log.emit, verb=verb)
            self.log.emit("PROGRAMMING COMPLETE.\n\nECU is ready to run\n")
            firmware.verify_versions(link, [image for image, _verb in self.steps], self.log.emit)
        except (firmware.FirmwareError, p.ProtocolError, p.ForbiddenCommand, TransportError, OSError) as exc:
            error = exc
        finally:
            if self.transport is not None:
                self.transport.close()
        self.finished_with.emit(error)


class FirmwareDialog(QtWidgets.QDialog):
    """Update Firmware. Reads the ECU without the normal connect, checks everything before the ECU is touched and refuses anything doubtful."""

    VERBS = {"Upgrade": "Upgrading", "Downgrade": "Downgrading", "Reinstall": "Reinstalling"}
    COLOURS = {"green": "#1b7f3b", "red": "#b3261e", "grey": "#666666"}

    def __init__(self, window, uri: str) -> None:
        super().__init__(window)
        self.setWindowTitle("Update Firmware")
        self.resize(560, 480)
        self.window_, self.uri = window, uri
        self.target: firmware.EcuTarget | None = None
        self.package: firmware.FirmwarePackage | None = None
        self.selection: firmware.Selection | None = None
        self.notes_read = False
        self.programming = False
        self.files = QtWidgets.QListWidget()
        self.files.currentRowChanged.connect(self._selected)
        self.browse_button = QtWidgets.QPushButton("Browse…")
        self.browse_button.clicked.connect(self.browse)
        self.message = QtWidgets.QLabel("")
        self.message.setWordWrap(True)
        self.memo = QtWidgets.QPlainTextEdit()
        self.memo.setReadOnly(True)
        self.progress = QtWidgets.QProgressBar()
        self.start = QtWidgets.QPushButton("Read Notes First")
        self.start.setEnabled(False)
        self.start.clicked.connect(self.clicked)
        self.close_button = QtWidgets.QPushButton("Close")
        self.close_button.clicked.connect(self.reject)
        self.version_label = QtWidgets.QLabel("")
        column = QtWidgets.QVBoxLayout(self)
        column.addWidget(self.version_label)
        column.addWidget(self.files)
        column.addWidget(self.browse_button)
        column.addWidget(self.message)
        column.addWidget(self.memo)
        column.addWidget(self.progress)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.start)
        row.addWidget(self.close_button)
        column.addLayout(row)
        self.ready = self._read_ecu()
        if not self.ready:
            return
        from .assets import asset

        folder = asset("Firmware")
        for path in sorted(folder.glob("*.lfp")) if folder else []:
            self._add_file(path)

    def _log(self, text: str) -> None:
        self.memo.appendPlainText(text)

    def _read_ecu(self) -> bool:
        from . import protocol as p

        buttons = MESSAGE_BUTTONS
        trap = lambda: QtWidgets.QMessageBox.question(  # noqa: E731
            self, "Update Firmware",
            "Error:  ECU is running in.....TRAP MODE\nAn exception has occurred and the engine will not start in this mode.\n"
            "Do you wish to continue with update to install new firmware?",
        ) == buttons.Yes
        retry = lambda: QtWidgets.QMessageBox.warning(  # noqa: E731
            self, "Update Firmware", firmware.NO_CONNECTION_TEXT, buttons.Retry | buttons.Cancel,
        ) == buttons.Retry
        try:
            transport = open_transport(self.uri)
        except (TransportError, OSError) as exc:
            QtWidgets.QMessageBox.warning(self, "Update Firmware", f"Failed to connect to hardware\n\n{exc}")
            return False
        try:
            link = p.Link(transport)
            state, comms_in_boot = firmware.check_state(link, self._log, trap, retry)
            self.target = firmware.read_target(link, state, comms_in_boot)
        except firmware.UpdateCancelled as exc:
            self._log(str(exc))
            return False
        except (p.ProtocolError, TransportError, OSError) as exc:
            QtWidgets.QMessageBox.warning(self, "Update Firmware", f"Failed to connect to hardware\n\n{exc}")
            return False
        finally:
            transport.close()
        for line in self.target.log:
            self._log(line)
        found = self.target.main
        self.version_label.setText(f"ECU version: {firmware.version_text(found) if found else 'not available'}"
                                   + (f"    Product: {self.target.product}" if self.target.product else "    Product: not available"))
        if not found:
            self._log("ECU Firmware information not available")
        self._log("\nRead the version notes before proceeding")
        return True

    def _add_file(self, path: Path) -> None:
        item = QtWidgets.QListWidgetItem(path.name)
        item.setData(ID_ROLE, str(path))
        self.files.addItem(item)
        self.files.setCurrentItem(item)

    def browse(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Firmware File", "", "Firmware File (*.img *.lfp)")
        if path:
            self._add_file(Path(path))

    def _is_image_file(self) -> bool:
        return bool(self.package and self.package.path and self.package.path.suffix.lower() == ".img")

    def images(self) -> list[firmware.FirmwareImage]:
        if self.package is None:
            return []
        if self._is_image_file():
            return list(self.package.images.values())
        return self.package.images_for(self.target.product)

    def _show(self, text: str, colour: str) -> None:
        self.message.setText(text)
        self.message.setStyleSheet(f"color:{self.COLOURS[colour]}")

    def _selected(self, row: int) -> None:
        self.package = self.selection = None
        self.start.setEnabled(False)
        if row < 0 or self.target is None:
            return
        try:
            self.package = firmware.load(Path(self.files.item(row).data(ID_ROLE)), self.window_.database.firmware_key)
        except (firmware.FirmwareError, OSError) as exc:
            self._show(f"This file cannot be used: {exc}", "red")
            return
        bare = self._is_image_file()
        self.selection = firmware.describe(self.package, self.images(), self.target, bare)
        self._show(self.selection.text, self.selection.colour)
        self.start.setText("Read Notes First" if bare and not self.notes_read else self.selection.button)
        self.start.setEnabled(self.selection.enabled)

    def clicked(self) -> None:
        selection = self.selection
        if selection is None or not selection.enabled or self.programming:
            return
        if not self.notes_read:
            self.notes_read = True
            self.window_.show_topic("ecu_firmware_version_information")
            self.start.setText(selection.button)
            return
        warnings = [
            "Have you read the firmware version notes?",
            "Warning : Programming will overwrite your existing firmware\nthough tuning information will be retained.\n\n"
            "You may be required to configure any new features after\nthis upgrade to ensure continued ECU operation.\n\n"
            "Under no circumstances should this process be interrupted.\nPlease leave ignition switch ON until update is complete.\n\n"
            "Save a copy of your tune first. Do you want to continue with update?",
        ]
        for text in warnings:
            if QtWidgets.QMessageBox.warning(self, "Update Firmware", text, MESSAGE_BUTTONS.Yes | MESSAGE_BUTTONS.No) != MESSAGE_BUTTONS.Yes:
                self._log("User cancelled update")
                return
            self._log(text)
        if selection.base_timing:
            QtWidgets.QMessageBox.warning(self, "Update Firmware", firmware.BASE_TIMING_TEXT)
            self._log(firmware.BASE_TIMING_TEXT)
        self.programming = True
        for widget in (self.start, self.files, self.browse_button, self.close_button):
            widget.setEnabled(False)
        steps = [(image, self.VERBS[word]) for image, word in selection.verdicts]
        self.worker = FirmwareWorker(self.uri, steps)
        self.worker.progress.connect(self._progress)
        self.worker.log.connect(self._log)
        self.worker.finished_with.connect(self._finished)
        self.worker.start()

    def _progress(self, sent: int, total: int) -> None:
        self.progress.setRange(0, total)
        self.progress.setValue(sent)

    def _finished(self, error) -> None:
        self.programming = False
        for widget in (self.files, self.browse_button, self.close_button):
            widget.setEnabled(True)
        self.start.setEnabled(bool(self.selection and self.selection.enabled))
        if error is None:
            text = "PROGRAMMING COMPLETE.\n\nECU is ready to run.\n\nIf the ECU reports Safe Mode, perform a Store and cycle its power."
            self._show(text, "green")
            QtWidgets.QMessageBox.information(self, "Update Firmware", text)
            return
        text = str(error) if isinstance(error, (firmware.EngineRunning, firmware.VersionMismatch)) else f"{firmware.FAILED_TEXT}\n\n{error}"
        self._show(text, "red")
        self._log(text)
        QtWidgets.QMessageBox.critical(self, "Firmware Updater Error", text)


class _CurrentFrame(QtWidgets.QStyledItemDelegate):
    """Outlines the section the cursor is in, in the marker colour, as PCLink does."""

    def paint(self, painter, option, index) -> None:
        super().paint(painter, option, index)
        if index.siblingAtColumn(0).data(CURRENT_ROLE):
            painter.save()
            painter.setPen(QtGui.QColor(230, 40, 40))
            box = option.rect.adjusted(0, 0, -1, -1)
            painter.drawLine(box.topLeft(), box.topRight())
            painter.drawLine(box.bottomLeft(), box.bottomRight())
            if index.column() == 0:
                painter.drawLine(box.topLeft(), box.bottomLeft())
            if index.column() == index.model().columnCount() - 1:
                painter.drawLine(box.topRight(), box.bottomRight())
            painter.restore()


CURRENT_ROLE = QtCore.Qt.ItemDataRole.UserRole + 1


def parse_clock(text: str) -> float | None:
    """m:ss.mmm, or plain seconds, back to seconds."""
    try:
        if ":" in text:
            minutes, rest = text.rsplit(":", 1)
            return int(minutes) * 60 + float(rest)
        return float(text)
    except ValueError:
        return None


class LogFileManager(QtWidgets.QWidget):
    """Open logs and their sections, with the same actions as the Logging menu.

    Each log is a row with its duration; its sections show duration, an overlay tick and the overlay's time offset."""

    COLUMNS = ("Name", "Duration", "", "Offset")

    def __init__(self, window) -> None:
        super().__init__()
        self.window_ = window
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(list(self.COLUMNS))
        self.tree.setRootIsDecorated(False)
        self.tree.setIndentation(8)
        self.tree.setItemDelegate(_CurrentFrame(self.tree))
        self.tree.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        for column, width in ((1, 62), (2, 22), (3, 56)):
            self.tree.setColumnWidth(column, width)
        self.tree.itemClicked.connect(self._clicked)
        self.tree.itemDoubleClicked.connect(self._double_clicked)
        self.tree.itemChanged.connect(self._changed)
        style = self.style()
        icons = QtWidgets.QStyle.StandardPixmap
        actions = (
            ("Open Log File", icons.SP_DialogOpenButton, lambda: window.open_log()),
            ("Save Log File", icons.SP_DialogSaveButton, lambda: window.save_log_file()),
            ("Save All Log Files", icons.SP_DriveFDIcon, lambda: window.save_all_logs()),
            ("Close Log File", icons.SP_DialogCloseButton, lambda: window.close_log_file()),
            ("Close All Log Files", icons.SP_DialogDiscardButton, lambda: window.close_all_logs()),
            ("Log File Details", icons.SP_FileDialogDetailedView, lambda: window.log_details()),
            ("Export", icons.SP_ArrowRight, lambda: window.export_log()),
            ("Refresh", icons.SP_BrowserReload, self.refresh),
            ("Play", icons.SP_MediaPlay, lambda: window.toggle_play()),
        )
        rows = [QtWidgets.QHBoxLayout(), QtWidgets.QHBoxLayout()]
        for number, (title, icon, action) in enumerate(actions):
            button = QtWidgets.QToolButton()
            button.setIcon(style.standardIcon(icon))
            button.setToolTip(title)
            button.setAutoRaise(True)
            button.clicked.connect(lambda _checked=False, run=action: run())
            rows[0 if number < 5 else 1].addWidget(button)
        for row in rows:
            row.addStretch()
        column = QtWidgets.QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(2)
        for row in rows:
            column.addLayout(row)
        column.addWidget(self.tree)
        window.log_session.changed.connect(self.refresh)
        window.log_session.cursor_moved.connect(self._mark_current)
        window.log_session.range_changed.connect(self._mark_current)

    def _log_duration(self, log) -> float:
        low, high = log.time_range()
        return high - low

    def refresh(self) -> None:
        session = self.window_.log_session
        self.tree.blockSignals(True)
        self.tree.clear()
        grey = QtGui.QBrush(QtGui.QColor(190, 190, 190))
        for index, log in enumerate(session.logs):
            name = log.details.get("Name") or (log.path.name if log.path else "Unsaved log")
            item = QtWidgets.QTreeWidgetItem(self.tree, [("* " if log.modified else "") + name, hms(self._log_duration(log)), "", ""])
            item.setData(0, ID_ROLE, (index, None))
            for column in range(4):
                item.setBackground(column, grey)
                item.setForeground(column, QtGui.QBrush(QtGui.QColor(0, 0, 0)))
                font = item.font(column)
                font.setBold(True)
                item.setFont(column, font)
            item.setToolTip(0, str(log.path or ""))
            for section in log.sections():
                key = (id(log), section.start)
                child = QtWidgets.QTreeWidgetItem(item, [section.name, hms(section.end - section.start), "", hms(session.offsets.get(key, 0.0))])
                child.setData(0, ID_ROLE, (index, section))
                child.setFlags(child.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable | QtCore.Qt.ItemFlag.ItemIsEditable)
                child.setCheckState(2, QtCore.Qt.CheckState.Checked if key in session.overlaid else QtCore.Qt.CheckState.Unchecked)
            item.setExpanded(True)
        self.tree.blockSignals(False)
        self._mark_current()

    def _mark_current(self) -> None:
        session = self.window_.log_session
        current = session.section_at()
        for top in range(self.tree.topLevelItemCount()):
            parent = self.tree.topLevelItem(top)
            for row in range(parent.childCount()):
                child = parent.child(row)
                index, section = child.data(0, ID_ROLE)
                child.setData(0, CURRENT_ROLE, index == session.active and section is not None and section == current)
        self.tree.viewport().update()

    def _clicked(self, item: QtWidgets.QTreeWidgetItem, column: int) -> None:
        index, section = item.data(0, ID_ROLE)
        session = self.window_.log_session
        if index != session.active:
            session.select(index)
        elif section and column != 2:
            session.set_range(section.start, section.end)
            session.set_cursor(section.start)

    def _double_clicked(self, item: QtWidgets.QTreeWidgetItem, column: int) -> None:
        index, section = item.data(0, ID_ROLE)
        if section is not None and column == 3:
            self.tree.editItem(item, 3)

    def _changed(self, item: QtWidgets.QTreeWidgetItem, column: int) -> None:
        index, section = item.data(0, ID_ROLE)
        session = self.window_.log_session
        if section is None or index >= len(session.logs):
            return
        log = session.logs[index]
        key = (id(log), section.start)
        if column == 2:
            on = item.checkState(2) == QtCore.Qt.CheckState.Checked
            QtCore.QTimer.singleShot(0, lambda: session.set_overlay(log, section, on))
        elif column == 3:
            seconds = parse_clock(item.text(3).strip())
            if seconds is not None:
                QtCore.QTimer.singleShot(0, lambda: session.set_overlay(log, section, key in session.overlaid, seconds))


class EventLogView(QtWidgets.QWidget):
    """Messages since starting, with the buttons PCLink puts here."""

    def __init__(self, clear_faults=None) -> None:
        super().__init__()
        self.list = QtWidgets.QListWidget()
        clear = QtWidgets.QPushButton("Clr Event Log")
        clear.clicked.connect(self.list.clear)
        self.clear_faults = QtWidgets.QPushButton("Clr ECU Fault Codes")
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.clear_faults)
        row.addWidget(clear)
        column = QtWidgets.QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.addLayout(row)
        column.addWidget(self.list)

    def add(self, text: str, important: bool = False) -> None:
        item = QtWidgets.QListWidgetItem(f"[{time.strftime('%I:%M:%S %p').lstrip('0')}] {text}")
        if important:
            item.setForeground(QtGui.QColor(220, 40, 40))
        self.list.setWordWrap(True)
        self.list.addItem(item)
        self.list.scrollToBottom()


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


def load_runtime_layout(name: str) -> Layout | None:
    from .assets import asset

    path = asset(name)
    if path is None:
        return None
    try:
        return Layout.load(path)
    except LayoutError:
        return None

