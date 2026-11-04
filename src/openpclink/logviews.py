"""Log analysis views. They share one session, so zoom and cursor can stay linked."""
from __future__ import annotations

import bisect
import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from PySide6 import QtCore, QtGui, QtWidgets

from . import tune
from .gauges import tcolor, to_tcolor
from .layoutfile import LayoutView
from .logfile import Log

PLOT_COLOURS = ("#ffffff", "#ff4040", "#40ff40", "#4080ff", "#ffe600", "#ff40ff", "#40ffff", "#ff9020")
OVERLAY_COLOURS = ("#0066a6", "#0178a5", "#a200a6", "#a60004", "#a66000")
THEME_DEFAULTS = {
    "DataAreaBackground": "#000000", "DataAreaBorder": "#c0c0c0", "GridLines": "#646464", "ViewText": "#f5f5f5",
    "Sliders": "#555555", "Cursors": "#ffff00", "LogCrosshairs": "#ffff00", "Markers": "#ff0000", "View Title Bar": "#3d3d3d",
}
BACKGROUND = QtGui.QColor(THEME_DEFAULTS["DataAreaBackground"])
GRID_COLOUR = QtGui.QColor(THEME_DEFAULTS["GridLines"])
TEXT_COLOUR = QtGui.QColor(THEME_DEFAULTS["ViewText"])
NO_DATA = "NO DATA"
STACKED, OVERLAPPED = 0, 1
POINT_STYLES = ("Square", "Circle", "Diamond", "Cross", "Plus", "Triangle")
LEFT, RIGHT = QtCore.Qt.AlignmentFlag.AlignLeft, QtCore.Qt.AlignmentFlag.AlignRight
CENTRE, VCENTRE = QtCore.Qt.AlignmentFlag.AlignCenter, QtCore.Qt.AlignmentFlag.AlignVCenter


def hms(seconds: float) -> str:
    """PCLink's m:ss.mmm clock."""
    sign = "-" if seconds < -0.0005 else ""
    millis = round(abs(seconds) * 1000)
    return f"{sign}{millis // 60000}:{millis // 1000 % 60:02d}.{millis % 1000:03d}"


def hash_number(value: float, places: int = 3) -> str:
    """Delphi's `####0.###`: trailing zeros go."""
    text = f"{value:.{places}f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def fixed_number(value: float, places: int) -> str:
    text = f"{value:.{places}f}"
    return text[1:] if text.startswith("-") and float(text) == 0 else text


class LogSession(QtCore.QObject):
    """The open logs, which is active, the cursor and the time range on show.

    The callbacks are set by the main window: units, decimals and theme colours all come from it.
    """

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
        self.overlaid: dict[tuple[int, float], float] = {}  # (log id, section start) to time offset
        self.offsets: dict[tuple[int, float], float] = {}
        self.describe = lambda data_id: str(data_id)
        self.colour_of = lambda _data_id: None
        self.open_zoom = 0
        self.display = lambda _series, value: value
        self.span = lambda _series, value: value
        self.unit_label = lambda series: series.units
        self.decimals = lambda _series: None
        self.theme_colour = lambda name: QtGui.QColor(THEME_DEFAULTS.get(name, "#ffffff"))
        self.overlay_colour = lambda index: QtGui.QColor(OVERLAY_COLOURS[index % len(OVERLAY_COLOURS)])
        self.parameter_name = lambda data_id: ""
        self.parameter_colour = lambda index: QtGui.QColor(PLOT_COLOURS[index % len(PLOT_COLOURS)])

    @property
    def log(self) -> Log | None:
        return self.logs[self.active] if 0 <= self.active < len(self.logs) else None

    @property
    def overlays(self) -> list[Log]:
        return [log for index, log in enumerate(self.logs) if index != self.active]

    def add(self, log: Log) -> None:
        self.logs.append(log)
        self.select(len(self.logs) - 1)
        if self.open_zoom == 1:
            self.zoom_shortest()

    def zoom_shortest(self) -> None:
        """OpenLogFileOptions 1: show the shortest section, the fastest lap or drag run."""
        sections = self.log.sections() if self.log is not None else []
        if sections:
            best = min(sections, key=lambda section: section.end - section.start)
            self.set_range(best.start, best.end)
            self.set_cursor(best.start)

    def select(self, index: int) -> None:
        self.active = index
        self.reset_range()
        self.changed.emit()

    def close_log(self, log: Log | None = None) -> None:
        log = log or self.log
        if log is None:
            return
        self.logs.remove(log)
        for key in [key for key in self.overlaid if key[0] == id(log)]:
            del self.overlaid[key]
        self.active = min(self.active, len(self.logs) - 1)
        self.reset_range()
        self.changed.emit()

    def close_all(self) -> None:
        self.logs.clear()
        self.overlaid.clear()
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

    def section_at(self, when: float | None = None, log: Log | None = None):
        log = log or self.log
        when = self.cursor if when is None else when
        sections = log.sections() if log else []
        return next((s for s in sections if s.start <= when <= s.end), sections[-1] if sections else None)

    def overlay_selection(self) -> list[tuple[Log, object, float]]:
        """The sections ticked in the Log File Manager: (log, section, offset)."""
        chosen = []
        for log in self.logs:
            for section in log.sections():
                key = (id(log), section.start)
                if key in self.overlaid:
                    chosen.append((log, section, self.overlaid[key]))
        return chosen

    def set_overlay(self, log: Log, section, on: bool, offset: float | None = None) -> None:
        key = (id(log), section.start)
        if on:
            self.overlaid[key] = self.offsets.get(key, 0.0) if offset is None else offset
        else:
            self.overlaid.pop(key, None)
        if offset is not None:
            self.offsets[key] = offset
        self.changed.emit()


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
    rate: float = 0.0


def _nearest(times: list[float], when: float) -> int:
    at = bisect.bisect_left(times, when)
    if at <= 0:
        return 0
    if at >= len(times):
        return len(times) - 1
    return at if times[at] - when < when - times[at - 1] else at - 1


def series_statistics(times: list[float], values: list[float], start: float | None = None, end: float | None = None) -> ChannelStats | None:
    """PCLink's per-series figures (FUN_00486F28): the mean and deviation are weighted by how long each sample lasted."""
    if len(values) < 1:
        return None
    start = times[0] if start is None else start
    end = times[-1] if end is None else end
    first, last = _nearest(times, start), _nearest(times, end)
    rate = (len(times) - 1) / (times[-1] - times[0]) if len(times) > 1 and times[-1] > times[0] else 0.0
    if last - first < 1:
        return ChannelStats(0.0, 0.0, 0.0, 0.0, 0, end - start, rate)
    window = values[first:last]
    low, high = min(window), max(window)
    span = times[last] - times[first]
    weights = [times[i + 1] - times[i] for i in range(first, last)]
    mean = sum(v * w for v, w in zip(window, weights)) / span if span > 0 else 0.0
    total = end - start
    variance = sum((v - mean) ** 2 * w for v, w in zip(window, weights)) / total if total > 0 else 0.0
    return ChannelStats(low, high, mean, math.sqrt(variance) if variance > 0 else 0.0, last - first, total, rate)


def channel_statistics(times: list[float], data: list[float]) -> ChannelStats | None:
    return series_statistics(times, data) if len(data) >= 2 else None


def color_for(value: float, spans: list[tuple[float, float, QtGui.QColor]]) -> QtGui.QColor | None:
    for low, high, colour in spans:
        if low <= value < high:
            return colour
    return spans[-1][2] if spans and value >= spans[-1][1] else spans[0][2] if spans else None


def _bool(text: str | None, default: bool) -> bool:
    return default if text is None else text.lower() == "true"


def _put(node: ET.Element, name: str, value) -> None:
    node.set(name, ("true" if value else "false") if isinstance(value, bool) else str(value))


class _LogView(QtWidgets.QWidget):
    """Base for views of the session. `view` is its entry in the layout."""

    NODE = ""
    NODE_DEFAULTS: dict[str, str] = {}

    def __init__(self, session: LogSession, view: LayoutView | None = None) -> None:
        super().__init__()
        self.session = session
        self.view = view or LayoutView(type(self).__name__)
        self.local_cursor = 0.0
        self.setMinimumSize(80, 50)
        self.changed_hook = None
        session.changed.connect(self.refresh)
        session.cursor_moved.connect(self.on_cursor)
        session.range_changed.connect(self.on_range)

    def node(self) -> ET.Element | None:
        """The view's own child element, made on first use so a saved layout carries its settings."""
        if not self.NODE:
            return None
        node = self.view.part(self.NODE)
        if node is None:
            node = ET.Element(self.NODE, dict(self.NODE_DEFAULTS))
            self.view.extra.append(node)
            self.init_node(node)
        return node

    def init_node(self, node: ET.Element) -> None:
        pass

    def option(self, name: str, default: bool = True) -> bool:
        node = self.node()
        return _bool(node.get(name) if node is not None else None, default)

    def set_option(self, name: str, value: bool) -> None:
        _put(self.node(), name, value)
        self.saved()

    def saved(self) -> None:
        self.update()
        if self.changed_hook:
            self.changed_hook()

    def linked(self, name: str) -> bool:
        return self.view.attributes.get(name, "true") == "true"

    def set_linked(self, name: str, value: bool) -> None:
        self.view.attributes[name] = "true" if value else "false"
        self.saved()

    def time_window(self) -> tuple[float, float]:
        """The time range this view shows: the shared one, or its own fractions of the log."""
        log = self.session.log
        if self.linked("ZoomLinked") or log is None or len(log.time) < 2:
            return self.session.start, self.session.end
        low, high = log.time[0], log.time[-1]
        start = float(self.view.attributes.get("DisplayTimeRangeStart", "0") or 0)
        end = float(self.view.attributes.get("DisplayTimeRangeEnd", "1") or 1)
        return low + start * (high - low), low + max(end, start + 1e-6) * (high - low)

    def set_time_window(self, start: float, end: float) -> None:
        log = self.session.log
        if log is None or len(log.time) < 2:
            return
        if self.linked("ZoomLinked"):
            self.session.set_range(start, end)
            return
        low, high = log.time[0], log.time[-1]
        span = (high - low) or 1.0
        start = min(max(start, low), high)
        end = min(max(end, start + span / 10000), high)
        self.view.attributes["DisplayTimeRangeStart"] = f"{(start - low) / span:g}"
        self.view.attributes["DisplayTimeRangeEnd"] = f"{(end - low) / span:g}"
        self.saved()

    def cursor(self) -> float:
        return self.session.cursor if self.linked("CursorLinked") else self.local_cursor

    def move_cursor(self, when: float) -> None:
        log = self.session.log
        if log is None or not log.time:
            return
        if self.linked("CursorLinked"):
            self.session.set_cursor(when)
        else:
            self.local_cursor = min(max(when, log.time[0]), log.time[-1])
            self.update()

    def cursor_index(self) -> int:
        return self.session.index_at(self.cursor())

    def on_cursor(self) -> None:
        self.update()

    def on_range(self) -> None:
        self.update()

    def refresh(self) -> None:
        self.update()

    def chosen(self, fallback: bool = True) -> list[tuple[int, int]]:
        """(channel index, display group) of the parameters this view wants that the log holds."""
        log = self.session.log
        if log is None:
            return []
        out = []
        for data_id, group in self.view.log_parameters:
            index = log.channel_of(data_id)
            if index is not None:
                out.append((index, group))
        return out or ([(i, i) for i in range(min(len(log.names), 4))] if fallback and not self.view.log_parameters else [])

    def colour(self, log: Log, index: int, order: int) -> QtGui.QColor:
        return self.session.colour_of(log.series[index].data_id) or self.session.parameter_colour(order)

    def convert(self, log: Log, index: int, value: float) -> float:
        return self.session.display(log.series[index], value)

    def unit(self, log: Log, index: int) -> str:
        return self.session.unit_label(log.series[index]) if index < len(log.series) else ""

    def text_of(self, log: Log, index: int, value: float) -> str:
        places = self.session.decimals(log.series[index])
        shown = self.convert(log, index, value)
        return fixed_number(shown, places) if places is not None else hash_number(shown, 3)

    def set_parameters(self, ids: list[int]) -> None:
        old = dict(self.view.log_parameters)
        spare = max(old.values(), default=-1) + 1
        out = []
        for data_id in ids:
            if data_id in old:
                out.append((data_id, old[data_id]))
            else:
                out.append((data_id, spare))
                spare += 1
        self.view.log_parameters = out
        self.sync_surfaces()
        self.refresh()

    def sync_surfaces(self) -> None:
        pass

    def properties_dialog(self, parent) -> bool:
        from .logdialogs import ViewPropertiesDialog

        if ViewPropertiesDialog(self, parent).exec():
            self.saved()
            return True
        return False


class _PlotGroupMixin:
    """The PlotSurfaceGroup child that time plots and XY plots share."""

    def init_group(self, node: ET.Element, extra: dict[str, str]) -> None:
        group = ET.SubElement(node, "PlotSurfaceGroup", {"DisplayMode": "0", **extra})
        for _ in range(len(self.group_ids())):
            ET.SubElement(group, "PlotSurface", {"Proportion": "-1"})

    def group_node(self) -> ET.Element:
        node = self.node()
        group = node.find("PlotSurfaceGroup")
        if group is None:
            group = ET.SubElement(node, "PlotSurfaceGroup", {"DisplayMode": "0"})
        return group

    def display_mode(self) -> int:
        return int(float(self.group_node().get("DisplayMode", "0") or 0))

    def set_display_mode(self, mode: int) -> None:
        self.group_node().set("DisplayMode", str(mode))
        self.saved()

    def toggle_display_mode(self) -> None:
        self.set_display_mode(STACKED if self.display_mode() == OVERLAPPED else OVERLAPPED)

    def group_ids(self) -> list[int]:
        return sorted({group for _, group in self.view.log_parameters})

    def proportions(self) -> list[float]:
        surfaces = self.group_node().findall("PlotSurface")
        values = [float(s.get("Proportion", "-1") or -1) for s in surfaces]
        count = len(self.group_ids())
        return (values + [-1.0] * count)[:count]

    def set_proportions(self, values: list[float]) -> None:
        group = self.group_node()
        for surface in group.findall("PlotSurface"):
            group.remove(surface)
        for value in values:
            ET.SubElement(group, "PlotSurface", {"Proportion": f"{value:g}"})
        self.saved()

    def sync_surfaces(self) -> None:
        count = len(self.group_ids())
        self.set_proportions((self.proportions() + [-1.0] * count)[:count])

    def shares(self, count: int) -> list[float]:
        """Each strip's share of the height: Proportion, with -1 splitting what is left evenly."""
        values = (self.proportions() + [-1.0] * count)[:count]
        fixed = sum(v for v in values if v > 0)
        free = [i for i, v in enumerate(values) if v <= 0]
        if fixed >= 1 and free or fixed > 1:
            scale = (0.8 if free else 1.0) / fixed
            values = [v * scale if v > 0 else v for v in values]
            fixed = sum(v for v in values if v > 0)
        rest = max(1.0 - fixed, 0.0) / len(free) if free else 0.0
        out = [v if v > 0 else rest for v in values]
        total = sum(out) or 1.0
        return [v / total for v in out]


class _TimeBase(_PlotGroupMixin, _LogView):
    """What the Time Plot and the Navigator share: sections bar, markers, time axis and the slider."""

    SECTION_HEIGHT = 15
    AXIS_HEIGHT = 22
    OFFSET_HEIGHT = 16
    SLIDER_HEIGHT = 16
    Y_AXIS_WIDTH = 46
    STRIP_GAP = 3
    VIEW_DEFAULTS = {"ShowOverlays": "true", "XAxisVisible": "true", "MarkersVisible": "true", "XScrollBarVisible": "true", "OffsetAxisVisible": "true"}
    GROUP_DEFAULTS = {"YAxisVisible": "true", "YScrollBarVisible": "true"}

    def __init__(self, session: LogSession, view: LayoutView | None = None) -> None:
        super().__init__(session, view)
        self.NODE_DEFAULTS = dict(self.VIEW_DEFAULTS)
        self.setMouseTracking(True)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.WheelFocus)
        self.drag: tuple | None = None
        self.zoom_from: float | None = None
        self.zoom_to: float | None = None
        self.selected_group = 0
        self.y_views: dict[int, tuple[float, float]] = {}
        self.node()

    def init_node(self, node: ET.Element) -> None:
        self.init_group(node, self.GROUP_DEFAULTS)

    # ---- settings ------------------------------------------------------

    def group_flag(self, name: str) -> bool:
        return _bool(self.group_node().get(name), True)

    def set_group_flag(self, name: str, value: bool) -> None:
        _put(self.group_node(), name, value)
        self.saved()

    def show_overlays(self) -> bool:
        return self.option("ShowOverlays")

    # ---- geometry ------------------------------------------------------

    def plot_rect(self) -> QtCore.QRectF:
        left = self.Y_AXIS_WIDTH if self.group_flag("YAxisVisible") else 3
        top = self.SECTION_HEIGHT + 3 if self.option("MarkersVisible") else 2
        bottom = self.height() - 2
        if self.option("XScrollBarVisible"):
            bottom -= self.SLIDER_HEIGHT + 2
        if self.option("OffsetAxisVisible") and self.session.overlay_selection() and self.show_overlays() and self.NODE == "TimePlot":
            bottom -= self.OFFSET_HEIGHT
        if self.option("XAxisVisible"):
            bottom -= self.AXIS_HEIGHT
        return QtCore.QRectF(left, top, max(self.width() - left - 3, 10), max(bottom - top, 10))

    def slider_rect(self) -> QtCore.QRectF:
        plot = self.plot_rect()
        return QtCore.QRectF(plot.left(), self.height() - self.SLIDER_HEIGHT - 1, plot.width(), self.SLIDER_HEIGHT)

    def x_of(self, when: float) -> float:
        plot = self.plot_rect()
        start, end = self.time_window()
        return plot.left() + (when - start) / ((end - start) or 1.0) * plot.width()

    def time_of(self, x: float) -> float:
        plot = self.plot_rect()
        start, end = self.time_window()
        return start + (x - plot.left()) / max(plot.width(), 1) * (end - start)

    def groups(self) -> list[list[tuple[int | None, int]]]:
        """Strips in display order. A wanted parameter the log lacks shows as NO DATA, as PCLink does."""
        log = self.session.log
        by_group: dict[int, list[tuple[int | None, int]]] = {}
        wanted = self.view.log_parameters or [(series.data_id, index) for index, series in enumerate(log.series[:4])]
        for data_id, group in wanted:
            by_group.setdefault(group, []).append((log.channel_of(data_id) if data_id else None, data_id))
        return [by_group[key] for key in sorted(by_group)]

    def group_keys(self) -> list[int]:
        return self.group_ids() or list(range(min(len(self.session.log.series), 4)))

    def strips(self, plot: QtCore.QRectF, count: int) -> list[QtCore.QRectF]:
        if self.display_mode() == OVERLAPPED or count < 2:
            return [plot] * count
        gaps = self.STRIP_GAP * (count - 1)
        usable = plot.height() - gaps
        out, top = [], plot.top()
        for share in self.shares(count):
            height = usable * share
            out.append(QtCore.QRectF(plot.left(), top, plot.width(), height))
            top += height + self.STRIP_GAP
        return out

    # ---- ranges --------------------------------------------------------

    def window_indices(self) -> tuple[int, int]:
        log = self.session.log
        start, end = self.time_window()
        first = max(bisect.bisect_left(log.time, start) - 1, 0)
        last = min(bisect.bisect_right(log.time, end) + 1, len(log.time))
        return first, last

    def auto_range(self, members: list[tuple[int | None, int]], first: int, last: int) -> tuple[float, float]:
        log = self.session.log
        lows, highs = [], []
        for index, _ in members:
            if index is None:
                continue
            data = log.channels[index][first:last] or log.channels[index][-1:]
            low, high = self.convert(log, index, min(data)), self.convert(log, index, max(data))
            lows.append(min(low, high))
            highs.append(max(low, high))
        if not lows:
            return -1.0, 1.0
        low, high = min(lows), max(highs)
        if high - low < 1e-9:
            return low - 1, high + 1
        return low, high

    def y_range(self, key: int, members, first: int, last: int) -> tuple[float, float]:
        return self.y_views.get(key) or self.auto_range(members, first, last)

    def y_of(self, value: float, low: float, high: float, strip: QtCore.QRectF) -> float:
        return strip.bottom() - 3 - (value - low) / ((high - low) or 1.0) * (strip.height() - 6)

    # ---- drawing -------------------------------------------------------

    def tcolour(self, name: str) -> QtGui.QColor:
        return self.session.theme_colour(name)

    def draw_markers(self, painter: QtGui.QPainter, log: Log, plot: QtCore.QRectF) -> None:
        if not self.option("MarkersVisible"):
            return
        pen = QtGui.QPen(self.tcolour("Markers"), 1, QtCore.Qt.PenStyle.DashLine)
        small = painter.font()
        small.setPixelSize(10)
        painter.setFont(small)
        start, end = self.time_window()
        for marker in log.markers:
            if not start <= marker.time <= end:
                continue
            x = self.x_of(marker.time)
            painter.setPen(pen)
            painter.drawLine(QtCore.QPointF(x, plot.top()), QtCore.QPointF(x, plot.bottom()))
            if x <= plot.left() + 3:
                continue
            painter.save()
            painter.translate(x - 3, plot.top() + 60)
            painter.rotate(-90)
            painter.drawText(QtCore.QPointF(0, 0), marker.name)
            painter.restore()

    def draw_sections(self, painter: QtGui.QPainter, log: Log, plot: QtCore.QRectF) -> None:
        if not self.option("MarkersVisible"):
            return
        red = self.tcolour("Markers")
        painter.setFont(self.font())
        painter.save()
        painter.setClipRect(QtCore.QRectF(plot.left(), 0, plot.width(), self.SECTION_HEIGHT))
        current = self.session.section_at(self.cursor(), log)
        for section in log.sections():
            left, right = self.x_of(section.start), self.x_of(section.end)
            if right < plot.left() or left > plot.right():
                continue
            box = QtCore.QRectF(left, 1, right - left, self.SECTION_HEIGHT - 2)
            painter.setPen(red)
            painter.drawRect(box)
            painter.setPen(red if current is not section else self.tcolour("ViewText"))
            painter.drawText(box.intersected(QtCore.QRectF(plot.left(), 0, plot.width(), self.SECTION_HEIGHT)), CENTRE, section.name)
        painter.restore()

    def file_origin(self) -> float:
        log = self.session.log
        return log.time[0] if log and log.time else 0.0

    def draw_time_axis(self, painter: QtGui.QPainter, plot: QtCore.QRectF, lines: bool = True) -> None:
        start, end = self.time_window()
        span = (end - start) or 1.0
        step = nice_step(span, max(int(plot.width()) // 110, 1))
        origin = self.file_origin()
        tick = math.ceil((start - origin) / step - 1e-9) * step
        grid = QtGui.QPen(self.tcolour("GridLines"), 1, QtCore.Qt.PenStyle.DotLine)
        text = self.tcolour("ViewText")
        small = painter.font()
        small.setPixelSize(11)
        painter.setFont(small)
        while tick + origin <= end + 1e-9:
            x = self.x_of(tick + origin)
            if lines:
                painter.setPen(grid)
                painter.drawLine(QtCore.QPointF(x, plot.top()), QtCore.QPointF(x, plot.bottom()))
            if self.option("XAxisVisible"):
                painter.setPen(text)
                painter.drawText(QtCore.QRectF(x - 40, plot.bottom() + 2, 80, self.AXIS_HEIGHT - 2), CENTRE, clock_text(tick, step))
            tick += step
        if self.option("XAxisVisible"):
            painter.setPen(text)
            painter.drawText(QtCore.QRectF(plot.left() - self.Y_AXIS_WIDTH + 2, plot.bottom() + 2, self.Y_AXIS_WIDTH, self.AXIS_HEIGHT - 2), LEFT | VCENTRE, "m:s")

    def draw_offset_axis(self, painter: QtGui.QPainter, plot: QtCore.QRectF) -> None:
        chosen = self.session.overlay_selection()
        if not (self.option("OffsetAxisVisible") and chosen and self.show_overlays() and self.NODE == "TimePlot"):
            return
        top = plot.bottom() + (self.AXIS_HEIGHT if self.option("XAxisVisible") else 0)
        start, end = self.time_window()
        step = nice_step((end - start) or 1.0, max(int(plot.width()) // 110, 1))
        base = self.session.section_at(self.cursor())
        base_start = base.start if base else 0.0
        log, section, offset = chosen[0]
        painter.setPen(self.session.overlay_colour(0))
        tick = math.ceil(start / step) * step
        while tick <= end + 1e-9:
            painter.drawText(QtCore.QRectF(self.x_of(tick) - 40, top, 80, self.OFFSET_HEIGHT), CENTRE, clock_text(tick - base_start - offset, step))
            tick += step
        painter.drawText(QtCore.QRectF(plot.left() - self.Y_AXIS_WIDTH + 2, top, self.Y_AXIS_WIDTH, self.OFFSET_HEIGHT), LEFT | VCENTRE, "offset")

    def draw_slider(self, painter: QtGui.QPainter) -> None:
        if not self.option("XScrollBarVisible"):
            return
        log = self.session.log
        track = self.slider_rect()
        colour = self.tcolour("Sliders")
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(colour.darker(250))
        painter.drawRect(track)
        low, high = log.time[0], log.time[-1]
        start, end = self.time_window()
        inner = track.adjusted(12, 2, -12, -2)
        left = inner.left() + (start - low) / ((high - low) or 1) * inner.width()
        right = inner.left() + (end - low) / ((high - low) or 1) * inner.width()
        painter.setBrush(colour)
        painter.drawRect(QtCore.QRectF(left, inner.top(), max(right - left, 6), inner.height()))
        painter.setBrush(self.tcolour("ViewText").darker(150))
        mid = track.center().y()
        painter.drawPolygon(QtGui.QPolygonF([QtCore.QPointF(track.left() + 3, mid), QtCore.QPointF(track.left() + 9, mid - 4), QtCore.QPointF(track.left() + 9, mid + 4)]))
        painter.drawPolygon(QtGui.QPolygonF([QtCore.QPointF(track.right() - 3, mid), QtCore.QPointF(track.right() - 9, mid - 4), QtCore.QPointF(track.right() - 9, mid + 4)]))

    def draw_cursor(self, painter: QtGui.QPainter, plot: QtCore.QRectF) -> None:
        x = self.x_of(self.cursor())
        if plot.left() <= x <= plot.right():
            painter.setPen(QtGui.QPen(self.tcolour("Cursors"), 1))
            painter.drawLine(QtCore.QPointF(x, plot.top()), QtCore.QPointF(x, plot.bottom()))
        if self.zoom_from is not None and self.zoom_to is not None:
            left, right = sorted((self.x_of(self.zoom_from), self.x_of(self.zoom_to)))
            painter.fillRect(QtCore.QRectF(left, plot.top(), right - left, plot.height()), QtGui.QColor(90, 140, 240, 70))

    def draw_overlays(self, painter: QtGui.QPainter, plot: QtCore.QRectF, strips, keys, groups) -> None:
        """Ticked sections from other logs (or the same one), moved to start where the section under the cursor starts."""
        if not self.show_overlays():
            return
        base = self.session.section_at(self.cursor())
        if base is None:
            return
        first, last = self.window_indices()
        log = self.session.log
        for number, (other, section, offset) in enumerate(self.session.overlay_selection()):
            colour = self.session.overlay_colour(number)
            shift = base.start + offset - section.start
            for strip, key, members in zip(strips, keys, groups):
                low, high = self.y_range(key, members, first, last)
                painter.save()
                painter.setClipRect(strip)
                painter.setPen(QtGui.QPen(colour, 1))
                for index, data_id in members:
                    if index is None:
                        continue
                    source = other.channel_of(data_id)
                    if source is None:
                        continue
                    a = bisect.bisect_left(other.time, section.start)
                    b = bisect.bisect_right(other.time, section.end)
                    points = QtGui.QPolygonF([
                        QtCore.QPointF(self.x_of(other.time[i] + shift), self.y_of(self.session.display(other.series[source], other.channels[source][i]), low, high, strip))
                        for i in thin(a, b, int(plot.width()) * 2)
                    ])
                    painter.drawPolyline(points)
                painter.restore()

    def apply_zoom_selection(self) -> None:
        if self.zoom_from is not None and self.zoom_to is not None and abs(self.zoom_to - self.zoom_from) > 0:
            start, end = sorted((self.zoom_from, self.zoom_to))
            self.set_time_window(start, end)
        self.zoom_from = self.zoom_to = None
        self.update()

    # ---- the slider ----------------------------------------------------

    def slider_press(self, x: float) -> None:
        log = self.session.log
        track = self.slider_rect()
        inner = track.adjusted(12, 2, -12, -2)
        low, high = log.time[0], log.time[-1]
        start, end = self.time_window()
        per_pixel = (high - low) / max(inner.width(), 1)
        left = inner.left() + (start - low) / per_pixel
        right = inner.left() + (end - low) / per_pixel
        width = end - start
        if x < track.left() + 12:
            self.set_time_window(start - width / 10, end - width / 10)
        elif x > track.right() - 12:
            self.set_time_window(start + width / 10, end + width / 10)
        elif abs(x - left) < 4:
            self.drag = ("slider-left", per_pixel, end)
        elif abs(x - right) < 4:
            self.drag = ("slider-right", per_pixel, start)
        elif left <= x <= right:
            self.drag = ("slider", per_pixel, x, start, end)
        else:
            self.set_time_window(start + (x - (left + right) / 2) * per_pixel, end + (x - (left + right) / 2) * per_pixel)

    def slider_move(self, x: float) -> None:
        kind = self.drag[0]
        per_pixel = self.drag[1]
        log = self.session.log
        start, end = self.time_window()
        if kind == "slider":
            shift = (x - self.drag[2]) * per_pixel
            self.set_time_window(self.drag[3] + shift, self.drag[4] + shift)
        elif kind == "slider-left":
            inner_left = self.slider_rect().left() + 12
            self.set_time_window(log.time[0] + (x - inner_left) * per_pixel, self.drag[2])
        else:
            inner_left = self.slider_rect().left() + 12
            self.set_time_window(self.drag[2], log.time[0] + (x - inner_left) * per_pixel)

    def mouseReleaseEvent(self, _event: QtGui.QMouseEvent) -> None:
        self.drag = None

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:
        x = event.position().x()
        if self.zoom_from is not None:
            self.zoom_to = self.time_of(x)
            self.update()
        elif self.drag and self.drag[0].startswith("slider") and self.session.log:
            self.slider_move(x)
        elif self.drag and self.drag[0] == "axis" and self.session.log:
            start, end = self.drag[2], self.drag[3]
            shift = (self.drag[1] - x) / max(self.plot_rect().width(), 1) * (end - start)
            self.set_time_window(start + shift, end + shift)
        elif self.drag and self.drag[0] == "offset" and self.session.log:
            self.drag_offset(x)
        elif self.drag and self.drag[0] == "split":
            self.drag_split(event.position().y())
        elif event.buttons() & QtCore.Qt.MouseButton.LeftButton and self.session.log and self.drag and self.drag[0] == "cursor":
            self.move_cursor(self.time_of(x))

    def drag_offset(self, x: float) -> None:
        chosen = self.session.overlay_selection()
        if not chosen:
            return
        log, section, _ = chosen[0]
        start, end = self.time_window()
        change = (x - self.drag[1]) / max(self.plot_rect().width(), 1) * (end - start)
        self.session.set_overlay(log, section, True, self.drag[2] + change)

    def drag_split(self, y: float) -> None:
        plot = self.plot_rect()
        count = len(self.groups())
        if count < 2:
            return
        shares = self.shares(count)
        row = self.drag[1]
        total = shares[row] + shares[row + 1]
        top = plot.top() + sum(shares[:row]) * (plot.height() - self.STRIP_GAP * (count - 1)) + self.STRIP_GAP * row
        fraction = (y - top) / max(plot.height() - self.STRIP_GAP * (count - 1), 1)
        fraction = min(max(fraction, 0.05), total - 0.05)
        shares[row], shares[row + 1] = fraction, total - fraction
        self.set_proportions(shares)

    def split_at(self, y: float) -> int | None:
        plot = self.plot_rect()
        count = len(self.groups())
        if count < 2 or self.display_mode() == OVERLAPPED:
            return None
        for row, strip in enumerate(self.strips(plot, count)[:-1]):
            if abs(y - (strip.bottom() + self.STRIP_GAP / 2)) <= 3:
                return row
        return None

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:
        log = self.session.log
        if log is None or not log.time:
            return
        key, mods = event.key(), event.modifiers()
        K, M = QtCore.Qt.Key, QtCore.Qt.KeyboardModifier
        index = self.cursor_index()
        start, end = self.time_window()
        width = end - start
        if key in (K.Key_Left, K.Key_Right):
            sign = 1 if key == K.Key_Right else -1
            if mods & M.ShiftModifier:
                self.set_time_window(start + sign * width / 10, end + sign * width / 10)
            else:
                step = max(len(log.time) // 100, 10) if mods & M.ControlModifier else 1
                self.move_cursor(log.time[min(max(index + sign * step, 0), len(log.time) - 1)])
        elif key in (K.Key_Up, K.Key_Down):
            zoom_in = key == K.Key_Up
            if mods & M.AltModifier:
                self.zoom_y(0.8 if zoom_in else 1.25)
            elif mods & M.ShiftModifier:
                self.pan_y(1 if zoom_in else -1)
            else:
                self.zoom_x(0.8 if zoom_in else 1.25, self.cursor())
        elif key in (K.Key_Plus, K.Key_Equal):
            self.zoom_x(0.8, self.cursor())
        elif key == K.Key_Minus:
            self.zoom_x(1.25, self.cursor())
        elif key == K.Key_PageUp:
            self.select_group(-1)
        elif key == K.Key_PageDown:
            self.select_group(1)
        elif key == K.Key_Z:
            self.set_time_window(log.time[0], log.time[-1])
            self.y_views.clear()
        elif key == K.Key_G and self.NODE == "TimePlot":
            self.toggle_display_mode()
        elif key == K.Key_L:
            self.set_option("ShowOverlays", not self.show_overlays())
        elif key == K.Key_O and self.NODE == "TimePlot":
            self.set_option("OffsetAxisVisible", not self.option("OffsetAxisVisible"))
        elif key == K.Key_Escape and self.zoom_from is not None:
            self.zoom_from = self.zoom_to = None
            self.update()
        else:
            super().keyPressEvent(event)

    def zoom_x(self, factor: float, around: float) -> None:
        start, end = self.time_window()
        self.set_time_window(around - (around - start) * factor, around + (end - around) * factor)

    def current_key(self) -> int | None:
        keys = self.group_keys()
        return keys[min(self.selected_group, len(keys) - 1)] if keys else None

    def select_group(self, step: int) -> None:
        count = len(self.group_keys())
        self.selected_group = min(max(self.selected_group + step, 0), max(count - 1, 0))
        self.update()

    def current_members(self, key: int | None):
        if key is None:
            return None
        keys = self.group_keys()
        return self.groups()[keys.index(key)] if key in keys else None

    def zoom_y(self, factor: float) -> None:
        key = self.current_key()
        members = self.current_members(key)
        if key is None or not members:
            return
        low, high = self.y_range(key, members, *self.window_indices())
        centre = (low + high) / 2
        half = (high - low) / 2 * factor
        self.y_views[key] = (centre - half, centre + half)
        self.update()

    def pan_y(self, direction: int) -> None:
        key = self.current_key()
        members = self.current_members(key)
        if key is None or not members:
            return
        low, high = self.y_range(key, members, *self.window_indices())
        shift = (high - low) / 10 * direction
        self.y_views[key] = (low + shift, high + shift)
        self.update()

    def wheelEvent(self, event: QtGui.QWheelEvent) -> None:
        if event.angleDelta().y() == 0:
            return
        factor = 0.8 if event.angleDelta().y() > 0 else 1.25
        if event.modifiers() & QtCore.Qt.KeyboardModifier.AltModifier or event.position().x() < self.plot_rect().left():
            self.zoom_y(factor)
        else:
            self.zoom_x(factor, self.time_of(event.position().x()))

    def populate_menu(self, menu: QtWidgets.QMenu) -> None:
        if self.NODE == "TimePlot":
            mode = menu.addAction("Display Mode Overlapped" if self.display_mode() == STACKED else "Display Mode Stacked", self.toggle_display_mode)
            mode.setShortcut("G")
        overlays = menu.addAction("Show Overlays", lambda: self.set_option("ShowOverlays", not self.show_overlays()))
        overlays.setCheckable(True)
        overlays.setChecked(self.show_overlays())
        menu.addAction("Zoom Out Time Axis", lambda: self.keyPressEvent(_key(QtCore.Qt.Key.Key_Z)))


def end_of(log: Log) -> float:
    return log.time[-1] if log.time else 0.0


class TimePlot(_TimeBase):
    """Parameters against time, one strip per display group, with sections, markers and a cursor."""

    NODE = "TimePlot"

    def paintEvent(self, _event: QtGui.QPaintEvent) -> None:
        painter = QtGui.QPainter(self)
        background = self.tcolour("DataAreaBackground")
        painter.fillRect(self.rect(), background)
        log = self.session.log
        if log is None or len(log.time) < 2:
            painter.setPen(self.tcolour("ViewText"))
            painter.drawText(self.rect(), CENTRE, "No log file open")
            return
        plot = self.plot_rect()
        groups = self.groups()
        keys = self.group_keys()
        strips = self.strips(plot, len(groups))
        first, last = self.window_indices()
        stacked = self.display_mode() == STACKED
        border, text = self.tcolour("DataAreaBorder"), self.tcolour("ViewText")
        painter.setPen(text)
        painter.drawRect(plot.adjusted(-0.5, -0.5, 0.5, 0.5))
        self.draw_sections(painter, log, plot)
        self.draw_time_axis(painter, plot, lines=False)
        painter.setPen(border)
        for strip in (strips if stacked else strips[:1]):
            painter.fillRect(strip, background)
            painter.drawRect(strip.adjusted(0, 0, -1, -1) if len(strips) > 1 and stacked else strip.adjusted(0, 0, 0, 0))
        order = 0
        orders = []
        for _ in groups:
            orders.append(order)
            order += len(_)
        for row, (members, strip, key) in enumerate(zip(groups, strips, keys)):
            low, high = self.y_range(key, members, first, last)
            painter.save()
            painter.setClipRect(strip)
            self.draw_grid(painter, strip, low, high)
            painter.restore()
            if stacked or row == self.selected_group or row == 0 and len(strips) == 1:
                self.draw_y_axis(painter, strip, low, high, row == self.selected_group and len(groups) > 1)
        for row, (members, strip, key) in enumerate(zip(groups, strips, keys)):
            low, high = self.y_range(key, members, first, last)
            painter.save()
            painter.setClipRect(strip)
            for line, (index, data_id) in enumerate(members):
                self.draw_member(painter, log, index, data_id, strip, low, high, first, last, orders[row] + line, line if stacked else orders[row] + line)
            painter.restore()
        self.draw_overlays(painter, plot, strips, keys, groups)
        self.draw_markers(painter, log, plot)
        self.draw_cursor(painter, plot)
        self.draw_offset_axis(painter, plot)
        self.draw_slider(painter)

    def draw_grid(self, painter: QtGui.QPainter, strip: QtCore.QRectF, low: float, high: float) -> None:
        painter.setPen(QtGui.QPen(self.tcolour("GridLines"), 1, QtCore.Qt.PenStyle.DotLine))
        for value in _ticks(low, high, max(int(strip.height()) // 28, 1)):
            y = self.y_of(value, low, high, strip)
            painter.drawLine(QtCore.QPointF(strip.left(), y), QtCore.QPointF(strip.right(), y))
        plot = self.plot_rect()
        start, end = self.time_window()
        step = nice_step((end - start) or 1.0, max(int(plot.width()) // 110, 1))
        origin = self.file_origin()
        tick = math.ceil((start - origin) / step - 1e-9) * step
        while tick + origin <= end + 1e-9:
            x = self.x_of(tick + origin)
            painter.drawLine(QtCore.QPointF(x, strip.top()), QtCore.QPointF(x, strip.bottom()))
            tick += step

    def draw_y_axis(self, painter: QtGui.QPainter, strip: QtCore.QRectF, low: float, high: float, selected: bool) -> None:
        if not self.group_flag("YAxisVisible"):
            return
        small = painter.font()
        small.setPixelSize(11)
        painter.setFont(small)
        painter.setPen(self.tcolour("ViewText"))
        for value in _ticks(low, high, max(int(strip.height()) // 28, 1)):
            y = self.y_of(value, low, high, strip)
            painter.drawText(QtCore.QRectF(0, y - 7, strip.left() - 3, 14), RIGHT | VCENTRE, f"{value:g}")
        if self.group_flag("YScrollBarVisible"):
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.setBrush(self.tcolour("Sliders"))
            painter.drawRect(QtCore.QRectF(strip.right() - 3, strip.top() + 1, 2, max(strip.height() - 2, 1)))
            painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)

    def draw_member(self, painter, log: Log, index, data_id: int, strip, low, high, first, last, order: int, line: int) -> None:
        small = painter.font()
        small.setPixelSize(11)
        small.setBold(False)
        painter.setFont(small)
        row_top = strip.top() + 2 + line * 15
        if index is None:
            label = f"{NO_DATA} - {self.session.describe(data_id)}"
            width = painter.fontMetrics().horizontalAdvance(label) + 6
            painter.fillRect(QtCore.QRectF(strip.left() + 1, row_top, width, 14), QtGui.QColor(32, 32, 32))
            painter.setPen(QtGui.QColor(160, 160, 160))
            painter.drawText(QtCore.QPointF(strip.left() + 4, row_top + 11), label)
            return
        colour = self.colour(log, index, order)
        data = log.channels[index]
        points = QtGui.QPolygonF([
            QtCore.QPointF(self.x_of(log.time[i]), self.y_of(self.convert(log, index, data[i]), low, high, strip))
            for i in thin(first, last, int(strip.width()) * 2)
        ])
        painter.setPen(QtGui.QPen(colour, 1))
        painter.drawPolyline(points)
        at = self.cursor_index()
        value = self.convert(log, index, data[at])
        cx = self.x_of(self.cursor())
        if strip.left() <= cx <= strip.right():
            painter.setPen(QtGui.QPen(self.tcolour("Cursors"), 3))
            y = self.y_of(value, low, high, strip)
            painter.drawLine(QtCore.QPointF(cx - 6, y), QtCore.QPointF(cx + 6, y))
        unit = self.unit(log, index)
        label = f"{log.names[index]}{f' ({unit})' if unit else ''}  {self.text_of(log, index, data[at])}"
        width = painter.fontMetrics().horizontalAdvance(label) + 6
        painter.fillRect(QtCore.QRectF(strip.left() + 1, row_top, width, 14), QtGui.QColor(32, 32, 32))
        painter.setPen(colour)
        painter.drawText(QtCore.QPointF(strip.left() + 4, row_top + 11), label)

    # ---- mouse ---------------------------------------------------------

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        self.setFocus()
        log = self.session.log
        if log is None or len(log.time) < 2 or event.button() != QtCore.Qt.MouseButton.LeftButton:
            return
        x, y = event.position().x(), event.position().y()
        plot = self.plot_rect()
        if self.zoom_from is not None:
            self.zoom_to = self.time_of(x)
            self.apply_zoom_selection()
            return
        if self.option("XScrollBarVisible") and self.slider_rect().contains(event.position()):
            self.slider_press(x)
            return
        row = self.split_at(y)
        if row is not None:
            self.drag = ("split", row)
        elif y < plot.top() and self.option("MarkersVisible"):
            section = self.session.section_at(self.time_of(x), log)
            if section:
                self.set_time_window(section.start, section.end)
                self.move_cursor(section.start)
        elif y > plot.bottom() and self.option("XAxisVisible") and y <= plot.bottom() + self.AXIS_HEIGHT:
            start, end = self.time_window()
            self.drag = ("axis", x, start, end)
        elif y > plot.bottom() + (self.AXIS_HEIGHT if self.option("XAxisVisible") else 0) and y < self.slider_rect().top() and self.session.overlay_selection():
            chosen = self.session.overlay_selection()[0]
            self.drag = ("offset", x, chosen[2])
        elif x < plot.left():
            self.select_row_at(y)
        else:
            self.select_row_at(y)
            self.drag = ("cursor",)
            self.move_cursor(self.time_of(x))

    def select_row_at(self, y: float) -> None:
        strips = self.strips(self.plot_rect(), len(self.groups()))
        for row, strip in enumerate(strips):
            if strip.top() <= y <= strip.bottom() and self.display_mode() == STACKED:
                self.selected_group = row
                self.update()
                return

    def mouseDoubleClickEvent(self, event: QtGui.QMouseEvent) -> None:
        log = self.session.log
        if log is None or event.position().y() < self.plot_rect().top():
            return
        self.zoom_from = self.zoom_to = self.time_of(event.position().x())
        self.update()


class Navigator(_TimeBase):
    """The whole log, small, with the zoomed part marked. Dragging moves the zoomed part."""

    NODE = "Navigator"
    VIEW_DEFAULTS = {"ShowOverlays": "false", "XAxisVisible": "true", "MarkersVisible": "true", "XScrollBarVisible": "false", "OffsetAxisVisible": "false"}
    GROUP_DEFAULTS = {"YAxisVisible": "false", "YScrollBarVisible": "false"}

    def time_window(self) -> tuple[float, float]:
        log = self.session.log
        return (log.time[0], log.time[-1]) if log and len(log.time) > 1 else (0.0, 1.0)

    def view_window(self) -> tuple[float, float]:
        return self.session.start, self.session.end

    def set_time_window(self, start: float, end: float) -> None:
        self.session.set_range(start, end)

    def plot_rect(self) -> QtCore.QRectF:
        rect = super().plot_rect()
        return rect

    def paintEvent(self, _event: QtGui.QPaintEvent) -> None:
        painter = QtGui.QPainter(self)
        painter.fillRect(self.rect(), self.tcolour("DataAreaBackground"))
        log = self.session.log
        if log is None or len(log.time) < 2:
            return
        plot = self.plot_rect()
        painter.setPen(self.tcolour("ViewText"))
        painter.drawRect(plot.adjusted(-0.5, -0.5, 0.5, 0.5))
        self.draw_sections(painter, log, plot)
        painter.save()
        painter.setClipRect(plot)
        self.draw_time_axis(painter, plot, lines=True)
        painter.setPen(QtGui.QPen(self.tcolour("GridLines"), 1, QtCore.Qt.PenStyle.DotLine))
        for step in range(1, 4):
            y = plot.top() + plot.height() * step / 4
            painter.drawLine(QtCore.QPointF(plot.left(), y), QtCore.QPointF(plot.right(), y))
        small = painter.font()
        small.setPixelSize(11)
        painter.setFont(small)
        members = self.groups()
        chips = 0
        for order, (index, data_id) in enumerate(m for group in members for m in group):
            if index is None:
                continue
            data = log.channels[index]
            low, high = min(data), max(data)
            high = high if high > low else low + 1
            painter.setPen(QtGui.QPen(self.colour(log, index, order), 1))
            painter.drawPolyline(QtGui.QPolygonF([
                QtCore.QPointF(self.x_of(log.time[i]), plot.bottom() - 3 - (data[i] - low) / (high - low) * (plot.height() - 6))
                for i in thin(0, len(data), int(plot.width()))
            ]))
            label = log.names[index]
            width = painter.fontMetrics().horizontalAdvance(label) + 6
            painter.fillRect(QtCore.QRectF(plot.left() + 1, plot.top() + 2 + chips * 15, width, 14), QtGui.QColor(32, 32, 32))
            painter.drawText(QtCore.QPointF(plot.left() + 4, plot.top() + 13 + chips * 15), label)
            chips += 1
        painter.restore()
        self.draw_markers(painter, log, plot)
        start, end = self.view_window()
        left, right = self.x_of(start), self.x_of(end)
        box = QtCore.QRectF(left, plot.top() + plot.height() * 0.25, max(right - left, 3), plot.height() * 0.5)
        painter.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255), 2))
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        painter.drawRect(box)
        self.draw_cursor(painter, plot)

    def _centre(self, x: float) -> None:
        start, end = self.view_window()
        span = end - start
        when = self.time_of(x)
        self.session.set_range(when - span / 2, when + span / 2)

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        log = self.session.log
        if log is None or len(log.time) < 2:
            return
        self.setFocus()
        plot = self.plot_rect()
        x, y = event.position().x(), event.position().y()
        if y < plot.top() and self.option("MarkersVisible"):
            section = self.session.section_at(self.time_of(x), log)
            if section:
                self.session.set_range(section.start, section.end)
            return
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self._centre(x)
            self.drag = ("navigate",)

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:
        if self.session.log and self.drag and self.drag[0] == "navigate" and event.buttons():
            self._centre(event.position().x())

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:
        if event.key() in (QtCore.Qt.Key.Key_Left, QtCore.Qt.Key.Key_Right, QtCore.Qt.Key.Key_Up, QtCore.Qt.Key.Key_Down, QtCore.Qt.Key.Key_Z, QtCore.Qt.Key.Key_L):
            if event.key() == QtCore.Qt.Key.Key_L:
                self.set_option("ShowOverlays", not self.show_overlays())
                return
            super().keyPressEvent(event)
        else:
            super().keyPressEvent(event)

    def zoom_x(self, factor: float, around: float) -> None:
        self.session.zoom(factor, around)

    def wheelEvent(self, event: QtGui.QWheelEvent) -> None:
        self.session.zoom(0.8 if event.angleDelta().y() > 0 else 1.25, self.session.cursor)


def _columns(view: LayoutView, count: int, default: int) -> list[int]:
    widths = [int(float(w)) for w in view.attributes.get("ColumnWidths", "").split(",") if w.strip()]
    return (widths + [default] * count)[:count]


class _TableView(_LogView):
    HEADERS: tuple[str, ...] = ()
    DEFAULT_WIDTH = 75

    def __init__(self, session: LogSession, view: LayoutView | None = None) -> None:
        super().__init__(session, view)
        self.table = QtWidgets.QTableWidget(0, len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(list(self.HEADERS))
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(20)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.NoSelection)
        self.table.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.table.horizontalHeader().setHighlightSections(False)
        self.table.horizontalHeader().setSectionsClickable(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setDefaultAlignment(LEFT | VCENTRE)
        self.table.setStyleSheet(
            "QTableWidget{background:#000;alternate-background-color:#141414;color:#f5f5f5;}"
            "QHeaderView::section{background:#555;color:#fff;font-weight:bold;border:0;padding-left:4px;text-align:left;}"
        )
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.table)
        self.apply_widths()
        self.table.horizontalHeader().sectionResized.connect(self._resized)
        self.refresh()

    def apply_widths(self) -> None:
        self.widths_loading = True
        for column, width in enumerate(_columns(self.view, self.table.columnCount(), self.DEFAULT_WIDTH)):
            self.table.setColumnWidth(column, width)
        self.widths_loading = False

    def _resized(self, _column: int, _old: int, _new: int) -> None:
        if getattr(self, "widths_loading", True):
            return
        header = self.table.horizontalHeader()
        self.view.attributes["ColumnWidths"] = ",".join(str(header.sectionSize(c)) for c in range(self.table.columnCount()))
        if self.changed_hook:
            self.changed_hook()

    def fill(self, rows: list[list[str]], colours: list[QtGui.QColor | None] | None = None) -> None:
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, text in enumerate(row):
                item = self.table.item(r, c)
                if item is None:
                    item = QtWidgets.QTableWidgetItem(text)
                    self.table.setItem(r, c, item)
                else:
                    item.setText(text)
                colour = colours[r] if colours and colours[r] is not None else None
                item.setForeground(colour if colour is not None else QtGui.QBrush(TEXT_COLOUR))

    def rows(self) -> list[list[str]]:
        return []

    def refresh(self) -> None:
        self.fill(self.rows())
        self.update()

    def on_cursor(self) -> None:
        self.refresh()

    def on_range(self) -> None:
        self.refresh()

    def listed(self) -> list[tuple[int | None, int]]:
        """(channel or None for no data, DataId) for each parameter the view lists, in its order."""
        log = self.session.log
        if log is None:
            return []
        if not self.view.log_parameters:
            return [(index, series.data_id) for index, series in enumerate(log.series)]
        return [(log.channel_of(data_id) if data_id else None, data_id) for data_id, _ in self.view.log_parameters]


class ParameterList(_TableView):
    """Section and file time at the cursor, then each listed parameter with its value."""

    HEADERS = ("Parameter", "Value", "Units")
    DEFAULT_WIDTH = 75

    def apply_widths(self) -> None:
        self.widths_loading = True
        for column, width in enumerate(_columns(self.view, 3, 75)):
            self.table.setColumnWidth(column, width)
        self.widths_loading = False

    def refresh(self) -> None:
        log = self.session.log
        rows, colours = [], []
        if log is not None and log.time:
            when = self.cursor()
            section = self.session.section_at(when)
            rows.append(["Section Time", hms(when - section.start) if section else hms(when), ""])
            rows.append(["File Time", hms(when - log.time[0]), ""])
            rows.append(["", "", ""])
            colours += [None, None, None]
            at = self.cursor_index()
            for order, (index, data_id) in enumerate(self.listed()):
                if index is None:
                    rows.append([self.session.describe(data_id), NO_DATA, ""])
                    colours.append(QtGui.QColor(128, 128, 128))
                else:
                    rows.append([log.names[index], self.text_of(log, index, log.channels[index][at]), self.unit(log, index)])
                    colours.append(self.colour(log, index, order))
        self.fill(rows, colours)
        if rows:
            self.table.setRowHeight(2, 8)
            for column in range(3):
                self.table.item(2, column).setBackground(QtGui.QColor(85, 85, 85))

    def rows(self) -> list[list[str]]:
        self.refresh()
        return [[self.table.item(r, c).text() for c in range(3)] for r in range(self.table.rowCount())]


class LoggedValues(_TableView):
    """Chosen parameters sample by sample, with the cursor's sample in the middle row."""

    def __init__(self, session: LogSession, view: LayoutView | None = None) -> None:
        self.HEADERS = ("Time (s)",)
        super().__init__(session, view)

    def refresh(self) -> None:
        log = self.session.log
        if log is None:
            self.table.setRowCount(0)
            return
        listed = self.listed()
        self.table.setColumnCount(len(listed) + 1)
        self.table.setHorizontalHeaderLabels(["Time (s)", *(log.names[i] if i is not None else self.session.describe(d) for i, d in listed)])
        self.apply_widths()
        at = self.cursor_index()
        half = max(self.table.viewport().height() // 20 // 2, 4)
        blank = [""] * (len(listed) + 1)
        rows = []
        for i in range(at - half, at + half + 1):
            if 0 <= i < len(log.time):
                rows.append([hms(log.time[i] - log.time[0]), *(NO_DATA if c is None else self.text_of(log, c, log.channels[c][i]) for c, _ in listed)])
            else:
                rows.append(blank)
        self.fill(rows)
        for column, (index, data_id) in enumerate(listed, 1):
            colour = QtGui.QColor(128, 128, 128) if index is None else self.colour(log, index, column - 1)
            for row in range(self.table.rowCount()):
                self.table.item(row, column).setForeground(colour)
            self.table.horizontalHeaderItem(column).setForeground(colour)
        for row in range(self.table.rowCount()):
            font = self.table.item(row, 0).font()
            font.setBold(True)
            self.table.item(row, 0).setFont(font)
        for column in range(self.table.columnCount()):
            self.table.item(half, column).setBackground(QtGui.QColor(48, 48, 48))


class StatisticsView(_TableView):
    """Figures for each parameter across the time range on show."""

    HEADERS = ("Parameter", "Units", "Sample Rate", "Samples", "Length", "Min", "Max", "Range", "Mean", "S.D.")
    DEFAULT_WIDTH = 75

    def apply_widths(self) -> None:
        self.widths_loading = True
        for column, width in enumerate(_columns(self.view, 10, 75)):
            self.table.setColumnWidth(column, width if self.view.attributes.get("ColumnWidths") else (150 if column == 0 else 75))
        self.widths_loading = False

    def on_cursor(self) -> None:
        pass

    def figures(self, log: Log, index: int) -> ChannelStats | None:
        series = log.series[index]
        if not series.times:
            return None
        if self.linked("ZoomLinked"):
            start, end = self.session.start, self.session.end
        else:
            start, end = series.times[0], series.times[-1]
        return series_statistics(series.times, series.values, start, end)

    def rows(self) -> list[list[str]]:
        log = self.session.log
        if log is None or not log.time:
            return []
        rows = []
        for index, data_id in self.listed():
            if index is None:
                rows.append([self.session.describe(data_id), "", "-", NO_DATA, "", "", "", "", "", ""])
                self.row_colours.append(QtGui.QColor(128, 128, 128))
                continue
            figures = self.figures(log, index)
            series = log.series[index]
            if figures is None:
                continue
            show = lambda value: hash_number(self.convert(log, index, value))  # noqa: E731
            spread = lambda value: hash_number(self.session.span(series, value))  # noqa: E731
            rows.append([
                log.names[index], self.unit(log, index), "-", str(figures.samples), hash_number(figures.length),
                show(figures.minimum), show(figures.maximum), spread(figures.maximum - figures.minimum),
                show(figures.average), spread(figures.deviation),
            ])
            self.row_colours.append(self.colour(log, index, len(rows) - 1))
        return rows

    def refresh(self) -> None:
        self.row_colours: list[QtGui.QColor | None] = []
        rows = self.rows()
        self.fill(rows, self.row_colours)
        for row in range(self.table.rowCount()):
            for column in range(3, 10):
                self.table.item(row, column).setTextAlignment(RIGHT | VCENTRE)
        self.update()


class _AxesPlot(_LogView):
    LEFT_MARGIN = 46
    AXIS_ROWS = 32

    def outer_area(self) -> QtCore.QRectF:
        return QtCore.QRectF(self.LEFT_MARGIN, 4, max(self.width() - self.LEFT_MARGIN - 4, 10), max(self.height() - 8 - self.AXIS_ROWS, 10))

    def plot_area(self) -> QtCore.QRectF:
        return self.outer_area()

    def draw_axes(self, painter: QtGui.QPainter, x_low: float, x_high: float, y_low: float, y_high: float, x_name: str, y_name: str, area: QtCore.QRectF | None = None, show_y: bool = True, name_colour: QtGui.QColor | None = None) -> None:
        area = area or self.plot_area()
        text = self.session.theme_colour("ViewText")
        grid = QtGui.QPen(self.session.theme_colour("GridLines"), 1, QtCore.Qt.PenStyle.DotLine)
        small = painter.font()
        small.setPixelSize(11)
        painter.setFont(small)
        painter.fillRect(area, self.session.theme_colour("DataAreaBackground"))
        for value in _ticks(x_low, x_high, max(int(area.width()) // 64, 2)):
            x = area.left() + (value - x_low) / ((x_high - x_low) or 1) * area.width()
            painter.setPen(grid)
            painter.drawLine(QtCore.QPointF(x, area.top()), QtCore.QPointF(x, area.bottom()))
            if x - area.left() >= 14 and area.right() - x >= 14:
                painter.setPen(text)
                painter.drawText(QtCore.QRectF(x - 30, area.bottom() + 1, 60, 14), CENTRE, f"{value:g}")
        for value in _ticks(y_low, y_high, max(int(area.height()) // 20, 2)):
            y = area.bottom() - (value - y_low) / ((y_high - y_low) or 1) * area.height()
            painter.setPen(grid)
            painter.drawLine(QtCore.QPointF(area.left(), y), QtCore.QPointF(area.right(), y))
            if show_y:
                painter.setPen(text)
                painter.drawText(QtCore.QRectF(area.left() - 46, y - 7, 43, 14), RIGHT | VCENTRE, f"{value:g}")
        if x_name:
            bar = QtCore.QRectF(area.left(), area.bottom() + 15, area.width(), 15)
            painter.fillRect(bar, QtGui.QColor(61, 61, 61))
            painter.setPen(name_colour or text)
            painter.drawText(bar, CENTRE, x_name)
        if y_name:
            painter.setPen(text)
            painter.drawText(QtCore.QRectF(area.left() + 4, area.top() + 2, area.width(), 14), LEFT, y_name)

    def no_data_chip(self, painter: QtGui.QPainter, area: QtCore.QRectF, line: int = 0, text: str = NO_DATA) -> None:
        width = painter.fontMetrics().horizontalAdvance(text) + 6
        painter.fillRect(QtCore.QRectF(area.left() + 1, area.top() + 2 + line * 15, width, 14), QtGui.QColor(61, 61, 61))
        painter.setPen(QtGui.QColor(160, 160, 160))
        painter.drawText(QtCore.QPointF(area.left() + 4, area.top() + 13 + line * 15), text)


def _ticks(low: float, high: float, count: int) -> list[float]:
    if high <= low:
        return [low]
    step = nice_step(high - low, count)
    first = math.ceil(low / step - 1e-9)
    return [i * step for i in range(first, math.floor(high / step + 1e-9) + 1)]


@dataclass
class XYSettings:
    """What a layout stores for an XY plot, a histogram or a GPS plot."""

    x_id: int = 0
    colour_id: int = 0
    use_colour: bool = False
    manual: bool = False
    manual_min: float = 0.0
    manual_max: float = 1.0
    y_manual: bool = False
    y_min: float = 0.0
    y_max: float = 1.0
    show_legend: bool = True
    show_overlays: bool = True
    show_lines: bool = False
    line_width: int = 1
    show_points: bool = True
    point_size: int = 1
    point_style: int = 0
    spans: tuple = ()
    range_name: str = "Unnamed"

    @classmethod
    def from_node(cls, node: ET.Element | None) -> "XYSettings":
        if node is None:
            return cls()
        spans = []
        for span in node.iter("ColorSpan"):
            spans.append((float(span.get("LowerBound", "0")), float(span.get("UpperBound", "0")), tcolor(int(span.get("Color", "0")))))
        colour_range = node.find("ColorRange")

        def number(name: str, default: float) -> float:
            try:
                return float(node.get(name, default))
            except ValueError:
                return default

        return cls(
            x_id=int(number("XAxisParameterDataID", 0)), colour_id=int(number("ColorRangeParameterDataID", 0)),
            use_colour=node.get("UseColorRange") == "true", manual=node.get("ManualRange") == "true",
            manual_min=number("ManualMin", 0), manual_max=number("ManualMax", 1),
            y_manual=node.get("YManualRange") == "true", y_min=number("YManualMin", 0), y_max=number("YManualMax", 1),
            show_legend=node.get("ShowColorLegend") != "false", show_overlays=node.get("ShowOverlays") != "false",
            show_lines=node.get("ShowLines") == "true", line_width=int(number("LineWidth", 1)),
            show_points=node.get("ShowPoints") != "false", point_size=int(number("PointSize", 1)),
            point_style=int(number("PointStyle", 0)), spans=tuple(spans),
            range_name=colour_range.get("Name", "Unnamed") if colour_range is not None else "Unnamed",
        )

    @classmethod
    def from_view(cls, view: LayoutView) -> "XYSettings":
        return cls.from_node(view.part("XYPlot"))

    def write(self, node: ET.Element, keys: tuple[str, ...] | None = None) -> None:
        values = {
            "ShowOverlays": self.show_overlays, "XAxisParameterDataID": self.x_id, "ColorRangeParameterDataID": self.colour_id,
            "UseColorRange": self.use_colour, "ManualRange": self.manual, "ManualMin": f"{self.manual_min:g}", "ManualMax": f"{self.manual_max:g}",
            "YManualRange": self.y_manual, "YManualMin": f"{self.y_min:g}", "YManualMax": f"{self.y_max:g}",
            "ShowColorLegend": self.show_legend, "ShowLines": self.show_lines, "LineWidth": self.line_width,
            "ShowPoints": self.show_points, "PointSize": self.point_size, "PointStyle": self.point_style,
        }
        for key, value in values.items():
            if keys is None or key in keys:
                _put(node, key, value)
        old = node.find("ColorRange")
        if old is not None:
            node.remove(old)
        colour_range = ET.SubElement(node, "ColorRange", {"Name": self.range_name})
        spans = ET.SubElement(colour_range, "ColorRanges")
        for low, high, colour in self.spans:
            ET.SubElement(spans, "ColorSpan", {"LowerBound": f"{low:g}", "UpperBound": f"{high:g}", "Color": str(to_tcolor(colour))})


XY_KEYS = ("ShowOverlays", "XAxisParameterDataID", "ColorRangeParameterDataID", "UseColorRange", "ManualRange", "ManualMin", "ManualMax", "ShowColorLegend", "ShowLines", "LineWidth", "ShowPoints", "PointSize", "PointStyle", "YManualRange", "YManualMin", "YManualMax")


class _SettingsPlot(_PlotGroupMixin, _AxesPlot):
    """What XY plots, histograms and the GPS plot share: the settings node, colour range, legend and keys."""

    NODE = "XYPlot"
    KEYS = XY_KEYS

    def __init__(self, session: LogSession, view: LayoutView | None = None) -> None:
        super().__init__(session, view)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.WheelFocus)
        existing = self.view.part(self.NODE)
        self.settings = XYSettings.from_node(existing)
        self.settings_loaded = existing is not None
        if existing is None and self.NODE != "XYPlot":
            self.settings.show_points = True
        self.node()
        self.store()

    def init_node(self, node: ET.Element) -> None:
        group = ET.SubElement(node, "PlotSurfaceGroup", {"DisplayMode": "0", "ColumnCount": "-1"})
        del group

    def store(self) -> None:
        self.settings.write(self.node(), self.KEYS)

    def saved(self) -> None:
        self.store()
        super().saved()

    def sync_surfaces(self) -> None:
        pass

    def proportions(self) -> list[float]:
        return []

    def display_mode(self) -> int:
        return int(float(self.group_node().get("DisplayMode", "0") or 0))

    def axis_window(self, log: Log) -> tuple[int, int]:
        start, end = self.time_window()
        first = bisect.bisect_left(log.time, start)
        last = bisect.bisect_right(log.time, end)
        return (first, last) if last > first else (0, len(log.time))

    def colour_at(self, log: Log, index: int, order: int, colour_channel: int | None, position: int) -> QtGui.QColor:
        if colour_channel is not None and self.settings.spans:
            value = self.session.display(log.series[colour_channel], log.channels[colour_channel][position])
            colour = color_for(value, list(self.settings.spans))
            if colour is not None:
                return colour
        return self.colour(log, index, order)

    def draw_legend(self, painter: QtGui.QPainter, area: QtCore.QRectF, log: Log | None = None) -> None:
        """PCLink's boxed list of colour ranges, "low ... high", in the top right of the plot."""
        spans = self.settings.spans
        if not (self.settings.use_colour and self.settings.show_legend and spans):
            return
        small = painter.font()
        small.setPixelSize(11)
        painter.setFont(small)
        labels = []
        for slot, (low, high, _colour) in enumerate(spans):
            labels.append(f"{'' if slot == 0 else f'{low:g} '}... {'' if slot == len(spans) - 1 else f'{high:g}'}".strip())
        metrics = painter.fontMetrics()
        width = max(metrics.horizontalAdvance(text) for text in labels) + 22
        row = 13
        box = QtCore.QRectF(area.right() - width - 3, area.top() + 3, width, len(labels) * row + 4)
        painter.fillRect(box, QtGui.QColor(0, 0, 0))
        painter.setPen(self.session.theme_colour("ViewText"))
        painter.drawRect(box)
        for slot, ((_l, _h, colour), text) in enumerate(zip(spans, labels)):
            top = box.top() + 2 + slot * row
            painter.fillRect(QtCore.QRectF(box.left() + 3, top + 1, 8, 10), colour)
            painter.setPen(self.session.theme_colour("ViewText"))
            painter.drawText(QtCore.QRectF(box.left() + 14, top, width - 14, row), LEFT | VCENTRE, text)

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:
        key = event.key()
        K = QtCore.Qt.Key
        if key == K.Key_G:
            self.group_node().set("DisplayMode", str(STACKED if self.display_mode() == OVERLAPPED else OVERLAPPED))
            self.saved()
        elif key == K.Key_L:
            self.settings.show_overlays = not self.settings.show_overlays
            self.saved()
        elif key == K.Key_H:
            self.settings.use_colour = not self.settings.use_colour
            self.saved()
        else:
            super().keyPressEvent(event)

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        self.setFocus()

    def populate_menu(self, menu: QtWidgets.QMenu) -> None:
        for title, key, state, toggle in (
            ("Overlapped", "G", self.display_mode() == OVERLAPPED, lambda: self.keyPressEvent(_key(QtCore.Qt.Key.Key_G))),
            ("Show Overlays", "L", self.settings.show_overlays, lambda: self.keyPressEvent(_key(QtCore.Qt.Key.Key_L))),
            ("Use Colour Range", "H", self.settings.use_colour, lambda: self.keyPressEvent(_key(QtCore.Qt.Key.Key_H))),
        ):
            action = menu.addAction(title, toggle)
            action.setShortcut(key)
            action.setCheckable(True)
            action.setChecked(state)

    def overlay_logs(self) -> list[tuple[Log, object, float]]:
        return self.session.overlay_selection() if self.settings.show_overlays else []


def _key(code) -> QtGui.QKeyEvent:
    return QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress, code, QtCore.Qt.KeyboardModifier.NoModifier)


def draw_point(painter: QtGui.QPainter, at: QtCore.QPointF, style: int, size: int) -> None:
    half = max(size, 1) + 0.5
    if style == 1:
        painter.drawEllipse(at, half, half)
    elif style == 2:
        painter.drawPolygon(QtGui.QPolygonF([at + QtCore.QPointF(0, -half - 1), at + QtCore.QPointF(half + 1, 0), at + QtCore.QPointF(0, half + 1), at + QtCore.QPointF(-half - 1, 0)]))
    elif style == 3:
        painter.drawLine(at + QtCore.QPointF(-half, -half), at + QtCore.QPointF(half, half))
        painter.drawLine(at + QtCore.QPointF(-half, half), at + QtCore.QPointF(half, -half))
    elif style == 4:
        painter.drawLine(at + QtCore.QPointF(-half, 0), at + QtCore.QPointF(half, 0))
        painter.drawLine(at + QtCore.QPointF(0, -half), at + QtCore.QPointF(0, half))
    elif style == 5:
        painter.drawPolygon(QtGui.QPolygonF([at + QtCore.QPointF(0, -half - 1), at + QtCore.QPointF(half + 1, half), at + QtCore.QPointF(-half - 1, half)]))
    else:
        painter.drawRect(QtCore.QRectF(at.x() - half, at.y() - half, half * 2, half * 2))


def tiles(area: QtCore.QRectF, count: int, columns: int) -> list[QtCore.QRectF]:
    """Equal tiles for tiled display mode: `columns` of -1 picks a near-square grid."""
    if count < 1:
        return []
    columns = columns if columns > 0 else math.ceil(math.sqrt(count))
    columns = min(columns, count)
    rows = math.ceil(count / columns)
    width, height = area.width() / columns, area.height() / rows
    return [QtCore.QRectF(area.left() + (i % columns) * width, area.top() + (i // columns) * height, width, height) for i in range(count)]


class XYPlot(_SettingsPlot):
    """One or more parameters against another, optionally coloured by a third."""

    def axis_channels(self) -> tuple[int | None, list[tuple[int, int]]]:
        log = self.session.log
        if log is None:
            return None, []
        x = log.channel_of(self.settings.x_id)
        ys = [(index, group) for index, group in self.chosen() if index != x]
        if x is None and log.names and not self.settings.x_id:
            x = 0
            ys = [(i, g) for i, g in ys if i != 0] or [(min(1, len(log.names) - 1), 0)]
        return x, ys

    def listed_groups(self, log: Log, x_index: int | None) -> list[list[tuple[int | None, int]]]:
        """Every parameter the view lists, by display group, with None for ones the log lacks."""
        by_group: dict[int, list[tuple[int | None, int]]] = {}
        wanted = self.view.log_parameters or [(series.data_id, index) for index, series in enumerate(log.series[:2])]
        for data_id, group in wanted:
            index = log.channel_of(data_id) if data_id else None
            if index is not None and index == x_index:
                continue
            by_group.setdefault(group, []).append((index, data_id))
        return [by_group[key] for key in sorted(by_group)]

    def paintEvent(self, _event: QtGui.QPaintEvent) -> None:
        painter = QtGui.QPainter(self)
        painter.fillRect(self.rect(), self.session.theme_colour("DataAreaBackground"))
        log = self.session.log
        if log is None or not log.time:
            return
        x_index, _ = self.axis_channels()
        members = self.listed_groups(log, x_index)
        if not members:
            return
        first, last = self.axis_window(log)
        columns = int(float(self.group_node().get("ColumnCount", "-1") or -1))
        overlapped = self.display_mode() == OVERLAPPED
        outer = self.outer_area()
        if overlapped:
            members = [[m for group in members for m in group]]
        cells = tiles(outer.adjusted(0, 0, 0, self.AXIS_ROWS), len(members), columns)
        colour_channel = log.channel_of(self.settings.colour_id) if self.settings.use_colour else None
        s = self.settings
        xs = [self.convert(log, x_index, v) for v in log.channels[x_index][first:last]] if x_index is not None else []
        if s.manual:
            x_low, x_high = s.manual_min, s.manual_max
        elif xs:
            x_low, x_high = min(xs), max(xs)
        else:
            x_low, x_high = -1.1, 1.1
        x_name = f"{log.names[x_index]} ({self.unit(log, x_index)})".replace(" ()", "") if x_index is not None else self.session.describe(s.x_id)
        x_colour = self.colour(log, x_index, 0) if x_index is not None else None
        order = 0
        columns_used = max(1, round(outer.width() / cells[0].width())) if cells else 1
        for number, (cell, entries) in enumerate(zip(cells, members)):
            area = cell.adjusted(0, 0, -(6 if len(cells) > 1 else 0), -self.AXIS_ROWS)
            present = [index for index, _ in entries if index is not None]
            values = [self.convert(log, y, v) for y in present for v in log.channels[y][first:last]]
            if s.y_manual:
                y_low, y_high = s.y_min, s.y_max
            elif values:
                y_low, y_high = min(values), max(values)
            else:
                y_low, y_high = -1.1, 1.1
            self.draw_axes(painter, x_low, x_high, y_low, y_high, x_name, "", area, show_y=number % columns_used == 0, name_colour=x_colour)
            bounds = (x_low, x_high, y_low, y_high)
            painter.save()
            painter.setClipRect(area)
            step = max(len(xs) // 8000, 1)
            for line, (index, data_id) in enumerate(entries):
                if index is not None and x_index is not None:
                    self.draw_series(painter, log, area, xs, index, first, last, step, order, colour_channel, bounds)
                order += 1
            if x_index is not None:
                self.draw_overlays_xy(painter, log, area, x_index, present, bounds)
            painter.restore()
            small = painter.font()
            small.setPixelSize(11)
            painter.setFont(small)
            chip = 0
            for index, data_id in entries:
                if index is None or x_index is None:
                    self.no_data_chip(painter, area, chip)
                else:
                    label = f"{log.names[index]} ({self.unit(log, index)})".replace(" ()", "")
                    painter.fillRect(QtCore.QRectF(area.left() + 1, area.top() + 2 + chip * 15, painter.fontMetrics().horizontalAdvance(label) + 6, 14), QtGui.QColor(61, 61, 61))
                    painter.setPen(self.colour(log, index, order))
                    painter.drawText(QtCore.QPointF(area.left() + 4, area.top() + 13 + chip * 15), label)
                chip += 1
            if x_index is not None:
                self.draw_cursor_point(painter, log, area, x_index, present, bounds)
            self.draw_legend(painter, area, log)

    def project(self, area, bounds, x: float, y: float) -> QtCore.QPointF:
        x_low, x_high, y_low, y_high = bounds
        return QtCore.QPointF(area.left() + (x - x_low) / ((x_high - x_low) or 1) * area.width(), area.bottom() - (y - y_low) / ((y_high - y_low) or 1) * area.height())

    def draw_series(self, painter, log, area, xs, y, first, last, step, order, colour_channel, bounds) -> None:
        s = self.settings
        base = self.colour(log, y, order)
        data = log.channels[y]
        previous = None
        for i in range(0, len(xs), step):
            point = self.project(area, bounds, xs[i], self.convert(log, y, data[first + i]))
            colour = self.colour_at(log, y, order, colour_channel, first + i) if colour_channel is not None else base
            if s.show_lines and previous is not None:
                painter.setPen(QtGui.QPen(colour, max(s.line_width, 1)))
                painter.drawLine(previous, point)
            if s.show_points:
                painter.setPen(QtGui.QPen(colour, 1))
                painter.setBrush(colour)
                draw_point(painter, point, s.point_style, s.point_size)
            previous = point
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)

    def draw_overlays_xy(self, painter, log, area, x_index, indexes, bounds) -> None:
        for number, (other, section, _offset) in enumerate(self.overlay_logs()):
            colour = self.session.overlay_colour(number)
            x_other = other.channel_of(log.series[x_index].data_id)
            if x_other is None:
                continue
            a, b = bisect.bisect_left(other.time, section.start), bisect.bisect_right(other.time, section.end)
            painter.setPen(QtGui.QPen(colour, 1))
            painter.setBrush(colour)
            for y in indexes:
                source = other.channel_of(log.series[y].data_id)
                if source is None:
                    continue
                previous = None
                for i in thin(a, b, 4000):
                    point = self.project(area, bounds, self.session.display(other.series[x_other], other.channels[x_other][i]), self.session.display(other.series[source], other.channels[source][i]))
                    if self.settings.show_lines and previous is not None:
                        painter.drawLine(previous, point)
                    draw_point(painter, point, self.settings.point_style, self.settings.point_size)
                    previous = point
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)

    def draw_cursor_point(self, painter, log, area, x_index, indexes, bounds) -> None:
        at = self.cursor_index()
        painter.setPen(QtGui.QPen(self.session.theme_colour("Cursors"), 2))
        for y in indexes:
            point = self.project(area, bounds, self.convert(log, x_index, log.channels[x_index][at]), self.convert(log, y, log.channels[y][at]))
            if area.contains(point):
                painter.drawLine(point + QtCore.QPointF(-6, 0), point + QtCore.QPointF(6, 0))
                painter.drawLine(point + QtCore.QPointF(0, -6), point + QtCore.QPointF(0, 6))


def histogram_bins(values: list[float], bins: int, low: float | None = None, high: float | None = None) -> tuple[list[int], float, float]:
    """Counts per equal-width bin, with the range they cover."""
    if not values or bins < 1:
        return [], 0.0, 0.0
    low = min(values) if low is None else low
    high = max(values) if high is None else high
    counts = [0] * bins
    width = (high - low) / bins
    for value in values:
        if value < low or value > high:
            continue
        counts[min(int((value - low) / width), bins - 1) if width else 0] += 1
    return counts, low, high


class Histogram(_SettingsPlot):
    """How long each parameter spent at each value, across the range on show."""

    NODE = "Histogram"
    BINS = 40
    KEYS = ("ShowOverlays", "ColorRangeParameterDataID", "UseColorRange", "ShowColorLegend", "ManualRange", "ManualMin", "ManualMax", "YManualRange", "YManualMin", "YManualMax")

    def y_units(self) -> str:
        return self.node().get("YUnits", "Samples")

    def set_y_units(self, units: str) -> None:
        self.node().set("YUnits", units)
        self.saved()

    def bins_for(self, log: Log, index: int, first: int, last: int, colour_channel: int | None):
        values = [self.convert(log, index, v) for v in log.channels[index][first:last]]
        manual = self.settings.manual
        low = self.settings.manual_min if manual else None
        high = self.settings.manual_max if manual else None
        counts, low, high = histogram_bins(values, self.BINS, low, high)
        stacks: list[list[int]] = []
        if colour_channel is not None and self.settings.spans:
            spans = list(self.settings.spans)
            stacks = [[0] * len(spans) for _ in counts]
            width = (high - low) / self.BINS if high > low else 0
            for offset, value in enumerate(values):
                if value < low or value > high:
                    continue
                bin_ = min(int((value - low) / width), self.BINS - 1) if width else 0
                shade = self.session.display(log.series[colour_channel], log.channels[colour_channel][first + offset])
                for slot, (a, b, _c) in enumerate(spans):
                    if a <= shade < b or slot == len(spans) - 1 and shade >= b or slot == 0 and shade < a:
                        stacks[bin_][slot] += 1
                        break
        return counts, low, high, stacks

    def paintEvent(self, _event: QtGui.QPaintEvent) -> None:
        painter = QtGui.QPainter(self)
        painter.fillRect(self.rect(), self.session.theme_colour("DataAreaBackground"))
        log = self.session.log
        chosen = self.chosen()
        if log is None or not log.time or not chosen:
            return
        first, last = self.axis_window(log)
        colour_channel = log.channel_of(self.settings.colour_id) if self.settings.use_colour else None
        outer = self.outer_area()
        overlapped = self.display_mode() == OVERLAPPED
        cells = [outer.adjusted(0, 0, 0, self.AXIS_ROWS)] if overlapped else tiles(outer.adjusted(0, 0, 0, self.AXIS_ROWS), len(chosen), int(float(self.group_node().get("ColumnCount", "-1") or -1)))
        percent = self.y_units() == "% Time"
        items = [chosen] if overlapped else [[c] for c in chosen]
        for cell, entries in zip(cells, items):
            area = cell.adjusted(0, 0, -(0 if overlapped else 6), -self.AXIS_ROWS)
            results = [(index,) + self.bins_for(log, index, first, last, colour_channel) for index, _ in entries]
            total = max(last - first, 1)
            scale = 100.0 / total if percent else 1.0
            tallest = max((max(r[1]) for r in results if r[1]), default=1) * scale
            y_low, y_high = (self.settings.y_min, self.settings.y_max) if self.settings.y_manual else (0, tallest or 1)
            x_low, x_high = results[0][2], results[0][3]
            name = ", ".join(log.names[r[0]] for r in results)
            self.draw_axes(painter, x_low, x_high, y_low, y_high, name, "% Time" if percent else "Samples", area)
            painter.save()
            painter.setClipRect(area)
            for number, (index, counts, low, high, stacks) in enumerate(results):
                width = area.width() / len(counts) if counts else 0
                for at, count in enumerate(counts):
                    height = count * scale / ((y_high - y_low) or 1) * area.height()
                    x = area.left() + at * width
                    if stacks and count:
                        bottom = area.bottom()
                        for slot, amount in enumerate(stacks[at]):
                            piece = amount / count * height
                            painter.fillRect(QtCore.QRectF(x, bottom - piece, max(width - 1, 1), piece), self.settings.spans[slot][2])
                            bottom -= piece
                    else:
                        colour = self.colour(log, index, number)
                        if overlapped and number:
                            painter.setPen(colour)
                            painter.drawRect(QtCore.QRectF(x, area.bottom() - height, max(width - 1, 1), height))
                        else:
                            painter.fillRect(QtCore.QRectF(x, area.bottom() - height, max(width - 1, 1), height), colour)
            for number, (other, section, _offset) in enumerate(self.overlay_logs()):
                for index, *_ in results:
                    source = other.channel_of(log.series[index].data_id)
                    if source is None:
                        continue
                    a, b = bisect.bisect_left(other.time, section.start), bisect.bisect_right(other.time, section.end)
                    counts, _l, _h = histogram_bins([self.session.display(other.series[source], v) for v in other.channels[source][a:b]], self.BINS, x_low, x_high)
                    width = area.width() / self.BINS
                    painter.setPen(self.session.overlay_colour(number))
                    shape = QtGui.QPolygonF([QtCore.QPointF(area.left() + i * width + width / 2, area.bottom() - c * (100.0 / max(b - a, 1) if percent else 1.0) / ((y_high - y_low) or 1) * area.height()) for i, c in enumerate(counts)])
                    painter.drawPolyline(shape)
            painter.restore()
            self.draw_cursor_line(painter, log, area, results[0][0], x_low, x_high)
        self.draw_legend(painter, outer, log)

    def draw_cursor_line(self, painter, log, area, index, x_low, x_high) -> None:
        value = self.convert(log, index, log.channels[index][self.cursor_index()])
        x = area.left() + (value - x_low) / ((x_high - x_low) or 1) * area.width()
        if area.left() <= x <= area.right():
            painter.setPen(QtGui.QPen(self.session.theme_colour("Cursors"), 1))
            painter.drawLine(QtCore.QPointF(x, area.top()), QtCore.QPointF(x, area.bottom()))


GPS_LATITUDE, GPS_LONGITUDE = 4251, 4252


class GpsPlot(_SettingsPlot):
    """The logged route: latitude against longitude on plain axes, as PCLink draws it."""

    NODE = "GPSPlot"

    def paintEvent(self, _event: QtGui.QPaintEvent) -> None:
        painter = QtGui.QPainter(self)
        painter.fillRect(self.rect(), self.session.theme_colour("DataAreaBackground"))
        log = self.session.log
        if log is None:
            return
        x_index, y_index = log.channel_of(GPS_LONGITUDE), log.channel_of(GPS_LATITUDE)
        if x_index is None or y_index is None or not log.time:
            painter.setPen(self.session.theme_colour("ViewText"))
            painter.drawText(self.rect(), CENTRE, "This log has no GPS position")
            return
        first, last = self.axis_window(log)
        last = max(last, first + 1)
        xs, ys = log.channels[x_index][first:last], log.channels[y_index][first:last]
        s = self.settings
        x_low, x_high = (s.manual_min, s.manual_max) if s.manual else (min(xs), max(xs))
        y_low, y_high = (s.y_min, s.y_max) if s.y_manual else (min(ys), max(ys))
        self.draw_axes(painter, x_low, x_high, y_low, y_high, "Longitude", "Latitude")
        area = self.plot_area()

        def point(x: float, y: float) -> QtCore.QPointF:
            return QtCore.QPointF(
                area.left() + (x - x_low) / ((x_high - x_low) or 1) * area.width(),
                area.bottom() - (y - y_low) / ((y_high - y_low) or 1) * area.height(),
            )

        painter.save()
        painter.setClipRect(area)
        step = max(len(xs) // 8000, 1)
        colour_channel = log.channel_of(s.colour_id) if s.use_colour else None
        base = self.colour(log, y_index, 2)
        previous = None
        for i in range(0, len(xs), step):
            here = point(xs[i], ys[i])
            colour = self.colour_at(log, y_index, 2, colour_channel, first + i) if colour_channel is not None else base
            painter.setPen(QtGui.QPen(colour, max(s.line_width, 1)))
            if previous is not None:
                painter.drawLine(previous, here)
            previous = here
        for number, (other, section, _offset) in enumerate(self.overlay_logs()):
            ox, oy = other.channel_of(GPS_LONGITUDE), other.channel_of(GPS_LATITUDE)
            if ox is None or oy is None:
                continue
            a, b = bisect.bisect_left(other.time, section.start), bisect.bisect_right(other.time, section.end)
            painter.setPen(QtGui.QPen(self.session.overlay_colour(number), 1))
            painter.drawPolyline(QtGui.QPolygonF([point(other.channels[ox][i], other.channels[oy][i]) for i in thin(a, b, 4000)]))
        painter.restore()
        at = self.cursor_index()
        painter.setPen(QtGui.QPen(self.session.theme_colour("LogCrosshairs"), 1))
        painter.setBrush(self.session.theme_colour("LogCrosshairs"))
        painter.drawEllipse(point(log.channels[x_index][at], log.channels[y_index][at]), 4, 4)
        self.draw_legend(painter, area, log)


PLAN_IMAGES = {"car.png": (224, 479), "tyre.png": (73, 88), "steering_wheel.png": (128, 128)}


def extract_plan_images(exe, destination) -> None:
    """The vehicle plan's car, tyre and steering wheel pictures are PNG resources in the user's own PCLink.exe."""
    import struct
    from pathlib import Path

    try:
        data = Path(exe).read_bytes()
    except OSError:
        return
    signature, position = b"\x89PNG\r\n\x1a\n", 0
    wanted = {size: name for name, size in PLAN_IMAGES.items()}
    while (at := data.find(signature, position)) >= 0:
        end = at + 8
        while end + 12 <= len(data):
            length, kind = struct.unpack_from(">I4s", data, end)
            end += 12 + length
            if kind == b"IEND":
                break
        name = wanted.get(struct.unpack_from(">II", data, at + 16))
        if name:
            Path(destination).mkdir(parents=True, exist_ok=True)
            (Path(destination) / name).write_bytes(data[at:end])
        position = max(end, at + 8)


def wheel_slip(near: float, far: float) -> float:
    """PCLink's front or rear slip (code at 0x5EAE02 and 0x5EAEBB): one wheel against the other, in percent."""
    if far != 0:
        return -(near / far - 1.0) * 100.0
    return -100.0 if near != 0 else 0.0


class VehiclePlan(_LogView):
    """A plan view of the car: wheel speeds, steering, pedals, gear and G force at the cursor.

    Layout attributes name the parameter behind each part; the defaults are PCLink's own."""

    IDS = {
        "LFWheel": 1437, "RFWheel": 1439, "LRWheel": 1438, "RRWheel": 1440, "EngineSpeed": 1000, "Gear": 2587,
        "Driven": 1462, "NonDriven": 1463, "Slip": 2662, "OilPres": 1796, "OilLevel": 1795, "Brake": 1790, "Steering": 1020,
        "FootPos": 1650, "ThrottlePos": 1078, "SteringROC": 1021, "Lateral": 1026, "Longitudinal": 1027,
    }
    GAUGES = (
        ("FootPos", "Accelerator", 0.0, 100.0), ("ThrottlePos", "Throttle", 0.0, 100.0), ("SlipGauge", "Wheel Slip", -100.0, 100.0),
        ("RPMGauge", "Engine Speed", 0.0, 10000.0), ("FrontSlip", "Front Slip", -20.0, 20.0), ("RearSlip", "Rear Slip", -20.0, 20.0),
        ("SteringROC", "Steering Rate", -100.0, 100.0),
    )
    BIND = {"SlipGauge": "Slip", "RPMGauge": "EngineSpeed", "FrontSlip": "", "RearSlip": "", "SteringROC": "SteringROC"}
    SLIP_LIMIT = 20.0

    def __init__(self, session: LogSession, view: LayoutView | None = None, live=None) -> None:
        super().__init__(session, view)
        self.live = live
        self.pictures: dict[str, QtGui.QPixmap] = {}
        self.setMinimumSize(280, 240)

    def bound(self, name: str) -> int:
        try:
            return int(float(self.view.attributes.get(name, self.IDS.get(name, 0))))
        except ValueError:
            return self.IDS.get(name, 0)

    def visible(self, name: str) -> bool:
        return self.view.attributes.get(name, "true") != "false"

    def value(self, data_id: int) -> float | None:
        if self.view.attributes.get("LogView", "true") == "true":
            log = self.session.log
            index = log.channel_of(data_id) if log else None
            if index is not None and log.series[index].times:
                return self.session.display(log.series[index], log.series[index].value_at(self.cursor()))
        return self.live(data_id) if self.live else None

    def named(self, name: str) -> float | None:
        return self.value(self.bound(name))

    def slips(self) -> tuple[float, float]:
        wheels = [self.named(n) or 0.0 for n in ("LFWheel", "RFWheel", "LRWheel", "RRWheel")]
        return wheel_slip(wheels[0], wheels[1]), wheel_slip(wheels[2], wheels[3])

    def picture(self, name: str) -> QtGui.QPixmap | None:
        if name not in self.pictures:
            from . import assets

            path = assets.asset("VehiclePlan", name)
            self.pictures[name] = QtGui.QPixmap(str(path)) if path else QtGui.QPixmap()
        return self.pictures[name] if not self.pictures[name].isNull() else None

    def bar(self, painter: QtGui.QPainter, box: QtCore.QRectF, title: str, value: float | None, low: float, high: float, vertical: bool = False) -> None:
        painter.setPen(TEXT_COLOUR)
        shown = "--" if value is None else fixed_number(value, 0)
        painter.drawText(QtCore.QRectF(box.left(), box.top() - 15, box.width() if not vertical else 90, 14), LEFT | VCENTRE, f"{title}: {shown}")
        painter.fillRect(box, GRID_COLOUR)
        if value is None:
            return
        fraction = min(max((value - low) / (high - low), 0.0), 1.0)
        colour = QtGui.QColor(PLOT_COLOURS[2])
        if low < 0:
            mid = box.left() + box.width() / 2 if not vertical else box.top() + box.height() / 2
            zero = (0 - low) / (high - low)
            if vertical:
                top, bottom = sorted((box.bottom() - zero * box.height(), box.bottom() - fraction * box.height()))
                painter.fillRect(QtCore.QRectF(box.left(), top, box.width(), bottom - top), colour)
            else:
                left, right = sorted((box.left() + zero * box.width(), box.left() + fraction * box.width()))
                painter.fillRect(QtCore.QRectF(left, box.top(), right - left, box.height()), colour)
            del mid
        elif vertical:
            painter.fillRect(QtCore.QRectF(box.left(), box.bottom() - fraction * box.height(), box.width(), fraction * box.height()), colour)
        else:
            painter.fillRect(QtCore.QRectF(box.left(), box.top(), box.width() * fraction, box.height()), colour)

    def paintEvent(self, _event: QtGui.QPaintEvent) -> None:
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform)
        painter.fillRect(self.rect(), self.session.theme_colour("DataAreaBackground"))
        width, height = self.width(), self.height()
        front, rear = self.slips()
        driven_front = self.view.attributes.get("DrivenWheels", "1") == "1"
        slip = self.named("Slip")
        slipping = slip is not None and abs(slip) >= self.SLIP_LIMIT
        marks = {
            "LF": front >= self.SLIP_LIMIT or front <= -self.SLIP_LIMIT or (slipping and driven_front),
            "RF": front >= self.SLIP_LIMIT or front <= -self.SLIP_LIMIT or (slipping and driven_front),
            "LR": rear >= self.SLIP_LIMIT or rear <= -self.SLIP_LIMIT or (slipping and not driven_front),
            "RR": rear >= self.SLIP_LIMIT or rear <= -self.SLIP_LIMIT or (slipping and not driven_front),
        }
        car_h = height * 0.78
        car_w = car_h * 224 / 479
        centre = QtCore.QPointF(width * 0.5, height * 0.5)
        car = QtCore.QRectF(centre.x() - car_w / 2, centre.y() - car_h / 2, car_w, car_h)
        image = self.picture("car.png")
        if image:
            painter.drawPixmap(car.toRect(), image)
        else:
            painter.setPen(TEXT_COLOUR)
            painter.drawRoundedRect(car, 14, 14)
        tyre_w, tyre_h = car_w * 0.2, car_w * 0.2 * 88 / 73
        tyre_image = self.picture("tyre.png")
        for key, attr, x, y in (("LF", "LFWheel", car.left() - tyre_w * 0.55, car.top() + car_h * 0.12), ("RF", "RFWheel", car.right() - tyre_w * 0.45, car.top() + car_h * 0.12),
                                ("LR", "LRWheel", car.left() - tyre_w * 0.55, car.bottom() - car_h * 0.12 - tyre_h), ("RR", "RRWheel", car.right() - tyre_w * 0.45, car.bottom() - car_h * 0.12 - tyre_h)):
            box = QtCore.QRectF(x, y, tyre_w, tyre_h)
            if tyre_image:
                painter.drawPixmap(box.toRect(), tyre_image)
            else:
                painter.setPen(TEXT_COLOUR)
                painter.drawRect(box)
            if marks[key]:
                painter.fillRect(box, QtGui.QColor(230, 40, 40, 150))
            value = self.named(attr)
            painter.setPen(TEXT_COLOUR)
            side = box.left() - 44 if key[0] == "L" and False else box.left()
            painter.drawText(QtCore.QRectF(side - 20, box.bottom() + 2 if key[1] == "F" else box.top() - 16, tyre_w + 40, 14), CENTRE, "--" if value is None else f"{value:.0f}")
        wheel = self.picture("steering_wheel.png")
        angle = self.named("Steering") or 0.0
        size = car_w * 0.45
        painter.save()
        painter.translate(centre.x(), car.top() + car_h * 0.5)
        painter.rotate(angle)
        if wheel:
            painter.drawPixmap(QtCore.QRectF(-size / 2, -size / 2, size, size).toRect(), wheel)
        else:
            painter.setPen(TEXT_COLOUR)
            painter.drawEllipse(QtCore.QRectF(-size / 2, -size / 2, size, size))
        painter.restore()
        gear = self.named("Gear")
        big = painter.font()
        big.setPixelSize(max(int(height / 12), 12))
        painter.setFont(big)
        painter.setPen(TEXT_COLOUR)
        painter.drawText(QtCore.QRectF(4, height * 0.5, width * 0.2, big.pixelSize() * 1.4), LEFT | VCENTRE, f"Gear {'--' if gear is None else f'{gear:.0f}'}")
        small = painter.font()
        small.setPixelSize(11)
        painter.setFont(small)
        shown = [g for g in self.GAUGES if self.visible(g[0])]
        left_column = [g for g in shown if g[0] in ("FootPos", "ThrottlePos", "FrontSlip", "RearSlip")]
        right_column = [g for g in shown if g[0] not in ("FootPos", "ThrottlePos", "FrontSlip", "RearSlip")]
        for column, x in ((left_column, 6), (right_column, width * 0.72)):
            lane = width * 0.24
            for row, (name, title, low, high) in enumerate(column):
                value = {"FrontSlip": front, "RearSlip": rear}.get(name) if name in ("FrontSlip", "RearSlip") else self.named(self.BIND.get(name, name))
                if name in ("FrontSlip", "RearSlip") and self.named("LFWheel") is None and self.named("LRWheel") is None:
                    value = None
                self.bar(painter, QtCore.QRectF(x, 24 + row * 38, lane, 10), title, value, low, high)
        self.draw_g_meter(painter, QtCore.QRectF(width * 0.72, height - width * 0.24 - 24, width * 0.24, width * 0.24))
        self.draw_switches(painter)

    def draw_switches(self, painter: QtGui.QPainter) -> None:
        items = []
        for title, name in (("Oil Pressure", "OilPres"), ("Oil Level", "OilLevel"), ("Brake", "Brake")):
            value = self.named(name)
            items.append((title, value))
        for number, (title, value) in enumerate(items):
            painter.setPen(TEXT_COLOUR)
            top = self.height() - 18 - number * 16
            painter.setBrush(QtGui.QColor(230, 40, 40) if value else GRID_COLOUR)
            painter.drawEllipse(QtCore.QRectF(6, top, 10, 10))
            painter.drawText(QtCore.QPointF(20, top + 9), f"{title}: {'--' if value is None else ('On' if value else 'Off')}")
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)

    def draw_g_meter(self, painter: QtGui.QPainter, box: QtCore.QRectF) -> None:
        if not self.visible("GMeterVisible") or box.width() < 40:
            return
        log = self.session.log
        trail = []
        peak = 1.0
        if log is not None:
            lat, lon = log.channel_of(self.bound("Lateral")), log.channel_of(self.bound("Longitudinal"))
            if lat is not None and lon is not None:
                at = self.cursor_index()
                for i in range(max(at - 40, 0), at + 1):
                    trail.append((self.convert(log, lat, log.channels[lat][i]), self.convert(log, lon, log.channels[lon][i])))
                peak = max([peak] + [max(abs(a), abs(b)) for a, b in zip(log.channels[lat], log.channels[lon])]) if log.channels[lat] else 1.0
        if not trail:
            lateral, longitudinal = self.named("Lateral"), self.named("Longitudinal")
            if lateral is not None and longitudinal is not None:
                trail = [(lateral, longitudinal)]
                peak = max(abs(lateral), abs(longitudinal), 1.0)
        painter.setPen(TEXT_COLOUR)
        painter.drawText(QtCore.QRectF(box.left(), box.top() - 15, box.width(), 14), LEFT | VCENTRE, f"G Force ({peak:.1f} g)")
        painter.drawEllipse(box)
        painter.setPen(GRID_COLOUR)
        painter.drawLine(QtCore.QPointF(box.left(), box.center().y()), QtCore.QPointF(box.right(), box.center().y()))
        painter.drawLine(QtCore.QPointF(box.center().x(), box.top()), QtCore.QPointF(box.center().x(), box.bottom()))
        points = [box.center() + QtCore.QPointF(a / peak * box.width() / 2, -b / peak * box.height() / 2) for a, b in trail]
        if len(points) > 1:
            painter.setPen(QtGui.QPen(QtGui.QColor(PLOT_COLOURS[2]), 1))
            painter.drawPolyline(QtGui.QPolygonF(points))
        if points:
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.setBrush(QtGui.QColor(255, 255, 0))
            painter.drawEllipse(points[-1], 4, 4)
            painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)


class MixtureMapView(_LogView):
    """Logged mixture against target, cell by cell, for the fuel table. Double-click corrects a cell."""

    LEFT, TOP, CELL_W, CELL_H, BAR = 44, 45, 75, 60, 16

    def __init__(self, session: LogSession, view: LayoutView | None = None, host=None) -> None:
        super().__init__(session, view)
        self.host = host
        self.settings = tune.MixtureSettings.from_attributes(self.view.attributes)
        self.grid: list[list[tune.MixtureCell]] = []
        self.message = "Right click to update map."
        self.progress: float | None = None
        self.across = QtWidgets.QScrollBar(QtCore.Qt.Orientation.Horizontal, self)
        self.down = QtWidgets.QScrollBar(QtCore.Qt.Orientation.Vertical, self)
        for bar in (self.across, self.down):
            bar.valueChanged.connect(self.update)
            bar.hide()

    def rebuild(self) -> None:
        if self.host is None:
            return
        self.progress = 0.0
        self.update()
        try:
            self.grid, error = self.host.build_mixture(self.settings, self.session.log, self._advance)
            self.message = f"Unable to draw mixture map.\nLog File error: {error}\nRight click to update map." if error else "Right click to update map."
        finally:
            self.progress = None
        self._fit_scrollbars(first=True)
        self.update()

    def _advance(self, fraction: float) -> None:
        self.progress = fraction
        self.repaint()

    def _scale(self) -> float:
        return self.host.mixture_scale() if self.host is not None and hasattr(self.host, "mixture_scale") else 1.0

    def _fit_scrollbars(self, first: bool = False) -> None:
        """Cells keep their size; what does not fit scrolls. The first populated cell starts at the corner."""
        if not self.grid or not self.grid[0]:
            self.across.hide()
            self.down.hide()
            return
        rows, columns = len(self.grid), len(self.grid[0])
        room_w, room_h = self.width() - self.LEFT, self.height() - self.TOP
        self.across.setVisible(columns * self.CELL_W > room_w)
        self.down.setVisible(rows * self.CELL_H > room_h)
        self.across.setGeometry(self.LEFT, self.height() - self.BAR, max(self.width() - self.LEFT - self.BAR, 1), self.BAR)
        self.down.setGeometry(self.width() - self.BAR, self.TOP, self.BAR, max(self.height() - self.TOP - self.BAR, 1))
        self.across.setRange(0, max(columns - max(room_w // self.CELL_W, 1), 0))
        self.down.setRange(0, max(rows - max(room_h // self.CELL_H, 1), 0))
        if first:
            data = [(r, c) for r, row in enumerate(self.grid) for c, cell in enumerate(row) if cell.has_data]
            self.across.setValue(min((c for _r, c in data), default=0))
            self.down.setValue(min((r for r, _c in data), default=0))

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        self._fit_scrollbars()

    def wheelEvent(self, event: QtGui.QWheelEvent) -> None:
        bar = self.down if self.down.isVisible() else self.across
        bar.setValue(bar.value() - (1 if event.angleDelta().y() > 0 else -1))

    def contextMenuEvent(self, event: QtGui.QContextMenuEvent) -> None:
        menu = QtWidgets.QMenu(self)
        spot = self._cell_at(event.pos())
        menu.addAction("Update Mixture Map", self.rebuild)
        correct = menu.addAction(f"Correct {self.host.mixture_table_name(self.settings.table) if self.host and hasattr(self.host, 'mixture_table_name') else 'Fuel Table'}", lambda: self._correct(spot))
        correct.setEnabled(spot is not None and self.host is not None and self.grid[spot[0]][spot[1]].has_data)
        if self.host:
            menu.addSeparator()
            menu.addAction("Properties…", lambda: self.host.mixture_setup(self))
        menu.exec(event.globalPos())

    def _cell_at(self, position) -> tuple[int, int] | None:
        if not self.grid or not self.grid[0]:
            return None
        position = QtCore.QPointF(position)
        if position.x() < self.LEFT or position.y() < self.TOP:
            return None
        row = int((position.y() - self.TOP) / self.CELL_H) + self.down.value()
        column = int((position.x() - self.LEFT) / self.CELL_W) + self.across.value()
        if 0 <= row < len(self.grid) and 0 <= column < len(self.grid[0]):
            return row, column
        return None

    def _correct(self, spot: tuple[int, int] | None) -> None:
        if spot is None or self.host is None:
            return
        cell = self.grid[spot[0]][spot[1]]
        if cell.has_data:
            self.host.correct_mixture_cell(self.settings.table, spot[0], spot[1], cell.actual_average, cell.target_average)
            cell.has_data, cell.corrected = False, True
            self.update()

    def mouseDoubleClickEvent(self, event: QtGui.QMouseEvent) -> None:
        self._correct(self._cell_at(event.position()))

    @staticmethod
    def _number(value: float) -> str:
        return f"{value:.2f}".rstrip("0").rstrip(".") or "0"

    def _axis_title(self, data_id: int) -> str:
        log = self.session.log
        index = log.channel_of(data_id) if log else None
        if index is None:
            return ""
        series = log.series[index]
        return f"{series.name} ({self.session.unit_label(series) or ''})"

    def paintEvent(self, _event: QtGui.QPaintEvent) -> None:
        painter = QtGui.QPainter(self)
        theme = self.session.theme_colour
        painter.fillRect(self.rect(), theme("DataAreaBackground"))
        if not self.grid or not self.grid[0]:
            painter.setPen(theme("ViewText"))
            painter.drawText(self.rect(), QtCore.Qt.AlignmentFlag.AlignCenter, self.message)
            self._paint_progress(painter)
            return
        rows, columns = len(self.grid), len(self.grid[0])
        first_row, first_column = self.down.value(), self.across.value()
        shown_rows = min(rows - first_row, (self.height() - self.TOP) // self.CELL_H + 1)
        shown_columns = min(columns - first_column, (self.width() - self.LEFT) // self.CELL_W + 1)
        s, scale = self.settings, self._scale()
        text = theme("ViewText")
        painter.setPen(text)
        painter.drawText(QtCore.QRectF(self.LEFT, 4, self.width() - self.LEFT, 16), QtCore.Qt.AlignmentFlag.AlignCenter, self._axis_title(s.rpm_id))
        painter.drawText(QtCore.QRectF(2, self.height() - 18, self.LEFT + 80, 16), QtCore.Qt.AlignmentFlag.AlignLeft, self._axis_title(s.load_id))
        painter.drawLine(self.LEFT - 12, self.TOP - 12, self.LEFT - 2, self.TOP - 2)
        painter.save()
        painter.setClipRect(QtCore.QRectF(self.LEFT, self.TOP, self.width() - self.LEFT, self.height() - self.TOP))
        font = painter.font()
        font.setPixelSize(11)
        painter.setFont(font)
        for r in range(shown_rows):
            for c in range(shown_columns):
                cell = self.grid[first_row + r][first_column + c]
                box = QtCore.QRectF(self.LEFT + c * self.CELL_W, self.TOP + r * self.CELL_H, self.CELL_W, self.CELL_H)
                if cell.corrected:
                    painter.fillRect(box, theme("ValueChanged"))
                elif cell.has_data:
                    painter.fillRect(box, QtGui.QColor(*tune.mixture_band(cell.error * scale, s.absolute_error * scale)))
                painter.setPen(theme("DataAreaBorder"))
                painter.drawRect(box)
                if not cell.has_data:
                    continue
                lines = []
                if s.show_actual:
                    lines.append(f"Actual={self._number(cell.actual_average * scale)}")
                if s.show_target:
                    lines.append(f"Target={self._number(cell.target_average * scale)}")
                if s.show_samples:
                    lines.append(f"Samples={cell.samples}")
                if s.show_error:
                    lines.append(f"Error={self._number(cell.error * scale)}")
                painter.setPen(QtGui.QColor(0, 0, 0))
                painter.drawText(box, QtCore.Qt.AlignmentFlag.AlignCenter, "\n".join(lines))
        painter.restore()
        painter.setPen(text)
        for c in range(shown_columns):
            x = self.LEFT + c * self.CELL_W
            painter.drawLine(x + self.CELL_W, self.TOP - 8, x + self.CELL_W, self.TOP)
            painter.drawText(QtCore.QRectF(x, self.TOP - 24, self.CELL_W, 16), QtCore.Qt.AlignmentFlag.AlignCenter, f"{round(self.grid[0][first_column + c].x[2], 2):g}")
        for r in range(shown_rows):
            y = self.TOP + r * self.CELL_H
            painter.drawLine(self.LEFT - 8, y + self.CELL_H, self.LEFT, y + self.CELL_H)
            painter.drawText(QtCore.QRectF(0, y, self.LEFT - 10, self.CELL_H), QtCore.Qt.AlignmentFlag.AlignCenter, f"{round(self.grid[first_row + r][0].y[2], 2):g}")
        self._paint_progress(painter)

    def _paint_progress(self, painter: QtGui.QPainter) -> None:
        if self.progress is None:
            return
        bar = QtCore.QRectF(0, 0, self.width() * 0.25, 16)
        bar.moveCenter(QtCore.QPointF(self.width() / 2, self.height() / 2))
        painter.setPen(self.session.theme_colour("DataAreaBorder"))
        painter.drawRect(bar)
        painter.fillRect(QtCore.QRectF(bar.left(), bar.top(), bar.width() * self.progress, bar.height()), self.session.theme_colour("OK"))


LOG_VIEW_TYPES = {
    "Time Plot": TimePlot, "Time Plot 2": TimePlot, "Navigator": Navigator, "Parameter List": ParameterList,
    "Value List": LoggedValues, "XY Plot": XYPlot, "XY Plot 2": XYPlot, "Histogram": Histogram, "Histogram 2": Histogram,
    "Statistics": StatisticsView, "GPS Plot": GpsPlot,
}
