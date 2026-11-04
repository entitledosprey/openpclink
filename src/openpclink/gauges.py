"""PCLink's live views: digital, bar, dial, curve and target gauges, and the value list.

Geometry follows PCLink's help pictures and the real program's screens: heavy white ticks on the dial,
a block-filled curve, a target pointer pair over a long scale. Sizes scale with the widget.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from PySide6 import QtCore, QtGui, QtWidgets

DEFAULT_GAUGE_COLOUR = 0xFFFF00  # Stored as 0xRRGGBB, as GDI+ takes it; the shipped dials' 16777002 is yellow, not cyan.
NO_COLOUR = 0x1FFFFFFF  # "No custom colour" in a layout file.
NO_WARNING = 65500  # PCLink's "off" for a warning limit.
WARNING_COLOUR = QtGui.QColor(255, 140, 0)
ERROR_COLOUR = QtGui.QColor(230, 30, 30)
OK_COLOUR = QtGui.QColor(60, 230, 90)
NEEDLE_COLOUR = QtGui.QColor(255, 255, 0)
# PCLink colours a parameter by a small index into its theme; set when the theme is applied.
PARAMETER_COLOURS: list[QtGui.QColor] = []
MULTIPLIERS = (0.01, 0.1, 1.0, 10.0, 100.0, 1000.0)
MULTIPLIER_TEXT = ("/ 100", "/ 10", "x 1", "x 10", "x 100", "x 1000")
FAULT_WORDS = ("fault", "error", "fail", "lost", "timeout", "short", "open circuit", "overrun", "blocked")
OK_WORDS = ("ok", "good", "valid", "synced", "synchronised", "synchronized", "ready")

AL = QtCore.Qt.AlignmentFlag
_THEME = {
    "base": QtGui.QColor(0, 0, 0), "text": QtGui.QColor(245, 245, 245), "lane": QtGui.QColor(61, 61, 61),
    "active": QtGui.QColor(53, 145, 244),
}


def set_theme(base: QtGui.QColor, text: QtGui.QColor, lane: QtGui.QColor, active: QtGui.QColor | None = None,
              ok: QtGui.QColor | None = None, soft: QtGui.QColor | None = None, hard: QtGui.QColor | None = None,
              cursor: QtGui.QColor | None = None) -> None:
    global OK_COLOUR, WARNING_COLOUR, ERROR_COLOUR, NEEDLE_COLOUR
    _THEME.update(base=base, text=text, lane=lane)
    if active is not None:
        _THEME["active"] = active
    OK_COLOUR = ok or OK_COLOUR
    WARNING_COLOUR = soft or WARNING_COLOUR
    ERROR_COLOUR = hard or ERROR_COLOUR
    NEEDLE_COLOUR = cursor or NEEDLE_COLOUR


def theme_colour(name: str) -> QtGui.QColor:
    return _THEME[name]


def tcolor(value: int) -> QtGui.QColor:
    """A Delphi TColor, which stores blue in its high byte."""
    return QtGui.QColor(value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF)


def to_tcolor(colour: QtGui.QColor) -> int:
    return colour.red() | colour.green() << 8 | colour.blue() << 16


def gauge_colour(value: int) -> QtGui.QColor:
    """A GaugeColor attribute: 0xRRGGBB, or no custom colour."""
    if value == NO_COLOUR or value < 0 or value > 0xFFFFFF:
        return NEEDLE_COLOUR
    return QtGui.QColor((value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF)


def to_gauge_colour(colour: QtGui.QColor) -> int:
    return colour.red() << 16 | colour.green() << 8 | colour.blue()


@dataclass
class GaugeSettings:
    """The attributes a layout file stores for a gauge."""

    manual: bool = False
    minimum: float = 0.0
    maximum: float = 100.0
    yellow: float = 80.0
    red: float = 90.0
    low_yellow: float = -1.0
    low_red: float = -1.0
    small_ticks: int = 5
    large_ticks: int = 10
    decimals: int = 1
    colour: int = DEFAULT_GAUGE_COLOUR
    high_quality: bool = True
    caption: str = ""
    multiplier: int = 2

    ATTRIBUTES = {
        "ManualConfig": "manual", "GaugeMin": "minimum", "GaugeMax": "maximum", "YellowLine": "yellow",
        "RedLine": "red", "LowYellowLine": "low_yellow", "LowRedLine": "low_red", "SmallTicks": "small_ticks",
        "LargeTicks": "large_ticks", "DecPlaces": "decimals", "GaugeColor": "colour", "HighQuality": "high_quality",
        "ViewCaption": "caption", "MultiplierIndex": "multiplier",
    }

    @classmethod
    def from_attributes(cls, attributes: dict[str, str]) -> "GaugeSettings":
        settings = cls()
        for name, field_name in cls.ATTRIBUTES.items():
            if name not in attributes:
                continue
            current = getattr(settings, field_name)
            text = attributes[name]
            try:
                value = text.lower() == "true" if isinstance(current, bool) else type(current)(float(text)) if not isinstance(current, str) else text
            except ValueError:
                continue
            setattr(settings, field_name, value)
        settings.small_ticks = min(max(settings.small_ticks, 1), 10)
        settings.large_ticks = min(max(settings.large_ticks, 1), 20)
        settings.decimals = min(max(settings.decimals, 0), 3)
        settings.multiplier = min(max(settings.multiplier, 0), len(MULTIPLIERS) - 1)
        return settings

    def attributes(self) -> dict[str, str]:
        attributes = {}
        for name, field_name in self.ATTRIBUTES.items():
            value = getattr(self, field_name)
            attributes[name] = ("true" if value else "false") if isinstance(value, bool) else f"{value:g}" if isinstance(value, float) else str(value)
        return attributes


@dataclass
class Reading:
    """One value as a view shows it."""

    data_id: int
    caption: str
    units: str
    value: float | None
    text: str
    minimum: float = 0.0
    maximum: float = 100.0
    high_warning: float | None = None
    high_error: float | None = None
    low_warning: float | None = None
    low_error: float | None = None
    colour: int = 0
    is_option: bool = False

    def level(self) -> int:
        """0 normal, 1 warning, 2 error."""
        if self.value is None:
            return 0
        if (self.high_error is not None and self.value >= self.high_error) or (self.low_error is not None and self.value <= self.low_error):
            return 2
        if (self.high_warning is not None and self.value >= self.high_warning) or (self.low_warning is not None and self.value <= self.low_warning):
            return 1
        return 0

    def label(self) -> str:
        """PCLink names a value with its units: Engine Speed (RPM)."""
        if self.units and not self.is_option and f"({self.units.strip()})" not in self.caption:
            return f"{self.caption} ({self.units})"
        return self.caption

    def plain(self) -> str:
        """The value without its units, which PCLink shows in the name instead."""
        units = self.units.strip()
        if units and not self.is_option and self.text.endswith(units):
            return self.text[: -len(units)].rstrip()
        return self.text

    def colour_of(self) -> QtGui.QColor:
        if PARAMETER_COLOURS:
            return PARAMETER_COLOURS[self.colour % len(PARAMETER_COLOURS)]
        return _THEME["text"]


def state_colour(level: int, normal: QtGui.QColor) -> QtGui.QColor:
    return ERROR_COLOUR if level == 2 else WARNING_COLOUR if level == 1 else normal


def flat_pen(colour: QtGui.QColor, width: float) -> QtGui.QPen:
    pen = QtGui.QPen(colour, width)
    pen.setCapStyle(QtCore.Qt.PenCapStyle.FlatCap)
    return pen


def fraction_of(value: float | None, low: float, high: float) -> float:
    if value is None or high <= low:
        return 0.0
    return min(max((value - low) / (high - low), 0.0), 1.0)


def tick_values(low: float, high: float, large: int, small: int) -> tuple[list[float], list[float]]:
    """Evenly spaced major ticks, with minor ones between them."""
    if high <= low:
        return [], []
    large = max(large, 1)
    small = max(small, 1)
    major = [low + (high - low) * i / large for i in range(large + 1)]
    minor = [
        major[i] + (major[i + 1] - major[i]) * j / small
        for i in range(large) for j in range(1, small)
    ]
    return major, minor


def tick_text(value: float, factor: float) -> str:
    shown = value / factor
    if abs(shown) < 1e-9:
        return "0"
    text = f"{shown:.3f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def make_font(painter: QtGui.QPainter, pixels: float, bold: bool = False, italic: bool = False) -> QtGui.QFont:
    font = QtGui.QFont(painter.font())
    font.setPixelSize(max(int(pixels), 6))
    font.setBold(bold)
    font.setItalic(italic)
    return font


def fit_font(painter: QtGui.QPainter, text: str, pixels: float, width: float, bold: bool = False, italic: bool = False) -> QtGui.QFont:
    """The largest font up to `pixels` that keeps `text` inside `width`."""
    font = make_font(painter, pixels, bold, italic)
    metrics = QtGui.QFontMetricsF(font)
    wide = metrics.horizontalAdvance(text)
    if wide > width > 0:
        font = make_font(painter, max(pixels * width / wide, 6), bold, italic)
    return font


class Gauge(QtWidgets.QWidget):
    """Shows the readings it is given. Subclasses draw them."""

    MINIMUM_SIZE = (40, 40)
    readings_wanted = 1
    show_units = True

    def __init__(self, settings: GaugeSettings | None = None) -> None:
        super().__init__()
        self.settings = settings or GaugeSettings()
        self.readings: list[Reading] = []
        self.setMinimumSize(*self.MINIMUM_SIZE)

    def set_readings(self, readings: list[Reading]) -> None:
        self.readings = readings
        self.update()

    def scale_range(self, reading: Reading) -> tuple[float, float]:
        if self.settings.manual:
            low, high = self.settings.minimum, self.settings.maximum
        else:
            low, high = reading.minimum, reading.maximum
        return (0.0, 100.0) if low == high else (low, high)

    def limits(self, reading: Reading) -> tuple[float | None, float | None, float | None, float | None]:
        """Low red, low yellow, yellow and red, where this gauge has them."""
        if self.settings.manual:
            s = self.settings
            return (s.low_red if s.low_red >= s.minimum else None, s.low_yellow if s.low_yellow >= s.minimum else None,
                    s.yellow if s.yellow <= s.maximum else None, s.red if s.red <= s.maximum else None)
        return reading.low_error, reading.low_warning, reading.high_warning, reading.high_error

    def level_of(self, reading: Reading) -> int:
        """Warning level against this gauge's own limits."""
        return self.level_at(reading, reading.value)

    def level_at(self, reading: Reading, value: float | None) -> int:
        if value is None:
            return 0
        low_red, low_yellow, yellow, red = self.limits(reading)
        if (red is not None and value >= red) or (low_red is not None and value <= low_red):
            return 2
        if (yellow is not None and value >= yellow) or (low_yellow is not None and value <= low_yellow):
            return 1
        return 0

    def factor(self) -> float:
        return MULTIPLIERS[min(max(self.settings.multiplier, 0), len(MULTIPLIERS) - 1)]

    def value_text(self, reading: Reading) -> str:
        if reading.is_option or reading.value is None:
            return reading.text
        return f"{reading.value:.{self.settings.decimals}f}"

    def value_with_units(self, reading: Reading) -> str:
        text = self.value_text(reading)
        return f"{text} {reading.units}".strip() if self.show_units and not reading.is_option else text

    def title(self, reading: Reading) -> str:
        return self.settings.caption or reading.caption

    def text_colour(self) -> QtGui.QColor:
        return _THEME["text"]

    def fill_colour(self, reading: Reading) -> QtGui.QColor:
        return state_colour(self.level_of(reading), gauge_colour(self.settings.colour))

    def tick_colour(self, reading: Reading, value: float) -> QtGui.QColor:
        """FUN_0055bd5c: a tick takes the warning colour of the band it sits in."""
        return state_colour(self.level_at(reading, value), self.text_colour())

    def paintEvent(self, _event: QtGui.QPaintEvent) -> None:
        painter = QtGui.QPainter(self)
        if self.settings.high_quality:
            painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
            painter.setRenderHint(QtGui.QPainter.RenderHint.TextAntialiasing)
        painter.fillRect(self.rect(), _THEME["base"])
        if self.readings:
            self.draw(painter)

    def draw(self, painter: QtGui.QPainter) -> None:
        raise NotImplementedError


class DigitalGauge(Gauge):
    """A stack of large numbers, one per value; the row's background turns with its warning level."""

    readings_wanted = 4

    def draw(self, painter: QtGui.QPainter) -> None:
        height = self.height() / len(self.readings)
        base = _THEME["base"]
        for index, reading in enumerate(self.readings):
            box = QtCore.QRectF(0, index * height, self.width(), height)
            level = reading.level()
            if level:
                painter.fillRect(box.adjusted(0, 0, 0, -1), ERROR_COLOUR if level == 2 else WARNING_COLOUR)
            elif index % 2:
                painter.fillRect(box, base.lighter(130) if base.lightness() > 20 else QtGui.QColor(18, 18, 18))
            if index:
                painter.setPen(QtGui.QPen(_THEME["lane"], 1))
                painter.drawLine(QtCore.QPointF(0, box.top()), QtCore.QPointF(box.right(), box.top()))
            label = reading.label()
            painter.setPen(reading.colour_of() if not level else QtGui.QColor(255, 255, 255))
            painter.setFont(fit_font(painter, label, max(min(height * 0.2, 18), 9), self.width() - 8, bold=True))
            painter.drawText(box.adjusted(4, 2, -4, 0), AL.AlignHCenter | AL.AlignTop, label)
            text = reading.plain()
            painter.setPen(QtGui.QColor(255, 255, 255) if level else self.text_colour())
            painter.setFont(fit_font(painter, text, max(height * 0.62, 10), self.width() - 10))
            painter.drawText(box.adjusted(0, height * 0.2, 0, 0), AL.AlignCenter, text)


class BarGauge(Gauge):
    """A vertical bar: title and value above, the bar rising on the left, a scale of coloured ticks on the right."""

    readings_wanted = 1

    def draw(self, painter: QtGui.QPainter) -> None:
        width = self.width() / len(self.readings)
        for index, reading in enumerate(self.readings):
            self._draw_one(painter, QtCore.QRectF(index * width, 0, width, self.height()), reading)

    def _draw_one(self, painter: QtGui.QPainter, box: QtCore.QRectF, reading: Reading) -> None:
        low, high = self.scale_range(reading)
        w, h = box.width(), box.height()
        title = self.title(reading)
        painter.setPen(reading.colour_of())
        painter.setFont(fit_font(painter, title, min(max(w * 0.17, 9), 20), w - 4, bold=True))
        painter.drawText(QtCore.QRectF(box.left(), 1, w, h * 0.08), AL.AlignHCenter | AL.AlignVCenter, title)
        text = self.value_with_units(reading)
        painter.setPen(self.text_colour())
        painter.setFont(fit_font(painter, text, min(max(w * 0.28, 10), 36), w - 4, bold=True, italic=True))
        painter.drawText(QtCore.QRectF(box.left(), h * 0.08, w, h * 0.1), AL.AlignHCenter | AL.AlignVCenter, text)
        top, bottom = h * 0.22, h - max(h * 0.045, 6)
        bar_left, bar_right = box.left() + w * 0.05, box.left() + w * 0.43
        tick_right, label_right = box.left() + w * 0.97, box.left() + w * 0.80

        def y_at(value: float) -> float:
            return bottom - fraction_of(value, low, high) * (bottom - top)

        filled = bottom - y_at(reading.value if reading.value is not None else low)
        if filled > 0:
            painter.fillRect(QtCore.QRectF(bar_left, bottom - filled, bar_right - bar_left, filled), self.fill_colour(reading))
        major, minor = tick_values(low, high, self.settings.large_ticks, self.settings.small_ticks)
        factor = self.factor()
        painter.setFont(make_font(painter, min(max(w * 0.13, 8), 14)))
        for values, length, thickness in ((minor, 0.09, 1.5), (major, 0.16, 2)):
            for value in values:
                y = y_at(value)
                painter.setPen(flat_pen(self.tick_colour(reading, value), thickness))
                painter.drawLine(QtCore.QPointF(tick_right - w * length, y), QtCore.QPointF(tick_right, y))
                if values is major:
                    painter.setPen(self.text_colour())
                    painter.drawText(QtCore.QRectF(box.left(), y - 9, label_right - box.left(), 18), AL.AlignRight | AL.AlignVCenter, tick_text(value, factor))
        if self.settings.multiplier != 2:
            painter.setPen(self.text_colour())
            painter.drawText(QtCore.QRectF(box.left(), h - 14, w * 0.5, 14), AL.AlignLeft | AL.AlignVCenter, MULTIPLIER_TEXT[self.settings.multiplier])


class _ArcGauge(Gauge):
    SWEEP_START = 225.0
    SWEEP = 270.0
    MINIMUM_SIZE = (60, 60)

    def angle_of(self, fraction: float) -> float:
        return self.SWEEP_START - self.SWEEP * fraction


class DialGauge(_ArcGauge):
    """A round gauge: silver rim, heavy coloured ticks, a tapered yellow needle on a hub."""

    def draw(self, painter: QtGui.QPainter) -> None:
        reading = self.readings[0]
        side = max(min(self.width(), self.height()) - 6, 20)
        centre = QtCore.QPointF(self.width() / 2, self.height() / 2)
        radius = side / 2
        low, high = self.scale_range(reading)
        self._draw_rim(painter, centre, radius)
        self._draw_ticks(painter, centre, radius, reading, low, high)
        self._draw_text(painter, centre, radius, reading)
        self._draw_needle(painter, centre, radius, fraction_of(reading.value, low, high), reading)

    def _draw_rim(self, painter: QtGui.QPainter, centre: QtCore.QPointF, radius: float) -> None:
        gradient = QtGui.QConicalGradient(centre, 135)
        for stop, shade in ((0.0, 230), (0.25, 110), (0.5, 200), (0.75, 90), (1.0, 230)):
            gradient.setColorAt(stop, QtGui.QColor(shade, shade, shade))
        width = max(radius * 0.055, 3)
        painter.setPen(QtGui.QPen(QtGui.QBrush(gradient), width))
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        painter.drawEllipse(centre, radius - width / 2, radius - width / 2)

    def _draw_ticks(self, painter: QtGui.QPainter, centre: QtCore.QPointF, radius: float, reading: Reading, low: float, high: float) -> None:
        major, minor = tick_values(low, high, self.settings.large_ticks, self.settings.small_ticks)
        outer = radius * 0.91
        factor = self.factor()
        painter.setFont(make_font(painter, max(radius * 0.095, 8), bold=True))
        for values, length, thickness in ((minor, 0.085, 0.022), (major, 0.16, 0.032)):
            for value in values:
                angle = math.radians(self.angle_of(fraction_of(value, low, high)))
                direction = QtCore.QPointF(math.cos(angle), -math.sin(angle))
                painter.setPen(flat_pen(self.tick_colour(reading, value), max(radius * thickness, 1.5)))
                painter.drawLine(centre + direction * outer * (1 - length * 1.0 / 0.91), centre + direction * outer)
                if values is major:
                    at = centre + direction * radius * 0.62
                    painter.setPen(self.text_colour())
                    painter.drawText(QtCore.QRectF(at.x() - radius * 0.2, at.y() - radius * 0.07, radius * 0.4, radius * 0.14), AL.AlignCenter, tick_text(value, factor))

    def _draw_text(self, painter: QtGui.QPainter, centre: QtCore.QPointF, radius: float, reading: Reading) -> None:
        width = radius * 1.1
        title = self.title(reading)
        painter.setPen(reading.colour_of())
        painter.setFont(fit_font(painter, title, radius * 0.14, width, bold=True))
        painter.drawText(QtCore.QRectF(centre.x() - width / 2, centre.y() - radius * 0.34, width, radius * 0.2), AL.AlignCenter, title)
        value = self.value_text(reading)
        painter.setPen(self.text_colour())
        painter.setFont(fit_font(painter, value, radius * 0.27, width * 0.8, bold=True, italic=True))
        painter.drawText(QtCore.QRectF(centre.x() - width / 2, centre.y() + radius * 0.06, width, radius * 0.3), AL.AlignCenter, value)
        suffix = reading.units if not reading.is_option else ""
        if self.settings.multiplier != 2:
            suffix = f"{MULTIPLIER_TEXT[self.settings.multiplier]} {suffix}".strip()
        if suffix:
            painter.setFont(fit_font(painter, suffix, radius * 0.2, width * 0.8, bold=True, italic=True))
            painter.drawText(QtCore.QRectF(centre.x() - width / 2, centre.y() + radius * 0.34, width, radius * 0.24), AL.AlignCenter, suffix)

    def _draw_needle(self, painter: QtGui.QPainter, centre: QtCore.QPointF, radius: float, fraction: float, reading: Reading) -> None:
        angle = math.radians(self.angle_of(fraction))
        along = QtCore.QPointF(math.cos(angle), -math.sin(angle))
        across = QtCore.QPointF(-along.y(), along.x())
        colour = gauge_colour(self.settings.colour)
        base = radius * 0.05
        tip = centre + along * radius * 0.84
        tail = centre - along * radius * 0.2
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(colour)
        painter.drawPolygon(QtGui.QPolygonF([tip, centre + across * base, tail + across * base, tail - across * base, centre - across * base]))
        painter.setBrush(colour.darker(150))
        painter.drawPolygon(QtGui.QPolygonF([tip, centre, tail, tail - across * base, centre - across * base]))
        painter.setBrush(colour)
        painter.drawEllipse(centre, radius * 0.06, radius * 0.06)


class CurveGauge(Gauge):
    """A shallow arc made of blocks that fill from the left, with the value large at the bottom right."""

    MINIMUM_SIZE = (80, 50)
    CENTRE = 0.76  # where the arc peaks, as a share of the width
    OUTER = (0.01, 0.85)  # top and fall of the outer edge, as a share of the height
    INNER = None  # the inner edge sits below the outer one by a band that is thickest at the peak

    def _edge(self, shape: tuple[float, float] | None, x: float, box: QtCore.QRectF) -> float:
        offset = (x / box.width() - self.CENTRE) / self.CENTRE
        outer = self.OUTER[0] + self.OUTER[1] * offset * offset
        if shape is not None:
            return box.top() + box.height() * outer * 0.92
        band = 0.19 + 0.19 * max(1 - offset * offset, 0) ** 1.5
        return box.top() + box.height() * (outer + band) * 0.92

    def draw(self, painter: QtGui.QPainter) -> None:
        reading = self.readings[0]
        box = QtCore.QRectF(0, 0, self.width(), self.height())
        low, high = self.scale_range(reading)
        blocks = self.settings.large_ticks * self.settings.small_ticks
        fraction = fraction_of(reading.value, low, high)
        edge = QtGui.QPen(self.text_colour(), 1)
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        for block in range(blocks):
            start, end = block / blocks, (block + 1) / blocks
            if start >= fraction and not (block == 0 and fraction > 0):
                break
            x0, x1 = start * box.width(), max(end * box.width() - 1.5, start * box.width() + 1)
            polygon = QtGui.QPolygonF([
                QtCore.QPointF(x0, self._edge(self.OUTER, x0, box)), QtCore.QPointF(x1, self._edge(self.OUTER, x1, box)),
                QtCore.QPointF(x1, self._edge(self.INNER, x1, box)), QtCore.QPointF(x0, self._edge(self.INNER, x0, box)),
            ])
            middle = low + (high - low) * (start + end) / 2
            painter.setBrush(state_colour(self.level_at(reading, middle), gauge_colour(self.settings.colour)))
            painter.drawPolygon(polygon)
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        painter.setPen(edge)
        for shape in (self.OUTER, self.INNER):
            path = QtGui.QPainterPath(QtCore.QPointF(0, self._edge(shape, 0, box)))
            for step in range(1, 41):
                x = box.width() * step / 40
                path.lineTo(x, self._edge(shape, x, box))
            painter.drawPath(path)
        major, _minor = tick_values(low, high, self.settings.large_ticks, 1)
        factor = self.factor()
        painter.setFont(make_font(painter, min(max(box.height() * 0.085, 8), 14)))
        for index, value in enumerate(major):
            x = min(max(index / self.settings.large_ticks * box.width(), 14), box.width() - 22)
            y = self._edge(self.INNER, x, box)
            painter.setPen(self.text_colour())
            painter.drawText(QtCore.QRectF(x - 25, y + 6, 50, 16), AL.AlignCenter, tick_text(value, factor))
        title = self.title(reading)
        painter.setPen(reading.colour_of())
        painter.setFont(fit_font(painter, title, max(box.height() * 0.08, 9), box.width() * 0.4, bold=True))
        painter.drawText(QtCore.QRectF(box.width() * 0.5, box.height() * 0.66, box.width() * 0.48, box.height() * 0.1), AL.AlignRight | AL.AlignVCenter, title)
        text = self.value_with_units(reading)
        painter.setPen(self.text_colour())
        painter.setFont(fit_font(painter, text, max(box.height() * 0.2, 12), box.width() * 0.45, bold=True, italic=True))
        painter.drawText(QtCore.QRectF(box.width() * 0.45, box.height() * 0.74, box.width() * 0.53, box.height() * 0.26), AL.AlignRight | AL.AlignVCenter, text)


class TargetGauge(Gauge):
    """Target (first parameter) over actual (second) on one long scale; a bar between the pointers shows the error."""

    readings_wanted = 2
    MINIMUM_SIZE = (160, 50)
    show_units = False

    def draw(self, painter: QtGui.QPainter) -> None:
        target = self.readings[0]
        actual = self.readings[1] if len(self.readings) > 1 else self.readings[0]
        w, h = self.width(), self.height()
        low, high = self.scale_range(actual)
        text_width = min(max(w * 0.15, 56), 120)
        scale_left, scale_right = text_width + 20, w - 30
        painter.setPen(self.text_colour())
        for reading, top in ((target, 0.02), (actual, 0.5)) if len(self.readings) > 1 else ((actual, 0.5),):
            painter.setFont(fit_font(painter, self.title(reading), h * 0.1, text_width, False))
            painter.drawText(QtCore.QRectF(6, h * top, text_width, h * 0.14), AL.AlignLeft | AL.AlignVCenter, self.title(reading))
            value = self.value_text(reading)
            painter.setFont(fit_font(painter, value, h * 0.22, text_width, bold=True, italic=True))
            painter.drawText(QtCore.QRectF(6, h * (top + 0.14), text_width, h * 0.28), AL.AlignLeft | AL.AlignVCenter, value)

        def x_at(value: float) -> float:
            return scale_left + fraction_of(value, low, high) * (scale_right - scale_left)

        major, minor = tick_values(low, high, self.settings.large_ticks, self.settings.small_ticks)
        factor = self.factor()
        top, big_bottom, small_bottom = h * 0.5, h * 0.74, h * 0.66
        pen_width = max(w / 600, 1.5)
        for values, bottom in ((minor, small_bottom), (major, big_bottom)):
            for value in values:
                painter.setPen(flat_pen(self.tick_colour(actual, value), pen_width))
                painter.drawLine(QtCore.QPointF(x_at(value), top), QtCore.QPointF(x_at(value), bottom))
        painter.setPen(self.text_colour())
        painter.setFont(make_font(painter, max(min(h * 0.19, (scale_right - scale_left) / max(len(major), 1) / 2.6), 8), bold=True))
        for value in major:
            painter.drawText(QtCore.QRectF(x_at(value) - 40, big_bottom + 1, 80, h - big_bottom - 1), AL.AlignHCenter | AL.AlignTop, tick_text(value, factor))
        if actual.value is None:
            return
        colour = gauge_colour(self.settings.colour)
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(colour)
        half = max(min(h * 0.16, 26), 6)
        up_x = x_at(actual.value)
        apex = h * 0.33
        painter.drawPolygon(QtGui.QPolygonF([QtCore.QPointF(up_x, apex), QtCore.QPointF(up_x - half, apex + half), QtCore.QPointF(up_x + half, apex + half)]))
        if len(self.readings) > 1 and target.value is not None:
            down_x = x_at(target.value)
            painter.drawPolygon(QtGui.QPolygonF([QtCore.QPointF(down_x, h * 0.165), QtCore.QPointF(down_x - half, h * 0.165 - half), QtCore.QPointF(down_x + half, h * 0.165 - half)]))
            left, right = sorted((down_x, up_x))
            painter.drawRect(QtCore.QRectF(left, h * 0.165, max(right - left, 1), h * 0.165))


class ValueList(QtWidgets.QWidget):
    """PCLink's Runtime Value List: names and values in columns, shaded in alternate rows, coloured per parameter."""

    ROW_HEIGHT = 17

    def __init__(self, columns: int = 1, bold: bool = False, sort_mode: int = 0, font_size: int = 0) -> None:
        super().__init__()
        self.columns = max(columns, 1)
        self.bold = bold
        self.sort_mode = sort_mode
        self.font_size = font_size
        self.readings: list[Reading] = []
        self.setMinimumSize(60, 30)

    def set_readings(self, readings: list[Reading]) -> None:
        self.readings = readings
        self.update()

    @staticmethod
    def status_colour(reading: Reading) -> QtGui.QColor | None:
        """Status values show as a coloured block: green when fine, red on a fault."""
        if not reading.is_option:
            return None
        text = reading.text.strip().lower()
        if text.startswith("no ") or text in OK_WORDS:
            return OK_COLOUR
        if any(word in text for word in FAULT_WORDS):
            return ERROR_COLOUR
        return None

    def paintEvent(self, _event: QtGui.QPaintEvent) -> None:
        painter = QtGui.QPainter(self)
        base = _THEME["base"]
        painter.fillRect(self.rect(), base)
        font = painter.font()
        font.setBold(self.bold)
        font.setPixelSize(13)
        if self.font_size:
            font.setPointSize(self.font_size)
        painter.setFont(font)
        count = len(self.readings)
        rows = max(-(-count // self.columns), 1)
        row_height = max(self.ROW_HEIGHT, QtGui.QFontMetrics(font).height() + 3)
        across = self.sort_mode != 1  # Sort Across fills each row before the next; Sort Down fills each column.
        width = self.width() / self.columns
        shade = base.lighter(150) if base.lightness() > 40 else QtGui.QColor(base.red() + 16, base.green() + 16, base.blue() + 16)
        if base.lightness() > 200:
            shade = base.darker(106)
        for index, reading in enumerate(self.readings):
            column, row = (index % self.columns, index // self.columns) if across else divmod(index, rows)
            if column >= self.columns:
                break
            box = QtCore.QRectF(column * width, row * row_height, width, row_height)
            if box.top() >= self.height():
                continue
            if row % 2:
                painter.fillRect(box, shade)
            level = reading.level()
            colour = reading.colour_of()
            inner = box.adjusted(4, 0, -4, 0)
            block = self.status_colour(reading)
            if level:
                painter.fillRect(box, ERROR_COLOUR if level == 2 else WARNING_COLOUR)
                colour = QtGui.QColor(0, 0, 0)
            elif block is not None:
                wide = QtGui.QFontMetricsF(font).horizontalAdvance(reading.plain()) + 14
                painter.fillRect(QtCore.QRectF(box.right() - wide - 2, box.top() + 1, wide, box.height() - 2), block)
            painter.setPen(colour)
            painter.drawText(inner, AL.AlignLeft | AL.AlignVCenter, reading.label())
            if block is not None and not level:
                painter.setPen(QtGui.QColor(0, 0, 0))
            painter.drawText(inner, AL.AlignRight | AL.AlignVCenter, reading.plain())


GAUGE_TYPES: dict[str, type[Gauge]] = {
    "Digital Gauge": DigitalGauge,
    "Bar Gauge": BarGauge,
    "Curve Gauge": CurveGauge,
    "Curved Bar": CurveGauge,
    "Dial Gauge": DialGauge,
    "Target Gauge": TargetGauge,
}
