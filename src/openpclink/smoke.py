"""A headless check of a built program: open the window, connect to a simulator, report what loaded.

    OpenPCLink --smoke serial:/dev/ttys004 [screenshot.png]

Run it with QT_QPA_PLATFORM=offscreen. It never writes to the ECU.
"""
from __future__ import annotations

import os
import sys
import time

from . import assets


def run(uri: str, picture: str | None = None, timeout: float = 30.0) -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets

    from .database import Database
    from .gui import MainWindow

    app = QtWidgets.QApplication(sys.argv[:1])
    database = Database.load()
    window = MainWindow(database)
    window.resize(1366, 768)
    window.show()
    failures: list[str] = []
    window.worker.failed.connect(failures.append)
    window.connect_to(uri)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and window.status_label.text() != "ONLINE":
        app.processEvents()
        time.sleep(0.01)
    for _ in range(50):
        app.processEvents()
        time.sleep(0.01)
    pages = [page.name for page in window.workspace.layout_.pages]
    ecu = window.worker.ecu
    print(f"Status:    {window.status_label.text()}")
    print(f"Layout:    {', '.join(pages)} ({'PCLink layout' if assets.layout_path('DefaultLayout') and len(pages) > 1 else 'built-in fallback'})")
    if ecu is not None and getattr(ecu, "info", None):
        print(f"ECU:       firmware {ecu.info.firmware}, {len(ecu.table)} parameters, {len(getattr(ecu, 'comms', {}))} comms settings")
    if picture:
        window.grab().save(picture)
    window.close()
    for failure in failures:
        print(f"Failed:    {failure}")
    return 0 if window.status_label.text() == "ONLINE" or ecu is not None else 1
