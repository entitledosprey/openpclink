"""ECU Log File Download: reading by polling and streaming, decoding, and the dialog's flow with PCLink's prompts."""
import os
import struct
from pathlib import Path
from unittest import mock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_STYLE_OVERRIDE", "Fusion")
os.environ.setdefault("OPENPCLINK_OPTIONS", "/nonexistent-openpclink-test/options.json")

from PySide6 import QtWidgets  # noqa: E402

from openpclink import eculog, tools  # noqa: E402
from openpclink import protocol as p  # noqa: E402
from openpclink.ecu import Ecu  # noqa: E402
from openpclink.logfile import read_link_log  # noqa: E402
from openpclink.sim import PtyServer, SimEcu  # noqa: E402
from openpclink.transports import SerialTransport  # noqa: E402
from tests.test_gui import app, database, dispose, pump, session  # noqa: E402,F401

MAGIC = 0xA5A55A5A
YES, NO = QtWidgets.QMessageBox.StandardButton.Yes, QtWidgets.QMessageBox.StandardButton.No


def make_log(rows: int, marks: dict[int, int] | None = None, channels=((1000, 1), (1078, 2), (1081, 5))) -> bytes:
    marks = marks or {}
    head = bytearray(16)
    head[1], head[3] = 1, len(channels)
    struct.pack_into(">II", head, 4, 5, 0xFFFFFFFF)
    head += struct.pack(">II", 2026 << 16 | 11 << 8 | 3, 14 << 24 | 30 << 16 | 5 << 8)
    for data_id, divider in channels:
        head += struct.pack(">II", data_id, divider)
    head += bytes(len(head) & 3)
    body = bytearray()
    for row in range(rows):
        if row in marks:
            body += struct.pack(">IIIII", MAGIC, marks[row], 0, 0, row)
        for data_id, divider in channels:
            if row % divider == 0:
                body += struct.pack(">f", 1000.0 + row + data_id)
    return bytes(head + body)


@pytest.fixture
def live():
    sim = SimEcu()
    sim.log_tick_rate = 100
    server = PtyServer(sim)
    ecu = Ecu(SerialTransport(server.path))
    ecu.connect()
    yield sim, ecu
    ecu.close()
    server.close()


def test_streaming_and_polling_give_the_same_bytes(live):
    sim, ecu = live
    data = make_log(4000, {0: 1})
    sim.ecu_logs = [("LOG0001", data)]
    info, entries = ecu.logs.list_files()
    entry = entries[0]
    assert entry.size == len(data) > 512 * 4
    assert ecu.logs.stream_log_file("LOG0001", entry.size) == data
    assert ecu.logs.read_log_file("LOG0001", entry.size) == data
    assert ecu.logs.download(entry, "7.8.1") == data
    assert ecu.logs.download(entry, "6.12.0") == data
    streamed = [frame for frame in sim.frames if frame[1] == p.CMD_FILE and struct.unpack_from(">I", frame[2])[0] == p.FILE_STREAM]
    assert streamed and struct.unpack_from(">II", streamed[0][2], 6)[1] == 20_000_000
    assert not streamed[-1][2][14:].strip(b"\0")  # PCLink ends with a request for the empty name


def test_old_firmware_polls_instead_of_streaming(live):
    sim, ecu = live
    sim.ecu_logs = [("LOG0001", make_log(300))]
    _info, entries = ecu.logs.list_files()
    sim.frames.clear()
    ecu.logs.download(entries[0], "6.17.3")
    assert not [f for f in sim.frames if f[1] == p.CMD_FILE and struct.unpack_from(">I", f[2])[0] == p.FILE_STREAM]
    ecu.logs.download(entries[0], "6.18.0")
    assert [f for f in sim.frames if f[1] == p.CMD_FILE and struct.unpack_from(">I", f[2])[0] == p.FILE_STREAM]


def test_polled_read_follows_pclink(live):
    sim, ecu = live
    data = make_log(2000)
    sim.ecu_logs = [("LOG0001", data)]
    sim.frames.clear()
    got = ecu.logs.read_log_file("LOG0001", len(data))
    assert got == data
    reads = [f[2] for f in sim.frames if f[1] == p.CMD_FILE]
    offsets = [struct.unpack_from(">I", r, 6)[0] for r in reads]
    assert offsets[0] == 0 and offsets[1:-1] == sorted(offsets[1:-1]) and offsets[-1] == 0
    assert reads[0][4:6] == b"\x01\x00" and reads[0][14:] == b"LOG0001\0"
    assert reads[-1][14:] == b"\0"
    header = ecu.logs.read_log_file("LOG0001", 100, header_only=True)
    assert header == data[:512] or header == data[:100]
    assert ecu.logs.read_log_file("LOG0001", 0) == b""


def test_cancelling_stops_the_download(live):
    sim, ecu = live
    data = make_log(4000)
    sim.ecu_logs = [("LOG0001", data)]
    seen = []

    def stop(sent):
        seen.append(sent)
        return False

    assert 0 < len(ecu.logs.stream_log_file("LOG0001", len(data), stop)) < len(data)
    assert len(seen) == 1
    seen.clear()
    assert 0 < len(ecu.logs.read_log_file("LOG0001", len(data), stop)) < len(data)
    assert len(seen) == 1
    assert ecu.logs.info().file_count == 1  # the link is still in step


def test_streaming_is_allowed_in_read_only_mode(live, monkeypatch):
    sim, ecu = live
    sim.ecu_logs = [("LOG0001", make_log(100))]
    monkeypatch.setenv(p.READ_ONLY_ENV, "1")
    _info, entries = ecu.logs.list_files()
    assert ecu.logs.stream_log_file("LOG0001", entries[0].size)


def test_markers_label_and_section_closing():
    info = eculog.LogInfo(100, 2, MAGIC, 32)
    a = make_log(600, {0: 1, 100: 3, 200: 4, 300: 2, 400: 1})
    b = make_log(300, {0: 1, 50: 4})
    logs = eculog.build_logs([("a", a), ("b", b)], info, lambda i: (str(i), ""), "Key Press")
    assert len(logs) == 1
    marks = [(m.kind, m.name, round(m.time, 2)) for m in logs[0].markers]
    assert marks == [
        (0, "Record Start", 0.0), (0, "Record Resume", 1.0), (1, "Key Press", 2.0), (0, "Record Start", 4.0),
        (0, "EOL", 5.99), (0, "EOL", 5.99), (0, "Record Start", 6.0), (0, "EOL", 6.49), (1, "Key Press", 6.5), (0, "EOL", 8.99),
    ]
    assert "Data Source" not in logs[0].details or not logs[0].details["Data Source"]


def test_rates_and_sizes_are_shown_as_pclink_shows_them():
    header = eculog.parse_header(make_log(10))
    assert header.bytes_per_second() == 6800  # 4 x (1000 + 500 + 200), whatever the tick
    assert header.duration(13668) == pytest.approx(2.0, abs=0.01)
    assert tools.LogDownloadDialog.size_text(13668) == "13.6kB"
    assert tools.LogDownloadDialog.size_text(3468) == "3.4kB"
    assert tools.LogDownloadDialog.size_text(2_345_678) == "2.3MB"
    assert [tools.LogDownloadDialog.rate_text(d) for d in (1, 2, 5)] == ["1000Hz", "500Hz", "200Hz"]


def open_dialog(app, window, sim):
    sim.log_tick_rate = 100
    sim.ecu_logs = [("LOG0001", make_log(2000, {0: 1})), ("LOG0002", make_log(500, {0: 1}))]
    dialog = tools.LogDownloadDialog(window, window)
    window.report_progress.connect(dialog.progress.setValue)
    assert pump(app, lambda: dialog.entries)
    return dialog


def select(dialog, *rows):
    dialog.grid.clearSelection()
    for row in rows:
        dialog.grid.selectRow(row) if row == rows[0] else dialog.grid.item(row, 0).setSelected(True)


def test_dialog_lists_like_pclink(session):
    app, window, sim = session
    dialog = open_dialog(app, window, sim)
    assert [dialog.grid.horizontalHeaderItem(c).text() for c in range(4)] == ["Log File", "Size", "Duration", "Log Date"]
    assert [dialog.grid.item(0, c).text() for c in range(4)] == ["0", f"{len(sim.ecu_logs[0][1]) // 100 / 10:.1f}kB", "00:00:02", "2026/11/03 14:30:05"]
    assert sim.logging_paused
    select(dialog, 0)
    assert [dialog.details.item(0, c).text() for c in range(5)] == ["2026/11/03 14:30:05", "5", "0s", "3", "6.8kB/s"]
    assert [[dialog.parameters.item(r, c).text() for c in (0, 2)] for r in range(3)] == [["0", "1000Hz"], ["1", "500Hz"], ["2", "200Hz"]]
    assert not dialog.cancel_button.isEnabled() and dialog.download_button.isEnabled()
    dialog.reject()


def test_download_saves_then_offers_to_open(session, tmp_path):
    app, window, sim = session
    dialog = open_dialog(app, window, sim)
    window.options.remember("log", str(tmp_path / "x.llg5"))
    select(dialog, 0)
    before = window.display_stack.currentIndex()
    target = tmp_path / "ECU Log test.llg5"
    questions = []
    with mock.patch.object(QtWidgets.QFileDialog, "getSaveFileName", return_value=(str(target), "")) as chooser, \
            mock.patch.object(QtWidgets.QMessageBox, "question", side_effect=lambda *a, **k: questions.append(a[2]) or YES):
        dialog.download()
        assert not dialog.download_button.isEnabled() and dialog.cancel_button.isEnabled()
        assert pump(app, lambda: target.exists() and questions and window.log_session.log is not None)
    assert chooser.call_args[0][2].endswith(".llg5") and Path(chooser.call_args[0][2]).name.startswith("ECU Log ")
    assert questions[0].startswith("Open downloaded log file for viewing now?\r\n") and str(target) in questions[0]
    saved = read_link_log(target)
    assert saved.details["Name"].startswith("ECU Internal Datalog - ") and saved.details["Firmware Version"] == window.worker.ecu.info.firmware
    assert [s.data_id for s in saved.series] == [1000, 1078, 1081] and len(saved.series[0].values) == 2000
    assert any(m.name == "Record Start" for m in saved.markers)
    assert window.log_session.log is not None and window.log_session.log.path == target
    assert window.display_stack.currentIndex() == before
    assert dialog.message.text() == "Download log file completed."
    assert dialog.download_button.isEnabled() and not dialog.cancel_button.isEnabled()
    dialog.reject()


def test_answering_no_does_not_open_the_log(session, tmp_path):
    app, window, sim = session
    dialog = open_dialog(app, window, sim)
    select(dialog, 0)
    target = tmp_path / "a.llg5"
    with mock.patch.object(QtWidgets.QFileDialog, "getSaveFileName", return_value=(str(target), "")), \
            mock.patch.object(QtWidgets.QMessageBox, "question", return_value=NO):
        dialog.download()
        assert pump(app, lambda: target.exists())
    pump(app, lambda: False, 0.2)
    assert window.log_session.log is None
    dialog.reject()


def test_cancelling_the_save_dialog_keeps_nothing(session, tmp_path):
    app, window, sim = session
    dialog = open_dialog(app, window, sim)
    select(dialog, 0)
    with mock.patch.object(QtWidgets.QFileDialog, "getSaveFileName", return_value=("", "")) as chooser:
        dialog.download()
        assert pump(app, lambda: chooser.called)
    assert window.log_session.log is None
    dialog.reject()


def test_two_files_with_the_same_channels_are_saved_as_one_log(session, tmp_path):
    app, window, sim = session
    dialog = open_dialog(app, window, sim)
    select(dialog, 0, 1)
    target = tmp_path / "both.llg5"
    with mock.patch.object(QtWidgets.QFileDialog, "getSaveFileName", return_value=(str(target), "")) as chooser, \
            mock.patch.object(QtWidgets.QMessageBox, "question", return_value=NO):
        dialog.download()
        assert pump(app, lambda: target.exists())
    assert chooser.call_count == 1
    assert len(read_link_log(target).series[0].values) == 2500
    dialog.reject()


def press_cancel_during_download(dialog, keep: int):
    real = eculog.EcuLogs.download

    def download(self, entry, firmware, progress=None):
        data = real(self, entry, firmware)[:keep]
        dialog.cancel()
        assert progress(len(data)) is False
        return data

    return mock.patch.object(eculog.EcuLogs, "download", download)


def test_cancel_asks_about_the_partial_file(session):
    app, window, sim = session
    dialog = open_dialog(app, window, sim)
    select(dialog, 0)
    asked = []
    with press_cancel_during_download(dialog, 600), mock.patch.object(QtWidgets.QFileDialog, "getSaveFileName", return_value=("", "")) as chooser, \
            mock.patch.object(QtWidgets.QMessageBox, "question", side_effect=lambda *a, **k: asked.append(a[2]) or NO):
        dialog.download()
        assert pump(app, lambda: asked)
    assert asked == ["Do you want to continue with the partial log file?"]
    assert not chooser.called
    assert dialog.message.text() == "Download log file cancelled"
    assert dialog.download_button.isEnabled() and not dialog.cancel_button.isEnabled()
    dialog.reject()


def test_cancel_keeps_the_partial_when_asked(session, tmp_path):
    app, window, sim = session
    dialog = open_dialog(app, window, sim)
    select(dialog, 0)
    target = tmp_path / "partial.llg5"
    with press_cancel_during_download(dialog, 600), mock.patch.object(QtWidgets.QFileDialog, "getSaveFileName", return_value=(str(target), "")), \
            mock.patch.object(QtWidgets.QMessageBox, "question", return_value=YES):
        dialog.download()
        assert pump(app, lambda: target.exists())
    saved = read_link_log(target)
    assert 0 < len(saved.series[0].values) < 2000


def test_delete_and_delete_all_confirm_first(session):
    app, window, sim = session
    dialog = open_dialog(app, window, sim)
    select(dialog, 0)
    asked = []
    with mock.patch.object(QtWidgets.QMessageBox, "question", side_effect=lambda *a, **k: asked.append(a[2]) or NO):
        dialog.delete_button.click()
        dialog.delete_all_button.click()
    assert asked == ["Are you sure you want to delete this log file?", "Are you sure you want to delete all of the log files?"]
    assert len(sim.ecu_logs) == 2
    with mock.patch.object(QtWidgets.QMessageBox, "question", return_value=YES):
        dialog.delete_button.click()
        assert pump(app, lambda: dialog.grid.rowCount() == 1)
        assert [n for n, _ in sim.ecu_logs] == ["LOG0002"]
        dialog.delete_all_button.click()
        assert pump(app, lambda: dialog.grid.rowCount() == 0)
    assert dialog.message.text() == "No log files found in ECU" and not sim.ecu_logs
    dialog.reject()


def test_closing_mid_download_asks_first(session):
    app, window, sim = session
    dialog = open_dialog(app, window, sim)
    dialog.busy = True
    asked = []
    with mock.patch.object(QtWidgets.QMessageBox, "question", side_effect=lambda *a, **k: asked.append(a[2]) or NO):
        dialog.reject()
    assert asked == ["Download progress will be lost, do you want to close?"]
    dialog.busy = False
    dialog.reject()


def test_stream_frames_match_real_pclink(live):
    sim, ecu = live
    sim.ecu_logs = [("LOG0001", make_log(100))]
    _info, entries = ecu.logs.list_files()
    sim.frames.clear()
    ecu.logs.stream_log_file("LOG0001", entries[0].size)
    frames = [p.build_request(p.ADDR_MAIN, cmd, payload).hex() for _addr, cmd, payload in sim.frames]
    assert frames == [
        "5a5b002e00180000000301000000000001312d004c4f473030303100328b",  # captured from PCLink 7.8.2
        "5a5b002e00110000000301000000000001312d0000b265",
    ]
