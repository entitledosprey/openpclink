"""PCLink's live views: digital, bar, dial, curve and target gauges, and the value list."""
from __future__ import annotations

import math
from dataclasses import dataclass

from PySide6 import QtCore, QtGui, QtWidgets

DEFAULT_GAUGE_COLOUR = 0xFF9630  # A TColor, so blue is the top byte.
NO_WARNING = 65500  # PCLink's "off" for a warning limit.
WARNING_COLOUR = QtGui.QColor(240, 220, 40)
ERROR_COLOUR = QtGui.QColor(230, 40, 40)
OK_COLOUR = QtGui.QColor(60, 230, 90)
# PCLink colours a parameter by a small index into its theme; set when the theme is applied.
PARAMETER_COLOURS: list[QtGui.QColor] = []


_THEME = {"base": QtGui.QColor(0, 0, 0), "text": QtGui.QColor(245, 245, 245), "lane": QtGui.QColor(61, 61, 61)}


def set_theme(base: QtGui.QColor, text: QtGui.QColor, lane: QtGui.QColor) -> None:
    _THEME.update(base=base, text=text, lane=lane)


def tcolor(value: int) -> QtGui.QColor:
    """A Delphi TColor, which stores blue in its high byte."""
    return QtGui.QColor(value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF)


def to_tcolor(colour: QtGui.QColor) -> int:
    return colour.red() | colour.green() << 8 | colour.blue() << 16


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
    large_ticks: int = 5
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


class Gauge(QtWidgets.QWidget):
    """Shows the readings it is given. Subclasses draw them."""

    MINIMUM_SIZE = (60, 40)
    readings_wanted = 1

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
            return self.settings.minimum, self.settings.maximum
        return reading.minimum, reading.maximum

    def limits(self, reading: Reading) -> tuple[float | None, float | None, float | None, float | None]:
        """Low red, low yellow, yellow and red, where this gauge has them."""
        if self.settings.manual:
            s = self.settings
            return (s.low_red if s.low_red >= s.minimum else None, s.low_yellow if s.low_yellow >= s.minimum else None,
                    s.yellow if s.yellow <= s.maximum else None, s.red if s.red <= s.maximum else None)
        return reading.low_error, reading.low_warning, reading.high_warning, reading.high_error

    def value_text(self, reading: Reading) -> str:
        if reading.is_option:
            return reading.text
        if self.settings.manual and reading.value is not None and self.settings.decimals >= 0:
            return f"{reading.value:.{self.settings.decimals}f}"
        return reading.text

    def title(self, reading: Reading) -> str:
        return self.settings.caption or reading.caption

    def text_colour(self) -> QtGui.QColor:
        return _THEME["text"]

    def fill_colour(self, reading: Reading) -> QtGui.QColor:
        level = reading.level()
        return ERROR_COLOUR if level == 2 else WARNING_COLOUR if level == 1 else tcolor(self.settings.colour)

    def paintEvent(self, _event: QtGui.QPaintEvent) -> None:
        painter = QtGui.QPainter(self)
        if self.settings.high_quality:
            painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), _THEME["base"])
        if self.readings:
            self.draw(painter)

    def draw(self, painter: QtGui.QPainter) -> None:
        raise NotImplementedError


class DigitalGauge(Gauge):
    """A stack of large numbers, one per value, coloured by warning level."""

    readings_wanted = 4

    def draw(self, painter: QtGui.QPainter) -> None:
        height = self.height() / len(self.readings)
        for index, reading in enumerate(self.readings):
            box = QtCore.QRectF(0, index * height, self.width(), height)
            painter.setPen(self.text_colour())
            caption = painter.font()
            caption.setPixelSize(max(int(height * 0.18), 9))
            painter.setFont(caption)
            painter.drawText(box.adjusted(4, 2, -4, 0), QtCore.Qt.AlignmentFlag.AlignHCenter | QtCore.Qt.AlignmentFlag.AlignTop, self.title(reading))
            number = painter.font()
            number.setPixelSize(max(int(min(height * 0.5, self.width() / max(len(self.value_text(reading)), 4) * 1.5)), 10))
            number.setBold(True)
            painter.setFont(number)
            level = reading.level()
            painter.setPen(ERROR_COLOUR if level == 2 else WARNING_COLOUR if level == 1 else OK_COLOUR)
            painter.drawText(box.adjusted(0, height * 0.18, 0, 0), QtCore.Qt.AlignmentFlag.AlignCenter, self.value_text(reading))
            number.setBold(False)
            painter.setFont(number)


class BarGauge(Gauge):
    """A vertical bar with warning bands. Values outside its range pin at the end."""

    readings_wanted = 1

    def draw(self, painter: QtGui.QPainter) -> None:
        width = self.width() / len(self.readings)
        for index, reading in enumerate(self.readings):
            self._draw_one(painter, QtCore.QRectF(index * width, 0, width, self.height()), reading)

    def _draw_one(self, painter: QtGui.QPainter, box: QtCore.QRectF, reading: Reading) -> None:
        low, high = self.scale_range(reading)
        small = painter.font()
        small.setPixelSize(max(int(min(box.width() * 0.16, 13)), 8))
        painter.setFont(small)
        painter.setPen(self.text_colour())
        painter.drawText(box.adjusted(1, 1, -1, 0), QtCore.Qt.AlignmentFlag.AlignHCenter | QtCore.Qt.AlignmentFlag.AlignTop, self.title(reading))
        painter.drawText(box.adjusted(1, 0, -1, -2), QtCore.Qt.AlignmentFlag.AlignHCenter | QtCore.Qt.AlignmentFlag.AlignBottom, self.value_text(reading))
        lane = box.adjusted(box.width() * 0.45, 22, -box.width() * 0.1, -22)
        painter.fillRect(lane, _THEME["lane"])

        def height_at(value: float) -> float:
            return lane.bottom() - fraction_of(value, low, high) * lane.height()

        low_red, low_yellow, yellow, red = self.limits(reading)
        for start, end, colour in ((yellow, red if red is not None else high, WARNING_COLOUR), (red, high, ERROR_COLOUR), (low_yellow, low_red if low_red is not None else low, WARNING_COLOUR), (low_red, low, ERROR_COLOUR)):
            if start is not None:
                band = QtCore.QRectF(lane.right() - 4, min(height_at(start), height_at(end)), 4, abs(height_at(start) - height_at(end)))
                painter.fillRect(band, colour)
        filled = fraction_of(reading.value, low, high) * lane.height()
        painter.fillRect(QtCore.QRectF(lane.left(), lane.bottom() - filled, lane.width() - 5, filled), self.fill_colour(reading))
        major, minor = tick_values(low, high, self.settings.large_ticks, self.settings.small_ticks)
        painter.setPen(self.text_colour())
        for value in major:
            y = height_at(value)
            painter.drawLine(QtCore.QPointF(lane.left() - 6, y), QtCore.QPointF(lane.left(), y))
            painter.drawText(QtCore.QRectF(box.left(), y - 8, lane.left() - box.left() - 7, 16), QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter, f"{value:g}")
        for value in minor:
            y = height_at(value)
            painter.drawLine(QtCore.QPointF(lane.left() - 3, y), QtCore.QPointF(lane.left(), y))


class _ArcGauge(Gauge):
    SWEEP_START = 225.0
    SWEEP = 270.0
    MINIMUM_SIZE = (110, 110)

    def angle_of(self, fraction: float) -> float:
        return self.SWEEP_START - self.SWEEP * fraction

    def _box(self) -> QtCore.QRectF:
        side = max(min(self.width(), self.height()) - 16, 10)
        return QtCore.QRectF((self.width() - side) / 2, (self.height() - side) / 2, side, side)

    def _draw_bands(self, painter: QtGui.QPainter, box: QtCore.QRectF, reading: Reading, thickness: float) -> None:
        low, high = self.scale_range(reading)
        low_red, low_yellow, yellow, red = self.limits(reading)
        for start, end, colour in ((yellow, high, WARNING_COLOUR), (red, high, ERROR_COLOUR), (low, low_yellow, WARNING_COLOUR), (low, low_red, ERROR_COLOUR)):
            if start is None or end is None:
                continue
            a, b = fraction_of(start, low, high), fraction_of(end, low, high)
            painter.setPen(flat_pen(colour, thickness))
            painter.drawArc(box, int(self.angle_of(a) * 16), int(-self.SWEEP * (b - a) * 16))

    def _draw_ticks(self, painter: QtGui.QPainter, box: QtCore.QRectF, reading: Reading) -> None:
        low, high = self.scale_range(reading)
        major, minor = tick_values(low, high, self.settings.large_ticks, self.settings.small_ticks)
        centre, radius = box.center(), box.width() / 2
        small = painter.font()
        small.setPixelSize(max(int(radius * 0.13), 8))
        painter.setFont(small)
        painter.setPen(QtGui.QPen(self.text_colour(), 1))
        for values, inner in ((major, 0.84), (minor, 0.91)):
            for value in values:
                angle = math.radians(self.angle_of(fraction_of(value, low, high)))
                direction = QtCore.QPointF(math.cos(angle), -math.sin(angle))
                painter.drawLine(centre + direction * radius * inner, centre + direction * radius * 0.97)
                if values is major:
                    at = centre + direction * radius * 0.70
                    painter.drawText(QtCore.QRectF(at.x() - 24, at.y() - 8, 48, 16), QtCore.Qt.AlignmentFlag.AlignCenter, f"{value:g}")


class DialGauge(_ArcGauge):
    """A round gauge with a needle."""

    def draw(self, painter: QtGui.QPainter) -> None:
        reading = self.readings[0]
        box = self._box()
        low, high = self.scale_range(reading)
        self._draw_bands(painter, box, reading, 5)
        self._draw_ticks(painter, box, reading)
        angle = math.radians(self.angle_of(fraction_of(reading.value, low, high)))
        centre, radius = box.center(), box.width() / 2
        painter.setPen(QtGui.QPen(self.fill_colour(reading), max(radius * 0.03, 2)))
        painter.drawLine(centre, centre + QtCore.QPointF(math.cos(angle), -math.sin(angle)) * radius * 0.82)
        painter.setPen(self.text_colour())
        painter.drawText(box.adjusted(0, radius * 1.15, 0, 0), QtCore.Qt.AlignmentFlag.AlignHCenter, self.value_text(reading))
        painter.drawText(box.adjusted(0, radius * 1.45, 0, 0), QtCore.Qt.AlignmentFlag.AlignHCenter, self.title(reading))


class CurveGauge(_ArcGauge):
    """A round gauge whose arc fills up instead of moving a needle."""

    def draw(self, painter: QtGui.QPainter) -> None:
        reading = self.readings[0]
        box = self._box()
        low, high = self.scale_range(reading)
        thickness = max(box.width() * 0.09, 6)
        lane = box.adjusted(thickness, thickness, -thickness, -thickness)
        painter.setPen(flat_pen(_THEME["lane"], thickness))
        painter.drawArc(lane, int(self.SWEEP_START * 16), int(-self.SWEEP * 16))
        self._draw_bands(painter, box.adjusted(2, 2, -2, -2), reading, 3)
        fraction = fraction_of(reading.value, low, high)
        painter.setPen(flat_pen(self.fill_colour(reading), thickness))
        painter.drawArc(lane, int(self.SWEEP_START * 16), int(-self.SWEEP * fraction * 16))
        painter.setPen(self.text_colour())
        big = painter.font()
        big.setPixelSize(max(int(box.width() * 0.17), 10))
        painter.setFont(big)
        painter.drawText(box, QtCore.Qt.AlignmentFlag.AlignCenter, self.value_text(reading))
        small = painter.font()
        small.setPixelSize(max(int(box.width() * 0.08), 8))
        painter.setFont(small)
        painter.drawText(box.adjusted(0, box.height() * 0.62, 0, 0), QtCore.Qt.AlignmentFlag.AlignHCenter, self.title(reading))


class TargetGauge(Gauge):
    """A horizontal scale with a target marker above and the actual value below."""

    readings_wanted = 2
    MINIMUM_SIZE = (160, 60)

    def draw(self, painter: QtGui.QPainter) -> None:
        target = self.readings[0]
        actual = self.readings[1] if len(self.readings) > 1 else self.readings[0]
        low, high = self.scale_range(actual)
        lane = QtCore.QRectF(12, self.height() * 0.40, self.width() - 24, self.height() * 0.22)
        painter.fillRect(lane, _THEME["lane"])

        def x_at(value: float) -> float:
            return lane.left() + fraction_of(value, low, high) * lane.width()

        low_red, low_yellow, yellow, red = self.limits(actual)
        for start, end, colour in ((yellow, high, WARNING_COLOUR), (red, high, ERROR_COLOUR), (low, low_yellow, WARNING_COLOUR), (low, low_red, ERROR_COLOUR)):
            if start is not None and end is not None:
                painter.fillRect(QtCore.QRectF(x_at(start), lane.bottom() - 3, x_at(end) - x_at(start), 3), colour)
        major, _minor = tick_values(low, high, self.settings.large_ticks, self.settings.small_ticks)
        painter.setPen(self.text_colour())
        small = painter.font()
        small.setPixelSize(max(int(self.height() * 0.14), 8))
        painter.setFont(small)
        for value in major:
            painter.drawLine(QtCore.QPointF(x_at(value), lane.bottom()), QtCore.QPointF(x_at(value), lane.bottom() + 5))
            painter.drawText(QtCore.QRectF(x_at(value) - 30, lane.bottom() + 5, 60, 16), QtCore.Qt.AlignmentFlag.AlignCenter, f"{value:g}")
        if len(self.readings) > 1 and target.value is not None:
            marker = QtGui.QPolygonF([
                QtCore.QPointF(x_at(target.value) - 6, lane.top() - 10),
                QtCore.QPointF(x_at(target.value) + 6, lane.top() - 10),
                QtCore.QPointF(x_at(target.value), lane.top()),
            ])
            painter.setBrush(QtGui.QColor(240, 220, 40))
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.drawPolygon(marker)
        if actual.value is not None:
            painter.fillRect(QtCore.QRectF(x_at(actual.value) - 2, lane.top() - 2, 4, lane.height() + 4), self.fill_colour(actual))
        painter.setPen(self.text_colour())
        painter.drawText(QtCore.QRectF(4, 0, self.width() / 2, lane.top() - 10), QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter, self.title(actual))
        text = self.value_text(actual) if len(self.readings) < 2 else f"{self.value_text(target)} / {self.value_text(actual)}"
        painter.drawText(QtCore.QRectF(self.width() / 2, 0, self.width() / 2 - 4, lane.top() - 10), QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter, text)


class ValueList(QtWidgets.QWidget):
    """PCLink's Runtime Value List: names and values in columns, coloured per parameter."""

    ROW_HEIGHT = 17

    def __init__(self, columns: int = 1, bold: bool = False) -> None:
        super().__init__()
        self.columns = max(columns, 1)
        self.bold = bold
        self.readings: list[Reading] = []
        self.setMinimumSize(100, 40)

    def set_readings(self, readings: list[Reading]) -> None:
        self.readings = readings
        self.update()

    def paintEvent(self, _event: QtGui.QPaintEvent) -> None:
        painter = QtGui.QPainter(self)
        painter.fillRect(self.rect(), _THEME["base"])
        font = painter.font()
        font.setBold(self.bold)
        font.setPixelSize(13)
        painter.setFont(font)
        rows = max(self.height() // self.ROW_HEIGHT, 1)
        width = self.width() / self.columns
        for index, reading in enumerate(self.readings):
            column, row = index // rows, index % rows
            if column >= self.columns:
                break
            box = QtCore.QRectF(column * width + 4, row * self.ROW_HEIGHT, width - 8, self.ROW_HEIGHT)
            level = reading.level()
            colour = PARAMETER_COLOURS[reading.colour % len(PARAMETER_COLOURS)] if PARAMETER_COLOURS else _THEME["text"]
            if level:
                painter.fillRect(box, ERROR_COLOUR if level == 2 else WARNING_COLOUR)
                colour = QtGui.QColor(0, 0, 0)
            painter.setPen(colour)
            painter.drawText(box, QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter, reading.caption)
            painter.drawText(box, QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter, reading.text)


GAUGE_TYPES: dict[str, type[Gauge]] = {
    "Digital Gauge": DigitalGauge,
    "Bar Gauge": BarGauge,
    "Curve Gauge": CurveGauge,
    "Curved Bar": CurveGauge,
    "Dial Gauge": DialGauge,
    "Target Gauge": TargetGauge,
}
