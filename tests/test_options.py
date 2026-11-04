"""Options: connection choices, directories, colour theme editing, unit conversion in logs."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6 import QtWidgets  # noqa: E402

from openpclink import logfile, theme as themes  # noqa: E402
from openpclink.logfile import Log, Series  # noqa: E402
from openpclink.options import Options  # noqa: E402
from openpclink.transports import PortInfo  # noqa: E402
from openpclink.units import UnitSystem  # noqa: E402

PORTS = [PortInfo("serial:/dev/ttyACM0", "G5"), PortInfo("ftdi:7068", "G4X")]


def test_auto_connects_to_first_port():
    assert Options().connection_uri(PORTS) == "serial:/dev/ttyACM0"
    assert Options().connection_uri([]) == ""


def test_manual_port_and_baud():
    options = Options(connection_manual=True, connection_port="/dev/ttyUSB1", baud_rate=57600)
    assert options.connection_uri(PORTS) == "serial:/dev/ttyUSB1@57600"
    assert Options(connection_manual=True, connection_port="Network").connection_uri(PORTS) == "udp:192.168.4.1"
    assert Options(connection_manual=True, connection_port="USB").connection_uri(PORTS) == "serial:/dev/ttyACM0"
    assert Options(connection_manual=True, connection_port="ftdi:7068").connection_uri(PORTS) == "ftdi:7068"


def test_directory_choice_and_last_used(tmp_path):
    options = Options(map_dir=str(tmp_path))
    assert options.folder("map") == str(tmp_path)
    options.use_map_dir = False
    options.remember("map", str(tmp_path / "sub" / "a.pclx"))
    assert options.folder("map") == str(tmp_path / "sub")
    options.save(tmp_path / "o.json")
    assert Options.load(tmp_path / "o.json").folder("map") == str(tmp_path / "sub")


def test_theme_list_matches_real_order():
    titles = [title for _key, title in themes.list_entries()]
    assert titles[:5] == ["Active Cell", "Active Control", "Cursors", "Data Area Background", "Data Area Border"]
    assert titles[-2:] == ["Soft Warning Value", "Hard Warning Value"]
    assert "Layout Background" in titles and "Background Edge" not in titles


def test_edited_theme_becomes_custom(tmp_path):
    from openpclink import tools

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    class Window:
        options = Options(theme="Blue")
        changed = 0

        def options_changed(self):
            self.changed += 1

    window = Window()
    dialog = tools.ColourThemeDialog(window)
    dialog._grow("parameter")
    assert dialog.theme.name == "Custom" and len(dialog.theme.parameters) == 13
    dialog._shrink("parameter", dialog.parameters)
    dialog.items.setCurrentRow(0)
    dialog._paint(0xFF0000)
    dialog._apply()
    assert window.options.theme == "Custom" and "Custom" in window.options.themes
    again = themes.theme_named(window.options)
    key = themes.list_entries()[0][0]
    assert again.items[key] == 0x0000FF and again.base == "Blue"
    assert [dialog.choice.itemText(i) for i in range(dialog.choice.count())] == ["Blue", "White", "Black", "Gray", "Custom"]
    del app


def test_log_values_follow_units():
    log = Log()
    log.series = [Series(1, "Coolant", unit_key="Temperature", times=[0.0, 1.0], values=[100.0, 100.0])]
    try:
        logfile.UNITS = UnitSystem({"temperature": "°F"})
        log.rebuild()
        assert log.channels[0][0] == pytest.approx(212.0) and log.unit_of(0) == "°F"
        assert log.series[0].values == [100.0, 100.0]
        logfile.UNITS = UnitSystem()
        log.rebuild()
        assert log.channels[0][0] == 100.0 and log.unit_of(0) == "°C"
    finally:
        logfile.UNITS = None


from tests.test_gui import app, database, session  # noqa: E402,F401


def test_parameter_properties_persist_apart_from_import(session, tmp_path, monkeypatch):
    from openpclink import assets, tools
    from openpclink.assets import ParameterDisplay, RuntimeConfig

    app, window, _sim = session
    saved = tmp_path / "DefaultParameterConfig.rtcr"
    monkeypatch.setattr(assets, "user_config_path", lambda: saved)
    import openpclink.paramspanel as paramspanel

    monkeypatch.setattr(paramspanel, "user_config_path", lambda: saved)
    data_id = next(i for i in window.runtime_config.entries)
    monkeypatch.setattr(tools.ParameterSetupDialog, "exec", lambda self: self.fields["maximum"].setText("123.0") or True)
    assert window.runtime_panel.properties(data_id)
    assert window.runtime_config.get(data_id).maximum == 123.0
    assert RuntimeConfig.load(saved).get(data_id).maximum == 123.0
    assert saved.exists() and isinstance(window.runtime_config.get(data_id), ParameterDisplay)


def test_fault_banner_and_event_log(session):
    from openpclink import faults

    app, window, sim = session
    assert faults.fault_text(window.database, 3) == "ECU Fault Code 3: Ethanol Sensor Fault"
    ecu = window.worker.ecu
    from openpclink.faults import fault_text

    window._show_fault(type("E", (), {"table": {1162: 0}, "get": staticmethod(lambda _i, *a: 3)}))
    assert window.workspace.banner.text() == "ECU Fault Code 3: Ethanol Sensor Fault"
    assert "ECU Fault Code 3: Ethanol Sensor Fault" in window.event_log.list.item(window.event_log.list.count() - 1).text()
    del ecu, fault_text


def test_ecu_information_lines(session, monkeypatch):
    from PySide6 import QtWidgets as W

    _app, window, _sim = session
    shown = []
    monkeypatch.setattr(W.QMessageBox, "information", lambda *a: shown.append(a[2]))
    window.ecu_information()
    lines = shown[0].split("\n")
    assert [line.split(":")[0] for line in lines] == [
        "MainCode Firmware Version", "BootCode Firmware Version", "Main Board Revision", "ECU Serial Number", "ECU Type", "LinkID", "ECUMinSpec", "ECU Enabled",
    ]


def test_units_menu_is_checkable(session):
    _app, window, _sim = session
    window.set_unit_system("Imperial")
    assert window.unit_actions["Imperial"].isChecked() and not window.unit_actions["Metric"].isChecked()
    window.set_unit_system("Metric")


def test_manual_tree_from_contents():
    from openpclink.manual import parse_contents

    text = '<UL><LI><OBJECT type="text/sitemap"><param name="Name" value="A"><param name="Local" value="a.htm"></OBJECT><UL><LI><OBJECT type="text/sitemap"><param name="Name" value="B"><param name="Local" value="b.htm"></OBJECT></UL></UL>'
    assert parse_contents(text) == [{"name": "A", "page": "a", "children": [{"name": "B", "page": "b", "children": []}]}]
    from pathlib import Path

    real = Path(__file__).parent.parent / "work" / "help" / "Help_File_ENG.hhc"
    if real.exists():
        topics = parse_contents(real.read_text(encoding="utf-8", errors="replace"))
        assert topics[0]["name"] == "Welcome" and any(topic["children"] for topic in topics)
