"""Quick Tune, Quick Trim, tuning markers and the Mixture Map, following PCLink's own rules."""
from __future__ import annotations

import bisect
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
            return Status("Fuel table undefined", "Select a fuel table.")
        if inputs.target is None:
            return Status("Oxy target undefined", "Set up a lambda target table.")
        if self.measured == 0:
            return Status("No WB Signal", "There is no wideband signal.", 0xFF)
        if not inputs.table_active:
            return Status("Table Inactive", "The selected fuel table is not active.")
        if s.ect_lockout > inputs.ect:
            return Status("ECT Lockout", "The engine is below the ECT lockout.", 0xFF)
        if not s.rpm_lower <= inputs.rpm <= s.rpm_upper:
            return Status("RPM Lockout", "Engine speed is outside the RPM lockouts.", 0xFF)
        if not s.load_lower <= inputs.load <= s.load_upper:
            return Status("Load Lockout", "Load is outside the load lockouts.", 0xFF)
        if not self._centred(inputs):
            return Status("Not in cell centre", "Not in cell centre", 0x0482FF)
        if self.started is None:
            self.started = inputs.now
        if inputs.now - self.started < s.time_in_centre:
            text = f"Waiting ({attempts})" if attempts else "Waiting..."
            return Status(text, "Waiting for the time in cell centre.", centred=True)
        within = abs(self.target - self.measured) < s.precision
        if self.mode == AUTOMATIC and (within or tuned):
            return Status("Target Reached", "This cell has reached its target.", 0x8000, centred=True, may_tune=self.running)
        if self.mode != AUTOMATIC or attempts < s.max_attempts:
            return Status("Ready", "Ready", 0x8000, ready=True, centred=True, may_tune=True)
        return Status("Max Attempts", "This cell has used its maximum tuning attempts.", centred=True, may_tune=True)

    def _centred(self, inputs: TuneInputs) -> bool:
        s = self.settings
        if inputs.rpm_cell is None:
            return False
        low, high, centre = inputs.rpm_cell
        reach = (high - low) * s.rpm_tolerance / 100
        if not centre - reach <= inputs.rpm <= centre + reach:
            return False
        if inputs.load_cell is None:
            return True
        low, high, centre = inputs.load_cell
        reach = (high - low) * s.load_tolerance / 100
        return centre - reach <= inputs.load <= centre + reach

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
                    int(number(f"{series}{n}SeriesDataID", 0)), a.get(f"{prefix}{n}Greater") == "true",
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


def build_mixture_map(log: Log | None, settings: MixtureSettings, xs: list[float], ys: list[float], target_lookup) -> list[list[MixtureCell]]:
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
    while when <= rpm.times[-1]:
        x, y = rpm.value_at(when), load.value_at(when)
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
        when += period
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
