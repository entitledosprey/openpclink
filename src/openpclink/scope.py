"""The trigger scope: a short high-speed capture the ECU makes on request."""
from __future__ import annotations

import struct
import time

from . import protocol as p
from .logfile import KIND_SECTION, Log, Marker, Series

SAMPLES = 7000
START, STOP = 0x0A, 0x06
READ_TRIG1, READ_TRIG2, READ_ANALOG, READ_DIGITAL, READ_STATE = 1, 2, 3, 4, 5
ENGINE_SPEED_ID = 1000
TRIG_1_TYPE_ID, TRIG_2_TYPE_ID = 3531, 3534
TRIG_1_ID, TRIG_2_ID, AN_VOLT_1_ID, STATE_ID = 3, 4, 1334, 1385
DIGITAL_IDS = (1159, 1160, 2541, 2542, 2543, 2544, 3430, 3422)
DIGITAL_NAMES = ("Trig1 Signal", "Trig2 Signal", "DI 1", "DI 2", "DI 3", "DI 4", "aux ign 1", "aux inj 1")
TRIGGER_SCALE = 0.028569647606382975
ANALOG_SCALE = 0.001220703125
TABS = {
    "Triggers": (TRIG_1_ID, TRIG_2_ID),
    "DI": (1159, 1160, 2541, 2542, 2543, 2544),
    "Trigger 1 State": (TRIG_1_ID, TRIG_2_ID, AN_VOLT_1_ID, STATE_ID, 3430, 3422),
}
RELUCTOR = 0


def sample_rate(rpm: float) -> int:
    """Five samples per crank degree, so a capture covers the same angle at any speed."""
    return min(round(max(rpm, 100) / 60 * 360) * 5, 60000)


def decode_analog(raw: int, channel: int) -> float:
    if channel == READ_ANALOG:
        value, scale = raw, ANALOG_SCALE
    else:
        value, scale = raw - 256 if raw > 127 else raw, TRIGGER_SCALE
    code = value * 8
    if code >= 0x2AC:
        code = value * 32 - 0x801
    elif code < -0x2AB:
        code = value * 32 + 0x801
    return code * scale


def capture(ecu, sleep=time.sleep) -> Log:
    """Run a capture and return it as a log, which is also how PCLink saves one."""
    rate = sample_rate(ecu.get(ENGINE_SPEED_ID))
    link = ecu.link

    def command(sub: int, a: int = 0, b: int = 0) -> bytes:
        return link.request(p.CMD_TRIGGER_SCOPE, struct.pack(">HHH", sub, a, b), 1.0)

    command(START, SAMPLES, rate)
    sleep(SAMPLES / rate)
    try:
        trig1 = [decode_analog(raw, READ_TRIG1) for raw in command(READ_TRIG1, SAMPLES)]
        trig2 = [decode_analog(raw, READ_TRIG2) for raw in command(READ_TRIG2, SAMPLES)]
        analog = [decode_analog(raw, READ_ANALOG) for raw in command(READ_ANALOG, SAMPLES)]
        digital = command(READ_DIGITAL, SAMPLES)
        state = [float(raw) for raw in command(READ_STATE, SAMPLES)]
    finally:
        command(STOP)
    period = 1.0 / rate
    log = Log()

    def add(data_id: int, name: str, units: str, values: list[float]) -> None:
        log.series.append(Series(data_id, name, units, period=period, times=[i * period for i in range(len(values))], values=values))

    def coupled(type_id: int, values: list[float]) -> tuple[str, list[float]]:
        if type_id in ecu.table and ecu.get(type_id) == RELUCTOR and values:
            mean = sum(values) / len(values)
            return "V AC", [value - mean for value in values]
        return "V DC", values

    units, values = coupled(TRIG_1_TYPE_ID, trig1)
    add(TRIG_1_ID, "Trigger Scope 1", units, values)
    units, values = coupled(TRIG_2_TYPE_ID, trig2)
    add(TRIG_2_ID, "Trigger Scope 2", units, values)
    add(AN_VOLT_1_ID, "AN Volt 1", "V", analog)
    add(STATE_ID, "Trigger 1 State", "", state)
    for bit, (data_id, name) in enumerate(zip(DIGITAL_IDS, DIGITAL_NAMES)):
        add(data_id, name, "", [float((raw >> bit) & 1) for raw in digital])
    log.details.update({"Name": "Trigger Scope", "Data Source": "Trigger Scope"})
    high = (SAMPLES - 1) * period
    log.markers = [Marker(KIND_SECTION, "SOL", 0.0), Marker(KIND_SECTION, "EOL", high)]
    log.rebuild()
    log.modified = True
    return log
