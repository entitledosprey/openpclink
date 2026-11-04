"""Properties dialog for live views: which values they show, and how a gauge or list is set up."""
from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

from .gauges import GAUGE_TYPES, MULTIPLIER_TEXT, GaugeSettings, gauge_colour, to_gauge_colour
from .layoutfile import GROUP_ALL, LayoutView

ID_ROLE = QtCore.Qt.ItemDataRole.UserRole
# How many values a view takes; the Digital Gauge and the list take as many as are chosen.
VALUE_LIMITS = {"Bar Gauge": 1, "Dial Gauge": 1, "Curved Bar": 1, "Curve Gauge": 1, "Target Gauge": 2}


class ValuesTab(QtWidgets.QWidget):
    """TRuntimeViewDataIdsPropertiesFrame: values always visible, and groups that show with a focused table."""

    def __init__(self, view: LayoutView, choices: list[tuple[int, str]], tables: list[tuple[int, str]]) -> None:
        super().__init__()
        self.view_type = view.type
        self.names = dict(choices)
        self.table_names = dict(tables)
        self.groups = {key: list(ids) for key, ids in view.groups.items()}
        self.groups.setdefault(GROUP_ALL, [])
        self.available = QtWidgets.QListWidget()
        self.search = QtWidgets.QLineEdit()
        self.all = QtWidgets.QRadioButton("All")
        self.tables_only = QtWidgets.QRadioButton("Tables")
        self.all.setChecked(True)
        filter_box = QtWidgets.QGroupBox("Filter")
        row = QtWidgets.QHBoxLayout(filter_box)
        row.addWidget(self.all)
        row.addWidget(self.tables_only)
        top = QtWidgets.QHBoxLayout()
        top.addWidget(self.search, 1)
        top.addWidget(filter_box)
        left = QtWidgets.QVBoxLayout()
        left.addLayout(top)
        left.addWidget(self.available, 1)
        self.group_list = QtWidgets.QListWidget()
        self.group_list.setMaximumHeight(110)
        self.remove_group = QtWidgets.QPushButton("Remove")
        self.visible_label = QtWidgets.QLabel()
        self.visible = QtWidgets.QListWidget()
        self.add = QtWidgets.QPushButton("▶")
        up, down, remove = QtWidgets.QPushButton("▲"), QtWidgets.QPushButton("▼"), QtWidgets.QPushButton("Remove")
        buttons = QtWidgets.QHBoxLayout()
        buttons.addWidget(up)
        buttons.addWidget(down)
        buttons.addStretch()
        buttons.addWidget(remove)
        right = QtWidgets.QVBoxLayout()
        right.addWidget(QtWidgets.QLabel("Table Visibility Groups"))
        right.addWidget(self.group_list)
        right.addWidget(self.remove_group, 0, QtCore.Qt.AlignmentFlag.AlignRight)
        right.addWidget(self.visible_label)
        right.addWidget(self.visible, 1)
        right.addLayout(buttons)
        column = QtWidgets.QHBoxLayout(self)
        column.addLayout(left, 3)
        column.addWidget(self.add)
        column.addLayout(right, 3)
        self.search.textChanged.connect(self._fill_available)
        self.all.toggled.connect(self._fill_available)
        self.group_list.currentRowChanged.connect(self._show_group)
        self.add.clicked.connect(self._add)
        self.available.itemDoubleClicked.connect(lambda _item: self._add())
        remove.clicked.connect(self._remove)
        up.clicked.connect(lambda: self._move(-1))
        down.clicked.connect(lambda: self._move(1))
        self.remove_group.clicked.connect(self._delete_group)
        self._fill_groups()
        self._fill_available()

    def _group_title(self, key: int) -> str:
        return "Always Visible" if key == GROUP_ALL else self.table_names.get(key, str(key))

    def _fill_groups(self, select: int = GROUP_ALL) -> None:
        self.group_list.blockSignals(True)
        self.group_list.clear()
        keys = [GROUP_ALL] + sorted(key for key in self.groups if key != GROUP_ALL)
        for key in keys:
            item = QtWidgets.QListWidgetItem(self._group_title(key))
            item.setData(ID_ROLE, key)
            self.group_list.addItem(item)
        self.group_list.setCurrentRow(keys.index(select) if select in keys else 0)
        self.group_list.blockSignals(False)
        self._show_group()

    def current_key(self) -> int:
        item = self.group_list.currentItem()
        return item.data(ID_ROLE) if item else GROUP_ALL

    def _show_group(self, _row: int = 0) -> None:
        key = self.current_key()
        self.visible_label.setText("Parameters Always Visible" if key == GROUP_ALL else f"Visible with {self._group_title(key)}")
        self.visible.clear()
        for data_id in self.groups.get(key, []):
            item = QtWidgets.QListWidgetItem(self.names.get(data_id, str(data_id)))
            item.setData(ID_ROLE, data_id)
            self.visible.addItem(item)
        self.remove_group.setEnabled(key != GROUP_ALL)

    def _fill_available(self, _arg=None) -> None:
        self.available.clear()
        text = self.search.text().lower()
        source = self.names if self.all.isChecked() else self.table_names
        for data_id, name in sorted(source.items(), key=lambda pair: pair[1]):
            if text in name.lower():
                item = QtWidgets.QListWidgetItem(name)
                item.setData(ID_ROLE, data_id)
                self.available.addItem(item)

    def _add(self) -> None:
        picked = [item.data(ID_ROLE) for item in self.available.selectedItems()]
        if not picked:
            return
        if not self.all.isChecked():
            for table in picked:
                if table in self.groups:
                    QtWidgets.QMessageBox.information(self, "Runtime Values", "Group already in list.")
                    return
                self.groups[table] = []
            self._fill_groups(picked[-1])
            return
        key = self.current_key()
        ids = self.groups.setdefault(key, [])
        limit = VALUE_LIMITS.get(self.view_type)
        skipped = False
        for data_id in picked:
            if limit is not None and len(ids) >= limit:
                skipped = True
                break
            if any(data_id in other for other in self.groups.values()):
                QtWidgets.QMessageBox.information(self, "Runtime Values", "Parameter already has visibility control.")
                continue
            ids.append(data_id)
        if skipped:
            QtWidgets.QMessageBox.information(self, "Runtime Values", "No more parameters allowed for this view type.")
        self._show_group()

    def _remove(self) -> None:
        row = self.visible.currentRow()
        if row >= 0:
            del self.groups[self.current_key()][row]
            self._show_group()
            self.visible.setCurrentRow(min(row, self.visible.count() - 1))

    def _move(self, step: int) -> None:
        row, ids = self.visible.currentRow(), self.groups[self.current_key()]
        if 0 <= row + step < len(ids) and row >= 0:
            ids[row], ids[row + step] = ids[row + step], ids[row]
            self._show_group()
            self.visible.setCurrentRow(row + step)

    def _delete_group(self) -> None:
        key = self.current_key()
        if key == GROUP_ALL:
            return
        sure = QtWidgets.QMessageBox.question(self, "Delete Group", "Are you sure you want to delete the selected visibilty group?")
        if sure == QtWidgets.QMessageBox.StandardButton.Yes:
            del self.groups[key]
            self._fill_groups()

    def result(self) -> dict[int, list[int]]:
        return {key: ids for key, ids in self.groups.items() if key == GROUP_ALL or ids is not None}


class GaugeSetupTab(QtWidgets.QWidget):
    """TGaugeViewPropertiesFrame: caption, anti-aliasing and the range, warnings and ticks."""

    def __init__(self, settings: GaugeSettings) -> None:
        super().__init__()
        self.settings = settings
        self.colour = settings.colour
        self.caption = QtWidgets.QLineEdit(settings.caption)
        self.smooth = QtWidgets.QCheckBox("Anti-aliased Graphics")
        self.smooth.setChecked(settings.high_quality)
        general = QtWidgets.QGroupBox("General")
        form = QtWidgets.QHBoxLayout(general)
        form.addWidget(QtWidgets.QLabel("Caption"))
        form.addWidget(self.caption, 1)
        form.addSpacing(30)
        form.addWidget(self.smooth)
        self.automatic = QtWidgets.QRadioButton("Use parameter Display Min/Max")
        self.manual = QtWidgets.QRadioButton("Manual")
        self.manual.setChecked(settings.manual)
        self.automatic.setChecked(not settings.manual)
        mode = QtWidgets.QGroupBox("Settings Mode")
        row = QtWidgets.QHBoxLayout(mode)
        row.addWidget(self.automatic)
        row.addWidget(self.manual)
        self.fields: dict[str, QtWidgets.QDoubleSpinBox] = {}
        grid = QtWidgets.QGridLayout()
        entries = (
            ("minimum", "Minimum", 0, 0), ("maximum", "Maximum", 0, 1), ("small_ticks", "Small Divisions", 1, 0), ("large_ticks", "Large Divisions", 1, 1),
            ("low_yellow", "Low Soft Warning Value", 2, 0), ("yellow", "High Soft Warning Value", 2, 1),
            ("low_red", "Low Hard Warning Value", 3, 0), ("red", "High Hard Warning Value", 3, 1), ("decimals", "Decimal Places", 4, 0),
        )
        for name, title, r, c in entries:
            spin = QtWidgets.QDoubleSpinBox()
            whole = name in ("small_ticks", "large_ticks", "decimals")
            spin.setRange({"small_ticks": 1, "large_ticks": 1, "decimals": 0}.get(name, -1e6), {"small_ticks": 10, "large_ticks": 20, "decimals": 3}.get(name, 1e6))
            spin.setDecimals(0 if whole else 3)
            spin.setValue(float(getattr(settings, name)))
            self.fields[name] = spin
            grid.addWidget(QtWidgets.QLabel(title), r * 2, c)
            grid.addWidget(spin, r * 2 + 1, c)
        self.multiplier = QtWidgets.QComboBox()
        self.multiplier.addItems(MULTIPLIER_TEXT)
        self.multiplier.setCurrentIndex(settings.multiplier)
        grid.addWidget(QtWidgets.QLabel("Multiplier"), 8, 1)
        grid.addWidget(self.multiplier, 9, 1)
        self.swatch = QtWidgets.QPushButton()
        self.swatch.clicked.connect(self._choose_colour)
        self._paint_swatch()
        grid.addWidget(QtWidgets.QLabel("Colour"), 10, 0)
        grid.addWidget(self.swatch, 11, 0, 1, 2)
        self.visual = QtWidgets.QWidget()
        self.visual.setLayout(grid)
        group = QtWidgets.QGroupBox("Visual")
        inner = QtWidgets.QVBoxLayout(group)
        inner.addWidget(mode)
        inner.addWidget(self.visual)
        inner.addStretch()
        column = QtWidgets.QVBoxLayout(self)
        column.addWidget(general)
        column.addWidget(group, 1)
        self.manual.toggled.connect(self.visual.setVisible)
        self.visual.setVisible(settings.manual)

    def _paint_swatch(self) -> None:
        self.swatch.setStyleSheet(f"background:{gauge_colour(self.colour).name()};min-height:22px")

    def _choose_colour(self) -> None:
        chosen = QtWidgets.QColorDialog.getColor(gauge_colour(self.colour), self, "Colour")
        if chosen.isValid():
            self.colour = to_gauge_colour(chosen)
            self._paint_swatch()

    def apply(self) -> None:
        s = self.settings
        s.caption, s.high_quality, s.manual = self.caption.text(), self.smooth.isChecked(), self.manual.isChecked()
        for name, spin in self.fields.items():
            setattr(s, name, int(spin.value()) if name in ("small_ticks", "large_ticks", "decimals") else spin.value())
        s.multiplier, s.colour = self.multiplier.currentIndex(), self.colour


class ListSetupTab(QtWidgets.QWidget):
    """TRuntimeValueListViewPropertiesFrame: title, column count, how values fill the columns, bold text."""

    def __init__(self, view: LayoutView) -> None:
        super().__init__()
        attributes = view.attributes
        self.title = QtWidgets.QLineEdit(attributes.get("Caption", ""))
        self.columns = QtWidgets.QSpinBox()
        self.columns.setRange(1, 10)
        self.columns.setValue(int(float(attributes.get("ColumnsCount", "1") or 1)))
        self.across = QtWidgets.QRadioButton("Sort Across")
        self.down = QtWidgets.QRadioButton("Sort Down")
        (self.down if attributes.get("SortMode") == "1" else self.across).setChecked(True)
        self.bold = QtWidgets.QCheckBox("Bold Text")
        self.bold.setChecked(attributes.get("BoldFont") == "true")
        values = QtWidgets.QGroupBox("Values")
        row = QtWidgets.QHBoxLayout(values)
        row.addWidget(self.across)
        row.addWidget(self.down)
        form = QtWidgets.QFormLayout(self)
        form.addRow("Title", self.title)
        form.addRow("Column Count", self.columns)
        form.addRow(values)
        form.addRow(self.bold)

    def apply(self, view: LayoutView) -> None:
        view.attributes.update(
            Caption=self.title.text(), ColumnsCount=str(self.columns.value()), SortMode="1" if self.down.isChecked() else "0",
            BoldFont="true" if self.bold.isChecked() else "false",
        )
        if not self.title.text():
            view.attributes.pop("CaptionStrId", None)


class ViewPropertiesDialog(QtWidgets.QDialog):
    """Properties for a live view: Runtime Values, and Gauge Setup (or list setup)."""

    def __init__(self, view: LayoutView, choices: list[tuple[int, str]], tables: list[tuple[int, str]], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Properties")
        self.resize(560, 520)
        self.view = view
        self.values = ValuesTab(view, choices, tables)
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(self.values, "Runtime Values")
        self.gauge = self.list_setup = None
        if view.type in GAUGE_TYPES and view.type != "Digital Gauge":
            self.gauge = GaugeSetupTab(GaugeSettings.from_attributes(view.attributes))
            self.tabs.addTab(self.gauge, "Gauge Setup")
        elif view.type == "Runtime Value List":
            self.list_setup = ListSetupTab(view)
            self.tabs.addTab(self.list_setup, "Runtime Value List")
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel | QtWidgets.QDialogButtonBox.StandardButton.Help)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        column = QtWidgets.QVBoxLayout(self)
        column.addWidget(self.tabs)
        column.addWidget(buttons)

    def apply(self) -> None:
        self.view.groups = self.values.result()
        if self.gauge is not None:
            self.gauge.apply()
            self.view.attributes.update(self.gauge.settings.attributes())
        if self.list_setup is not None:
            self.list_setup.apply(self.view)
