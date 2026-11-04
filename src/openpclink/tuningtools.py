"""Quick Tune, Quick Trim, Mixture Map setup and Knock Audio, laid out as PCLink lays them out."""
from __future__ import annotations

import struct
import time

from PySide6 import QtCore, QtGui, QtWidgets

from . import tune
from .gauges import tcolor
from .tools import ok_cancel
from .units import UnitSystem

Q = QtCore.Qt
GAUGE_LEFT, GAUGE_SPAN, GAUGE_TICKS = 91, 440, 50


def unit_system(window) -> UnitSystem:
    """The window's units with the ECU's stoichiometric ratio, and lambda forced when the fuel equation says so."""
    ecu = window.worker.ecu
    live = lambda data_id: ecu.get(data_id) if ecu is not None and data_id in ecu.table else 0.0  # noqa: E731
    return UnitSystem(window.options.unit_choice(), live(tune.STOICH_RATIO) or 14.7, live(tune.FUEL_EQUATION_MODE) == 2)


class TargetGauge(QtWidgets.QWidget):
    """The strip across the top of Quick Tune: measured above, target below, a bar joining them."""

    ZONES = ((0, 2, (255, 0, 0)), (3, 5, (255, 100, 0)), (6, 40, (100, 100, 100)), (41, 44, (255, 100, 0)), (45, 50, (255, 0, 0)))

    def __init__(self) -> None:
        super().__init__()
        self.low, self.high = 0.7, 1.2
        self.measured: float | None = None
        self.target: float | None = None
        self.setFixedSize(560, 57)

    def set_values(self, measured: float | None, target: float | None) -> None:
        self.measured, self.target = measured, target
        self.update()

    def _x(self, value: float) -> float:
        span = self.high - self.low
        fraction = min(max((value - self.low) / span, 0.0), 1.0) if span > 0 else 0.5
        return GAUGE_LEFT + GAUGE_SPAN * fraction

    def paintEvent(self, _event: QtGui.QPaintEvent) -> None:
        painter = QtGui.QPainter(self)
        painter.fillRect(self.rect(), QtGui.QColor(0, 0, 0))
        for n in range(GAUGE_TICKS + 1):
            colour = next(c for first, last, c in self.ZONES if first <= n <= last)
            painter.setPen(QtGui.QColor(*colour))
            x = round(GAUGE_LEFT + GAUGE_SPAN * n / GAUGE_TICKS)
            reach = 7 if n % 5 == 0 else 5
            painter.drawLine(x, 36 - reach, x, 36 + reach)
        middle = (self.low + self.high) / 2
        measured = middle if self.measured is None else self.measured
        target = middle if self.target is None else self.target
        yellow = QtGui.QColor(255, 255, 0)
        painter.setPen(Q.PenStyle.NoPen)
        painter.setBrush(yellow)
        a, b = self._x(measured), self._x(target)
        if self.measured is not None and self.target is not None:
            painter.drawRect(QtCore.QRectF(min(a, b), 10, abs(a - b), 10))
        painter.drawPolygon(QtGui.QPolygonF([QtCore.QPointF(a - 8, 0), QtCore.QPointF(a + 8, 0), QtCore.QPointF(a, 10)]))
        painter.drawPolygon(QtGui.QPolygonF([QtCore.QPointF(b - 8, 29), QtCore.QPointF(b + 8, 29), QtCore.QPointF(b, 19)]))


class RangeEdit(QtWidgets.QLineEdit):
    """A number box that refuses what is out of range with PCLink's own message."""

    changed = QtCore.Signal()

    def __init__(self, low: float, high: float, decimals: int) -> None:
        super().__init__()
        self.low, self.high, self.decimals = low, high, decimals
        self.number = low
        self.setAlignment(Q.AlignmentFlag.AlignRight | Q.AlignmentFlag.AlignVCenter)
        self.editingFinished.connect(self._finished)

    def value(self) -> float:
        return self.number

    def set_value(self, value: float) -> None:
        self.number = min(max(value, self.low), self.high)
        self.setText(f"{self.number:.{self.decimals}f}")

    def _finished(self) -> None:
        try:
            typed = float(self.text())
        except ValueError:
            typed = None
        if typed is None or not self.low <= typed <= self.high:
            QtWidgets.QMessageBox.information(self.window(), "Quick Tune", f"Please enter a value between {self.low:f} and {self.high:f}")
            self.setText(f"{self.number:.{self.decimals}f}")
            return
        if typed != self.number:
            self.number = typed
            self.setText(f"{typed:.{self.decimals}f}")
            self.changed.emit()


class QuickTuneWindow(QtWidgets.QWidget):
    """Quick Tune: corrects fuel table cells from the wideband while the engine holds a cell."""

    WIDEBAND_IDS = (3040, 3041, tune.LAMBDA_AVERAGE)
    WIDEBAND_NAMES = ("Lambda 1", "Lambda 2", "Lambda Average RTV")
    # name, title, column, kind of value, unit, low, high, decimals
    FIELDS = (
        ("filter", "Filter", 0, None, "", 1, 100, 0),
        ("gauge_minimum", "Gauge Minimum", 0, "Lambda", "", 6, 30, 1),
        ("gauge_maximum", "Gauge Maximum", 0, "Lambda", "", 6, 30, 1),
        ("precision", "Precision", 1, "LambdaSpan", "", 0, 10, 2),
        ("time_in_centre", "Time In Cell Center", 1, None, "s", 0, 5, 1),
        ("load_tolerance", "Load Center Tolerance", 1, None, "%", 1, 50, 0),
        ("rpm_tolerance", "RPM Center Tolerance", 1, None, "%", 1, 50, 0),
        ("tune_ahead_trim", "Tune Ahead Trim", 1, None, "%", -20, 20, 1),
        ("rpm_upper", "RPM Lockout Upper", 2, None, "RPM", 0, 15000, 0),
        ("rpm_lower", "RPM Lockout Lower", 2, None, "RPM", 0, 15000, 0),
        ("load_upper", "Load Lockout Upper", 2, "Pressure", "", -100, 600, 0),
        ("load_lower", "Load Lockout Lower", 2, "Pressure", "", -100, 600, 0),
        ("ect_lockout", "ECT lockout", 2, "Temperature", "", 0, 300, 0),
        ("max_attempts", "Max Auto Attempts", 2, None, "", 1, 10, 0),
    )

    def __init__(self, window) -> None:
        super().__init__(window, Q.WindowType.Tool | Q.WindowType.WindowStaysOnTopHint)
        self.window_ = window
        self.engine = tune.QuickTune()
        self.engine.settings.load_options(window.options.quick_tune)
        self.units = unit_system(window)
        self.last = None
        self.edits: dict[str, RangeEdit] = {}
        self.suffixes: dict[str, QtWidgets.QLabel] = {}
        self.gauge = TargetGauge()
        self.status = QtWidgets.QLabel("OFF")
        font = self.status.font()
        font.setBold(True)
        font.setPixelSize(20)
        self.status.setFont(font)
        self.status.setAlignment(Q.AlignmentFlag.AlignCenter)
        self.mode = QtWidgets.QButtonGroup(self)
        modes = QtWidgets.QGroupBox("Mode")
        row = QtWidgets.QHBoxLayout(modes)
        row.setContentsMargins(6, 0, 6, 2)
        for index, title in enumerate(("Off", "Manual", "Auto")):
            button = QtWidgets.QRadioButton(title)
            self.mode.addButton(button, index)
            row.addWidget(button)
        self.mode.button(tune.OFF).setChecked(True)
        self.mode.idClicked.connect(self._mode_changed)
        self.show_settings = QtWidgets.QCheckBox("Show Settings")
        self.tune_button = QtWidgets.QPushButton("Tune Cell (F10)")
        self.tune_button.setEnabled(False)
        self.tune_button.clicked.connect(self.press_tune)
        reset = QtWidgets.QPushButton("Reset Cell (F11)")
        reset.clicked.connect(self.reset_cell)
        help_button = QtWidgets.QPushButton("Help")
        help_button.clicked.connect(lambda: window.show_help() if hasattr(window, "show_help") else None)
        self.settings_box = self._settings_panel()
        self.show_settings.toggled.connect(self._settings_toggled)
        top = QtWidgets.QHBoxLayout()
        top.addWidget(modes)
        top.addWidget(self.status, 1)
        top.addWidget(self.show_settings)
        buttons = QtWidgets.QHBoxLayout()
        for button in (self.tune_button, reset, help_button):
            buttons.addWidget(button, 1)
        column = QtWidgets.QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 2)
        column.setSpacing(2)
        column.addWidget(self.gauge)
        for layout in (top, buttons):
            wrap = QtWidgets.QWidget()
            wrap.setLayout(layout)
            layout.setContentsMargins(4, 0, 4, 0)
            column.addWidget(wrap)
        column.addWidget(self.settings_box)
        self.show_settings.setChecked(bool(window.options.quick_tune.get("ShowSettings", False)))
        self.settings_box.setVisible(self.show_settings.isChecked())
        self.setFixedWidth(563)
        self.refresh_fields()

    # ---- settings panel ---------------------------------------------------

    def _settings_panel(self) -> QtWidgets.QGroupBox:
        box = QtWidgets.QGroupBox("Quick Tune Settings")
        grid = QtWidgets.QGridLayout(box)
        grid.setContentsMargins(8, 2, 8, 6)
        grid.setVerticalSpacing(0)
        s = self.engine.settings
        self.table_choice = QtWidgets.QComboBox()
        self.source_choice = QtWidgets.QComboBox()
        self.source_choice.addItems(self.WIDEBAND_NAMES)
        self.source_choice.setCurrentIndex(min(max(s.wideband_index, 0), 2))
        self.source_choice.setToolTip("Select the WB Oxy values that will be averaged to give the measured WB oxy value.")
        self.ahead_choice = QtWidgets.QComboBox()
        self.ahead_choice.addItems(tune.TUNE_AHEAD_OPTIONS)
        self.ahead_choice.setCurrentIndex(min(max(s.tune_ahead, 0), 4))
        self.table_choice.currentIndexChanged.connect(self._table_changed)
        self.source_choice.currentIndexChanged.connect(self._source_changed)
        self.ahead_choice.currentIndexChanged.connect(self._ahead_changed)
        rows = [0, 0, 0]

        def place(column: int, title: str, widget: QtWidgets.QWidget, suffix: QtWidgets.QLabel | None = None) -> None:
            label = QtWidgets.QLabel(title)
            grid.addWidget(label, rows[column], column * 2, 1, 2)
            inner = QtWidgets.QHBoxLayout()
            inner.setContentsMargins(0, 0, 0, 0)
            widget.setFixedWidth(120)
            inner.addWidget(widget)
            if suffix is not None:
                suffix.setFixedWidth(46)
                inner.addWidget(suffix)
            inner.addStretch(1)
            holder = QtWidgets.QWidget()
            holder.setLayout(inner)
            grid.addWidget(holder, rows[column] + 1, column * 2, 1, 2)
            rows[column] += 2

        place(0, "Fuel Table", self.table_choice)
        place(0, "Measured WB Oxy sources:", self.source_choice)
        for name, title, column, kind, unit, low, high, decimals in self.FIELDS:
            edit = RangeEdit(low, high, decimals)
            edit.changed.connect(lambda key=name: self._field_changed(key))
            self.edits[name] = edit
            self.suffixes[name] = QtWidgets.QLabel(unit)
            if name == "tune_ahead_trim":
                place(1, "Tune Ahead", self.ahead_choice)
            place(column, title, edit, self.suffixes[name])
        return box

    def _convert(self, kind: str | None):
        """(to display, to base, display unit) for a field's kind of value; each takes the number only."""
        units = self.units
        if kind is None:
            return None
        if kind == "LambdaSpan":
            zero = units.to_base(0.0, "Lambda")
            return (lambda v: units.span_to_display(v, "Lambda")), (lambda d: units.to_base(d, "Lambda") - zero), units.label("Lambda")
        return (lambda v: units.to_display(v, kind)), (lambda d: units.to_base(d, kind)), units.label(kind)

    def refresh_fields(self) -> None:
        """Fill every box from the settings in the units now chosen, and list the fuel tables the ECU offers."""
        self.units = unit_system(self.window_)
        s = self.engine.settings
        for name, title, column, kind, unit, low, high, decimals in self.FIELDS:
            edit, suffix = self.edits[name], self.suffixes[name]
            way = self._convert(kind)
            if way is None:
                edit.low, edit.high, edit.decimals = low, high, decimals
                edit.set_value(getattr(s, name))
                continue
            to_display, _to_base, label = way
            if kind == "Lambda":  # The limits are written as AFR.
                scale = 1 / self.units.stoich if label == "lambda" else 1.0
                edit.low, edit.high = low * scale, high * scale
            else:
                edit.low, edit.high = sorted((to_display(low), to_display(high)))
            edit.decimals = decimals if kind in ("Lambda", "LambdaSpan") else self.units.decimals(decimals, kind)
            suffix.setText(label)
            edit.set_value(to_display(getattr(s, name)))
        self._list_tables()
        self._gauge_range()

    def _list_tables(self) -> None:
        ecu = self.window_.worker.ecu
        live = lambda data_id: ecu.get(data_id) if ecu is not None and data_id in ecu.table else 0.0  # noqa: E731
        tables = [tune.FUEL_TABLE_1]
        if live(tune.DUAL_FUEL_MODE) != 0:
            tables.append(tune.FUEL_TABLE_2)
        self.table_choice.blockSignals(True)
        self.table_choice.clear()
        for data_id in tables:
            name = self.window_.describe(data_id)[0] if self.window_.database.known(data_id) else f"Fuel Table {len(self.table_choice) + 1}"
            self.table_choice.addItem(name, data_id)
        self.table_choice.setCurrentIndex(min(max(self.engine.settings.table_index, 0), len(tables) - 1))
        self.table_choice.blockSignals(False)
        self.setWindowTitle(f"Quick Tune {self.table_choice.currentText()}")

    def _field_changed(self, name: str) -> None:
        s = self.engine.settings
        kind = next(field[3] for field in self.FIELDS if field[0] == name)
        value = self.edits[name].value()
        way = self._convert(kind)
        if way is not None:
            value = way[1](value)
        current = getattr(s, name)
        setattr(s, name, int(value) if isinstance(current, int) else value)
        self._gauge_range()
        self.save_settings()

    def _gauge_range(self) -> None:
        s = self.engine.settings
        self.gauge.low, self.gauge.high = s.gauge_minimum, s.gauge_maximum
        self.gauge.update()

    def _table_changed(self, index: int) -> None:
        self.engine.settings.table_index = index
        self.setWindowTitle(f"Quick Tune {self.table_choice.currentText()}")
        self.save_settings()

    def _source_changed(self, index: int) -> None:
        self.engine.settings.wideband_index = index
        self.engine.samples.clear()
        self.save_settings()

    def _ahead_changed(self, index: int) -> None:
        self.engine.settings.tune_ahead = index
        self.save_settings()

    def _settings_toggled(self, shown: bool) -> None:
        self.settings_box.setVisible(shown)
        self.adjustSize()
        self.save_settings()

    def save_settings(self) -> None:
        saved = self.window_.options.quick_tune
        saved.update(self.engine.settings.to_options())
        saved["ShowSettings"] = self.show_settings.isChecked()
        self.window_.options.save(self.window_.options_path)

    # ---- running ------------------------------------------------------------

    def table_id(self) -> int:
        return self.table_choice.currentData() or tune.FUEL_TABLE_1

    def wideband_id(self) -> int:
        return self.WIDEBAND_IDS[self.source_choice.currentIndex()]

    def showEvent(self, event: QtGui.QShowEvent) -> None:
        self.mode.button(tune.OFF).setChecked(True)
        self._mode_changed(tune.OFF)
        self.engine.samples.clear()
        self.refresh_fields()
        self.adjustSize()
        super().showEvent(event)

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        self.save_settings()
        super().closeEvent(event)

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:
        if event.key() == Q.Key.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)

    def _mode_changed(self, mode: int) -> None:
        self.engine.mode = mode
        self.engine.running = False
        self.tune_button.setCheckable(mode == tune.AUTOMATIC)
        self.tune_button.setChecked(False)
        self.tune_button.setText("Start Tune (F10)" if mode == tune.AUTOMATIC else "Tune Cell (F10)")

    def set_mode(self, mode: int) -> None:
        self.mode.button(mode).setChecked(True)
        self._mode_changed(mode)

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
            self.set_mode(tune.MANUAL)
            return
        self.window_.marks.attempts.pop((self.table_id(), *self.engine.cell), None) if self.engine.cell else None
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
        live = lambda data_id: ecu.get(data_id) if data_id in ecu.table else 0.0  # noqa: E731
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
                    axis = _used(ecu.get_all(parameter.column_axis_id))
                    rpm_cell = tune.neighbour_bounds(axis, min(cell[1], len(axis) - 1))
                if rows > 1 and parameter.row_axis_id in ecu.table:
                    axis = _used(ecu.get_all(parameter.row_axis_id))
                    load_cell = tune.neighbour_bounds(axis, min(cell[0], len(axis) - 1))
                if parameter.row_source_id in ecu.table:
                    source = int(ecu.get(parameter.row_source_id))
                    load = live(source)
        active_id = {tune.FUEL_TABLE_1: tune.ACTIVE_FUEL_1, tune.FUEL_TABLE_2: tune.ACTIVE_FUEL_2}.get(table_id)
        active = active_id not in ecu.table or ecu.get(active_id) == 1
        wideband = live(self.wideband_id()) if self.wideband_id() in ecu.table else None
        return tune.TuneInputs(
            wideband, self._target(ecu, cell, rpm_cell, load), has_table, active, live(tune.ENGINE_SPEED), load,
            live(tune.ECT), rpm_cell, load_cell, cell, time.monotonic(),
        )

    def _target(self, ecu, cell, rpm_cell, load: float) -> float | None:
        """The runtime target when the ECU has it, otherwise the target table read at the engine's place."""
        if tune.LAMBDA_TARGET_RUNTIME in ecu.table:
            return ecu.get(tune.LAMBDA_TARGET_RUNTIME)
        table = self.window_.database.parameter(tune.LAMBDA_TARGET_TABLE) if self.window_.database.known(tune.LAMBDA_TARGET_TABLE) else None
        if table is None or tune.LAMBDA_TARGET_TABLE not in ecu.table:
            return None
        xs = _used(ecu.get_all(table.column_axis_id)) if table.column_axis_id in ecu.table else []
        ys = _used(ecu.get_all(table.row_axis_id)) if table.row_axis_id in ecu.table else []
        columns = table.columns if table.columns > 1 else max(len(xs), 1)
        raw = ecu.get_all(tune.LAMBDA_TARGET_TABLE)
        values = [raw[r * columns + c] for r in range(max(len(ys), 1)) for c in range(len(xs))]
        overlay = ecu.get(3114) if ecu.get(3113) == 1 and 3114 in ecu.table else 0.0
        return tune.lookup_target((xs, ys, values), ecu.get(tune.ENGINE_SPEED) if tune.ENGINE_SPEED in ecu.table else 0.0, load, overlay)

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
        if self.engine.mode == tune.OFF:
            self.gauge.set_values(None, None)
        else:
            self.gauge.set_values(self.engine.measured, self.engine.target)
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
            self.set_mode(tune.MANUAL)
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
    """Quick Trim: corrects the selected cells once, from a target, a reading and any closed loop trim.

    Values come in and go out as lambda; the boxes show the unit chosen in Options.
    PCLink also runs a Tune over the selection afterwards. For cells Quick Tune has measured that is a second
    correction from a stale reading, so it is left out here on purpose.
    """

    def __init__(self, target: float, measured: float, correction: float, parent=None, units: UnitSystem | None = None) -> None:
        super().__init__(parent)
        self.units = units or UnitSystem()
        label = self.units.label("Lambda")
        self.setWindowTitle("Quick Trim")
        grid = QtWidgets.QGridLayout(self)
        low, high = 5 / self.units.stoich if label == "lambda" else 5, 30 / self.units.stoich if label == "lambda" else 30
        decimals = self.units.decimals(3, "Lambda")
        self.target = self._spin(self.units.to_display(target, "Lambda"), low, high, decimals)
        self.measured = self._spin(self.units.to_display(measured, "Lambda"), low, high, decimals)
        self.correction = self._spin(correction, -50, 50, 1)
        for line, (title, spin, unit) in enumerate(((f"Target {label}", self.target, label), (f"Measured {label}", self.measured, label), ("CL Lambda  Fuel  Corr.", self.correction, "%"))):
            grid.addWidget(QtWidgets.QLabel(title), line, 0, Q.AlignmentFlag.AlignRight)
            grid.addWidget(spin, line, 1)
            grid.addWidget(QtWidgets.QLabel(unit), line, 2)
        buttons = ok_cancel(self)
        help_button = buttons.addButton(QtWidgets.QDialogButtonBox.StandardButton.Help)
        help_button.clicked.connect(lambda: parent.window().show_help() if parent is not None and hasattr(parent.window(), "show_help") else None)
        grid.addWidget(buttons, 3, 0, 1, 3)

    def target_value(self) -> float:
        return self.units.to_base(self.target.value(), "Lambda")

    def measured_value(self) -> float:
        return self.units.to_base(self.measured.value(), "Lambda")

    @staticmethod
    def _spin(value: float, low: float, high: float, decimals: int) -> QtWidgets.QDoubleSpinBox:
        spin = QtWidgets.QDoubleSpinBox()
        spin.setRange(low, high)
        spin.setDecimals(decimals)
        spin.setButtonSymbols(QtWidgets.QAbstractSpinBox.ButtonSymbols.NoButtons)
        spin.setAlignment(Q.AlignmentFlag.AlignRight)
        spin.setValue(value)
        return spin


class MixtureSetupDialog(QtWidgets.QDialog):
    """Mixture Map Setup, laid out as PCLink lays it out."""

    def __init__(self, settings: tune.MixtureSettings, tables: list[tuple[int, str]], choices: list[tuple[int, str]], parent=None, unit: str = "lambda", scale: float = 1.0) -> None:
        super().__init__(parent)
        self.setWindowTitle("Mixture Map Setup")
        self.settings = settings
        self.scale = scale
        self.setMinimumWidth(515)
        outer = QtWidgets.QVBoxLayout(self)
        outer.setSpacing(2)
        table_box = QtWidgets.QGroupBox("Fuel Table")
        row = QtWidgets.QHBoxLayout(table_box)
        self.table = QtWidgets.QComboBox()
        for data_id, name in tables:
            self.table.addItem(name, data_id)
        self.table.setCurrentIndex(max(self.table.findData(settings.table), 0))
        self.highlight = QtWidgets.QDoubleSpinBox()
        self.highlight.setRange(0.01 * scale, 10000 * scale)
        self.highlight.setDecimals(2)
        self.highlight.setButtonSymbols(QtWidgets.QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.highlight.setAlignment(Q.AlignmentFlag.AlignRight)
        self.highlight.setValue(settings.absolute_error * scale)
        row.addWidget(self.table, 1)
        row.addWidget(QtWidgets.QLabel("Highlight Range"))
        row.addWidget(self.highlight)
        row.addWidget(QtWidgets.QLabel(unit))
        outer.addWidget(table_box)
        source_box = QtWidgets.QGroupBox("Log Data Source")
        across = QtWidgets.QHBoxLayout(source_box)
        form = QtWidgets.QFormLayout()
        self.sources = {}
        for name, title in (("rpm_id", "RPM Source"), ("mixture_id", "Wideband Source"), ("load_id", "Fuel Load"), ("load2_id", "Target Load")):
            self.sources[name] = self._source(choices, getattr(settings, name))
            form.addRow(title, self.sources[name])
        across.addLayout(form, 1)
        view_box = QtWidgets.QGroupBox("View")
        view_column = QtWidgets.QVBoxLayout(view_box)
        self.views = {}
        for name, title in (("show_actual", "Actual"), ("show_target", "Target"), ("show_samples", "Samples"), ("show_error", "Error")):
            box = QtWidgets.QCheckBox(title)
            box.setChecked(getattr(settings, name))
            self.views[name] = box
            view_column.addWidget(box)
        across.addWidget(view_box)
        outer.addWidget(source_box)
        filters_box = QtWidgets.QGroupBox("Filters")
        grid = QtWidgets.QGridLayout(filters_box)
        grid.setVerticalSpacing(0)
        grid.addWidget(QtWidgets.QLabel("Enable"), 0, 0)
        self.min_enabled = QtWidgets.QCheckBox()
        self.min_enabled.setChecked(settings.min_samples_enabled)
        self.min_samples = self._number(settings.min_samples, 0, 1000)
        self.min_samples.setSuffix(" Samples")
        self.min_samples.setToolTip("Number of samples required before the cell is populated.")
        self.zone_enabled = QtWidgets.QCheckBox()
        self.zone_enabled.setChecked(settings.zone_enabled)
        self.zone = self._number(settings.zone_area, 0, 100)
        self.zone.setSuffix(" % of Zone")
        self.zone.setToolTip("Percentage of area from the cells centre where samples are considered.")
        grid.addWidget(self.min_enabled, 1, 0)
        grid.addWidget(QtWidgets.QLabel("Minimum Samples"), 1, 1)
        grid.addWidget(self.min_samples, 2, 1)
        grid.addWidget(self.zone_enabled, 3, 0)
        grid.addWidget(QtWidgets.QLabel("Active Zone Area"), 3, 1)
        grid.addWidget(self.zone, 4, 1)
        self.filters = []
        existing = settings.filters or [(0, True, 0.0, index >= 3, False) for index in range(6)]
        for index, (data_id, greater, value, transient, enabled) in enumerate(existing):
            line = 5 + index * 2
            use = QtWidgets.QCheckBox()
            use.setChecked(enabled)
            source = self._source([(0, ""), *choices], data_id)
            comparison = QtWidgets.QComboBox()
            comparison.addItems([">", "<"])
            comparison.setCurrentIndex(0 if greater else 1)
            amount = self._number(value, -1000, 10000)
            amount.setSuffix(" Units/Sec" if transient else " Units")
            tip = "E.g. If '<' is selected, samples will only be included if the corresponding parameter value is changing at a rate less than what is specified." if transient else "E.g. If '>' is selected, samples will only be included if the corresponding parameter value is greater than what is specified."
            comparison.setToolTip(tip)
            across_row = QtWidgets.QHBoxLayout()
            for widget in (source, comparison, amount):
                across_row.addWidget(widget)
            grid.addWidget(use, line + 1, 0)
            grid.addWidget(QtWidgets.QLabel(f"{'Transient' if transient else 'Conditional'} Filter {index % 3 + 1}"), line, 1)
            grid.addLayout(across_row, line + 1, 1)
            self.filters.append((use, source, comparison, amount, transient))
        outer.addWidget(filters_box)
        buttons = ok_cancel(self, self._ok)
        help_button = buttons.addButton(QtWidgets.QDialogButtonBox.StandardButton.Help)
        help_button.clicked.connect(lambda: parent.show_help() if hasattr(parent, "show_help") else None)
        outer.addWidget(buttons)

    @staticmethod
    def _number(value: float, low: float, high: float) -> QtWidgets.QDoubleSpinBox:
        spin = QtWidgets.QDoubleSpinBox()
        spin.setRange(low, high)
        spin.setDecimals(1)
        spin.setButtonSymbols(QtWidgets.QAbstractSpinBox.ButtonSymbols.NoButtons)
        spin.setAlignment(Q.AlignmentFlag.AlignRight)
        spin.setValue(value)
        return spin

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
        s.absolute_error = self.highlight.value() / self.scale
        for name, box in self.sources.items():
            setattr(s, name, box.currentData())
        for name, box in self.views.items():
            setattr(s, name, box.isChecked())
        s.min_samples_enabled, s.min_samples = self.min_enabled.isChecked(), int(self.min_samples.value())
        s.zone_enabled, s.zone_area = self.zone_enabled.isChecked(), self.zone.value()
        s.filters = [(source.currentData(), comparison.currentIndex() == 0, amount.value(), transient, use.isChecked()) for use, source, comparison, amount, transient in self.filters]
        self.accept()


class AudioPropertiesDialog(QtWidgets.QDialog):
    """What PCLink asks for when an audio view is added: the input and how fast the waterfall draws."""

    def __init__(self, inputs: QtWidgets.QComboBox, rate: int, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Parameters")
        self.inputs = QtWidgets.QComboBox()
        for index in range(inputs.count()):
            self.inputs.addItem(inputs.itemText(index), inputs.itemData(index))
        self.inputs.setCurrentIndex(inputs.currentIndex())
        group = QtWidgets.QGroupBox("Waterfall Draw Rate")
        column = QtWidgets.QVBoxLayout(group)
        self.rates = QtWidgets.QButtonGroup(self)
        for index, title in enumerate(("Fast", "Medium", "Slow")):
            button = QtWidgets.QRadioButton(title)
            self.rates.addButton(button, index)
            column.addWidget(button)
        self.rates.button(rate).setChecked(True)
        outer = QtWidgets.QVBoxLayout(self)
        outer.addWidget(QtWidgets.QLabel("Input Device"))
        outer.addWidget(self.inputs)
        outer.addWidget(group)
        outer.addWidget(ok_cancel(self))


class KnockAudioWindow(QtWidgets.QWidget):
    """Knock Audio: the computer's sound input as a trace, spectrum or waterfall. No ECU traffic.

    Needs QtMultimedia for a live input; without it the window still opens and says there is no input.
    """

    RATE, BLOCK = 44100, 1024
    MODES = ("Sampled", "FFT", "Waterfall")
    COLUMNS = 400
    SKIPS = (1, 2, 4)  # Fast, Medium, Slow: blocks passed per waterfall column.

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Audio Input")
        self.resize(760, 420)
        self.samples: list[float] = [0.0] * self.BLOCK
        self.spectrum: list[float] = [0.0] * (self.BLOCK // 2)
        self.history: list[list[float]] = [[0.0] * (self.BLOCK // 2)] * self.COLUMNS
        self.waterfall = QtGui.QImage(self.COLUMNS, self.BLOCK // 2, QtGui.QImage.Format.Format_RGB32)
        self.waterfall.fill(0)
        self.column = 0
        self.blocks = 0
        self.point: int | None = None
        self.source = None
        self.device = None
        self.buffer = bytearray()
        self.mode = QtWidgets.QComboBox()
        self.mode.addItems(self.MODES)
        self.inputs = QtWidgets.QComboBox()
        self.rate = QtWidgets.QComboBox()
        self.rate.addItems(("Fast", "Medium", "Slow"))
        self.rate.setCurrentIndex(1)
        self.contrast = QtWidgets.QSlider(Q.Orientation.Horizontal)
        self.contrast.setRange(1, 100)
        self.contrast.setValue(30)
        self.contrast.setToolTip("Contrast")
        self.volume = QtWidgets.QSlider(Q.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(100)
        self.volume.setToolTip("Volume")
        self.canvas = _AudioCanvas(self)
        record = QtWidgets.QToolButton()
        record.setIcon(self._icon(QtGui.QColor(220, 0, 0), round_shape=True))
        record.clicked.connect(self.start)
        stop = QtWidgets.QToolButton()
        stop.setIcon(self._icon(QtGui.QColor(60, 120, 220), round_shape=False))
        stop.clicked.connect(self.stop)
        bar = QtWidgets.QHBoxLayout()
        for widget in (record, stop, self.mode):
            bar.addWidget(widget)
        bar.addWidget(QtWidgets.QLabel("Contrast"))
        bar.addWidget(self.contrast, 1)
        bar.addWidget(QtWidgets.QLabel("Volume"))
        bar.addWidget(self.volume, 1)
        column = QtWidgets.QVBoxLayout(self)
        column.addWidget(self.canvas, 1)
        column.addLayout(bar)
        self.mode.currentIndexChanged.connect(self.canvas.update)
        self._list_devices()
        self.inputs.currentIndexChanged.connect(self.start)

    @staticmethod
    def _icon(colour: QtGui.QColor, round_shape: bool) -> QtGui.QIcon:
        pixmap = QtGui.QPixmap(16, 16)
        pixmap.fill(Q.GlobalColor.transparent)
        painter = QtGui.QPainter(pixmap)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setBrush(colour)
        painter.setPen(Q.PenStyle.NoPen)
        (painter.drawEllipse if round_shape else painter.drawRect)(2, 2, 12, 12)
        painter.end()
        return QtGui.QIcon(pixmap)

    def properties(self) -> bool:
        """The Parameters page: input device and waterfall draw rate."""
        dialog = AudioPropertiesDialog(self.inputs, self.rate.currentIndex(), self)
        if not dialog.exec():
            return False
        self.rate.setCurrentIndex(dialog.rates.checkedId())
        self.inputs.setCurrentIndex(dialog.inputs.currentIndex())
        return True

    def _list_devices(self) -> None:
        try:
            from PySide6.QtMultimedia import QMediaDevices
        except ImportError:
            self.inputs.addItem("No input devices")
            return
        for device in QMediaDevices.audioInputs():
            self.inputs.addItem(device.description(), device)
        if not self.inputs.count():
            self.inputs.addItem("No input devices")

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
            block, self.buffer = bytes(self.buffer[:size]), self.buffer[size:]
            if len(self.buffer) > size:
                continue  # A slow computer shows every other block rather than falling behind.
            self.add_block(struct.unpack(f"<{self.BLOCK}h", block))

    def add_block(self, block) -> None:
        gain = self.volume.value() / 100
        self.samples = [value * gain / 32768 for value in block]
        self.spectrum = tune.fft_magnitudes(self.samples)
        self.blocks += 1
        if self.blocks % self.SKIPS[self.rate.currentIndex()] == 0:
            scale = self.contrast.value() * 200
            for y, value in enumerate(self.spectrum):
                level = int(value * scale)
                self.waterfall.setPixel(self.column, len(self.spectrum) - 1 - y, 0xFFFF00FF if level > 250 else 0xFF000000 | min(level, 255) << 8)
            self.history[self.column] = self.spectrum
            self.column = (self.column + 1) % self.COLUMNS
        self.canvas.update()


class _AudioCanvas(QtWidgets.QWidget):
    def __init__(self, owner: KnockAudioWindow) -> None:
        super().__init__()
        self.owner = owner

    def contextMenuEvent(self, event: QtGui.QContextMenuEvent) -> None:
        owner = self.owner
        menu = QtWidgets.QMenu(self)
        point = int(event.pos().x() / max(self.width(), 1) * owner.COLUMNS)
        menu.addAction("Show FFT for point", lambda: self._show_point(point)).setEnabled(owner.mode.currentIndex() == 2)
        menu.addSeparator()
        menu.addAction("Properties", owner.properties)
        menu.exec(event.globalPos())

    def _show_point(self, column: int) -> None:
        self.owner.point = column
        self.owner.mode.setCurrentIndex(1)

    def paintEvent(self, _event: QtGui.QPaintEvent) -> None:
        painter = QtGui.QPainter(self)
        painter.fillRect(self.rect(), QtGui.QColor(0, 0, 0))
        owner = self.owner
        width, height = self.width(), self.height()
        mode = owner.mode.currentIndex()
        if mode == 2:
            painter.drawImage(self.rect(), owner.waterfall)
            self._frequency_axis(painter, height, vertical=True)
            return
        if mode == 0:
            self._sampled(painter)
            return
        data = owner.samples if mode == 0 else (owner.history[owner.point] if owner.point is not None else owner.spectrum)
        painter.setPen(QtGui.QColor(0, 0, 255) if mode == 0 else QtGui.QColor(80, 160, 255))
        points = QtGui.QPolygonF()
        tallest = max(max(data), 1e-9) if mode == 1 else 1.0
        for i in range(0, len(data), max(len(data) // max(width, 1), 1)):
            y = height / 2 - data[i] * height / 2 if mode == 0 else height - data[i] / tallest * height
            points.append(QtCore.QPointF(i / len(data) * width, y))
        painter.drawPolyline(points)
        if mode == 1:
            self._frequency_axis(painter, width, vertical=False)

    def _sampled(self, painter: QtGui.QPainter) -> None:
        """Eight bands across the plot, the middle one solid, and the trace from edge to edge."""
        area = QtCore.QRectF(10, 10, self.width() - 20, self.height() - 20)
        painter.setPen(QtGui.QColor(255, 255, 255))
        painter.drawRect(area)
        for n in range(1, 8):
            y = area.top() + area.height() * n / 8
            painter.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255) if n == 4 else QtGui.QColor(110, 110, 110), 1, Q.PenStyle.SolidLine if n == 4 else Q.PenStyle.DotLine))
            painter.drawLine(QtCore.QPointF(area.left(), y), QtCore.QPointF(area.right(), y))
        data = self.owner.samples
        painter.setPen(QtGui.QColor(0, 0, 255))
        points = QtGui.QPolygonF()
        for i in range(0, len(data), max(len(data) // max(int(area.width()), 1), 1)):
            points.append(QtCore.QPointF(area.left() + i / len(data) * area.width(), area.center().y() - data[i] * area.height() / 2))
        painter.drawPolyline(points)

    def _frequency_axis(self, painter: QtGui.QPainter, length: int, vertical: bool) -> None:
        """Ticks every 5000 Hz up to half the sample rate."""
        owner = self.owner
        for hertz in range(5000, owner.RATE // 2, 5000):
            along = hertz / (owner.RATE / 2)
            painter.setPen(QtGui.QColor(128, 128, 128))
            if vertical:
                y = self.height() - along * self.height()
                painter.drawLine(QtCore.QPointF(0, y), QtCore.QPointF(self.width(), y))
                painter.setPen(QtGui.QColor(255, 255, 255))
                painter.drawText(QtCore.QPointF(4, y - 2), f"{hertz // 1000} kHz")
            else:
                x = along * self.width()
                painter.drawLine(QtCore.QPointF(x, 0), QtCore.QPointF(x, self.height()))
                painter.setPen(QtGui.QColor(255, 255, 255))
                painter.drawText(QtCore.QPointF(x + 2, 12), f"{hertz // 1000} kHz")
        painter.setPen(QtGui.QColor(255, 255, 255))
        painter.drawText(QtCore.QPointF(4, 12), "Frequency")
