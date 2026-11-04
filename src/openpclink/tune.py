"""Quick Tune, Quick Trim, tuning markers and the Mixture Map, following PCLink's own rules."""
from __future__ import annotations

import bisect
import struct
from dataclasses import dataclass, field

from .logfile import Log

RUNTIME_MARKER, PART_TUNED, TUNED = 1, 2, 4
FUEL_TABLE_1, FUEL_TABLE_2 = 2365, 2367
ACTIVE_FUEL_1, ACTIVE_FUEL_2 = 2576, 2577
DUAL_FUEL_MODE = 3133
TABLE_4D_MODE, TABLE_5D_MODE = 3055, 2893
LAMBDA_TARGET_RUNTIME, LAMBDA_TARGET_TABLE = 2936, 3890
LAMBDA_AVERAGE = 3042
CL_MODE, CL_CORRECTION = 2394, 2392
ENGINE_SPEED, ECT = 1000, 1004
STOICH_RATIO, FUEL_EQUATION_MODE = 2876, 3737
MAX_CELL_SAMPLES = 1000
TRANSIENT_WINDOW = 0.25


def tune_value(old: float, measured: float, target: float, additive: bool = False) -> float:
    """PCLink's correction. It is not measured/target, and is worked in lambda even when AFR shows."""
    return (measured - target) * 100.0 + old if additive else (measured - target + 1.0) * old


def quick_trim_value(cell: float, measured: float, target: float, correction_percent: float) -> float:
    return cell * (measured / (1 - correction_percent * 0.01) - target + 1.0)


def is_additive(table_id: int, dual_mode: float) -> bool:
    return not (table_id == FUEL_TABLE_1 or (table_id == FUEL_TABLE_2 and dual_mode == 1))


def lookup_target(table: tuple[list[float], list[float], list[float]] | None, rpm: float, load: float, overlay: float = 0.0) -> float | None:
    """The lambda target from table 3890 when the runtime target 2936 is absent, with the AFR overlay added."""
    if table is None:
        return None
    xs, ys, values = table
    return bilinear(xs, ys, values, rpm, load) + overlay


def bilinear(xs: list[float], ys: list[float], values: list[float], x: float, y: float) -> float:
    """A table lookup, holding at the table's edges."""
    columns = len(xs)

    def split(axis: list[float], at: float) -> tuple[int, int, float]:
        if len(axis) < 2 or at <= axis[0]:
            return 0, 0, 0.0
        if at >= axis[-1]:
            return len(axis) - 1, len(axis) - 1, 0.0
        high = bisect.bisect_right(axis, at)
        low = high - 1
        return low, high, (at - axis[low]) / ((axis[high] - axis[low]) or 1.0)

    c0, c1, fx = split(xs, x)
    r0, r1, fy = split(ys, y)

    def cell(row: int, column: int) -> float:
        return values[row * columns + column]

    top = cell(r0, c0) + (cell(r0, c1) - cell(r0, c0)) * fx
    bottom = cell(r1, c0) + (cell(r1, c1) - cell(r1, c0)) * fx
    return top + (bottom - top) * fy


def cell_bounds(axis: list[float], index: int) -> tuple[float, float, float]:
    """A cell's low edge, high edge and centre on one axis: half way to its neighbours."""
    centre = axis[index]
    low = (axis[index - 1] + centre) / 2 if index > 0 else centre - (axis[1] - centre) / 2 if len(axis) > 1 else centre
    high = (axis[index + 1] + centre) / 2 if index + 1 < len(axis) else centre + (centre - axis[-2]) / 2 if len(axis) > 1 else centre
    return low, high, centre


def neighbour_bounds(axis: list[float], index: int) -> tuple[float, float, float]:
    """What Quick Tune measures its centre test against: the breakpoint either side and the cell's own (PCLink 0044ddf8)."""
    last = len(axis) - 1
    return axis[max(index - 1, 0)], axis[min(index + 1, last)], axis[index]


class CellMarks:
    """What PCLink remembers per table cell: markers, attempts and the last measurement."""

    def __init__(self) -> None:
        self.flags: dict[tuple[int, int, int], int] = {}
        self.attempts: dict[tuple[int, int, int], int] = {}
        self.measured: dict[tuple[int, int, int], float] = {}

    def flag(self, table_id: int, row: int, column: int) -> int:
        return self.flags.get((table_id, row, column), 0)

    def set_flag(self, table_id: int, row: int, column: int, bit: int, on: bool = True) -> None:
        key = (table_id, row, column)
        value = self.flags.get(key, 0)
        if bit == TUNED and on:
            if not value & TUNED:
                self.attempts[key] = 0
            value &= ~PART_TUNED
        self.flags[key] = value | bit if on else value & ~bit

    def toggle(self, table_id: int, cells: list[tuple[int, int]], bit: int) -> None:
        """The first selected cell decides the new state for all of them."""
        if not cells:
            return
        turn_on = not self.flag(table_id, *cells[0]) & bit
        for row, column in cells:
            self.set_flag(table_id, row, column, bit, turn_on)

    def clear_table(self, table_id: int) -> None:
        for key in [key for key in self.flags if key[0] == table_id]:
            del self.flags[key]

    def clear_all(self) -> None:
        self.flags.clear()


def within_centre(value: float, cell: tuple[float, float, float], tolerance: float) -> bool:
    low, high, centre = cell
    reach = (high - low) * tolerance / 100
    return centre - reach < value <= centre + reach


QT_NAMES = {
    "table_index": "Table", "wideband_index": "WBSourcesItem", "tune_ahead": "TuneAhead", "filter": "Filter", "precision": "Precision",
    "time_in_centre": "TimeInCellCenter", "max_attempts": "MaxAutoAttempts", "load_tolerance": "LoadCenterTollerace",
    "rpm_tolerance": "RPMCenterTollerace", "rpm_lower": "RPMLockoutLower", "rpm_upper": "RPMLockoutUpper",
    "load_lower": "LoadLockoutLower", "load_upper": "LoadLockoutUpper", "ect_lockout": "ECTLockout",
    "gauge_minimum": "GaugeMinimum", "gauge_maximum": "GaugeMaximum", "tune_ahead_trim": "TuneAheadTrim",
}


@dataclass
class QuickTuneSettings:
    """PCLink's defaults."""

    table_index: int = 0
    wideband_index: int = 0
    tune_ahead: int = 0
    filter: int = 1
    precision: float = 0.01
    time_in_centre: float = 3.0
    max_attempts: int = 8
    load_tolerance: float = 20.0
    rpm_tolerance: float = 20.0
    rpm_lower: float = 500.0
    rpm_upper: float = 8000.0
    load_lower: float = -100.0
    load_upper: float = 250.0
    ect_lockout: float = 70.0
    gauge_minimum: float = 0.7
    gauge_maximum: float = 1.2
    tune_ahead_trim: float = 2.5


    def to_options(self) -> dict:
        return {QT_NAMES[name]: getattr(self, name) for name in QT_NAMES}

    def load_options(self, saved: dict) -> None:
        """Take what an options file holds, keeping the type of each default."""
        for name, key in QT_NAMES.items():
            if key in saved:
                try:
                    setattr(self, name, type(getattr(self, name))(saved[key]))
                except (TypeError, ValueError):
                    pass


OFF, MANUAL, AUTOMATIC = 0, 1, 2
TUNE_AHEAD_OPTIONS = ("Off", "Copy Right", "Copy Down", "Copy Right and Down", "Fill Right and Down")


@dataclass
class TuneInputs:
    """What one update of Quick Tune looks at. None means not available."""

    wideband: float | None
    target: float | None
    has_table: bool
    table_active: bool
    rpm: float
    load: float
    ect: float
    rpm_cell: tuple[float, float, float] | None
    load_cell: tuple[float, float, float] | None
    cell: tuple[int, int] | None
    now: float


@dataclass
class Status:
    text: str
    hint: str
    colour: int = 0
    ready: bool = False
    centred: bool = False
    may_tune: bool = False


class QuickTune:
    """PCLink's status logic, evaluated in its order. Holds no Qt and does no ECU traffic."""

    def __init__(self, settings: QuickTuneSettings | None = None) -> None:
        self.settings = settings or QuickTuneSettings()
        self.mode = OFF
        self.running = False  # Automatic mode's start/stop button.
        self.samples: list[float] = []
        self.started: float | None = None
        self.cell: tuple[int, int] | None = None
        self.ahead_memory = 0.0
        self.measured = 0.0
        self.target = 0.0

    def reset_cell(self) -> None:
        self.started = None
        self.ahead_memory = 0.0

    def update(self, inputs: TuneInputs, attempts: int = 0, tuned: bool = False) -> Status:
        s = self.settings
        if inputs.wideband is not None:
            self.samples.append(inputs.wideband)
            del self.samples[: max(len(self.samples) - max(s.filter, 1), 0)]
        self.measured = sum(self.samples) / len(self.samples) if self.samples else 0.0
        self.target = inputs.target or 0.0
        if inputs.cell != self.cell:
            self.cell = inputs.cell
            self.started = None
        if self.mode == OFF:
            return Status("OFF", "Quick tune is off.")
        if not inputs.has_table:
            return Status("Fuel table undefined", "Target fuel table not defined.")
        if inputs.target is None:
            return Status("Oxy target undefined", "The Oxy target is not defined.")
        if self.measured == 0:
            return Status("No WB Signal", "No WB signal is present or configured.", 0xFF)
        if not inputs.table_active:
            return Status("Table Inactive", "Selected table is not active.")
        if s.ect_lockout > inputs.ect:
            return Status("ECT Lockout", "Engine coolant temperature is not above the ECT lockout.", 0xFF)
        if not s.rpm_lower <= inputs.rpm <= s.rpm_upper:
            return Status("RPM Lockout", "RPM is not between the upper and lower RPM lockouts.", 0xFF)
        if not s.load_lower <= inputs.load <= s.load_upper:
            return Status("Load Lockout", "Load is not between the upper and lower Load lockouts.", 0xFF)
        if not self._centred(inputs):
            return Status("Not in cell centre", "Not in cell centre", 0x0482FF)
        if self.started is None:
            self.started = inputs.now
        if inputs.now - self.started < s.time_in_centre:
            text = f"Waiting ({attempts})" if attempts else "Waiting..."
            return Status(text, "Waiting for time in Cell centre to expire.", centred=True)
        within = abs(self.target - self.measured) < s.precision
        if self.mode == AUTOMATIC and (within or tuned):
            return Status("Target Reached", "The measured AFR is within the specified precision of the target.", 0x8000, centred=True, may_tune=self.running)
        if self.mode != AUTOMATIC or attempts < s.max_attempts:
            return Status("Ready", "Ready", 0x8000, ready=True, centred=True, may_tune=True)
        return Status("Max Attempts", "The maximum number of auto attempts for these current cell has been reached.", centred=True, may_tune=True)

    def _centred(self, inputs: TuneInputs) -> bool:
        s = self.settings
        if inputs.rpm_cell is None:
            return False
        if not within_centre(inputs.rpm, inputs.rpm_cell, s.rpm_tolerance):
            return False
        return inputs.load_cell is None or within_centre(inputs.load, inputs.load_cell, s.load_tolerance)

    def tune_ahead(self, row: int, column: int, value: float, rows: int, columns: int, tuned) -> list[tuple[int, int, float]]:
        """Cells to change after a step; `tuned(row, column)` protects finished cells."""
        step = value * self.settings.tune_ahead_trim / 100
        ahead = self.settings.tune_ahead
        out: list[tuple[int, int, float]] = []
        if ahead in (1, 3):
            out.append((row, column + 1, value + step))
        if ahead in (2, 3):
            out.append((row + 1, column, value + step))
        if ahead == 3:
            out.append((row + 1, column + 1, value + step))
        if ahead == 4 and value > self.ahead_memory:
            self.ahead_memory = value
            for r in range(row, rows):
                for c in range(column, columns):
                    if (r, c) != (row, column):
                        out.append((r, c, value + step * ((r - row) + (c - column))))
        return [(r, c, v) for r, c, v in out if r < rows and c < columns and not tuned(r, c)]


@dataclass
class MixtureCell:
    x: tuple[float, float, float]
    y: tuple[float, float, float]
    actual: list[float] = field(default_factory=list)
    target: list[float] = field(default_factory=list)
    samples: int = 0
    actual_average: float = 0.0
    target_average: float = 0.0
    error: float = 0.0
    has_data: bool = False
    corrected: bool = False


@dataclass
class MixtureFilter:
    channel: int
    greater: bool
    value: float
    transient: bool = False


@dataclass
class MixtureSettings:
    """PCLink's attributes, with its defaults for a new view."""

    table: int = FUEL_TABLE_1
    rpm_id: int = ENGINE_SPEED
    load_id: int = 1091
    load2_id: int = 1091
    mixture_id: int = 3040
    filters: list = field(default_factory=list)  # (data id, greater, value, transient, enabled)
    zone_enabled: bool = True
    zone_area: float = 25.0
    min_samples_enabled: bool = True
    min_samples: int = 5
    show_target: bool = True
    show_actual: bool = True
    show_samples: bool = True
    show_error: bool = True
    absolute_error: float = 0.27

    @classmethod
    def from_attributes(cls, a: dict[str, str]) -> "MixtureSettings":
        def number(name: str, default: float) -> float:
            try:
                return float(a.get(name, default))
            except ValueError:
                return default

        s = cls(
            table=int(number("FuelTable", FUEL_TABLE_1)), rpm_id=int(number("RPMSeriesDataID", ENGINE_SPEED)),
            load_id=int(number("LoadSeriesDataID", 1091)), load2_id=int(number("Load2SeriesDataID", 1091)),
            mixture_id=int(number("MixtureSeriesDataID", 3040)),
            zone_enabled=a.get("ZoneAreaEnabled", "true") == "true", zone_area=number("ActiveZoneArea", 25.0),
            min_samples_enabled=a.get("MinSamplesEnabled", "true") == "true", min_samples=int(number("MinSamples", 5)),
            show_target=a.get("ShowTarget", "true") == "true", show_actual=a.get("ShowActual", "true") == "true",
            show_samples=a.get("ShowSamples", "true") == "true", show_error=a.get("ShowError", "true") == "true",
            absolute_error=number("AbsoluteError", 0.27),
        )
        for prefix, series, transient in (("Cond", "CondFilter", False), ("Delta", "DeltaFilter", True)):
            for n in (1, 2, 3):
                s.filters.append((
                    int(number(f"{series}{n}SeriesDataID", 0)), a.get(f"{prefix}{n}Greater", "true") == "true",
                    number(f"{prefix}{n}Value", 0.0), transient, a.get(f"{prefix}{n}Enabled") == "true",
                ))
        return s

    def attributes(self) -> dict[str, str]:
        flag = lambda value: "true" if value else "false"  # noqa: E731
        a = {
            "FuelTable": str(self.table), "RPMSeriesDataID": str(self.rpm_id), "LoadSeriesDataID": str(self.load_id),
            "Load2SeriesDataID": str(self.load2_id), "MixtureSeriesDataID": str(self.mixture_id),
            "ZoneAreaEnabled": flag(self.zone_enabled), "ActiveZoneArea": f"{self.zone_area:g}",
            "MinSamplesEnabled": flag(self.min_samples_enabled), "MinSamples": str(self.min_samples),
            "ShowTarget": flag(self.show_target), "ShowActual": flag(self.show_actual),
            "ShowSamples": flag(self.show_samples), "ShowError": flag(self.show_error),
            "AbsoluteError": f"{self.absolute_error:g}",
        }
        for index, (data_id, greater, value, transient, enabled) in enumerate(self.filters):
            prefix, series, n = ("Delta", "DeltaFilter", index - 2) if transient else ("Cond", "CondFilter", index + 1)
            a[f"{series}{n}SeriesDataID"] = str(data_id)
            a[f"{prefix}{n}Greater"] = flag(greater)
            a[f"{prefix}{n}Value"] = f"{value:g}"
            a[f"{prefix}{n}Enabled"] = flag(enabled)
        return a


def _single(value: float) -> float:
    return struct.unpack("<f", struct.pack("<f", value))[0]


class MixtureError(Exception):
    pass


def _passes(log: Log, settings: MixtureSettings, when: float) -> bool:
    for data_id, greater, value, transient, enabled in settings.filters:
        if not enabled or not data_id:
            continue
        index = log.channel_of(data_id)
        if index is None:
            continue
        series = log.series[index]
        if transient:
            earlier = when - TRANSIENT_WINDOW
            if earlier < series.times[0]:
                return False
            change = abs(series.value_at(when) - series.value_at(earlier))
            limit = value * TRANSIENT_WINDOW
        else:
            change, limit = series.value_at(when), value
        if not (change > limit if greater else change < limit):
            return False
    return True


def build_mixture_map(log: Log | None, settings: MixtureSettings, xs: list[float], ys: list[float], target_lookup, progress=None) -> list[list[MixtureCell]]:
    """Averages logged mixture by fuel table cell. `target_lookup(rpm, load2)` is the target table."""
    if log is None:
        raise MixtureError("No log file found.")

    def channel(data_id: int, message: str):
        index = log.channel_of(data_id)
        if index is None:
            raise MixtureError(message)
        return log.series[index]

    rpm = channel(settings.rpm_id, "Engine speed parameter missing.")
    load = channel(settings.load_id, "Fuel table parameter missing.")
    load2 = channel(settings.load2_id, "Fuel table parameter missing.")
    mixture = channel(settings.mixture_id, "Wideband parameter missing.")
    target_index = log.channel_of(LAMBDA_TARGET_RUNTIME)
    target_series = log.series[target_index] if target_index is not None else None
    if target_series is None and target_lookup is None:
        raise MixtureError("Target table parameter missing.")
    grid = [[MixtureCell(cell_bounds(xs, c), cell_bounds(ys, r) if ys else (-1e9, 1e9, 0.0)) for c in range(len(xs))] for r in range(max(len(ys), 1))]
    shrink = 1 - settings.zone_area / 100 if settings.zone_enabled else 0.0
    period = rpm.period if rpm.period > 0 else 0.1
    if len(rpm.times) < 2:
        return grid
    when = rpm.times[0]
    steps = int(_single(_single(rpm.times[-1]) - _single(when)) / _single(period))  # PCLink holds log times as 4-byte floats.
    for done in range(steps):
        if progress is not None and done % max(steps // 10, 1) == 0:
            progress(done / steps)
        x, y = rpm.value_at(when), load.value_at(when)
        when += period  # PCLink reads the mixture and target one step on from the engine's place.
        for row in grid:
            for cell in row:
                x_low = cell.x[0] + (cell.x[2] - cell.x[0]) * shrink
                x_high = cell.x[1] - (cell.x[1] - cell.x[2]) * shrink
                y_low = cell.y[0] + (cell.y[2] - cell.y[0]) * shrink
                y_high = cell.y[1] - (cell.y[1] - cell.y[2]) * shrink
                if x_low < x < x_high and y_low < y < y_high and cell.samples < MAX_CELL_SAMPLES and _passes(log, settings, when):
                    cell.actual.append(mixture.value_at(when))
                    cell.target.append(target_series.value_at(when) if target_series else target_lookup(x, load2.value_at(when)))
                    cell.samples += 1
    for row in grid:
        for cell in row:
            if settings.min_samples_enabled and cell.samples < settings.min_samples:
                cell.samples = 0
            if cell.samples >= 1:
                cell.actual_average = sum(cell.actual) / len(cell.actual)
                cell.target_average = sum(cell.target) / len(cell.target)
                cell.error = abs(cell.target_average - cell.actual_average)
                cell.has_data = True
    return grid


def fft_magnitudes(samples: list[float]) -> list[float]:
    """Magnitudes of a power-of-two block, for the knock audio view."""
    n = len(samples)
    real = list(samples)
    imag = [0.0] * n
    j = 0
    for i in range(1, n):
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j |= bit
        if i < j:
            real[i], real[j] = real[j], real[i]
    import math

    size = 2
    while size <= n:
        angle = -2 * math.pi / size
        w_r, w_i = math.cos(angle), math.sin(angle)
        for start in range(0, n, size):
            c_r, c_i = 1.0, 0.0
            half = size // 2
            for k in range(start, start + half):
                t_r = real[k + half] * c_r - imag[k + half] * c_i
                t_i = real[k + half] * c_i + imag[k + half] * c_r
                real[k + half], imag[k + half] = real[k] - t_r, imag[k] - t_i
                real[k], imag[k] = real[k] + t_r, imag[k] + t_i
                c_r, c_i = c_r * w_r - c_i * w_i, c_r * w_i + c_i * w_r
        size *= 2
    return [math.hypot(real[i], imag[i]) / n for i in range(n // 2)]


def mixture_palette() -> list[tuple[int, int, int]]:
    """FUN_0048335c: eight bands, green first, yellow in the middle, red last, the rest worked between."""
    colours = []
    for k in range(8):
        if k <= 4:
            colours.append((round(255 * k / 4), 255, 0))
        else:
            colours.append((255, round(255 * (1 - (k - 4) / 3)), 0))
    return colours


def mixture_band(error: float, full_scale: float) -> tuple[int, int, int]:
    palette = mixture_palette()
    if full_scale <= 0:
        return palette[-1]
    return palette[max(0, min(int(error / full_scale * len(palette)), len(palette) - 1))]
