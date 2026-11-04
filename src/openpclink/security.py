"""Password protection, ECU Unlock and Compare: what the menus show when locked, and the dialogs and state around them."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6 import QtCore, QtWidgets

from .mapfile import MapEntry, MapFile

# What PCLink leaves in each menu while the ECU is locked (seen in PCLink 7.8.2). Everything else is hidden.
LOCKED_MENUS: dict[str, frozenset[str] | None] = {
    "File": frozenset({"Open", "Save", "Save as", "Save as Encrypted", "Exit"}),
    "Options": frozenset({"Connection", "Preferences", "Language", "Setup ECU Parameters"}),
    "ECU Controls": frozenset({
        "Connect", "Disconnect", "ECU Log File Download", "Store", "Restore to Factory Settings",
        "Setup Password Protection", "Unlock Password Protection", "TPS Setup", "MAP Sensor Calibration",
    }),
    "Help": None,
}

LOCKED_STORE = "Unable to Store, Password Protection Enabled"
LOCKED_FILE = "The file is locked to a different ECU serial number."
NOT_OPENED = "Unable to open file named:"
DOWNLOAD_FAILED = "Download failed due to communications error!"
ENGINE_RUNNING = "Operation cannot be completed while engine is running"
G5_FILE_ON_G4X = "You have a G4X ECU connected and have selected a G5 Basemap.\n Select OK to disconnect the ECU before opening the file."
G4X_FILE_ON_G5 = "You have a G5 ECU connected and have selected a G4X Basemap.\n Select OK to disconnect the ECU before opening the file."
NEWER_SETTINGS = "This ECU contains settings that will not be set up by this file. Check the settings before running.\nDo you wish to continue?"
G5_LINK_ID = 50000


def _plain(text: str) -> str:
    return text.replace("…", "").replace("...", "").replace("&", "").strip()


def apply_lock_menus(menubar: QtWidgets.QMenuBar, locked: bool) -> None:
    """Hide every menu, and every item, that PCLink hides while the ECU is locked, and restore them after."""
    hidden: list = getattr(menubar, "_lock_hidden", [])
    for action in hidden:
        action.setVisible(True)
    hidden = []
    if locked:
        for menu_action in menubar.actions():
            menu = menu_action.menu()
            name = _plain(menu_action.text())
            if menu is None:
                continue
            if name not in LOCKED_MENUS:
                menu_action.setVisible(False)
                hidden.append(menu_action)
                continue
            hidden += _filter(menu, LOCKED_MENUS[name])
    menubar._lock_hidden = hidden


def _filter(menu: QtWidgets.QMenu, allowed: frozenset[str] | None) -> list:
    hidden = []
    if allowed is None:
        return hidden
    seen = False
    shown_separator = None
    for action in menu.actions():
        if action.isSeparator():
            if seen:
                shown_separator, seen = action, False
            else:
                action.setVisible(False)
                hidden.append(action)
        elif _plain(action.text()) in allowed:
            seen = True
        else:
            action.setVisible(False)
            hidden.append(action)
    if not seen and shown_separator is not None:
        shown_separator.setVisible(False)
        hidden.append(shown_separator)
    return hidden


class EcuUnlockDialog(QtWidgets.QDialog):
    """PCLink's ECU Unlock box: the features, the serial number, a code to send, and Help, Unlock and Close.

    The dialog only collects the code. The ECU decides whether it is right.
    """

    unlock_requested = QtCore.Signal(str)

    def __init__(self, serial: int, features: list[tuple[str, bool]], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("ECU Unlock")
        group = QtWidgets.QGroupBox("Unlocked Features")
        column = QtWidgets.QVBoxLayout(group)
        self.feature_boxes: list[QtWidgets.QCheckBox] = []
        for caption, enabled in features:
            box = QtWidgets.QCheckBox(caption)
            box.setChecked(enabled)
            box.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            box.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
            column.addWidget(box)
            self.feature_boxes.append(box)
        column.addStretch(1)
        self.serial = QtWidgets.QLineEdit(str(serial))
        self.serial.setReadOnly(True)
        self.code = QtWidgets.QLineEdit()
        self.code.setMaxLength(32)
        side = QtWidgets.QFormLayout()
        side.addRow("ECU Serial Number", self.serial)
        side.addRow("Unlock Code", self.code)
        top = QtWidgets.QHBoxLayout()
        top.addWidget(group, 1)
        top.addLayout(side, 1)
        buttons = QtWidgets.QDialogButtonBox()
        self.help_button = buttons.addButton("Help", QtWidgets.QDialogButtonBox.ButtonRole.HelpRole)
        self.unlock_button = buttons.addButton("Unlock", QtWidgets.QDialogButtonBox.ButtonRole.ActionRole)
        self.close_button = buttons.addButton("Close", QtWidgets.QDialogButtonBox.ButtonRole.RejectRole)
        self.unlock_button.clicked.connect(lambda: self.unlock_requested.emit(self.code.text()))
        self.close_button.clicked.connect(self.reject)
        outer = QtWidgets.QVBoxLayout(self)
        outer.addLayout(top)
        outer.addWidget(buttons)


@dataclass
class CompareState:
    """The map file Compare is showing beside the live values."""

    map: MapFile
    path: Path

    def entry(self, data_id: int) -> MapEntry | None:
        return self.map.entries.get(data_id)
