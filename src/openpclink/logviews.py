"""Log analysis views. They share one session, so zoom and cursor can stay linked."""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass

from PySide6 import QtCore, QtGui, QtWidgets

from . import tune
from .gauges import tcolor
from .layoutfile import LayoutView
from .logfile import Log

PLOT_COLOURS = ("#ffffff", "#ff4040", "#40ff40", "#4080ff", "#ffe600", "#ff40ff", "#40ffff", "#ff9020")
BACKGROUND = QtGui.QColor(10, 10, 10)
GRID_COLOUR = QtGui.QColor(55, 55, 55)
TEXT_COLOUR = QtGui.QColor(220, 220, 220)


class LogSession(QtCore.QObject):
    """The open logs, which is active, the cursor and the time range on show."""

    changed = QtCore.Signal()
    cursor_moved = QtCore.Signal()
    range_changed = QtCore.Signal()

    def __init__(self) -> None:
        super().__init__()
        self.logs: list[Log] = []
        self.active = -1
        self.cursor = 0.0
        self.start = 0.0
        self.end = 1.0
        self.describe = lambda data_id: str(data_id)
        self.colour_of = lambda _data_id: None

    @property
    def log(self) -> Log | None:
        return self.logs[self.active] if 0 <= self.active < len(self.logs) else None

    @property
    def overlays(self) -> list[Log]:
        return [log for index, log in enumerate(self.logs) if index != self.active]

    def add(self, log: Log) -> None:
        self.logs.append(log)
        self.select(len(self.logs) - 1)

    def select(self, index: int) -> None:
        self.active = index
        self.reset_range()
        self.changed.emit()

    def close_log(self, log: Log | None = None) -> None:
        log = log or self.log
        if log is None:
            return
        self.logs.remove(log)
        self.active = min(self.active, len(self.logs) - 1)
        self.reset_range()
        self.changed.emit()

    def close_all(self) -> None:
        self.logs.clear()
        self.active = -1
        self.changed.emit()

    def reset_range(self) -> None:
        log = self.log
        self.start, self.end = (log.time[0], log.time[-1]) if log and len(log.time) > 1 else (0.0, 1.0)
        self.cursor = self.start
        self.range_changed.emit()

    def set_cursor(self, when: float) -> None:
        log = self.log
        if log is None or not log.time:
            return
        self.cursor = min(max(when, log.time[0]), log.time[-1])
        self.cursor_moved.emit()

    def set_range(self, start: float, end: float) -> None:
        log = self.log
        if log is None or len(log.time) < 2:
            return
        low, high = log.time[0], log.time[-1]
        span = max(end - start, (high - low) / 10000 or 0.001)
        start = min(max(start, low), max(high - span, low))
        self.start, self.end = start, min(start + span, high)
        self.range_changed.emit()

    def zoom(self, factor: float, around: float | None = None) -> None:
        around = (self.start + self.end) / 2 if around is None else around
        self.set_range(around - (around - self.start) * factor, around + (self.end - around) * factor)

    def index_at(self, when: float | None = None) -> int:
        log = self.log
        if log is None or not log.time:
            return 0
        return min(bisect.bisect_left(log.time, self.cursor if when is None else when), len(log.time) - 1)


def nice_step(span: float, count: int) -> float:
    """A round step that gives about `count` grid lines."""
    if span <= 0 or count < 1:
        return 1.0
    raw = span / count
    power = 10 ** math.floor(math.log10(raw))
    return next((m * power for m in (1, 2, 5, 10) if raw <= m * power), 10 * power)


def clock_text(seconds: float, step: float) -> str:
    """Minutes and seconds, with decimals only when the axis needs them."""
    minutes, rest = divmod(abs(seconds), 60)
    places = 0 if step >= 1 else min(3, max(1, -math.floor(math.log10(step))))
    sign = "-" if seconds < 0 else ""
    return f"{sign}{int(minutes)}:{rest:0{2 + (places + 1 if places else 0)}.{places}f}"


def thin(indices_start: int, indices_end: int, width: int) -> range:
    return range(indices_start, indices_end, max((indices_end - indices_start) // max(width, 1), 1))


@dataclass
class ChannelStats:
    minimum: float
    maximum: float
    average: float
    deviation: float
    samples: int
    length: float


def channel_statistics(times: list[float], data: list[float]) -> ChannelStats | None:
    """PCLink weights its mean and deviation by how long each sample lasted."""
    if len(data) < 2:
        return None
    count = len(data) - 1
    low, high = min(data[:count]), max(data[:count])
    span = times[-1] - times[0]
    mean = sum(data[i] * (times[i + 1] - times[i]) for i in range(count)) / span if span > 0 else 0.0
    variance = sum((data[i] - mean) ** 2 * (times[i + 1] - times[i]) for i in range(count)) / span if span > 0 else 0.0
    return ChannelStats(low, high, mean, math.sqrt(max(variance, 0.0)), count, span)


def color_for(value: float, spans: list[tuple[float, float, QtGui.QColor]]) -> QtGui.QColor | None:
    for low, high, colour in spans:
        if low <= value < high:
            return colour
    return spans[-1][2] if spans and value >= spans[-1][1] else spans[0][2] if spans else None


class _LogView(QtWidgets.QWidget):
    """Base for views of the session. `view` is its entry in the layout."""

    def __init__(self, session: LogSession, view: LayoutView | None = None) -> None:
        super().__init__()
        self.session = session
        self.view = view or LayoutView(type(self).__name__)
        self.setMinimumSize(80, 50)
        session.changed.connect(self.refresh)
        session.cursor_moved.connect(self.on_cursor)
        session.range_changed.connect(self.on_range)

    def linked(self, name: str) -> bool:
        return self.view.attributes.get(name, "true") == "true"

    def on_cursor(self) -> None:
        self.update()

    def on_range(self) -> None:
        self.update()

    def refresh(self) -> None:
        self.update()

    def chosen(self) -> list[tuple[int, int]]:
        """(channel index, display group) of the parameters this view wants that the log holds."""
        log = self.session.log
        if log is None:
            return []
        out = []
        for data_id, group in self.view.log_parameters:
            index = log.channel_of(data_id)
            if index is not None:
                out.append((index, group))
        return out or [(i, i) for i in range(min(len(log.names), 4))]

    def set_parameters(self, ids: list[int]) -> None:
        self.view.log_parameters = [(data_id, order) for order, data_id in enumerate(ids)]
        self.refresh()


class TimePlot(_LogView):
    """Parameters against time, one strip per display group, with sections, markers and a cursor."""

    AXIS_HEIGHT = 22
    SECTION_HEIGHT = 14
    Y_AXIS_WIDTH = 46

    def __init__(self, session: LogSession, view: LayoutView | None = None) -> None:
        super().__init__(session, view)
        self.setMouseTracking(True)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.WheelFocus)
        self._selecting: float | None = None

    def _x(self, when: float) -> float:
        span = (self.session.end - self.session.start) or 1.0
        return self.Y_AXIS_WIDTH + (when - self.session.start) / span * (self.width() - self.Y_AXIS_WIDTH)

    def _time(self, x: float) -> float:
        span = self.session.end - self.session.start
        return self.session.start + (x - self.Y_AXIS_WIDTH) / max(self.width() - self.Y_AXIS_WIDTH, 1) * span

    def groups(self) -> list[list[tuple[int | None, int]]]:
        """Strips in display order. A wanted parameter the log lacks shows as NO DATA, as PCLink does."""
        log = self.session.log
        groups: dict[int, list[tuple[int | None, int]]] = {}
        wanted = self.view.log_parameters or [(series.data_id, index) for index, series in enumerate(log.series[:4])]
        for data_id, group in wanted:
            groups.setdefault(group, []).append((log.channel_of(data_id) if data_id else None, data_id))
        return list(groups.values())

    def paintEvent(self, _event: QtGui.QPaintEvent) -> None:
        painter = QtGui.QPainter(self)
        painter.fillRect(self.rect(), BACKGROUND)
        log = self.session.log
        if log is None or len(log.time) < 2:
            painter.setPen(TEXT_COLOUR)
            painter.drawText(self.rect(), QtCore.Qt.AlignmentFlag.AlignCenter, "No log file open")
            return
        groups = self.groups()
        top = self.SECTION_HEIGHT
        height = (self.height() - self.AXIS_HEIGHT - top) / max(len(groups), 1)
        self._draw_sections(painter, log)
        self._draw_time_axis(painter)
        first = bisect.bisect_left(log.time, self.session.start)
        last = min(bisect.bisect_right(log.time, self.session.end) + 1, len(log.time))
        order = 0
        for row, members in enumerate(groups):
            strip = top + row * height
            painter.setPen(GRID_COLOUR)
            painter.drawLine(QtCore.QPointF(self.Y_AXIS_WIDTH, strip + height), QtCore.QPointF(self.width(), strip + height))
            present = [index for index, _ in members if index is not None]
            if present:
                self._draw_y_axis(painter, log, present[0], strip, height)
            for line, (index, data_id) in enumerate(members):
                if index is None:
                    painter.setPen(GRID_COLOUR.lighter(160))
                    painter.drawText(QtCore.QPointF(self.Y_AXIS_WIDTH + 4, strip + 13 + line * 14), f"NO DATA - {self.session.describe(data_id)}")
                else:
                    self._draw_channel(painter, log, index, strip, height, first, last, line, order)
                order += 1
        self._draw_markers(painter, log)
        x = self._x(self.session.cursor)
        painter.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255), 1))
        painter.drawLine(QtCore.QPointF(x, top), QtCore.QPointF(x, self.height() - self.AXIS_HEIGHT))
        if self._selecting is not None:
            painter.fillRect(QtCore.QRectF(self._x(self._selecting), top, x - self._x(self._selecting), self.height() - self.AXIS_HEIGHT - top), QtGui.QColor(90, 140, 240, 70))

    def _draw_sections(self, painter: QtGui.QPainter, log: Log) -> None:
        for section in log.sections():
            left, right = max(self._x(section.start), self.Y_AXIS_WIDTH), min(self._x(section.end), self.width())
            if right <= left:
                continue
            box = QtCore.QRectF(left, 0, right - left, self.SECTION_HEIGHT)
            painter.setPen(QtGui.QColor(230, 40, 40))
            painter.drawRect(box)
            painter.drawText(box, QtCore.Qt.AlignmentFlag.AlignCenter, section.name)

    def _draw_markers(self, painter: QtGui.QPainter, log: Log) -> None:
        pen = QtGui.QPen(QtGui.QColor(230, 40, 40), 1, QtCore.Qt.PenStyle.DashLine)
        for marker in log.markers:
            if not self.session.start <= marker.time <= self.session.end:
                continue
            x = self._x(marker.time)
            painter.setPen(pen)
            painter.drawLine(QtCore.QPointF(x, self.SECTION_HEIGHT), QtCore.QPointF(x, self.height() - self.AXIS_HEIGHT))
            painter.save()
            painter.translate(x - 3, self.SECTION_HEIGHT + 60)
            painter.rotate(-90)
            painter.drawText(QtCore.QPointF(0, 0), marker.name)
            painter.restore()

    def _draw_y_axis(self, painter: QtGui.QPainter, log: Log, index: int, top: float, height: float) -> None:
        data = log.channels[index]
        low, high = min(data), max(data)
        small = painter.font()
        small.setPixelSize(10)
        painter.setFont(small)
        for value in _ticks(low, high if high > low else low + 1, max(int(height) // 28, 1)):
            y = top + height - 3 - (value - low) / ((high - low) or 1.0) * (height - 6)
            painter.setPen(GRID_COLOUR)
            painter.drawLine(QtCore.QPointF(self.Y_AXIS_WIDTH, y), QtCore.QPointF(self.width(), y))
            painter.setPen(TEXT_COLOUR)
            painter.drawText(QtCore.QRectF(0, y - 7, self.Y_AXIS_WIDTH - 3, 14), QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter, f"{value:g}")

    def _draw_time_axis(self, painter: QtGui.QPainter) -> None:
        span = (self.session.end - self.session.start) or 1.0
        step = nice_step(span, max(self.width() // 110, 1))
        tick = math.ceil(self.session.start / step) * step
        bottom = self.height() - self.AXIS_HEIGHT
        while tick <= self.session.end:
            x = self._x(tick)
            painter.setPen(GRID_COLOUR)
            painter.drawLine(QtCore.QPointF(x, self.SECTION_HEIGHT), QtCore.QPointF(x, bottom))
            painter.setPen(TEXT_COLOUR)
            painter.drawText(QtCore.QRectF(x - 40, bottom, 80, self.AXIS_HEIGHT), QtCore.Qt.AlignmentFlag.AlignCenter, clock_text(tick, step))
            tick += step
        painter.drawText(QtCore.QRectF(2, bottom, self.Y_AXIS_WIDTH, self.AXIS_HEIGHT), QtCore.Qt.AlignmentFlag.AlignVCenter, "m:s")

    def _draw_channel(self, painter: QtGui.QPainter, log: Log, index: int, top: float, height: float, first: int, last: int, line: int, order: int) -> None:
        data = log.channels[index]
        low, high = min(data), max(data)
        span = (high - low) or 1.0
        colour = self.session.colour_of(log.series[index].data_id) or QtGui.QColor(PLOT_COLOURS[order % len(PLOT_COLOURS)])
        points = QtGui.QPolygonF([
            QtCore.QPointF(self._x(log.time[i]), top + height - 3 - (data[i] - low) / span * (height - 6))
            for i in thin(first, last, self.width() * 2)
        ])
        painter.setPen(QtGui.QPen(colour, 1))
        painter.drawPolyline(points)
        value = data[self.session.index_at()]
        painter.drawText(QtCore.QPointF(self.Y_AXIS_WIDTH + 4, top + 13 + line * 14), f"{log.names[index]}: {value:g} {log.unit_of(index)}".rstrip())

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        when = self._time(event.position().x())
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self.session.set_cursor(when)
            if event.modifiers() & QtCore.Qt.KeyboardModifier.ShiftModifier:
                self._selecting = when

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.buttons() & QtCore.Qt.MouseButton.LeftButton:
            self.session.set_cursor(self._time(event.position().x()))

    def mouseReleaseEvent(self, _event: QtGui.QMouseEvent) -> None:
        if self._selecting is not None:
            start, end = sorted((self._selecting, self.session.cursor))
            self._selecting = None
            if end > start:
                self.session.set_range(start, end)
            self.update()

    def mouseDoubleClickEvent(self, _event: QtGui.QMouseEvent) -> None:
        self.session.reset_range()

    def wheelEvent(self, event: QtGui.QWheelEvent) -> None:
        self.session.zoom(0.8 if event.angleDelta().y() > 0 else 1.25, self._time(event.position().x()))

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:
        log = self.session.log
        if log is None or not log.time:
            return
        key, index = event.key(), self.session.index_at()
        if key == QtCore.Qt.Key.Key_Right:
            self.session.set_cursor(log.time[min(index + 1, len(log.time) - 1)])
        elif key == QtCore.Qt.Key.Key_Left:
            self.session.set_cursor(log.time[max(index - 1, 0)])
        elif key in (QtCore.Qt.Key.Key_Plus, QtCore.Qt.Key.Key_Equal):
            self.session.zoom(0.8, self.session.cursor)
        elif key == QtCore.Qt.Key.Key_Minus:
            self.session.zoom(1.25, self.session.cursor)
        else:
            super().keyPressEvent(event)


class Navigator(_LogView):
    """The whole log, small, with the zoomed part marked. Dragging moves the zoomed part."""

    def _time(self, x: float) -> float:
        log = self.session.log
        return log.time[0] + x / max(self.width(), 1) * (log.time[-1] - log.time[0])

    def paintEvent(self, _event: QtGui.QPaintEvent) -> None:
        painter = QtGui.QPainter(self)
        painter.fillRect(self.rect(), BACKGROUND)
        log = self.session.log
        if log is None or len(log.time) < 2:
            return
        duration = log.time[-1] - log.time[0] or 1.0
        for order, (index, _group) in enumerate(self.chosen()):
            data = log.channels[index]
            low, span = min(data), (max(data) - min(data)) or 1.0
            painter.setPen(QtGui.QColor(PLOT_COLOURS[order % len(PLOT_COLOURS)]))
            painter.drawPolyline(QtGui.QPolygonF([
                QtCore.QPointF((log.time[i] - log.time[0]) / duration * self.width(), self.height() - 2 - (data[i] - low) / span * (self.height() - 4))
                for i in thin(0, len(data), self.width())
            ]))
        left = (self.session.start - log.time[0]) / duration * self.width()
        right = (self.session.end - log.time[0]) / duration * self.width()
        painter.fillRect(QtCore.QRectF(left, 0, right - left, self.height()), QtGui.QColor(90, 140, 240, 70))
        x = (self.session.cursor - log.time[0]) / duration * self.width()
        painter.setPen(QtGui.QColor(255, 255, 255))
        painter.drawLine(QtCore.QPointF(x, 0), QtCore.QPointF(x, self.height()))

    def _centre(self, x: float) -> None:
        span = self.session.end - self.session.start
        when = self._time(x)
        self.session.set_range(when - span / 2, when + span / 2)

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        if self.session.log:
            self._centre(event.position().x())

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:
        if self.session.log and event.buttons():
            self._centre(event.position().x())


class _TableView(_LogView):
    HEADERS: tuple[str, ...] = ()

    def __init__(self, session: LogSession, view: LayoutView | None = None) -> None:
        super().__init__(session, view)
        self.table = QtWidgets.QTableWidget(0, len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(list(self.HEADERS))
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.table)
        self.refresh()

    def fill(self, rows: list[list[str]]) -> None:
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, text in enumerate(row):
                item = self.table.item(r, c)
                if item is None:
                    self.table.setItem(r, c, QtWidgets.QTableWidgetItem(text))
                else:
                    item.setText(text)

    def rows(self) -> list[list[str]]:
        return []

    def refresh(self) -> None:
        self.fill(self.rows())
        self.update()

    def on_cursor(self) -> None:
        self.refresh()

    def on_range(self) -> None:
        self.refresh()


class ParameterList(_TableView):
    """Each logged parameter with its value at the cursor."""

    HEADERS = ("Parameter", "Value", "Units")

    def rows(self) -> list[list[str]]:
        log = self.session.log
        if log is None or not log.time:
            return []
        at = self.session.index_at()
        return [[log.names[i], f"{log.channels[i][at]:g}", log.unit_of(i)] for i in range(len(log.names))]


class LoggedValues(_TableView):
    """Chosen parameters sample by sample, around the cursor."""

    WINDOW = 200

    def __init__(self, session: LogSession, view: LayoutView | None = None) -> None:
        self.HEADERS = ("Time",)
        super().__init__(session, view)

    def refresh(self) -> None:
        log = self.session.log
        if log is None:
            self.table.setRowCount(0)
            return
        chosen = [index for index, _ in self.chosen()]
        self.table.setColumnCount(len(chosen) + 1)
        self.table.setHorizontalHeaderLabels(["Time", *(log.names[i] for i in chosen)])
        at = self.session.index_at()
        start = max(at - self.WINDOW // 2, 0)
        rows = [
            [f"{log.time[i]:.3f}", *(f"{log.channels[c][i]:g}" for c in chosen)]
            for i in range(start, min(start + self.WINDOW, len(log.time)))
        ]
        self.fill(rows)
        self.table.selectRow(at - start)


class StatisticsView(_TableView):
    """Figures for each parameter across the time range on show."""

    HEADERS = ("Parameter", "Units", "Sample Rate", "Samples", "Length", "Min", "Max", "Range", "Mean", "S.D.")

    def on_cursor(self) -> None:
        pass

    def rows(self) -> list[list[str]]:
        log = self.session.log
        if log is None or not log.time:
            return []
        first = bisect.bisect_left(log.time, self.session.start)
        last = bisect.bisect_right(log.time, self.session.end)
        rows = []
        for index in range(len(log.names)):
            figures = channel_statistics(log.time[first:last], log.channels[index][first:last])
            if figures:
                rows.append([
                    log.names[index], log.unit_of(index), "-", str(figures.samples), f"{figures.length:.3f}",
                    f"{figures.minimum:.3f}", f"{figures.maximum:.3f}", f"{figures.maximum - figures.minimum:.3f}",
                    f"{figures.average:.3f}", f"{figures.deviation:.3f}",
                ])
        return rows


class _AxesPlot(_LogView):
    MARGIN = 34

    def plot_area(self) -> QtCore.QRectF:
        return QtCore.QRectF(self.MARGIN + 12, 6, self.width() - self.MARGIN - 18, self.height() - self.MARGIN + 8 - 14)

    def draw_axes(self, painter: QtGui.QPainter, x_low: float, x_high: float, y_low: float, y_high: float, x_name: str, y_name: str) -> None:
        area = self.plot_area()
        painter.setPen(GRID_COLOUR)
        painter.drawRect(area)
        for value in _ticks(x_low, x_high, max(int(area.width()) // 80, 2)):
            x = area.left() + (value - x_low) / ((x_high - x_low) or 1) * area.width()
            painter.setPen(GRID_COLOUR)
            painter.drawLine(QtCore.QPointF(x, area.top()), QtCore.QPointF(x, area.bottom()))
            painter.setPen(TEXT_COLOUR)
            painter.drawText(QtCore.QRectF(x - 30, area.bottom() + 1, 60, 14), QtCore.Qt.AlignmentFlag.AlignCenter, f"{value:g}")
        for value in _ticks(y_low, y_high, max(int(area.height()) // 40, 2)):
            y = area.bottom() - (value - y_low) / ((y_high - y_low) or 1) * area.height()
            painter.setPen(GRID_COLOUR)
            painter.drawLine(QtCore.QPointF(area.left(), y), QtCore.QPointF(area.right(), y))
            painter.setPen(TEXT_COLOUR)
            painter.drawText(QtCore.QRectF(0, y - 7, area.left() - 2, 14), QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter, f"{value:g}")
        painter.setPen(TEXT_COLOUR)
        painter.drawText(QtCore.QRectF(area.left(), area.bottom() + 13, area.width(), 14), QtCore.Qt.AlignmentFlag.AlignCenter, x_name)
        painter.drawText(QtCore.QRectF(area.left() + 4, area.top() + 2, area.width(), 14), QtCore.Qt.AlignmentFlag.AlignLeft, y_name)


def _ticks(low: float, high: float, count: int) -> list[float]:
    if high <= low:
        return [low]
    step = nice_step(high - low, count)
    first = math.ceil(low / step)
    return [i * step for i in range(first, math.floor(high / step) + 1)]


@dataclass
class XYSettings:
    """What a layout stores for an XY plot."""

    x_id: int = 0
    colour_id: int = 0
    use_colour: bool = False
    manual: bool = False
    manual_min: float = 0.0
    manual_max: float = 1.0
    show_legend: bool = True
    show_lines: bool = False
    line_width: int = 1
    show_points: bool = True
    point_size: int = 1
    spans: tuple = ()

    @classmethod
    def from_view(cls, view: LayoutView) -> "XYSettings":
        node = view.part("XYPlot")
        if node is None:
            return cls()
        spans = []
        for span in node.iter("ColorSpan"):
            spans.append((float(span.get("LowerBound", "0")), float(span.get("UpperBound", "0")), tcolor(int(span.get("Color", "0")))))
        return cls(
            x_id=int(node.get("XAxisParameterDataID", "0") or 0),
            colour_id=int(node.get("ColorRangeParameterDataID", "0") or 0),
            use_colour=node.get("UseColorRange") == "true",
            manual=node.get("ManualRange") == "true",
            manual_min=float(node.get("ManualMin", "0")),
            manual_max=float(node.get("ManualMax", "1")),
            show_legend=node.get("ShowColorLegend") != "false",
            show_lines=node.get("ShowLines") == "true",
            line_width=int(node.get("LineWidth", "1")),
            show_points=node.get("ShowPoints") != "false",
            point_size=int(node.get("PointSize", "1")),
            spans=tuple(spans),
        )


class XYPlot(_AxesPlot):
    """One or more parameters against another, optionally coloured by a third."""

    def __init__(self, session: LogSession, view: LayoutView | None = None) -> None:
        super().__init__(session, view)
        self.settings = XYSettings.from_view(self.view)

    def axis_channels(self) -> tuple[int | None, list[int]]:
        log = self.session.log
        if log is None:
            return None, []
        x = log.channel_of(self.settings.x_id)
        ys = [index for index, _ in self.chosen() if index != x]
        if x is None and log.names:
            x = 0
            ys = [i for i in ys if i != 0] or [min(1, len(log.names) - 1)]
        return x, ys

    def paintEvent(self, _event: QtGui.QPaintEvent) -> None:
        painter = QtGui.QPainter(self)
        painter.fillRect(self.rect(), BACKGROUND)
        log = self.session.log
        x_index, ys = self.axis_channels()
        if log is None or x_index is None or not log.time or not ys:
            return
        first = bisect.bisect_left(log.time, self.session.start) if self.linked("ZoomLinked") else 0
        last = bisect.bisect_right(log.time, self.session.end) if self.linked("ZoomLinked") else len(log.time)
        if last <= first:
            first, last = 0, len(log.time)
        xs = log.channels[x_index][first:last]
        x_low, x_high = (self.settings.manual_min, self.settings.manual_max) if self.settings.manual else (min(xs), max(xs))
        y_values = [value for y in ys for value in log.channels[y][first:last]]
        y_low, y_high = min(y_values), max(y_values)
        self.draw_axes(painter, x_low, x_high, y_low, y_high, log.names[x_index], ", ".join(log.names[y] for y in ys))
        area = self.plot_area()
        painter.save()
        painter.setClipRect(area)
        colour_channel = log.channel_of(self.settings.colour_id) if self.settings.use_colour else None
        step = max(len(xs) // 8000, 1)
        for order, y in enumerate(ys):
            data = log.channels[y][first:last]
            base = QtGui.QColor(PLOT_COLOURS[order % len(PLOT_COLOURS)])
            previous = None
            for i in range(0, len(xs), step):
                point = QtCore.QPointF(
                    area.left() + (xs[i] - x_low) / ((x_high - x_low) or 1) * area.width(),
                    area.bottom() - (data[i] - y_low) / ((y_high - y_low) or 1) * area.height(),
                )
                colour = base
                if colour_channel is not None and self.settings.spans:
                    colour = color_for(log.channels[colour_channel][first + i], list(self.settings.spans)) or base
                painter.setPen(QtGui.QPen(colour, self.settings.line_width))
                if self.settings.show_lines and previous is not None:
                    painter.drawLine(previous, point)
                if self.settings.show_points:
                    painter.setPen(QtGui.QPen(colour, max(self.settings.point_size, 1) + 1))
                    painter.drawPoint(point)
                previous = point
        painter.restore()
        if self.settings.use_colour and self.settings.show_legend and self.settings.spans:
            self._draw_legend(painter, area)

    def _draw_legend(self, painter: QtGui.QPainter, area: QtCore.QRectF) -> None:
        height = min(area.height() / len(self.settings.spans), 14)
        for row, (low, _high, colour) in enumerate(reversed(self.settings.spans)):
            box = QtCore.QRectF(area.right() - 56, area.top() + 2 + row * height, 10, height)
            painter.fillRect(box, colour)
            painter.setPen(TEXT_COLOUR)
            painter.drawText(box.adjusted(13, -1, 44, 1), QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter, f"{low:g}")


def histogram_bins(values: list[float], bins: int) -> tuple[list[int], float, float]:
    """Counts per equal-width bin, with the range they cover."""
    if not values or bins < 1:
        return [], 0.0, 0.0
    low, high = min(values), max(values)
    counts = [0] * bins
    width = (high - low) / bins
    for value in values:
        counts[min(int((value - low) / width), bins - 1) if width else 0] += 1
    return counts, low, high


class Histogram(_AxesPlot):
    """How long each parameter spent at each value, across the range on show."""

    BINS = 40

    def paintEvent(self, _event: QtGui.QPaintEvent) -> None:
        painter = QtGui.QPainter(self)
        painter.fillRect(self.rect(), BACKGROUND)
        log = self.session.log
        chosen = self.chosen()
        if log is None or not log.time or not chosen:
            return
        index = chosen[0][0]
        first = bisect.bisect_left(log.time, self.session.start)
        last = bisect.bisect_right(log.time, self.session.end)
        counts, low, high = histogram_bins(log.channels[index][first:last], self.BINS)
        if not counts:
            return
        tallest = max(counts)
        self.draw_axes(painter, low, high, 0, tallest, log.names[index], "Samples")
        area = self.plot_area()
        width = area.width() / len(counts)
        painter.setBrush(QtGui.QColor(PLOT_COLOURS[3]))
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        for at, count in enumerate(counts):
            bar = count / tallest * area.height()
            painter.drawRect(QtCore.QRectF(area.left() + at * width, area.bottom() - bar, max(width - 1, 1), bar))


GPS_LATITUDE, GPS_LONGITUDE = 4251, 4252


class GpsPlot(_AxesPlot):
    """The logged route: latitude against longitude on plain axes, as PCLink draws it."""

    def paintEvent(self, _event: QtGui.QPaintEvent) -> None:
        painter = QtGui.QPainter(self)
        painter.fillRect(self.rect(), BACKGROUND)
        log = self.session.log
        if log is None:
            return
        x_index, y_index = log.channel_of(GPS_LONGITUDE), log.channel_of(GPS_LATITUDE)
        if x_index is None or y_index is None or not log.time:
            painter.setPen(TEXT_COLOUR)
            painter.drawText(self.rect(), QtCore.Qt.AlignmentFlag.AlignCenter, "This log has no GPS position")
            return
        first = bisect.bisect_left(log.time, self.session.start)
        last = max(bisect.bisect_right(log.time, self.session.end), first + 1)
        xs, ys = log.channels[x_index][first:last], log.channels[y_index][first:last]
        x_low, x_high, y_low, y_high = min(xs), max(xs), min(ys), max(ys)
        self.draw_axes(painter, x_low, x_high, y_low, y_high, "Longitude", "Latitude")
        area = self.plot_area()

        def point(x: float, y: float) -> QtCore.QPointF:
            return QtCore.QPointF(
                area.left() + (x - x_low) / ((x_high - x_low) or 1) * area.width(),
                area.bottom() - (y - y_low) / ((y_high - y_low) or 1) * area.height(),
            )

        painter.setPen(QtGui.QColor(PLOT_COLOURS[2]))
        step = max(len(xs) // 8000, 1)
        painter.drawPolyline(QtGui.QPolygonF([point(xs[i], ys[i]) for i in range(0, len(xs), step)]))
        at = self.session.index_at()
        painter.setBrush(QtGui.QColor(255, 255, 0))
        painter.drawEllipse(point(log.channels[x_index][at], log.channels[y_index][at]), 4, 4)


class VehiclePlan(_LogView):
    """A plan view of the car: wheel speeds, steering, pedals, gear and G force at the cursor."""

    WHEELS = ((1437, "LF", 0.2, 0.2), (1439, "RF", 0.8, 0.2), (1438, "LR", 0.2, 0.8), (1440, "RR", 0.8, 0.8))
    BARS = ((1650, "Accelerator", 0.0, 100.0), (1078, "Throttle", 0.0, 100.0), (2662, "Wheel Slip", -100.0, 100.0), (1000, "Engine speed", 0.0, 10000.0))
    GEAR, STEERING, LATERAL, LONGITUDINAL = 2587, 1020, 1026, 1027

    def __init__(self, session: LogSession, view: LayoutView | None = None, live=None) -> None:
        super().__init__(session, view)
        self.live = live
        self.setMinimumSize(280, 240)

    def value(self, data_id: int) -> float | None:
        if self.view.attributes.get("LogView", "true") == "true":
            log = self.session.log
            index = log.channel_of(data_id) if log else None
            if index is not None and log.series[index].times:
                return log.series[index].value_at(self.session.cursor)
        return self.live(data_id) if self.live else None

    def paintEvent(self, _event: QtGui.QPaintEvent) -> None:
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), BACKGROUND)
        painter.setPen(TEXT_COLOUR)
        car = QtCore.QRectF(self.width() * 0.08, 10, self.width() * 0.42, self.height() - 20)
        painter.drawRoundedRect(car.adjusted(car.width() * 0.2, 0, -car.width() * 0.2, 0), 12, 12)
        for data_id, name, x, y in self.WHEELS:
            value = self.value(data_id)
            box = QtCore.QRectF(car.left() + car.width() * x - 26, car.top() + car.height() * y - 18, 52, 36)
            painter.drawRect(box)
            painter.drawText(box, QtCore.Qt.AlignmentFlag.AlignCenter, f"{name}\n{'--' if value is None else f'{value:.0f}'}")
        gear, steering = self.value(self.GEAR), self.value(self.STEERING)
        painter.drawText(car, QtCore.Qt.AlignmentFlag.AlignCenter, f"Gear {'--' if gear is None else f'{gear:.0f}'}\nSteering {'--' if steering is None else f'{steering:.0f}'}")
        panel = self.width() * 0.56
        lane = self.width() - panel - 8
        for row, (data_id, name, low, high) in enumerate(self.BARS):
            value = self.value(data_id)
            y = 8 + row * 42
            painter.setPen(TEXT_COLOUR)
            painter.drawText(QtCore.QPointF(panel, y + 11), f"{name}: {'--' if value is None else f'{value:.0f}'}")
            painter.fillRect(QtCore.QRectF(panel, y + 15, lane, 10), GRID_COLOUR)
            if value is not None:
                painter.fillRect(QtCore.QRectF(panel, y + 15, lane * min(max((value - low) / (high - low), 0), 1), 10), QtGui.QColor(PLOT_COLOURS[2]))
        lateral, longitudinal = self.value(self.LATERAL), self.value(self.LONGITUDINAL)
        side = min(lane, self.height() - 190)
        if side > 30:
            box = QtCore.QRectF(panel, self.height() - side - 8, side, side)
            painter.setPen(TEXT_COLOUR)
            painter.drawEllipse(box)
            painter.drawText(box.adjusted(side + 6, 0, side + 80, 0), QtCore.Qt.AlignmentFlag.AlignVCenter, "G Force")
            if lateral is not None and longitudinal is not None:
                reach = max(abs(lateral), abs(longitudinal), 1.0)
                dot = box.center() + QtCore.QPointF(lateral / reach * side / 2, -longitudinal / reach * side / 2)
                painter.setBrush(QtGui.QColor(255, 255, 0))
                painter.drawEllipse(dot, 4, 4)
                painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)


class MixtureMapView(_LogView):
    """Logged mixture against target, cell by cell, for the fuel table. Double-click corrects a cell."""

    def __init__(self, session: LogSession, view: LayoutView | None = None, host=None) -> None:
        super().__init__(session, view)
        self.host = host
        self.settings = tune.MixtureSettings.from_attributes(self.view.attributes)
        self.grid: list[list[tune.MixtureCell]] = []
        self.message = "Right click to update map."

    def rebuild(self) -> None:
        if self.host is None:
            return
        self.grid, self.message = self.host.build_mixture(self.settings, self.session.log)
        self.update()

    def contextMenuEvent(self, event: QtGui.QContextMenuEvent) -> None:
        menu = QtWidgets.QMenu(self)
        menu.addAction("Update Mixture Map", self.rebuild)
        if self.host:
            menu.addAction("Properties…", lambda: self.host.mixture_setup(self))
        menu.exec(event.globalPos())

    def _cell_at(self, position: QtCore.QPointF) -> tuple[int, int] | None:
        if not self.grid or not self.grid[0]:
            return None
        row = len(self.grid) - 1 - int(position.y() / self.height() * len(self.grid))
        column = int(position.x() / self.width() * len(self.grid[0]))
        if 0 <= row < len(self.grid) and 0 <= column < len(self.grid[0]):
            return row, column
        return None

    def mouseDoubleClickEvent(self, event: QtGui.QMouseEvent) -> None:
        spot = self._cell_at(event.position())
        if spot is None or self.host is None:
            return
        cell = self.grid[spot[0]][spot[1]]
        if cell.has_data:
            self.host.correct_mixture_cell(self.settings.table, spot[0], spot[1], cell.actual_average, cell.target_average)
            cell.has_data, cell.corrected = False, True
            self.update()

    def paintEvent(self, _event: QtGui.QPaintEvent) -> None:
        painter = QtGui.QPainter(self)
        painter.fillRect(self.rect(), BACKGROUND)
        if not self.grid or not self.grid[0]:
            painter.setPen(TEXT_COLOUR)
            painter.drawText(self.rect(), QtCore.Qt.AlignmentFlag.AlignCenter, self.message)
            return
        rows, columns = len(self.grid), len(self.grid[0])
        width, height = self.width() / columns, self.height() / rows
        font = painter.font()
        font.setPixelSize(max(int(min(height / 5, width / 5)), 7))
        painter.setFont(font)
        s = self.settings
        for r, row in enumerate(self.grid):
            for c, cell in enumerate(row):
                box = QtCore.QRectF(c * width, (rows - 1 - r) * height, width, height)
                if cell.corrected:
                    painter.fillRect(box, QtGui.QColor(160, 160, 0))
                elif cell.has_data:
                    painter.fillRect(box, error_colour(cell.error, s.absolute_error))
                painter.setPen(GRID_COLOUR)
                painter.drawRect(box)
                if not cell.has_data:
                    continue
                lines = []
                if s.show_actual:
                    lines.append(f"{cell.actual_average:.2f}")
                if s.show_target:
                    lines.append(f"{cell.target_average:.2f}")
                if s.show_samples:
                    lines.append(f"{cell.samples}")
                if s.show_error:
                    lines.append(f"{cell.error:.2f}")
                painter.setPen(QtGui.QColor(0, 0, 0))
                painter.drawText(box, QtCore.Qt.AlignmentFlag.AlignCenter, "\n".join(lines))


def error_colour(error: float, full_scale: float) -> QtGui.QColor:
    """Green through yellow to red, reaching red at the highlight range."""
    fraction = min(error / full_scale, 1.0) if full_scale > 0 else 1.0
    if fraction < 0.5:
        return QtGui.QColor(round(255 * fraction * 2), 255, 0)
    return QtGui.QColor(255, round(255 * (1 - fraction) * 2), 0)


LOG_VIEW_TYPES = {
    "Time Plot": TimePlot, "Time Plot 2": TimePlot, "Navigator": Navigator, "Parameter List": ParameterList,
    "Value List": LoggedValues, "XY Plot": XYPlot, "XY Plot 2": XYPlot, "Histogram": Histogram, "Histogram 2": Histogram,
    "Statistics": StatisticsView, "GPS Plot": GpsPlot,
}
