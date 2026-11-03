"""PCLink's rules DLL running in the emulator. Skipped without a real PCLink."""
import pytest

from openpclink.database import Database
from openpclink.importer import import_pclink
from openpclink.mapfile import MapFile
from openpclink.rules import FLAG_STARTUP, RulesEngine


@pytest.fixture(scope="module")
def database(tmp_path_factory):
    from tests.conftest import PROGRAM

    if not (PROGRAM / "D_menu.dll").exists():
        pytest.skip("needs an extracted PCLink in work/")
    return Database.load(import_pclink(PROGRAM, tmp_path_factory.mktemp("db") / "db.json.gz"))


@pytest.fixture
def engine(database):
    from tests.conftest import PROGRAM

    tune = MapFile.load(PROGRAM / "Sample Maps" / "Xtreme default map.pclx", database.map_key)
    values = {k: e for k, e in tune.entries.items()}

    def read(data_id, index=0):
        entry = values[data_id].values
        return entry[index] if index < len(entry) else 0.0

    engine = RulesEngine(
        (PROGRAM / "D_menu.dll").read_bytes(), read, lambda i: i in values,
        lambda i: database.known(i) and database.parameter(i).visible,
    )
    for data_id, entry in sorted(values.items()):
        if entry.values and entry.kind not in (1, 8) and database.known(data_id):
            engine.value_changed(data_id, entry.values[0], entry.values[0], FLAG_STARTUP)
    engine.tune = values
    return engine


RPM_LIMIT_FORM = ("Engine Protection", "RPM Limit")


def shown(engine, database):
    ids = [i for i in engine.tune if database.known(i) and database.parameter(i).path == RPM_LIMIT_FORM and engine.visible(i)]
    return [database.parameter(i).caption for i in sorted(ids, key=lambda i: -database.parameter(i).priority)]


def test_rpm_limit_form_matches_real_pclink_when_off(engine, database):
    assert shown(engine, database) == ["RPM Limit Mode"]


def test_rpm_limit_form_matches_real_pclink_when_on(engine, database):
    engine.tune[2754].values[0] = 1
    engine.value_changed(2754, 0, 1)
    assert shown(engine, database) == ["RPM Limit Mode", "Dual Tables", "Advanced Mode"]


def test_rules_report_related_changes_without_applying_them(engine):
    engine.tune[2754].values[0] = 1
    changes = engine.value_changed(2754, 0, 1)
    assert [(c.data_id, c.value) for c in changes] == [(3460, 56.0)]
    assert engine.tune[3460].values[0] != 56.0


def test_the_engine_is_quick_enough_to_use_on_every_edit(engine):
    import time

    start = time.monotonic()
    for _ in range(20):
        engine.value_changed(2754, 0, 1)
    assert (time.monotonic() - start) / 20 < 0.1


def test_a_dll_it_cannot_load_is_reported():
    with pytest.raises(Exception):
        RulesEngine(b"not a dll", lambda *_: 0.0, lambda _: False, lambda _: True)


def test_the_app_does_not_import_the_emulator():
    """The product is all native: the emulator is a test tool only."""
    import subprocess
    import sys

    code = "import sys, openpclink.gui, openpclink.cli; print('unicorn' in sys.modules or 'pefile' in sys.modules or 'openpclink.rules' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env={"QT_QPA_PLATFORM": "offscreen", "PYTHONPATH": "src"})
    assert out.stdout.strip() == "False", out.stderr
