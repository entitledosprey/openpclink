"""Port detection and FTDI setup, with USB faked. This is not a hardware test."""
from types import SimpleNamespace
from unittest import mock

from openpclink import transports


def _serial(device, vid, pid):
    return SimpleNamespace(device=device, vid=vid, pid=pid, description="desc")


def _detect(serial_ports, usb_devices, **kwargs):
    with mock.patch("serial.tools.list_ports.comports", return_value=serial_ports), mock.patch(
        "usb.core.find", return_value=usb_devices
    ):
        return [port.uri for port in transports.list_ports(**kwargs)]


def test_detects_only_link_devices():
    ports = [
        _serial("/dev/cu.usbmodem1", 0x0483, 0xA451),
        _serial("/dev/cu.other", 0x0483, 0x5740),
        _serial("/dev/cu.Bluetooth", None, None),
    ]
    usb = [SimpleNamespace(idProduct=0x7068), SimpleNamespace(idProduct=0x706F), SimpleNamespace(idProduct=0x6001)]
    assert _detect(ports, usb) == ["serial:/dev/cu.usbmodem1", "ftdi:7068", "ftdi:706f"]


def test_show_all_adds_other_usb_serial_but_not_bluetooth():
    ports = [_serial("/dev/cu.other", 0x0483, 0x5740), _serial("/dev/cu.Bluetooth", None, None)]
    assert _detect(ports, [], all_serial=True) == ["serial:/dev/cu.other"]


def _open_ftdi(hi_speed):
    with mock.patch("pyftdi.ftdi.Ftdi") as ftdi_class:
        device = ftdi_class.return_value
        device.is_H_series = hi_speed
        transports.FtdiTransport(0x7068)
    return device


def test_ftdi_is_configured_as_pclink_configures_it():
    device = _open_ftdi(True)
    device.open.assert_called_once_with(transports.FTDI_VID, 0x7068, serial=None)
    device.set_baudrate.assert_called_once_with(4_700_000, constrain=False)
    device.set_line_property.assert_called_once_with(8, 1, "N")
    assert _open_ftdi(False).set_baudrate.call_args.args[0] == 1_171_875


def test_g4x_claimed_by_a_system_driver_is_offered_as_serial_at_both_rates():
    ports = [_serial("/dev/cu.usbserial-1", 0x0403, 0x7069)]
    assert _detect(ports, []) == ["serial:/dev/cu.usbserial-1@1171875", "serial:/dev/cu.usbserial-1@4700000"]


def test_udev_rule_sorts_before_seat_late_so_uaccess_works():
    from pathlib import Path

    rules = list((Path(__file__).parent.parent / "packaging").glob("*.rules"))
    assert [path.name for path in rules] == ["70-link-ecu.rules"]
    assert rules[0].name < "73-seat-late.rules"


def test_permission_error_explains_the_fix(monkeypatch):
    error = PermissionError(13, "Access denied (insufficient permissions)")
    monkeypatch.setattr(transports.sys, "platform", "linux")
    assert "70-link-ecu.rules" in transports._ftdi_open_message(error)
    monkeypatch.setattr(transports.sys, "platform", "darwin")
    assert "sudo" in transports._ftdi_open_message(error)
