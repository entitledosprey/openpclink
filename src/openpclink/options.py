"""PCLink's options: what they hold, where they persist, and their dialog."""
from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from PySide6 import QtWidgets

from .importer import default_db_path
from .units import CHOICES, DEFAULT_IMPERIAL, DEFAULT_METRIC, GROUP_TITLES


def default_options_path() -> Path:
    override = os.environ.get("OPENPCLINK_OPTIONS")
    return Path(override) if override else default_db_path().with_name("options.json")


def documents_dir() -> Path:
    return Path.home() / ("Documents" if sys.platform == "darwin" else "") / "OpenPCLink"


@dataclass
class Options:
    start_full_screen: bool = False
    hide_runtime_values: bool = True
    search_on_enter: bool = False
    auto_save_layout: bool = True
    windows_font: str = ""
    view_font: str = ""
    system: str = "Metric"
    metric: dict = field(default_factory=lambda: dict(DEFAULT_METRIC))
    imperial: dict = field(default_factory=lambda: dict(DEFAULT_IMPERIAL))
    use_map_dir: bool = True
    use_log_dir: bool = True
    use_layout_dir: bool = True
    map_dir: str = ""
    log_dir: str = ""
    layout_dir: str = ""
    theme: str = "Black"
    themes: dict = field(default_factory=dict)
    language: str = "English"
    connection_manual: bool = False
    connection_port: str = ""
    baud_rate: int = 38400
    slow_connection: bool = False
    log_settings: dict = field(default_factory=dict)
    recent_layout: str = ""
    recent_map: str = ""

    @classmethod
    def load(cls, path: Path | None = None) -> "Options":
        path = path or default_options_path()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        known = {name: value for name, value in raw.items() if name in cls.__dataclass_fields__}
        return cls(**known)

    def save(self, path: Path | None = None) -> None:
        path = path or default_options_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=1), encoding="utf-8")

    def unit_choice(self) -> dict[str, str]:
        return self.imperial if self.system == "Imperial" else self.metric

    def folder(self, kind: str) -> str:
        """The folder a file dialog opens in. Empty when the option is off."""
        if not getattr(self, f"use_{kind}_dir"):
            return ""
        return getattr(self, f"{kind}_dir") or str(documents_dir() / {"map": "Maps", "log": "Logs", "layout": "Layouts"}[kind])


class _UnitsTable(QtWidgets.QGroupBox):
    """A radio group for each kind of value, as on PCLink's Units tab."""

    def __init__(self, title: str, choice: dict[str, str]) -> None:
        super().__init__(title)
        self.buttons: dict[str, dict[str, QtWidgets.QRadioButton]] = {}
        grid = QtWidgets.QGridLayout(self)
        for at, (group, options) in enumerate(CHOICES.items()):
            box = QtWidgets.QGroupBox(GROUP_TITLES[group])
            column = QtWidgets.QVBoxLayout(box)
            self.buttons[group] = {}
            for name in options:
                button = QtWidgets.QRadioButton(name)
                button.setChecked(choice.get(group) == name)
                column.addWidget(button)
                self.buttons[group][name] = button
            grid.addWidget(box, at // 3, at % 3)

    def value(self) -> dict[str, str]:
        return {group: next((name for name, button in row.items() if button.isChecked()), "") for group, row in self.buttons.items()}


class OptionsDialog(QtWidgets.QDialog):
    """PCLink's tabbed options: Connection, Preferences, Units and Directories."""

    TABS = ("Connection", "Preferences", "Units", "Directories")

    def __init__(self, options: Options, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Options")
        self.options = options
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(self._connection_tab(), "Connection")
        self.tabs.addTab(self._preferences_tab(), "Preferences")
        self.tabs.addTab(self._units_tab(), "Units")
        self.tabs.addTab(self._directories_tab(), "Directories")
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        column = QtWidgets.QVBoxLayout(self)
        column.addWidget(self.tabs)
        column.addWidget(buttons)

    def show_tab(self, name: str) -> None:
        self.tabs.setCurrentIndex(self.TABS.index(name))

    def _connection_tab(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(page)
        self.mode_auto = QtWidgets.QRadioButton("Auto")
        self.mode_manual = QtWidgets.QRadioButton("Manual")
        (self.mode_manual if self.options.connection_manual else self.mode_auto).setChecked(True)
        mode = QtWidgets.QGroupBox("Connection Mode")
        row = QtWidgets.QVBoxLayout(mode)
        row.addWidget(self.mode_manual)
        row.addWidget(self.mode_auto)
        self.port = QtWidgets.QLineEdit(self.options.connection_port)
        self.port.setPlaceholderText("Auto, or serial:/dev/x, ftdi:7068, udp:192.168.4.1")
        self.baud = QtWidgets.QComboBox()
        self.baud.addItems(["9600", "19200", "38400", "57600", "115200"])
        self.baud.setCurrentText(str(self.options.baud_rate))
        self.slow = QtWidgets.QCheckBox("Slow Connection")
        self.slow.setChecked(self.options.slow_connection)
        form.addRow(mode)
        form.addRow("Connection Port", self.port)
        form.addRow("Baud Rate", self.baud)
        form.addRow(self.slow)
        return page

    def _preferences_tab(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        column = QtWidgets.QVBoxLayout(page)
        self.full_screen = QtWidgets.QCheckBox("Start in Full Screen Mode")
        self.full_screen.setChecked(self.options.start_full_screen)
        self.hide_runtime = QtWidgets.QCheckBox("Hide Runtime Values on Settings")
        self.hide_runtime.setChecked(self.options.hide_runtime_values)
        self.search_enter = QtWidgets.QCheckBox("Search on Enter key press")
        self.search_enter.setChecked(self.options.search_on_enter)
        self.auto_save = QtWidgets.QCheckBox("Automatically Save Layout")
        self.auto_save.setChecked(self.options.auto_save_layout)
        for box in (self.full_screen, self.hide_runtime, self.search_enter, self.auto_save):
            column.addWidget(box)
        self.windows_font = self.options.windows_font
        self.view_font = self.options.view_font
        self.windows_sample = self._font_group(column, "Windows Font", "windows_font")
        self.view_sample = self._font_group(column, "View Font", "view_font")
        column.addStretch()
        return page

    def _font_group(self, column: QtWidgets.QVBoxLayout, title: str, attribute: str) -> QtWidgets.QLabel:
        from PySide6 import QtGui

        box = QtWidgets.QGroupBox(title)
        inner = QtWidgets.QVBoxLayout(box)
        button = QtWidgets.QPushButton("Font")
        sample = QtWidgets.QLabel("Sample 123")
        if getattr(self, attribute):
            font = QtGui.QFont()
            font.fromString(getattr(self, attribute))
            sample.setFont(font)

        def choose() -> None:
            font, chosen = QtWidgets.QFontDialog.getFont(sample.font(), self, title)
            if chosen:
                sample.setFont(font)
                setattr(self, attribute, font.toString())

        button.clicked.connect(choose)
        inner.addWidget(button)
        inner.addWidget(sample)
        column.addWidget(box)
        return sample

    def _units_tab(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        column = QtWidgets.QVBoxLayout(page)
        self.metric_units = _UnitsTable("Metric", self.options.metric)
        self.imperial_units = _UnitsTable("Imperial", self.options.imperial)
        column.addWidget(self.metric_units)
        column.addWidget(self.imperial_units)
        return page

    def _directories_tab(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        form = QtWidgets.QVBoxLayout(page)
        self.folders = {}
        for kind, title in (("map", "Basemaps"), ("log", "Log Files"), ("layout", "Layout Files")):
            box = QtWidgets.QGroupBox(title)
            inner = QtWidgets.QGridLayout(box)
            use = QtWidgets.QCheckBox("Use this directory")
            use.setChecked(getattr(self.options, f"use_{kind}_dir"))
            path = QtWidgets.QLineEdit(self.options.folder(kind) or getattr(self.options, f"{kind}_dir"))
            browse = QtWidgets.QPushButton("…")
            browse.clicked.connect(lambda _checked=False, edit=path: self._browse(edit))
            inner.addWidget(use, 0, 0, 1, 2)
            inner.addWidget(path, 1, 0)
            inner.addWidget(browse, 1, 1)
            form.addWidget(box)
            self.folders[kind] = (use, path)
        form.addStretch()
        return page

    def _browse(self, edit: QtWidgets.QLineEdit) -> None:
        chosen = QtWidgets.QFileDialog.getExistingDirectory(self, "Choose folder", edit.text())
        if chosen:
            edit.setText(chosen)

    def _accept(self) -> None:
        o = self.options
        o.connection_manual = self.mode_manual.isChecked()
        o.connection_port = self.port.text().strip()
        o.baud_rate = int(self.baud.currentText())
        o.slow_connection = self.slow.isChecked()
        o.start_full_screen = self.full_screen.isChecked()
        o.hide_runtime_values = self.hide_runtime.isChecked()
        o.search_on_enter = self.search_enter.isChecked()
        o.auto_save_layout = self.auto_save.isChecked()
        o.windows_font, o.view_font = self.windows_font, self.view_font
        o.metric, o.imperial = self.metric_units.value(), self.imperial_units.value()
        for kind, (use, path) in self.folders.items():
            setattr(o, f"use_{kind}_dir", use.isChecked())
            setattr(o, f"{kind}_dir", path.text().strip())
        self.accept()
