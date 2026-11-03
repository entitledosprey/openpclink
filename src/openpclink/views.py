"""Drawn views: table colouring and the 3D surface."""
from __future__ import annotations

import bisect

from PySide6 import QtCore, QtGui, QtWidgets


_HEAT_STOPS = ((90, 140, 240), (120, 220, 170), (240, 235, 120), (245, 140, 90))


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
