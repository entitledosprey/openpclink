"""Properties dialogs for the log views, and the colour range editor."""
from __future__ import annotations

import colorsys
import xml.etree.ElementTree as ET
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from .gauges import tcolor, to_tcolor
from .logviews import POINT_STYLES, Histogram, LoggedValues, GpsPlot, Navigator, ParameterList, StatisticsView, TimePlot, XYPlot, XYSettings

BUTTONS = QtWidgets.QDialogButtonBox.StandardButton


def default_colours(count: int) -> list[QtGui.QColor]:
    """Green through yellow to red, the way PCLink's own colour ranges start out."""
    out = []
    for i in range(count):
        hue = (1 - i / max(count - 1, 1)) / 3
        r, g, b = colorsys.hsv_to_rgb(hue, 1.0, 1.0)
        out.append(QtGui.QColor(round(r * 255), round(g * 255), round(b * 255)))
    return out


def build_spans(low: float, high: float, colours: list[QtGui.QColor]) -> list[tuple[float, float, QtGui.QColor]]:
    step = (high - low) / max(len(colours), 1)
    return [(low + i * step, low + (i + 1) * step, colour) for i, colour in enumerate(colours)]


def interpolate(colours: list[QtGui.QColor], rows: list[int]) -> list[QtGui.QColor]:
    """A smooth blend between the first and last chosen colours."""
    if len(rows) < 3:
        return colours
    first, last = colours[rows[0]], colours[rows[-1]]
    out = list(colours)
    for step, row in enumerate(rows):
        t = step / (len(rows) - 1)
        out[row] = QtGui.QColor(*(round(a + (b - a) * t) for a, b in zip(first.getRgb()[:3], last.getRgb()[:3])))
    return out


def save_colour_range(path: Path, name: str, spans) -> None:
    root = ET.Element("LinkStorage", {"Version": "100", "Type": ".clr"})
    colour_range = ET.SubElement(root, "ColorRange", {"Name": name})
    ranges = ET.SubElement(colour_range, "ColorRanges")
    for low, high, colour in spans:
        ET.SubElement(ranges, "ColorSpan", {"LowerBound": f"{low:g}", "UpperBound": f"{high:g}", "Color": str(to_tcolor(colour))})
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def load_colour_range(path: Path) -> tuple[str, list]:
    root = ET.parse(path).getroot()
    colour_range = root.find(".//ColorRange")
    name = colour_range.get("Name", "Unnamed") if colour_range is not None else "Unnamed"
    spans = [(float(s.get("LowerBound", "0")), float(s.get("UpperBound", "0")), tcolor(int(s.get("Color", "0")))) for s in root.iter("ColorSpan")]
    if not spans:
        raise ValueError("that file holds no colour range")
    return name, spans


class ColourRangeDialog(QtWidgets.QDialog):
    """Colour Range Setup: min, max, divisions, or take them from a parameter; double-click a colour to change it."""

    def __init__(self, session, spans, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Colour Range Setup")
        self.session = session
        self.name = "Unnamed"
        self.low = QtWidgets.QDoubleSpinBox()
        self.high = QtWidgets.QDoubleSpinBox()
        self.divisions = QtWidgets.QSpinBox()
        for spin in (self.low, self.high):
            spin.setRange(-1e9, 1e9)
            spin.setDecimals(3)
        self.divisions.setRange(2, 32)
        self.parameter = QtWidgets.QComboBox()
        self.parameter.addItem("(manual)", 0)
        log = session.log
        if log is not None:
            for series in log.series:
                if series.data_id and series.times:
                    self.parameter.addItem(series.name, series.data_id)
        self.table = QtWidgets.QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["From", "Colour"])
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.cellDoubleClicked.connect(self._pick)
        self.colours: list[QtGui.QColor] = []
        if spans:
            self.low.setValue(spans[0][0])
            self.high.setValue(spans[-1][1])
            self.divisions.setValue(len(spans))
            self.colours = [colour for _l, _h, colour in spans]
        else:
            self.low.setValue(0)
            self.high.setValue(100)
            self.divisions.setValue(8)
            self.colours = default_colours(8)
        self.parameter.currentIndexChanged.connect(self._from_parameter)
        self.divisions.valueChanged.connect(self._divide)
        self.low.valueChanged.connect(self._fill)
        self.high.valueChanged.connect(self._fill)
        blend = QtWidgets.QPushButton("Interpolate Selected")
        blend.clicked.connect(self._blend)
        save = QtWidgets.QPushButton("Save to File")
        save.clicked.connect(self._save)
        load = QtWidgets.QPushButton("Load from File")
        load.clicked.connect(self._load)
        form = QtWidgets.QFormLayout()
        form.addRow("Parameter", self.parameter)
        form.addRow("Minimum", self.low)
        form.addRow("Maximum", self.high)
        form.addRow("Divisions", self.divisions)
        buttons = QtWidgets.QHBoxLayout()
        for button in (blend, save, load):
            buttons.addWidget(button)
        box = QtWidgets.QDialogButtonBox(BUTTONS.Ok | BUTTONS.Cancel)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        column = QtWidgets.QVBoxLayout(self)
        column.addLayout(form)
        column.addWidget(self.table)
        column.addLayout(buttons)
        column.addWidget(box)
        self._fill()

    def _fill(self) -> None:
        spans = self.spans()
        self.table.setRowCount(len(spans))
        for row, (low, _high, colour) in enumerate(spans):
            self.table.setItem(row, 0, QtWidgets.QTableWidgetItem(f"{low:g}"))
            swatch = QtWidgets.QTableWidgetItem("")
            swatch.setBackground(colour)
            self.table.setItem(row, 1, swatch)

    def _divide(self, count: int) -> None:
        self.colours = default_colours(count)
        self._fill()

    def _from_parameter(self) -> None:
        data_id = self.parameter.currentData()
        log = self.session.log
        index = log.channel_of(data_id) if log and data_id else None
        if index is None:
            return
        values = [self.session.display(log.series[index], v) for v in log.series[index].values]
        self.low.blockSignals(True)
        self.high.blockSignals(True)
        self.low.setValue(min(values))
        self.high.setValue(max(values))
        self.low.blockSignals(False)
        self.high.blockSignals(False)
        self._fill()

    def _pick(self, row: int, _column: int) -> None:
        colour = QtWidgets.QColorDialog.getColor(self.colours[row], self)
        if colour.isValid():
            self.colours[row] = colour
            self._fill()

    def _blend(self) -> None:
        rows = sorted({index.row() for index in self.table.selectedIndexes()})
        self.colours = interpolate(self.colours, rows)
        self._fill()

    def _save(self) -> None:
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save Colour Range", "", "Colour Range (*.clr)")
        if path:
            save_colour_range(Path(path), self.name, self.spans())

    def _load(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Load Colour Range", "", "Colour Range (*.clr);;All Files (*)")
        if not path:
            return
        try:
            self.name, spans = load_colour_range(Path(path))
        except (OSError, ValueError, ET.ParseError) as exc:
            QtWidgets.QMessageBox.warning(self, "Colour Range", str(exc))
            return
        self.colours = [colour for _l, _h, colour in spans]
        self.low.setValue(spans[0][0])
        self.high.setValue(spans[-1][1])
        self.divisions.blockSignals(True)
        self.divisions.setValue(len(spans))
        self.divisions.blockSignals(False)
        self._fill()

    def spans(self) -> list[tuple[float, float, QtGui.QColor]]:
        return build_spans(self.low.value(), self.high.value(), self.colours[: self.divisions.value()] or default_colours(self.divisions.value()))


class ViewPropertiesDialog(QtWidgets.QDialog):
    """Right click > Properties: the parameters on the view and its Display tab."""

    def __init__(self, body, parent=None) -> None:
        super().__init__(parent)
        self.body = body
        self.session = body.session
        self.setWindowTitle("Properties")
        self.resize(480, 460)
        self.tabs = QtWidgets.QTabWidget()
        self.rows: list[list[int]] = [[data_id, group] for data_id, group in body.view.log_parameters]
        self.tabs.addTab(self._parameters_tab(), "Parameters")
        self.tabs.addTab(self._display_tab(), "Display")
        box = QtWidgets.QDialogButtonBox(BUTTONS.Ok | BUTTONS.Cancel)
        box.accepted.connect(self._ok)
        box.rejected.connect(self.reject)
        column = QtWidgets.QVBoxLayout(self)
        column.addWidget(self.tabs)
        column.addWidget(box)

    # ---- parameters ----------------------------------------------------

    def _name(self, data_id: int) -> str:
        log = self.session.log
        index = log.channel_of(data_id) if log else None
        return log.names[index] if index is not None else self.session.describe(data_id)

    def _parameters_tab(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        self.list = QtWidgets.QListWidget()
        self.grouped = isinstance(self.body, (TimePlot, XYPlot, Histogram))
        add = QtWidgets.QPushButton("Add Parameter")
        add.clicked.connect(self._add)
        group = QtWidgets.QPushButton("Add Group")
        group.clicked.connect(self._add_group)
        group.setVisible(isinstance(self.body, TimePlot))
        remove = QtWidgets.QPushButton("Remove")
        remove.clicked.connect(self._remove)
        up = QtWidgets.QPushButton("Up")
        up.clicked.connect(lambda: self._move(-1))
        down = QtWidgets.QPushButton("Down")
        down.clicked.connect(lambda: self._move(1))
        row = QtWidgets.QHBoxLayout()
        for button in (add, group, remove, up, down):
            row.addWidget(button)
        column = QtWidgets.QVBoxLayout(page)
        column.addWidget(self.list)
        column.addLayout(row)
        self._fill_list()
        return page

    def _fill_list(self) -> None:
        self.list.clear()
        for data_id, group in self.rows:
            self.list.addItem(f"Group {group + 1}: {self._name(data_id)}" if self.grouped else self._name(data_id))

    def _add(self) -> None:
        from .tools import ParameterChooser

        log = self.session.log
        choices = [(series.data_id, series.name) for series in log.series if series.data_id] if log else []
        dialog = ParameterChooser("Select Parameter", choices, [], self)
        if not dialog.exec():
            return
        group = self.rows[self.list.currentRow()][1] if self.list.currentRow() >= 0 and not isinstance(self.body, TimePlot) else max((g for _, g in self.rows), default=-1) + 1
        for data_id in dialog.ids():
            self.rows.append([data_id, group if not isinstance(self.body, TimePlot) else group])
            if isinstance(self.body, TimePlot):
                group += 1
        self._fill_list()

    def _add_group(self) -> None:
        from .tools import ParameterChooser

        log = self.session.log
        choices = [(series.data_id, series.name) for series in log.series if series.data_id] if log else []
        dialog = ParameterChooser("Select Parameters for the Group", choices, [], self)
        if dialog.exec() and dialog.ids():
            group = max((g for _, g in self.rows), default=-1) + 1
            self.rows.extend([data_id, group] for data_id in dialog.ids())
            self._fill_list()

    def _remove(self) -> None:
        row = self.list.currentRow()
        if row >= 0:
            del self.rows[row]
            self._fill_list()

    def _move(self, step: int) -> None:
        row = self.list.currentRow()
        target = row + step
        if row < 0 or not 0 <= target < len(self.rows):
            return
        self.rows[row], self.rows[target] = self.rows[target], self.rows[row]
        self._fill_list()
        self.list.setCurrentRow(target)

    # ---- display -------------------------------------------------------

    def _check(self, text: str, state: bool) -> QtWidgets.QCheckBox:
        box = QtWidgets.QCheckBox(text)
        box.setChecked(state)
        return box

    def _display_tab(self) -> QtWidgets.QWidget:
        body, view = self.body, self.body.view
        page = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(page)
        self.caption = QtWidgets.QLineEdit(view.attributes.get("Caption", ""))
        form.addRow("Caption", self.caption)
        self.zoom_linked = self._check("Global Zoom" if not isinstance(body, StatisticsView) else "Common Time Range", body.linked("ZoomLinked"))
        self.cursor_linked = self._check("Global Cursor", body.linked("CursorLinked"))
        form.addRow(self.zoom_linked)
        if not isinstance(body, (StatisticsView, ParameterList, LoggedValues)):
            form.addRow(self.cursor_linked)
        elif not isinstance(body, StatisticsView):
            form.addRow(self.cursor_linked)
        self.checks: dict[str, QtWidgets.QCheckBox] = {}
        self.group_checks: dict[str, QtWidgets.QCheckBox] = {}
        if isinstance(body, (TimePlot, Navigator)):
            self.mode = QtWidgets.QComboBox()
            self.mode.addItems(["Stacked", "Overlapped"])
            self.mode.setCurrentIndex(body.display_mode())
            form.addRow("Display Mode", self.mode)
            for name, text in (("ShowOverlays", "Overlays"), ("XAxisVisible", "X Axis Component"), ("XScrollBarVisible", "X Axis Scroll Bar Component"), ("MarkersVisible", "Markers"), ("OffsetAxisVisible", "Offset Axis")):
                self.checks[name] = self._check(text, body.option(name))
                form.addRow(self.checks[name])
            for name, text in (("YAxisVisible", "Y Axis Component"), ("YScrollBarVisible", "Y Axis Scroll Bar Component")):
                self.group_checks[name] = self._check(text, body.group_flag(name))
                form.addRow(self.group_checks[name])
        elif isinstance(body, (XYPlot, Histogram, GpsPlot)):
            self._plot_settings(form)
        return page

    def _plot_settings(self, form: QtWidgets.QFormLayout) -> None:
        body = self.body
        s = body.settings
        log = self.session.log
        names = [(series.name, series.data_id) for series in log.series if series.data_id] if log else []

        def combo(current: int) -> QtWidgets.QComboBox:
            box = QtWidgets.QComboBox()
            box.addItem("(none)", 0)
            for name, data_id in names:
                box.addItem(name, data_id)
            at = box.findData(current)
            box.setCurrentIndex(max(at, 0))
            return box

        def spin(value: float) -> QtWidgets.QDoubleSpinBox:
            box = QtWidgets.QDoubleSpinBox()
            box.setRange(-1e9, 1e9)
            box.setDecimals(3)
            box.setValue(value)
            return box

        if not isinstance(body, GpsPlot):
            self.mode = QtWidgets.QComboBox()
            self.mode.addItems(["Tiled", "Overlapped"])
            self.mode.setCurrentIndex(body.display_mode())
            form.addRow("Display Mode", self.mode)
        if isinstance(body, XYPlot):
            self.x_param = combo(s.x_id)
            form.addRow("X Parameter", self.x_param)
        self.manual = self._check("X Manual Range" if not isinstance(body, GpsPlot) else "Longitude Manual Range", s.manual)
        self.x_min, self.x_max = spin(s.manual_min), spin(s.manual_max)
        form.addRow(self.manual)
        form.addRow("X Min", self.x_min)
        form.addRow("X Max", self.x_max)
        self.y_manual = self._check("Y Manual Range", s.y_manual)
        self.y_min, self.y_max = spin(s.y_min), spin(s.y_max)
        form.addRow(self.y_manual)
        form.addRow("Y Min", self.y_min)
        form.addRow("Y Max", self.y_max)
        if isinstance(body, Histogram):
            self.units = QtWidgets.QComboBox()
            self.units.addItems(["Samples", "% Time"])
            self.units.setCurrentText(body.y_units())
            form.addRow("Y Units", self.units)
        self.use_colour = self._check("Use Colour Range", s.use_colour)
        self.colour_param = combo(s.colour_id)
        self.legend = self._check("Show Colour Legend", s.show_legend)
        self.overlays = self._check("Overlays", s.show_overlays)
        edit = QtWidgets.QPushButton("Edit Colour Range…")
        edit.clicked.connect(self._edit_range)
        self.spans = tuple(s.spans)
        self.range_name = s.range_name
        form.addRow(self.use_colour)
        form.addRow("Colour Parameter", self.colour_param)
        form.addRow(edit)
        form.addRow(self.legend)
        form.addRow(self.overlays)
        if isinstance(body, (XYPlot, GpsPlot)):
            self.lines = self._check("Show Lines", s.show_lines)
            self.line_width = QtWidgets.QSpinBox()
            self.line_width.setRange(1, 10)
            self.line_width.setValue(s.line_width)
            self.points = self._check("Show Points", s.show_points)
            self.point_size = QtWidgets.QSpinBox()
            self.point_size.setRange(1, 10)
            self.point_size.setValue(s.point_size)
            self.point_style = QtWidgets.QComboBox()
            self.point_style.addItems(POINT_STYLES)
            self.point_style.setCurrentIndex(min(s.point_style, len(POINT_STYLES) - 1))
            form.addRow(self.lines)
            form.addRow("Line Width", self.line_width)
            form.addRow(self.points)
            form.addRow("Point Size", self.point_size)
            form.addRow("Point Style", self.point_style)

    def _edit_range(self) -> None:
        dialog = ColourRangeDialog(self.session, list(self.spans), self)
        dialog.name = self.range_name
        if dialog.exec():
            self.spans = tuple(dialog.spans())
            self.range_name = dialog.name
            at = self.colour_param.findData(dialog.parameter.currentData())
            if at > 0:
                self.colour_param.setCurrentIndex(at)

    # ---- apply ---------------------------------------------------------

    def _ok(self) -> None:
        body, view = self.body, self.body.view
        view.log_parameters = [(data_id, group) for data_id, group in self.rows]
        view.attributes["ZoomLinked"] = "true" if self.zoom_linked.isChecked() else "false"
        view.attributes["CursorLinked"] = "true" if self.cursor_linked.isChecked() else "false"
        if self.caption.text() or "Caption" in view.attributes:
            view.attributes["Caption"] = self.caption.text()
        if isinstance(body, (TimePlot, Navigator)):
            body.group_node().set("DisplayMode", str(self.mode.currentIndex()))
            for name, box in self.checks.items():
                body.node().set(name, "true" if box.isChecked() else "false")
            for name, box in self.group_checks.items():
                body.group_node().set(name, "true" if box.isChecked() else "false")
            body.sync_surfaces()
        elif isinstance(body, (XYPlot, Histogram, GpsPlot)):
            s = body.settings
            if not isinstance(body, GpsPlot):
                body.group_node().set("DisplayMode", str(self.mode.currentIndex()))
            if isinstance(body, XYPlot):
                s.x_id = self.x_param.currentData() or 0
            s.manual, s.manual_min, s.manual_max = self.manual.isChecked(), self.x_min.value(), self.x_max.value()
            s.y_manual, s.y_min, s.y_max = self.y_manual.isChecked(), self.y_min.value(), self.y_max.value()
            s.use_colour, s.colour_id = self.use_colour.isChecked(), self.colour_param.currentData() or 0
            s.show_legend, s.show_overlays = self.legend.isChecked(), self.overlays.isChecked()
            s.spans, s.range_name = tuple(self.spans), self.range_name
            if isinstance(body, Histogram):
                body.node().set("YUnits", self.units.currentText())
            if isinstance(body, (XYPlot, GpsPlot)):
                s.show_lines, s.line_width = self.lines.isChecked(), self.line_width.value()
                s.show_points, s.point_size, s.point_style = self.points.isChecked(), self.point_size.value(), self.point_style.currentIndex()
        self.accept()
