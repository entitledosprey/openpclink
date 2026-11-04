"""The Parameters panel (Setup ECU Parameters): search, save and load of display settings, and Properties."""
from __future__ import annotations

from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from .assets import RuntimeConfig, user_config_path

ID_ROLE = QtCore.Qt.ItemDataRole.UserRole
FILTER = "Runtime Configuration Files (*.rtcr)"
DEFAULT_NAME = "DefaultParameterConfig.rtcr"


class ParametersPanel(QtWidgets.QWidget):
    """The live parameter tree with PCLink's toolbar over it."""

    def __init__(self, window, tree: QtWidgets.QTreeWidget) -> None:
        super().__init__()
        self.window_ = window
        self.tree = tree
        self.search = QtWidgets.QLineEdit()
        self.search.textChanged.connect(self.filter)
        self.sort = QtWidgets.QToolButton()
        self.sort.setText("A-Z")
        self.sort.setCheckable(True)
        self.sort.setToolTip("Sort alphabetically")
        self.sort.toggled.connect(self.tree.setSortingEnabled)
        save = QtWidgets.QToolButton()
        save.setText("Save")
        save.setToolTip("Save parameter display settings")
        save.clicked.connect(self.save)
        load = QtWidgets.QToolButton()
        load.setText("Load")
        load.setToolTip("Load parameter display settings")
        load.clicked.connect(self.load)
        self.save_button, self.load_button = save, load
        row = QtWidgets.QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        for widget in (self.sort, self.search, save, load):
            row.addWidget(widget)
        column = QtWidgets.QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.addLayout(row)
        column.addWidget(self.tree)
        self.tree.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._menu)

    def filter(self, text: str) -> None:
        needle = text.strip().lower()

        def walk(item: QtWidgets.QTreeWidgetItem) -> bool:
            leaf = isinstance(item.data(0, ID_ROLE), int)
            shown = not needle or (leaf and needle in item.text(0).lower())
            for index in range(item.childCount()):
                shown = walk(item.child(index)) or shown
            item.setHidden(not shown)
            if needle and not leaf:
                item.setExpanded(shown)
            return shown

        for index in range(self.tree.topLevelItemCount()):
            walk(self.tree.topLevelItem(index))

    def _menu(self, point: QtCore.QPoint) -> None:
        item = self.tree.itemAt(point)
        if item is None or not isinstance(item.data(0, ID_ROLE), int):
            return
        menu = QtWidgets.QMenu(self)
        menu.addAction("What's This?", lambda: self._whats_this(item.data(0, ID_ROLE)))
        menu.addAction("Properties", lambda: self.properties(item.data(0, ID_ROLE)))
        menu.exec(self.tree.viewport().mapToGlobal(point))

    def _whats_this(self, data_id: int) -> None:
        url = self.window_.database.parameter(data_id).help_url
        if url:
            self.window_.show_topic(url)
        else:
            self.window_.statusBar().showMessage("No manual page for this value.", 3000)

    def properties(self, data_id: int) -> bool:
        from . import tools

        window = self.window_
        parameter = window.database.parameter(data_id)
        kind = window.kind_of(parameter)
        units = window.units
        theme = window.theme
        log = window.log_session.log
        series = next((s for s in log.series if s.data_id == data_id), None) if log else None
        log_info = (1.0 / series.period if series and series.period else 0.0, len(series.values)) if series else None
        decimals = units.decimals(parameter.decimals if parameter.decimals is not None else 1, kind)
        dialog = tools.ParameterSetupDialog(
            parameter.caption, window.display_units(parameter), window.runtime_config.get(data_id), window,
            [theme.parameter_colour(i) for i in range(len(theme.parameters))], decimals, log_info,
            (lambda value: units.to_display(value, kind), lambda value: units.to_base(value, kind)),
        )
        if not dialog.exec():
            return False
        window.runtime_config.entries[data_id] = dialog.result_display()
        self._persist()
        window.workspace.refresh_live()
        return True

    def _persist(self) -> None:
        try:
            self.window_.runtime_config.save(user_config_path())
        except OSError as exc:
            self.window_.statusBar().showMessage(f"Could not save parameter settings: {exc}")

    def save(self) -> None:
        folder = Path(self.window_.options.folder("layout"))
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save Parameter Settings", str(folder / DEFAULT_NAME), FILTER)
        if path:
            self.window_.options.remember("layout", path)
            self.window_.runtime_config.save(Path(path))

    def load(self) -> None:
        folder = Path(self.window_.options.folder("layout"))
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Load Parameter Settings", str(folder), FILTER)
        if not path:
            return
        self.window_.options.remember("layout", path)
        config = RuntimeConfig.load(Path(path))
        if not config.entries:
            self.window_.show_error("That file holds no parameter settings.")
            return
        self.window_.runtime_config = config
        self._persist()
        self.window_.workspace.refresh_live()
