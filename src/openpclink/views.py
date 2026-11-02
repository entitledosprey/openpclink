"""Drawn views: table colouring and 3D surface, and the log viewer."""
from __future__ import annotations

import bisect

from PySide6 import QtCore, QtGui, QtWidgets

from .logfile import Log

_HEAT_STOPS = ((90, 140, 240), (120, 220, 170), (240, 235, 120), (245, 140, 90))
_LOG_COLOURS = ("#2a7de1", "#d9453d", "#1f9e62", "#d68a00", "#7b4fd1", "#0a9aa8", "#c2408e", "#6a7a2a")


def heat_colour(value: float, low: float, high: float) -> tuple[int, int, int]:
    """Blue through green and yellow to red, across a table's own range."""
    if high <= low:
        return _HEAT_STOPS[1]
    position = min(max((value - low) / (high - low), 0.0), 1.0) * (len(_HEAT_STOPS) - 1)
    index = min(int(position), len(_HEAT_STOPS) - 2)
    blend = position - index
    a, b = _HEAT_STOPS[index], _HEAT_STOPS[index + 1]
    return tuple(round(a[i] + (b[i] - a[i]) * blend) for i in range(3))


def used_axis_length(breakpoints: list[float]) -> int:
    """Tables can hold spare rows or columns; the axis stops rising where they begin."""
    used = 1
    while used < len(breakpoints) and breakpoints[used] > breakpoints[used - 1]:
        used += 1
    return used


def axis_cell(value: float, breakpoints: list[float]) -> int | None:
    """The breakpoint nearest a live value."""
    if not breakpoints:
        return None
    axis = breakpoints[: used_axis_length(breakpoints)]
    at = bisect.bisect_left(axis, value)
    if at == 0:
        return 0
    if at >= len(axis):
        return len(axis) - 1
    return at if axis[at] - value < value - axis[at - 1] else at - 1


class SurfaceView(QtWidgets.QWidget):
    """A rotatable wireframe of a table."""

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumHeight(320)
        self.values: list[float] = []
        self.columns = 1
        self.yaw = 0.8
        self.pitch = 0.6
        self._drag: QtCore.QPointF | None = None

    def set_values(self, values: list[float], columns: int) -> None:
        self.values, self.columns = values, max(columns, 1)
        if self.isVisible():
            self.update()

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        self._drag = event.position()

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:
        if self._drag is not None:
            change = event.position() - self._drag
            self._drag = event.position()
            self.yaw += change.x() * 0.01
            self.pitch = min(max(self.pitch + change.y() * 0.01, 0.1), 1.4)
            self.update()

    def paintEvent(self, _event: QtGui.QPaintEvent) -> None:
        if not self.values:
            return
        import math

        rows = len(self.values) // self.columns
        low, high = min(self.values), max(self.values)
        span = (high - low) or 1.0
        scale = min(self.width(), self.height()) * 0.55
        cos_y, sin_y, cos_p, sin_p = math.cos(self.yaw), math.sin(self.yaw), math.cos(self.pitch), math.sin(self.pitch)

        def project(row: int, column: int) -> QtCore.QPointF:
            x = column / max(self.columns - 1, 1) - 0.5
            y = row / max(rows - 1, 1) - 0.5
            z = (self.values[row * self.columns + column] - low) / span * 0.5
            turned_x = x * cos_y - y * sin_y
            turned_y = x * sin_y + y * cos_y
            return QtCore.QPointF(
                self.width() / 2 + turned_x * scale,
                self.height() * 0.6 + (turned_y * sin_p - z * cos_p) * scale,
            )

        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        for row in range(rows):
            for column in range(self.columns):
                colour = QtGui.QColor(*heat_colour(self.values[row * self.columns + column], low, high)).darker(115)
                painter.setPen(QtGui.QPen(colour, 1.2))
                if column + 1 < self.columns:
                    painter.drawLine(project(row, column), project(row, column + 1))
                if row + 1 < rows:
                    painter.drawLine(project(row, column), project(row + 1, column))


class LogPlot(QtWidgets.QWidget):
    """Plots chosen channels, each scaled to its own range, with a read-out cursor."""

    cursor_moved = QtCore.Signal(int)

    def __init__(self, log: Log) -> None:
        super().__init__()
        self.log = log
        self.shown: list[int] = []
        self.cursor = 0
        self.setMinimumSize(640, 360)
        self.setMouseTracking(True)

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:
        if not self.log.time or self.width() <= 1:
            return
        fraction = min(max(event.position().x() / self.width(), 0.0), 1.0)
        target = self.log.time[0] + fraction * (self.log.time[-1] - self.log.time[0])
        self.cursor = min(bisect.bisect_left(self.log.time, target), len(self.log.time) - 1)
        self.cursor_moved.emit(self.cursor)
        self.update()

    def paintEvent(self, _event: QtGui.QPaintEvent) -> None:
        painter = QtGui.QPainter(self)
        painter.fillRect(self.rect(), self.palette().base())
        times = self.log.time
        if len(times) < 2:
            return
        duration = (times[-1] - times[0]) or 1.0
        width, height = self.width(), self.height()
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        step = max(len(times) // max(width, 1), 1)
        for order, index in enumerate(self.shown):
            data = self.log.channels[index]
            low, high = min(data), max(data)
            span = (high - low) or 1.0
            line = QtGui.QPolygonF(
                [
                    QtCore.QPointF((times[i] - times[0]) / duration * width, height - 8 - (data[i] - low) / span * (height - 16))
                    for i in range(0, len(times), step)
                ]
            )
            painter.setPen(QtGui.QPen(QtGui.QColor(_LOG_COLOURS[order % len(_LOG_COLOURS)]), 1.4))
            painter.drawPolyline(line)
        x = (times[self.cursor] - times[0]) / duration * width
        painter.setPen(QtGui.QPen(self.palette().text().color(), 1))
        painter.drawLine(QtCore.QPointF(x, 0), QtCore.QPointF(x, height))


class LogViewer(QtWidgets.QWidget):
    def __init__(self, log: Log, title: str) -> None:
        super().__init__()
        self.setWindowTitle(f"Log: {title}")
        self.resize(1100, 620)
        self.log = log
        self.plot = LogPlot(log)
        self.channels = QtWidgets.QTableWidget(len(log.names), 2)
        self.channels.setHorizontalHeaderLabels(["Channel", "At cursor"])
        self.channels.horizontalHeader().setStretchLastSection(True)
        self.channels.verticalHeader().hide()
        self.channels.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        for row, name in enumerate(log.names):
            item = QtWidgets.QTableWidgetItem(name)
            item.setFlags(item.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(QtCore.Qt.CheckState.Checked if row < 4 else QtCore.Qt.CheckState.Unchecked)
            self.channels.setItem(row, 0, item)
            self.channels.setItem(row, 1, QtWidgets.QTableWidgetItem(""))
        self.channels.resizeColumnToContents(0)
        self.channels.itemChanged.connect(self._sync)
        self.plot.cursor_moved.connect(self._show_cursor)
        splitter = QtWidgets.QSplitter()
        splitter.addWidget(self.plot)
        splitter.addWidget(self.channels)
        splitter.setSizes([800, 300])
        QtWidgets.QVBoxLayout(self).addWidget(splitter)
        self._sync()

    def _sync(self) -> None:
        self.plot.shown = [
            row for row in range(self.channels.rowCount())
            if self.channels.item(row, 0).checkState() == QtCore.Qt.CheckState.Checked
        ]
        self.plot.update()

    def _show_cursor(self, index: int) -> None:
        self.channels.blockSignals(True)
        for row, data in enumerate(self.log.channels):
            self.channels.item(row, 1).setText(f"{data[index]:g}")
        self.channels.blockSignals(False)
