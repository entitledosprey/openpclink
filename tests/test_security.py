"""Passwords, ECU Unlock, the locked menu, files locked to a serial, loading a map and Compare, on the simulator."""
import os
import struct

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from openpclink import crypto  # noqa: E402
from openpclink import protocol as p  # noqa: E402
from openpclink.ecu import Ecu, EcuProtected, WriteRefused  # noqa: E402
from openpclink.mapfile import MapEntry, MapFile  # noqa: E402
from openpclink.sim import PtyServer, SimEcu  # noqa: E402
from openpclink.transports import SerialTransport  # noqa: E402

KEY = tuple(range(1, 5))


@pytest.fixture
def traced():
    sim = SimEcu()
    frames: list[tuple[int, bytes]] = []
    original = sim.handle

    def handle(frame):
        if len(frame) >= 8:
            frames.append((struct.unpack(">H", frame[2:4])[0], frame[6:-2]))
        return original(frame)

    sim.handle = handle
    server = PtyServer(sim)
    ecu = Ecu(SerialTransport(server.path))
    ecu.connect()
    frames.clear()
    yield ecu, sim, frames
    ecu.close()
    server.close()


def commands(frames):
    return [cmd for cmd, _ in frames if cmd not in (p.CMD_POLL_RUNTIME, p.CMD_READ_SECTION2, p.CMD_READ_CONFIG, p.CMD_MEMORY_INFO)]


def written_offsets(frames):
    return [struct.unpack(">H", body[:2])[0] for cmd, body in frames if cmd == p.CMD_WRITE_CONFIG]


def test_setup_password_clears_the_user_password_stores_then_sets_the_master_and_stores(traced):
    ecu, sim, frames = traced
    user, master = ecu.location(2295), ecu.location(2294)
    ecu.set_password("abc")
    config = ecu._need_info().runtime_size
    offsets = written_offsets(frames)
    assert offsets == [user.offset - config + 2 * i for i in range(20)] + [master.offset - config + 2 * i for i in range(20)]
    sequence = commands(frames)
    assert sequence[:20] == [p.CMD_WRITE_CONFIG] * 20 and sequence[20] == p.CMD_STORE
    assert sequence[21:41] == [p.CMD_WRITE_CONFIG] * 20 and sequence[41] == p.CMD_STORE
    assert ecu.is_protected
    assert [int(ecu._read_raw_number(master, i)) for i in range(3)] == [0x61, 0x62, 0x63]


def test_permanent_unlock_writes_the_user_password_then_stores_and_temporary_writes_nothing(traced):
    ecu, sim, frames = traced
    ecu.set_password("abc")
    frames.clear()
    assert not ecu.unlock("xyz", temporary=True)
    assert frames == []
    assert ecu.unlock("abc", temporary=True)
    assert frames == [] and not ecu.is_protected
    fresh = Ecu(ecu.transport)
    fresh.connect()
    assert fresh.is_protected
    frames.clear()
    assert fresh.unlock("abc", temporary=False)
    assert commands(frames) == [p.CMD_WRITE_CONFIG] * 20 + [p.CMD_STORE]
    assert not fresh.is_protected


def test_a_password_that_is_too_short_or_not_plain_is_refused(traced):
    ecu, sim, frames = traced
    for bad in ("ab", "x" * 21, "café"):
        with pytest.raises(WriteRefused):
            ecu.set_password(bad)
    assert frames == []


def test_ecu_unlock_sends_the_four_words_swapped_and_leaves_the_decision_to_the_ecu(traced):
    ecu, sim, frames = traced
    ecu.feature_unlock("0123456789abcdef")
    assert sim.feature_codes[-1] == bytes.fromhex("4567 0123 cdef 89ab".replace(" ", ""))
    for code, message in (("12", "too short"), ("0123456789abcdef0", "too long"), ("0123456789abcdez", "correct format")):
        with pytest.raises(WriteRefused, match=message):
            ecu.feature_unlock(code)
    assert len(sim.feature_codes) == 1


def test_load_map_is_bracketed_by_bulk_mode_ascending_and_written_twice(traced):
    ecu, sim, frames = traced
    sim.engine_running = False
    ids = [d for d, loc in sorted(ecu.table.items()) if loc.offset >= ecu._need_info().runtime_size and not loc.is_string and not loc.is_bitfield and loc.count == 1 and d not in (2294, 2295, 2303, 2324)][:4]
    entries = [MapEntry(d, ecu.table[d].kind, 1, ecu.table[d].scale, ecu.table[d].add, False, False, [ecu.get(d) + 1 * (ecu.table[d].scale or 1)]) for d in reversed(ids)]
    ecu.load_map(entries)
    sequence = commands(frames)
    assert sequence[0] == p.CMD_BULK_DOWNLOAD and sequence[-1] == p.CMD_BULK_DOWNLOAD
    quiet = [body for cmd, body in frames if cmd == p.CMD_BULK_DOWNLOAD]
    assert quiet == [b"\x01", b"\x00"]
    offsets = written_offsets(frames)
    assert len(offsets) == 2 * len(ids)
    half = len(offsets) // 2
    assert offsets[:half] == sorted(offsets[:half]) and offsets[:half] == offsets[half:]
    assert p.CMD_STORE not in sequence


def test_a_locked_ecu_refuses_a_map_load_and_changes_nothing(traced):
    ecu, sim, frames = traced
    ecu.set_password("abc")
    frames.clear()
    with pytest.raises(EcuProtected):
        ecu.load_map([MapEntry(300, 1, 1, 1.0, 0.0, False, False, [1.0])])
    assert frames == []


def test_memo_is_sent_when_set_and_store_sends_only_store(traced):
    ecu, sim, frames = traced
    ecu.set_memo("Hello")
    assert [cmd for cmd, _ in frames] == [p.CMD_WRITE_MEMO, p.CMD_WRITE_MEMO]
    assert frames[0][1] == b"\x00\x00" + "Hello".encode("utf-16-le")
    assert frames[1][1] == b"\x00\x01"
    frames.clear()
    ecu.store()
    assert commands(frames)[0] == p.CMD_STORE and p.CMD_WRITE_MEMO not in commands(frames)


def test_file_lock_is_marked_for_the_capture_and_cleared_after(traced):
    ecu, sim, frames = traced
    loc = ecu.location(2303)
    ecu.set_file_lock(True)
    assert ecu.memory[loc.offset] & loc.mask
    ecu.set_file_lock(False)
    assert not ecu.memory[loc.offset] & loc.mask
    assert [cmd for cmd, _ in frames] == [p.CMD_WRITE_CONFIG] * 2


def test_read_only_mode_does_not_send_the_file_lock_mark(traced, monkeypatch):
    ecu, sim, frames = traced
    monkeypatch.setenv(p.READ_ONLY_ENV, "1")
    ecu.set_file_lock(True)
    assert frames == []


def test_encrypted_file_keeps_the_master_password_and_marks_2303_but_not_the_user_password(traced):
    from openpclink.database import Database

    ecu, sim, frames = traced
    ecu.set_password("abc")
    ecu.unlock("abc", temporary=True)
    captured = MapFile.from_ecu(ecu, _NoDatabase(), keep_lock=True)
    assert captured.entries[2294].values[:3] == [0x61, 0x62, 0x63]
    assert captured.entries[2295].values == [0.0] * 20
    assert captured.entries[2303].values == [1.0]
    plain = MapFile.from_ecu(ecu, _NoDatabase())
    assert plain.entries[2294].values == [0.0] * 20 and plain.entries[2303].values == [0.0]


def test_a_file_locked_to_a_serial_opens_only_with_that_serial_and_shows_nothing_otherwise(tmp_path, traced):
    ecu, sim, frames = traced
    captured = MapFile.from_ecu(ecu, _NoDatabase(), keep_lock=True)
    path = tmp_path / "locked.pclx"
    captured.save(path, KEY, locked_to=424242)
    assert MapFile.load(path, KEY, 424242).entries
    for other in (None, 1, ecu.info.serial):
        with pytest.raises(crypto.ProtectedFile):
            MapFile.load(path, KEY, other)


def test_locked_copy_marks_the_copy_and_leaves_the_map_alone(traced):
    ecu, sim, frames = traced
    original = MapFile.from_ecu(ecu, _NoDatabase())
    copy = original.locked_copy()
    assert copy.entries[2303].values == [1.0] and original.entries[2303].values == [0.0]


class _NoDatabase:
    def known(self, data_id):
        return False


# ---- the window --------------------------------------------------------------

def _window(tmp_path_factory):
    from PySide6 import QtWidgets

    from openpclink.database import Database
    from openpclink.importer import import_pclink
    from tests.conftest import PROGRAM

    if not (PROGRAM / "ECU.cfgr").exists():
        pytest.skip("needs an extracted PCLink in work/")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return app, Database.load(import_pclink(PROGRAM, tmp_path_factory.mktemp("db") / "db.json.gz"))


@pytest.fixture(scope="module")
def gui(tmp_path_factory):
    return _window(tmp_path_factory)


def _menu_texts(window, name):
    menu = next(a.menu() for a in window.menuBar().actions() if a.text() == name)
    return [a.text() for a in menu.actions() if a.isVisible() and not a.isSeparator()]


def _visible_menus(window):
    return [a.text() for a in window.menuBar().actions() if a.isVisible()]


def test_locked_ecu_keeps_only_the_menu_items_pclink_leaves(gui):
    from openpclink.gui import MainWindow

    app, database = gui
    window = MainWindow(database)
    full = {name: _menu_texts(window, name) for name in ("File", "Options", "ECU Controls", "Help")}
    window.protected = True
    window._apply_lock_state()
    assert _visible_menus(window) == ["File", "Options", "ECU Controls", "Help"]
    assert _menu_texts(window, "File") == ["Open", "Save", "Save as…", "Save as Encrypted", "Exit"]
    assert _menu_texts(window, "Options") == ["Connection…", "Preferences…", "Language", "Setup ECU Parameters"]
    locked = _menu_texts(window, "ECU Controls")
    assert locked == [
        "Connect", "ECU Log File Download", "Store", "Restore to Factory Settings",
        "Setup Password Protection", "Unlock Password Protection", "TPS Setup", "MAP Sensor Calibration",
    ]
    assert _menu_texts(window, "Help") == full["Help"]
    window.protected = False
    window._apply_lock_state()
    assert {name: _menu_texts(window, name) for name in full} == full
    assert len(_visible_menus(window)) == 9
    window.close()


def _connected(gui, tmp_path_factory=None):
    from PySide6 import QtWidgets

    from openpclink.gui import MainWindow
    from tests.test_gui import pump

    app, database = gui
    sim = SimEcu()
    sim.engine_running = False
    server = PtyServer(sim)
    window = MainWindow(database)
    window.errors = []
    window.show_error = window.errors.append
    window.connect_to(f"serial:{server.path}")
    assert pump(app, lambda: window.status_label.text() == "ONLINE")
    return app, window, sim, server


def test_compare_shows_the_files_value_beside_the_live_one_until_closed(gui, tmp_path, monkeypatch):
    from PySide6 import QtWidgets

    from tests.test_gui import dispose

    app, window, sim, server = _connected(gui)
    ecu = window.worker.ecu
    other = MapFile.from_ecu(ecu, window.database)
    data_id = next(d for d, e in other.entries.items() if not e.read_only and len(e.values) == 1 and d not in (2294, 2295, 2303, 2324) and window.database.known(d) and not window.database.parameter(d).options and not ecu.table[d].is_bitfield and not ecu.table[d].is_string)
    other.entries[data_id].values = [other.entries[data_id].values[0] + 5]
    path = tmp_path / "other.pclx"
    other.save(path, window.database.map_key)
    monkeypatch.setattr(QtWidgets.QFileDialog, "getOpenFileName", lambda *a, **k: (str(path), ""))
    window.compare_map()
    assert window.compare_action_close.isEnabled()
    parameter = window.database.parameter(data_id)
    spin = QtWidgets.QDoubleSpinBox()
    field = window.compare_field(parameter, spin)
    labels = field.findChildren(QtWidgets.QLabel)
    assert field is not spin and labels[0].text() == window._compare_text(parameter, other.entries[data_id]) != ""
    assert window.compare_field(window.database.parameter(2294), spin) is spin
    window.compare_close()
    assert not window.compare_action_close.isEnabled()
    assert window.compare_field(parameter, spin) is spin
    dispose(app, window)
    server.close()


def test_a_file_locked_to_another_serial_is_refused_with_pclinks_words_and_nothing_is_written(gui, tmp_path, monkeypatch):
    from PySide6 import QtWidgets

    from tests.test_gui import dispose

    app, window, sim, server = _connected(gui)
    other = MapFile.from_ecu(window.worker.ecu, window.database)
    path = tmp_path / "foreign.pclx"
    other.save(path, window.database.map_key, locked_to=window.worker.ecu.info.serial + 1)
    shown = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "information", lambda parent, title, text, *a: shown.append(text))
    before = len(sim.writes)
    window.download_map(path)
    assert shown and shown[0].startswith("Unable to open file named: foreign.pclx") and "locked to a different ECU serial number" in shown[0]
    assert len(sim.writes) == before and window.worker.ecu.info.serial != 0
    dispose(app, window)
    server.close()
