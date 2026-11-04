"""PCLink's WiFi selection form: the ECUs that answered a Discover broadcast, to pick one."""
from __future__ import annotations

from PySide6 import QtCore, QtWidgets

from .transports import discover_wifi


class WifiSelectionDialog(QtWidgets.QDialog):
    def __init__(self, parent: QtWidgets.QWidget | None = None, search=discover_wifi) -> None:
        super().__init__(parent)
        self.setWindowTitle("Search For ECU")
        self.search = search
        self.found: list[tuple[str, str]] = []
        self.status = QtWidgets.QLabel()
        self.list = QtWidgets.QListWidget()
        self.refresh_button = QtWidgets.QPushButton("Refresh")
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.addButton(self.refresh_button, QtWidgets.QDialogButtonBox.ButtonRole.ActionRole)
        self.ok_button = buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok)
        column = QtWidgets.QVBoxLayout(self)
        column.addWidget(self.list)
        column.addWidget(self.status)
        column.addWidget(buttons)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.refresh_button.clicked.connect(self.refresh)
        self.list.itemDoubleClicked.connect(lambda _item: self.accept())
        self.resize(360, 260)
        QtCore.QTimer.singleShot(0, self.refresh)

    def refresh(self) -> None:
        self.status.setText("Refreshing...")
        QtWidgets.QApplication.processEvents()
        self.found = self.search()
        self.list.clear()
        for address, name in self.found:
            self.list.addItem(f"{address}  {name}".strip())
        if self.found:
            self.list.setCurrentRow(0)
        self.status.setText("Refreshed" if self.found else "No ECUs found")
        self.ok_button.setEnabled(bool(self.found))

    def chosen(self) -> str | None:
        row = self.list.currentRow()
        return f"udp:{self.found[row][0]}" if 0 <= row < len(self.found) else None
