"""PC logging: records live values to CSV, and reads such logs back."""
from __future__ import annotations

import csv
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO


class PcLogger:
    def __init__(self, path: Path, channels: list[tuple[int, str]]) -> None:
        self.channels = channels
        self.path = Path(path)
        self._file: TextIO = open(self.path, "w", newline="", encoding="utf-8")
        self._writer = csv.writer(self._file)
        self._writer.writerow(["Time (s)", *(name for _, name in channels)])
        self._start = time.monotonic()
        self.samples = 0

    def sample(self, read) -> None:
        """`read(data_id)` returns the current value."""
        elapsed = time.monotonic() - self._start
        self._writer.writerow([f"{elapsed:.3f}", *(f"{read(data_id):.6g}" for data_id, _ in self.channels)])
        self.samples += 1

    def close(self) -> None:
        self._file.close()


@dataclass
class Log:
    names: list[str]
    time: list[float]
    channels: list[list[float]]


def read_log(path: Path) -> Log:
    with open(path, newline="", encoding="utf-8") as stream:
        rows = csv.reader(stream)
        header = next(rows)
        names = header[1:]
        times: list[float] = []
        channels: list[list[float]] = [[] for _ in names]
        for row in rows:
            if len(row) != len(header):
                continue  # A log cut off by a crash can end mid-line.
            times.append(float(row[0]))
            for channel, cell in zip(channels, row[1:]):
                channel.append(float(cell))
    return Log(names, times, channels)
