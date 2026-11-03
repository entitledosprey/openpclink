"""PCLink's unit options. The ECU works in base units; these convert for display and back.

Factors and rounding are PCLink's own, including psi and kPa not being exact reciprocals.
"""
from __future__ import annotations

from dataclasses import dataclass

TEMPERATURE, PRESSURE, SPEED, DISTANCE, LAMBDA, BIG_PRESSURE = "temperature", "pressure", "speed", "distance", "lambda", "big_pressure"
GROUP_TITLES = {
    TEMPERATURE: "Temperature", PRESSURE: "Pressure", SPEED: "Speed", DISTANCE: "Distance",
    LAMBDA: "Lambda", BIG_PRESSURE: "DI/Brake Pressure",
}
CHOICES = {
    PRESSURE: ("kPa", "psi"), DISTANCE: ("m", "ft"), LAMBDA: ("lambda", "AFR"), TEMPERATURE: ("°C", "°F"),
    SPEED: ("kph", "mph"), BIG_PRESSURE: ("kPa", "psi", "bar", "MPa"),
}
DEFAULT_METRIC = {PRESSURE: "kPa", DISTANCE: "m", LAMBDA: "lambda", TEMPERATURE: "°C", SPEED: "kph", BIG_PRESSURE: "kPa"}
DEFAULT_IMPERIAL = {PRESSURE: "psi", DISTANCE: "ft", LAMBDA: "AFR", TEMPERATURE: "°F", SPEED: "mph", BIG_PRESSURE: "psi"}
STOICH_DEFAULT = 14.7
_BASE_UNITS = {
    "Temperature": "°C", "TempDifference": "°C", "Pressure": "kPa", "Lambda": "lambda", "Speed": "kph",
    "Distance": "m", "BigDistance": "km", "Volume": "l", "VolumeDistance": "l/100km", "BigPressure": "kPa",
    "BigAcceleration": "km/h/s",
}
_ALIASES = {"kPa": "Pressure", "°C": "Temperature", "km/h": "Speed", "λ": "Lambda", "L": "Volume"}


@dataclass(frozen=True)
class _Way:
    label: str
    to_display: object
    to_base: object
    decimals: object = None


_SAME = None


class UnitSystem:
    """The chosen unit for each kind of value, as PCLink converts them."""

    def __init__(self, choice: dict[str, str] | None = None, stoich: float = STOICH_DEFAULT, force_lambda: bool = False) -> None:
        self.choice = dict(DEFAULT_METRIC)
        self.choice.update(choice or {})
        self.stoich = stoich or STOICH_DEFAULT
        self.force_lambda = force_lambda

    def _way(self, value_type: str) -> _Way | None:
        chosen = self.choice
        speed_imperial = chosen[SPEED] == "mph"
        if value_type == "Temperature" and chosen[TEMPERATURE] == "°F":
            return _Way("°F", lambda c: c * 1.8 + 32, lambda f: round((f - 32) / 1.8))
        if value_type == "TempDifference" and chosen[TEMPERATURE] == "°F":
            return _Way("°F", lambda c: c * 1.8, lambda f: round(f / 1.8))
        if value_type == "Pressure" and chosen[PRESSURE] == "psi":
            return _Way("psi", lambda k: k * 0.1450377, lambda p: round(p * 6.89475729, 1), lambda d: d or 1)
        if value_type == "Lambda" and chosen[LAMBDA] == "AFR" and not self.force_lambda:
            stoich = self.stoich
            return _Way("AFR", lambda v: v * stoich, lambda a: a / stoich, lambda d: max(d - 1, 0))
        if value_type == "Speed" and speed_imperial:
            return _Way("mph", lambda k: k * 0.621371, lambda m: m * 1.609344)
        if value_type == "Distance" and chosen[DISTANCE] == "ft":
            return _Way("ft", lambda m: m * 3.28084, lambda f: f * 0.3048)
        if value_type == "BigDistance" and speed_imperial:
            return _Way("mi", lambda k: k * 0.621371, lambda m: m * 1.609344)
        if value_type == "Volume" and speed_imperial:
            return _Way("gal(US)", lambda l: l * 0.264172, lambda g: g * 3.78541)
        if value_type == "VolumeDistance" and speed_imperial:
            flip = lambda x: 235.215 / x if x > 0 else 0.0  # noqa: E731
            return _Way("mpg(US)", flip, flip)
        if value_type == "BigAcceleration" and speed_imperial:
            return _Way("mph/s", lambda k: k * 0.621371, lambda m: m * 1.609344)
        if value_type == "BigPressure":
            unit = chosen[BIG_PRESSURE]
            if unit == "psi":
                return _Way("psi", lambda k: k * 0.1450377, lambda p: p * 6.89475729, lambda d: 2 if d else 1)
            if unit == "bar":
                return _Way("bar", lambda k: k * 0.01, lambda b: b * 100, lambda _d: 2)
            if unit == "MPa":
                return _Way("MPa", lambda k: k * 0.001, lambda m: m * 1000, lambda _d: 2)
        return _SAME

    @staticmethod
    def _type_of(kind: str) -> str:
        return _ALIASES.get(kind, kind)

    def label(self, kind: str) -> str:
        """The unit to show for a ValueType or base unit. Anything else shows as it is."""
        way = self._way(self._type_of(kind))
        if way:
            return way.label
        return _BASE_UNITS.get(kind, kind)

    def to_display(self, value: float, kind: str) -> float:
        way = self._way(self._type_of(kind))
        return way.to_display(value) if way else value

    def to_base(self, value: float, kind: str) -> float:
        way = self._way(self._type_of(kind))
        return way.to_base(value) if way else value

    def decimals(self, decimals: int, kind: str) -> int:
        way = self._way(self._type_of(kind))
        return way.decimals(decimals) if way and way.decimals else decimals

    def span_to_display(self, value: float, kind: str) -> float:
        """For a difference between values: no offset applies."""
        return self.to_display(value, kind) - self.to_display(0.0, kind)
