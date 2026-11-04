"""Update Firmware and Restore to Factory Settings, run against the simulator only.

Nothing here has touched a real ECU. The frame order and payloads were compared with real
PCLink 7.8.2 running under Wine against the same simulator (work/agents/firmware)."""
import os
import struct

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from openpclink import firmware, protocol as p  # noqa: E402
from openpclink.ecu import Ecu, WriteRefused  # noqa: E402
from openpclink.sim import PtyServer, SimEcu  # noqa: E402
from openpclink.transports import SerialTransport  # noqa: E402

KEY = bytes(range(1, 9))


def build_image(address=0x80000, version=(7, 8, 2), body=bytes(range(256)) * 12):
    masked = (int.from_bytes(body, "big") ^ int.from_bytes((KEY * (len(body) // 8 + 1))[: len(body)], "big")).to_bytes(len(body), "big")
    total = sum(struct.unpack(f">{len(masked) // 4}I", masked)) & 0xFFFFFFFF
    header = bytearray(68)
    struct.pack_into(">H", header, 4, 68)
    struct.pack_into(">H", header, 6, 1)
    header[8] = version[0]
    struct.pack_into(">HH", header, 9, version[1], version[2])
    struct.pack_into(">III", header, 20, address, len(body) // 4, (-total) & 0xFFFFFFFF)
    struct.pack_into(">H", header, 0, p.crc16(bytes(header[2:])))
    return bytes(header), body


def package_of(entries, images):
    return firmware.parse_package(package_bytes(entries, images), KEY)


def package_bytes(entries, images):
    """entries: {product: [image ids]}, images: {id: (device, version, address, body)}"""
    out = struct.pack("<7I", 1, 1, 9, 2, 0, len(entries), len(images))
    for product, ids in entries.items():
        out += struct.pack("<3I", 1, product, len(ids)) + struct.pack(f"<{len(ids)}I", *ids)
    for image_id, (device, version, address, body) in images.items():
        header, body = build_image(address, version, body)
        block = header + body
        out += struct.pack("<8I", 1, image_id, device, *version, 0, len(block)) + block
    return out


COMMS_BODY = bytes(range(255, -1, -1)) * 5
MAIN_BODY = bytes(range(256)) * 9


@pytest.fixture
def package():
    return package_of(
        {1012: [1, 2], 1001: [2]},
        {1: (155, (1, 6, 2), 0x08041000, COMMS_BODY), 2: (1007, (7, 8, 2), 0x80000, MAIN_BODY)},
    )


class Bench:
    def __init__(self, family=1012):
        self.sim = SimEcu(hardware_family=family)
        self.sim.engine_running = False
        self.sim.comms_firmware = (1, 5, 0)
        self.sim.firmware = (7, 8, 1)
        self.server = PtyServer(self.sim)
        self.transport = SerialTransport(self.server.path)
        self.link = p.Link(self.transport)
        self.reopened = []

    def reopen(self, wait):
        self.reopened.append(wait)

    def sequence(self):
        return [(addr, cmd) for addr, cmd, _payload in self.sim.frames]

    def close(self):
        self.transport.close()
        self.server.close()


@pytest.fixture
def bench():
    b = Bench()
    yield b
    b.close()


def collapse(sequence):
    out = []
    for item in sequence:
        if item[1] == p.CMD_FW_DATA and out and out[-1][1] == p.CMD_FW_DATA and out[-1][0] == item[0]:
            continue
        out.append(item)
    return out


def test_main_image_frame_order_matches_real_pclink(bench, package):
    image = package.images[2]
    state = firmware.program_image(bench.link, image, bench.reopen, sleep=lambda _s: None)
    assert state == firmware.STATE_RUNNING
    assert collapse(bench.sequence()) == [
        (0x5B, 3), (0x5B, 3), (0x5B, 3), (0x5B, 0x22), (0x5B, 0x11),
        (0x5B, 3), (0x5B, 3), (0x5B, 3), (0x5B, 3),
        (0x5B, 0x10), (0x5B, 0x15), (0x5B, 0x14), (0x5B, 0x12), (0x5B, 3),
    ]
    assert bench.reopened == []
    assert bench.link.addr == p.ADDR_MAIN


def test_comms_image_is_sent_to_the_comms_address_and_reopens_the_port(bench, package):
    image = package.images[1]
    firmware.program_image(bench.link, image, bench.reopen, sleep=lambda _s: None)
    assert collapse(bench.sequence()) == [
        (0x7D, 3), (0x7D, 3), (0x7D, 3), (0x7D, 0x22), (0x7D, 3), (0x7D, 0x11),
        (0x7D, 3), (0x7D, 3), (0x7D, 3), (0x7D, 3),
        (0x7D, 0x10), (0x7D, 0x15), (0x7D, 0x14), (0x7D, 0x22), (0x7D, 0x22), (0x7D, 3), (0x7D, 3),
    ]
    assert bench.reopened == [firmware.ENTER_BOOT_REOPEN_WAIT_S, firmware.LEAVE_BOOT_COMMS_WAIT_S]
    assert bench.link.addr == p.ADDR_MAIN


def test_header_and_chunks_are_exact(bench, package):
    image = package.images[2]
    firmware.program_image(bench.link, image, bench.reopen, sleep=lambda _s: None)
    header = [payload for _a, cmd, payload in bench.sim.frames if cmd == p.CMD_FW_HEADER]
    assert header == [image.header]
    chunks = [payload for _a, cmd, payload in bench.sim.frames if cmd == p.CMD_FW_DATA]
    assert all(len(c) - 8 <= 1024 for c in chunks)
    assert b"".join(c[8:] for c in chunks) == image.body
    sent = 0
    for chunk in chunks:
        address, words = struct.unpack_from(">II", chunk)
        assert address == image.address + sent and words * 4 == len(chunk) - 8
        sent += len(chunk) - 8


def test_package_programs_comms_then_main_and_the_versions_are_checked(bench, package):
    images = package.images_for(1012)
    for image in images:
        firmware.program_image(bench.link, image, bench.reopen, sleep=lambda _s: None)
    target = firmware.verify_versions(bench.link, images)
    assert target.main == (7, 8, 2) and target.comms == (1, 6, 2)
    assert [a for a, cmd in bench.sequence() if cmd == p.CMD_FW_START] == [0x7D, 0x5B]


def test_a_version_that_does_not_change_is_reported(bench, package):
    bench.sim.flash_updates_version = False
    image = package.images[2]
    firmware.program_image(bench.link, image, bench.reopen, sleep=lambda _s: None)
    with pytest.raises(firmware.VersionMismatch, match="7.8.2 was programmed"):
        firmware.verify_versions(bench.link, [image])


def test_chunk_retry_only_on_error_two(bench, package):
    image = package.images[2]
    real = bench.link.request_dangerous
    seen = {"data": 0}

    def flaky(cmd, payload=b"", timeout=p.DEFAULT_TIMEOUT, expect_reply=True):
        if cmd == p.CMD_FW_DATA:
            seen["data"] += 1
            if seen["data"] <= 2:
                raise p.EcuError(cmd, 2)
        return real(cmd, payload, timeout, expect_reply)

    bench.link.request_dangerous = flaky
    firmware.program_image(bench.link, image, bench.reopen, sleep=lambda _s: None)
    assert seen["data"] == len(range(0, len(image.body), 1024)) + 2

    bench.link.request_dangerous = lambda cmd, *a, **k: (_ for _ in ()).throw(p.EcuError(cmd, 3)) if cmd == p.CMD_FW_DATA else real(cmd, *a, **k)
    with pytest.raises(p.EcuError):
        firmware.program_image(bench.link, image, bench.reopen, sleep=lambda _s: None)
    assert bench.link.addr == p.ADDR_MAIN


def test_first_chunk_waits_longest_then_1500_ms(bench, package):
    timeouts = []
    real = bench.link.request_dangerous

    def spy(cmd, payload=b"", timeout=p.DEFAULT_TIMEOUT, expect_reply=True):
        timeouts.append((cmd, timeout, expect_reply))
        return real(cmd, payload, timeout, expect_reply)

    bench.link.request_dangerous = spy
    firmware.program_image(bench.link, package.images[2], bench.reopen, sleep=lambda _s: None)
    data = [t for cmd, t, _r in timeouts if cmd == p.CMD_FW_DATA]
    assert data[0] == 30.0 and set(data[1:]) == {1.5}
    assert (p.CMD_FW_HEADER, 1.0, True) in timeouts
    assert (p.CMD_FW_REQUEST_MAIN, p.DEFAULT_TIMEOUT, False) in timeouts


def test_engine_running_stops_before_anything_is_programmed(bench, package):
    bench.sim.engine_running = True
    with pytest.raises(firmware.EngineRunning):
        firmware.program_image(bench.link, package.images[2], bench.reopen, sleep=lambda _s: None)
    assert "start" not in bench.sim.firmware_stage and bench.link.addr == p.ADDR_MAIN


def test_failed_leave_boot_names_the_mode(bench, package):
    bench.sim._flashed_version = lambda comms: None
    real = bench.sim._firmware_step

    def stuck(cmd, payload):
        reply = real(cmd, payload)
        if cmd == p.CMD_FW_REQUEST_MAIN:
            bench.sim.state = 4
        return reply

    bench.sim._firmware_step = stuck
    with pytest.raises(firmware.FirmwareError, match="ECU reported Mode: Trap mode"):
        firmware.program_image(bench.link, package.images[2], bench.reopen, sleep=lambda _s: None)


def test_firmware_commands_cannot_go_through_the_ordinary_request(bench):
    for cmd in (p.CMD_FW_START, p.CMD_FW_REQUEST_BOOT, p.CMD_FW_REQUEST_MAIN, p.CMD_FW_DATA, p.CMD_FW_HEADER, p.CMD_FW_RESET, p.CMD_FACTORY_RESET):
        with pytest.raises(p.ForbiddenCommand):
            bench.link.request(cmd)


def test_read_only_mode_blocks_programming(bench, package, monkeypatch):
    monkeypatch.setenv(p.READ_ONLY_ENV, "1")
    with pytest.raises(p.ForbiddenCommand):
        firmware.program_image(bench.link, package.images[2], bench.reopen, sleep=lambda _s: None)
    assert "start" not in bench.sim.firmware_stage


# ---- reading the ECU without the normal connect ----------------------------------------


def no_ask():
    raise AssertionError("asked")


def test_target_in_normal_mode_reads_main_then_comms_in_order(bench):
    state, comms_boot = firmware.check_state(bench.link, lambda _t: None, no_ask, no_ask)
    target = firmware.read_target(bench.link, state, comms_boot)
    assert (state, comms_boot) == (1, False)
    assert target.product == 1012 and target.main == (7, 8, 1) and target.comms == (1, 5, 0)
    assert bench.sequence() == [(0x5B, 3), (0x5B, 0x16), (0x5B, 0x18), (0x7D, 0x16), (0x7D, 0x18)]
    assert bench.link.addr == p.ADDR_MAIN


@pytest.mark.parametrize("state, text", [(1, "NORMAL MODE"), (2, "SAFE MODE"), (3, "BOOT MODE")])
def test_state_messages(bench, state, text):
    bench.sim.state = state
    lines = []
    assert firmware.check_state(bench.link, lines.append, no_ask, no_ask) == (state, False)
    assert text in lines[0]


def test_boot_mode_target_is_read_when_the_ecu_answers(bench):
    bench.sim.state = 3
    state, comms_boot = firmware.check_state(bench.link, lambda _t: None, no_ask, no_ask)
    target = firmware.read_target(bench.link, state, comms_boot)
    assert target.in_boot and target.product == 1012


def test_boot_mode_without_info_has_no_product_so_only_an_image_file_applies(bench, package):
    bench.sim.state = bench.sim.comms_state = 3
    bench.sim.boot_info_answers = False
    state, comms_boot = firmware.check_state(bench.link, lambda _t: None, no_ask, no_ask)
    target = firmware.read_target(bench.link, state, comms_boot)
    assert target.product == 0 and target.main is None and target.in_boot
    selection = firmware.describe(package, package.images_for(target.product), target, False)
    assert not selection.enabled and selection.message == firmware.NO_IMAGES_TEXT
    header, body = build_image(0x80000, (7, 8, 2))
    image = firmware.FirmwareImage(0, 0, (7, 8, 2), 0x80000, header, body)
    bare = firmware.FirmwarePackage((7, 8, 2), images={0: image})
    selection = firmware.describe(bare, [image], target, True)
    assert selection.enabled and selection.button == "Upgrade"


def test_trap_mode_asks_and_no_stops(bench):
    bench.sim.state = 4
    with pytest.raises(firmware.UpdateCancelled):
        firmware.check_state(bench.link, lambda _t: None, lambda: False, no_ask)
    assert firmware.check_state(bench.link, lambda _t: None, lambda: True, no_ask) == (4, False)


def test_unreadable_state_tries_comms_then_retry_or_cancel(bench):
    bench.sim.state = 9
    bench.sim.comms_state = 3
    assert firmware.check_state(bench.link, lambda _t: None, no_ask, no_ask) == (9, True)
    bench.sim.comms_state = 1
    asked = []

    def retry():
        asked.append(1)
        return len(asked) < 2

    with pytest.raises(firmware.UpdateCancelled, match="Failed to connect to hardware"):
        firmware.check_state(bench.link, lambda _t: None, no_ask, retry)
    assert len(asked) == 2


# ---- what the dialog says ------------------------------------------------------------------


def target(main=(7, 8, 1), comms=(1, 5, 0), product=1012, state=1):
    return firmware.EcuTarget(product, main, comms, state)


def test_package_notes_and_buttons(package):
    selection = firmware.describe(package, package.images_for(1012), target(), False)
    assert (selection.button, selection.colour, selection.enabled) == ("Upgrade", "green", True)
    assert selection.message == "Firmware Package Version: 1.9.2.\nPackage Comms Firmware Version: 1.6.2.\nPackage ECU Firmware Version: 7.8.2."
    same = firmware.describe(package, package.images_for(1012), target((7, 8, 2), (1, 6, 2)), False)
    assert (same.button, same.colour) == ("Reinstall", "grey")
    older = firmware.describe(package, package.images_for(1012), target((7, 9, 0), (1, 6, 2)), False)
    assert (older.button, older.colour) == ("Downgrade", "red")
    wrong = firmware.describe(package, package.images_for(1012), target((6, 31, 2)), False)
    assert not wrong.enabled and wrong.colour == "grey"
    none = firmware.describe(package, package.images_for(999), target(), False)
    assert none.message == firmware.NO_IMAGES_TEXT and not none.enabled


def test_old_ecu_and_missing_comms_are_refused(package):
    assert firmware.describe(package, package.images_for(1012), target((4, 9, 9)), False).message == firmware.TOO_OLD_TEXT
    comms_only = [package.images[1]]
    bare = firmware.describe(package, comms_only, target(product=1001, comms=None), True)
    assert bare.message == firmware.NO_COMMS_TEXT and not bare.enabled


def test_base_timing_warning(package):
    header, body = build_image(0x80000, (6, 31, 2))
    image = firmware.FirmwareImage(0, 0, (6, 31, 2), 0x80000, header, body)
    bare = firmware.FirmwarePackage((6, 31, 2), images={0: image})
    old = firmware.describe(bare, [image], target((6, 19, 0), product=1001), True)
    assert old.base_timing and firmware.BASE_TIMING_TEXT in old.text
    both_new = firmware.describe(bare, [image], target((6, 25, 0), product=1001), True)
    assert not both_new.base_timing
    assert firmware.describe(package, package.images_for(1001), target((6, 10, 0), product=1001), False).base_timing is False  # incompatible
    wrongfile = firmware.describe(bare, [image], target((7, 8, 1), product=1012), True)
    assert "G5 ECU" in wrongfile.message and not wrongfile.enabled


# ---- the window ----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def app():
    from PySide6 import QtWidgets

    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_wifi_is_refused_and_nothing_is_sent(app, monkeypatch):
    from unittest import mock

    from openpclink import gui

    window = mock.MagicMock()
    window.worker.ecu = None
    window._uri = "udp:192.168.4.1"
    dialog = mock.MagicMock()
    dialog.exec.return_value = True
    dialog.uri.return_value = "udp:192.168.4.1"
    monkeypatch.setattr(gui, "ConnectDialog", lambda *_a: dialog)
    gui.MainWindow.update_firmware(window)
    window.show_error.assert_called_once_with("Can't update Firmware over Wifi")
    window.worker.post.assert_not_called()


# ---- factory settings ----------------------------------------------------------------------


@pytest.fixture
def live():
    sim = SimEcu(hardware_family=1012)
    sim.engine_running = False
    server = PtyServer(sim)
    ecu = Ecu(SerialTransport(server.path))
    ecu.connect()
    ecu.poll_runtime()
    yield sim, ecu
    ecu.close()
    server.close()


def test_factory_order_matches_real_pclink(live):
    sim, ecu = live
    sim.frames.clear()
    waits = []
    ecu.restore_factory(sleep=waits.append)
    sequence = [(a, c) for a, c, _ in sim.frames if c != p.CMD_POLL_RUNTIME]
    resets = [i for i, item in enumerate(sequence) if item[1] == p.CMD_FACTORY_RESET]
    assert [sequence[i][0] for i in resets] == [0x5B, 0x7D]
    assert all(item[1] == p.CMD_READ_CONFIG for item in sequence[resets[0] + 1 : resets[1]])
    assert sequence[resets[1] + 1][1] == p.CMD_COMMS_READ_RECORD
    assert waits == [0.5, 0.5] and ecu.link.addr == p.ADDR_MAIN


def test_factory_comms_step_is_for_products_from_1012_only():
    for family, comms_resets in ((1012, 1), (1000, 0)):
        sim = SimEcu(hardware_family=family)
        sim.engine_running = False
        server = PtyServer(sim)
        ecu = Ecu(SerialTransport(server.path))
        try:
            ecu.connect()
            ecu.poll_runtime()
            sim.frames.clear()
            ecu.restore_factory(sleep=lambda _s: None)
            assert len([a for a, c, _ in sim.frames if c == p.CMD_FACTORY_RESET and a == 0x7D]) == comms_resets
        finally:
            ecu.close()
            server.close()


def test_factory_refused_while_the_engine_runs_sends_nothing(live):
    sim, ecu = live
    sim.engine_running = True
    sim._tick()
    sim.frames.clear()
    with pytest.raises(WriteRefused):
        ecu.restore_factory(sleep=lambda _s: None)
    assert p.CMD_FACTORY_RESET not in [c for _a, c, _ in sim.frames]


def test_factory_failure_retries_once_then_reports_and_restores_the_address(live):
    sim, ecu = live
    calls = []

    def failing(cmd, payload=b"", timeout=p.DEFAULT_TIMEOUT, expect_reply=True):
        calls.append((ecu.link.addr, cmd))
        raise p.EcuError(cmd, 3)

    ecu.link.request_dangerous = failing
    with pytest.raises(WriteRefused, match="Unable to Reload"):
        ecu.restore_factory(sleep=lambda _s: None)
    assert calls == [(0x5B, p.CMD_FACTORY_RESET)] * 2 and ecu.link.addr == p.ADDR_MAIN


def test_factory_engine_running_reply_is_not_retried(live):
    sim, ecu = live
    calls = []

    def failing(cmd, payload=b"", timeout=p.DEFAULT_TIMEOUT, expect_reply=True):
        calls.append(cmd)
        raise p.EcuError(cmd, 4)

    ecu.link.request_dangerous = failing
    with pytest.raises(WriteRefused, match="engine may be running"):
        ecu.restore_factory(sleep=lambda _s: None)
    assert len(calls) == 1


def test_comms_reset_failure_leaves_the_address_on_main(live):
    sim, ecu = live
    real = ecu.link.request_dangerous

    def comms_fails(cmd, payload=b"", timeout=p.DEFAULT_TIMEOUT, expect_reply=True):
        if ecu.link.addr == p.ADDR_COMMS:
            raise p.EcuError(cmd, 3)
        return real(cmd, payload, timeout, expect_reply)

    ecu.link.request_dangerous = comms_fails
    with pytest.raises(WriteRefused):
        ecu.restore_factory(sleep=lambda _s: None)
    assert ecu.link.addr == p.ADDR_MAIN


def test_factory_works_on_a_locked_ecu(live):
    sim, ecu = live
    ecu.restore_factory(sleep=lambda _s: None)
    assert sim.factory_resets >= 1


def test_a_bare_image_file_is_for_the_main_processor_only():
    header, body = build_image(0x08041000, (1, 6, 2))
    image = firmware.FirmwareImage(0, 0, (1, 6, 2), 0x08041000, header, body)
    bare = firmware.FirmwarePackage((1, 6, 2), images={0: image})
    selection = firmware.describe(bare, [image], target(), True)
    assert not selection.enabled and selection.message == firmware.NOT_MAIN_IMAGE_TEXT


def run_dialog(app, sim, tmp_path, entries_images, answers=(True, True), engine=False):
    import time
    from unittest import mock

    from PySide6 import QtWidgets

    from openpclink import tools

    entries, images = entries_images
    path = tmp_path / "test.lfp"
    path.write_bytes(package_bytes(entries, images))
    sim.engine_running = engine
    server = PtyServer(sim)
    window = QtWidgets.QWidget()
    window.database = mock.Mock(firmware_key=KEY)
    topics = []
    window.show_topic = topics.append
    shown = []
    yes = QtWidgets.QMessageBox.StandardButton.Yes
    no = QtWidgets.QMessageBox.StandardButton.No
    replies = iter(answers)
    with mock.patch.object(QtWidgets.QMessageBox, "warning", lambda *a, **k: (shown.append(a[2]), yes if next(replies, True) else no)[1]), \
            mock.patch.object(QtWidgets.QMessageBox, "information", lambda *a, **k: shown.append(a[2])), \
            mock.patch.object(QtWidgets.QMessageBox, "critical", lambda *a, **k: shown.append(a[2])), \
            mock.patch("openpclink.assets.asset", lambda _n: None):
        try:
            dialog = tools.FirmwareDialog(window, "serial:" + server.path)
            assert dialog.ready
            dialog._add_file(path)
            dialog.start.click()
            assert topics and not sim.firmware_stage
            dialog.start.click()
            deadline = time.time() + 30
            while (dialog.programming or not sim.firmware_stage and answers == (True, True)) and time.time() < deadline:
                app.processEvents()
                time.sleep(0.01)
            for _ in range(20):
                app.processEvents()
                time.sleep(0.01)
            return dialog, shown, topics
        finally:
            server.close()


IMAGES = ({1012: [1, 2]}, {1: (155, (1, 6, 2), 0x08041000, COMMS_BODY), 2: (1007, (7, 8, 2), 0x80000, MAIN_BODY)})


def test_dialog_programs_after_notes_and_both_confirmations(app, tmp_path):
    sim = SimEcu(hardware_family=1012)
    sim.comms_firmware = (1, 5, 0)
    dialog, shown, topics = run_dialog(app, sim, tmp_path, IMAGES)
    assert sim.firmware == (7, 8, 2) and sim.comms_firmware == (1, 6, 2)
    assert shown[0] == "Have you read the firmware version notes?" and "PROGRAMMING COMPLETE" in shown[-1]
    assert "Upgrading Comms Firmware to : 1.6.2." in dialog.memo.toPlainText()
    assert [stage for stage in sim.firmware_stage if stage == "start"] == ["start", "start"]


def test_dialog_answering_no_sends_nothing_to_program(app, tmp_path):
    sim = SimEcu(hardware_family=1012)
    sim.comms_firmware = (1, 5, 0)
    dialog, shown, _topics = run_dialog(app, sim, tmp_path, IMAGES, answers=(False,))
    assert sim.firmware_stage == [] and sim.firmware == (7, 8, 1)
    assert "User cancelled update" in dialog.memo.toPlainText()


def test_dialog_reports_a_running_engine_and_programs_nothing(app, tmp_path):
    sim = SimEcu(hardware_family=1012)
    sim.comms_firmware = (1, 5, 0)
    dialog, shown, _topics = run_dialog(app, sim, tmp_path, IMAGES, engine=True)
    assert "start" not in sim.firmware_stage and sim.firmware == (7, 8, 1)
    assert "engine is running" in dialog.message.text()
