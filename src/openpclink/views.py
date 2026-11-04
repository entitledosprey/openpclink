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
    """A rotatable filled surface of a table: gradient colours, ghost box, origin triad, cross hairs."""

    cell_clicked = QtCore.Signal(int, int, int, int)
    YAW, PITCH = 0.8, 0.6

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumHeight(320)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
        self.values: list[float] = []
        self.columns = 1
        self.yaw, self.pitch, self.zoom = self.YAW, self.PITCH, 1.0
        self.ghost_box = self.colour_gradient = self.key_rotation = self.hinged = False
        self.selection: list[tuple[int, int]] = []
        self.trace: tuple[float, float] | None = None
        self.info: list[str] = []
        self.colours = {
            "low": QtGui.QColor(255, 176, 176), "middle": QtGui.QColor(255, 255, 176), "high": QtGui.QColor(176, 255, 176),
            "centre": QtGui.QColor(60, 60, 60), "edge": QtGui.QColor(30, 30, 30), "text": QtGui.QColor(230, 230, 230),
            "accent": QtGui.QColor(48, 150, 255),
        }
        self._turn: QtCore.QPointF | None = None
        self._pick: tuple[int, int] | None = None
        self._points: list[QtCore.QPointF] = []

    def configure(self, ghost_box: bool, colour_gradient: bool, key_rotation: bool, hinged: bool) -> None:
        self.ghost_box, self.colour_gradient, self.key_rotation, self.hinged = ghost_box, colour_gradient, key_rotation, hinged

    def set_theme(self, theme) -> None:
        for key, item in (("low", "GradientLow"), ("middle", "GradientMiddle"), ("high", "GradientHigh"), ("centre", "Background Centre"),
                          ("edge", "Background Edge"), ("text", "ViewText"), ("accent", "Active Control")):
            self.colours[key] = theme.colour(item)
        self.update()

    def set_values(self, values: list[float], columns: int) -> None:
        self.values, self.columns = values, max(columns, 1)
        self._redraw()

    def set_selection(self, cells: list[tuple[int, int]]) -> None:
        self.selection = cells
        self._redraw()

    def set_trace(self, position: tuple[float, float] | None) -> None:
        self.trace = position
        self._redraw()

    def set_info(self, lines: list[str]) -> None:
        self.info = lines
        self._redraw()

    def _redraw(self) -> None:
        if self.isVisible():
            self.update()

    def reset_view(self) -> None:
        self.yaw, self.pitch, self.zoom = self.YAW, self.PITCH, 1.0
        self.update()

    def rotate_key(self, key: int, shift: bool) -> None:
        keys = QtCore.Qt.Key
        if shift and key in (keys.Key_Up, keys.Key_Down):
            self.zoom = min(max(self.zoom * (1.1 if key == keys.Key_Up else 0.9), 0.3), 4.0)
        elif key in (keys.Key_Left, keys.Key_Right):
            self.yaw += 0.1 if key == keys.Key_Right else -0.1
        elif not self.hinged:
            self.pitch = min(max(self.pitch + (0.1 if key == keys.Key_Down else -0.1), 0.1), 1.4)
        self.update()

    def wheelEvent(self, event: QtGui.QWheelEvent) -> None:
        self.zoom = min(max(self.zoom * (1.1 if event.angleDelta().y() > 0 else 0.9), 0.3), 4.0)
        self.update()

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        self.setFocus()
        if event.button() == QtCore.Qt.MouseButton.RightButton:
            self._turn = event.position()
        elif event.button() == QtCore.Qt.MouseButton.LeftButton:
            self._pick = self._nearest(event.position())
            if self._pick:
                self.cell_clicked.emit(*self._pick, *self._pick)

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.buttons() & QtCore.Qt.MouseButton.RightButton and self._turn is not None:
            change = event.position() - self._turn
            self._turn = event.position()
            self.yaw += change.x() * 0.01
            if not self.hinged:
                self.pitch = min(max(self.pitch + change.y() * 0.01, 0.1), 1.4)
            self.update()
        elif event.buttons() & QtCore.Qt.MouseButton.LeftButton and self._pick:
            end = self._nearest(event.position())
            if end:
                self.cell_clicked.emit(*self._pick, *end)

    def _rows(self) -> int:
        return len(self.values) // self.columns

    def _nearest(self, at: QtCore.QPointF) -> tuple[int, int] | None:
        best, found = 24.0 ** 2, None
        for index, point in enumerate(self._points):
            distance = (point.x() - at.x()) ** 2 + (point.y() - at.y()) ** 2
            if distance < best:
                best, found = distance, divmod(index, self.columns)
        return found

    def _height_at(self, row: float, column: float, low: float, span: float) -> float:
        rows, columns = self._rows(), self.columns
        r0, c0 = min(int(row), rows - 1), min(int(column), columns - 1)
        r1, c1 = min(r0 + 1, rows - 1), min(c0 + 1, columns - 1)
        fr, fc = row - r0, column - c0

        def at(r: int, c: int) -> float:
            return (self.values[r * columns + c] - low) / span * 0.8

        top = at(r0, c0) + (at(r0, c1) - at(r0, c0)) * fc
        bottom = at(r1, c0) + (at(r1, c1) - at(r1, c0)) * fc
        return top + (bottom - top) * fr

    def paintEvent(self, _event: QtGui.QPaintEvent) -> None:
        import math

        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        glow = QtGui.QRadialGradient(self.width() / 2, self.height() / 2, max(self.width(), self.height()) * 0.7)
        glow.setColorAt(0, self.colours["centre"])
        glow.setColorAt(1, self.colours["edge"])
        painter.fillRect(self.rect(), glow)
        painter.setPen(self.colours["text"])
        for line, text in enumerate(self.info):
            painter.drawText(8, 18 + line * 18, text)
        if not self.values:
            return
        rows, columns = self._rows(), self.columns
        low, high = min(self.values), max(self.values)
        span = (high - low) or 1.0
        scale = min(self.width(), self.height()) * 0.55 * self.zoom
        cos_y, sin_y, cos_p, sin_p = math.cos(self.yaw), math.sin(self.yaw), math.cos(self.pitch), math.sin(self.pitch)

        def project(x: float, y: float, z: float) -> tuple[QtCore.QPointF, float]:
            turned_x = x * cos_y - y * sin_y
            turned_y = x * sin_y + y * cos_y
            return QtCore.QPointF(self.width() / 2 + turned_x * scale, self.height() * 0.62 + (turned_y * sin_p - z * cos_p) * scale), turned_y

        def vertex(row: float, column: float) -> tuple[QtCore.QPointF, float]:
            return project(column / max(columns - 1, 1) - 0.5, row / max(rows - 1, 1) - 0.5, self._height_at(row, column, low, span))

        grid = [[vertex(r, c) for c in range(columns)] for r in range(rows)]
        self._points = [point for line in grid for point, _ in line]
        if self.ghost_box:
            painter.setPen(QtGui.QPen(QtGui.QColor(150, 150, 150), 1))
            corners = [project(x, y, 0.0)[0] for x, y in ((-0.5, -0.5), (0.5, -0.5), (0.5, 0.5), (-0.5, 0.5))]
            painter.drawPolygon(QtGui.QPolygonF(corners))
            for x, y in ((-0.5, -0.5), (0.5, -0.5)):
                painter.drawLine(project(x, y, 0.0)[0], project(x, y, 0.85)[0])
        quads = []
        for row in range(rows - 1):
            for column in range(columns - 1):
                corner = (grid[row][column], grid[row][column + 1], grid[row + 1][column + 1], grid[row + 1][column])
                depth = sum(item[1] for item in corner) / 4
                value = sum(self.values[(row + dr) * columns + column + dc] for dr in (0, 1) for dc in (0, 1)) / 4
                quads.append((depth, corner, value, row, column))
        for depth, corner, value, row, column in sorted(quads, key=lambda quad: quad[0]):
            fill = _blend(self.colours, (value - low) / span) if self.colour_gradient else QtGui.QColor(176, 176, 176)
            slope = self.values[(row + 1) * columns + column] - self.values[row * columns + column + 1]
            fill = fill.lighter(100 + int(max(min(slope / span * 30, 15), -15)))
            painter.setBrush(fill)
            painter.setPen(QtGui.QPen(QtGui.QColor(50, 50, 50, 170), 0.8))
            painter.drawPolygon(QtGui.QPolygonF([item[0] for item in corner]))
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        accent = self.colours["accent"]
        if self.trace is not None and rows > 0:
            painter.setPen(QtGui.QPen(accent, 2))
            row_line = QtGui.QPolygonF([vertex(self.trace[0], c)[0] for c in range(columns)])
            column_line = QtGui.QPolygonF([vertex(r, self.trace[1])[0] for r in range(rows)])
            painter.drawPolyline(row_line)
            painter.drawPolyline(column_line)
        painter.setPen(QtGui.QPen(accent, 1.5))
        painter.setBrush(accent)
        for index, (row, column) in enumerate(self.selection[:64]):
            if row < rows and column < columns:
                point = grid[row][column][0]
                if index == 0:
                    painter.drawPolygon(QtGui.QPolygonF([point + QtCore.QPointF(-6, -16), point + QtCore.QPointF(6, -16), point + QtCore.QPointF(0, -4)]))
                else:
                    painter.drawEllipse(point, 3, 3)
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        origin = project(-0.5, 0.5, 0.0)[0]
        painter.setPen(QtGui.QPen(accent, 2))
        for x, y, z in ((-0.4, 0.5, 0.0), (-0.5, 0.4, 0.0), (-0.5, 0.5, 0.1)):
            painter.drawLine(origin, project(x, y, z)[0])


def _blend(colours: dict, position: float) -> QtGui.QColor:
    """Gradient Low through Middle to High."""
    position = min(max(position, 0.0), 1.0) * 2
    index = min(int(position), 1)
    blend = position - index
    a, b = colours[("low", "middle")[index]], colours[("middle", "high")[index]]
    return QtGui.QColor(*(round(getattr(a, name)() + (getattr(b, name)() - getattr(a, name)()) * blend) for name in ("red", "green", "blue")))
