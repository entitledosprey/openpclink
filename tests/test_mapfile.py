"""Map files, offline editing and logs. Uses real sample maps when available."""
import pytest

from openpclink import crypto
from openpclink.database import Database
from openpclink.ecu import Ecu, EcuProtected, WriteRefused
from openpclink.importer import import_pclink
from openpclink.logfile import PcLogger, read_log
from openpclink.mapfile import MapFile, OfflineEcu, compare
from openpclink.sim import PtyServer, SimEcu
from openpclink.transports import SerialTransport
from openpclink.views import axis_cell, heat_colour


@pytest.fixture(scope="module")
def database(tmp_path_factory):
    from tests.conftest import PROGRAM

    if not (PROGRAM / "ECU.cfgr").exists():
        pytest.skip("needs an extracted PCLink in work/")
    return Database.load(import_pclink(PROGRAM, tmp_path_factory.mktemp("db") / "db.json.gz"))


@pytest.fixture
def sample(database):
    from tests.conftest import PROGRAM

    return PROGRAM / "Sample Maps" / "Voodoo Neo 4 default map.pcl5"


def test_map_round_trips_with_every_value_intact(database, sample, tmp_path):
    original = MapFile.load(sample, database.map_key)
    target = tmp_path / "out.pcl5"
    original.save(target, database.map_key)
    again = MapFile.load(target, database.map_key)
    assert again.memory == original.memory
    assert again.entries.keys() == original.entries.keys()
    for data_id, entry in original.entries.items():
        other = again.entries[data_id]
        assert (other.kind, other.count, other.read_only, other.rows, other.columns) == (
            entry.kind, entry.count, entry.read_only, entry.rows, entry.columns)
        assert other.values == pytest.approx(entry.values), data_id
        assert other.text == entry.text
    assert compare(original, again) == []


def test_saved_map_is_tagged_unprotected(database, sample, tmp_path):
    target = tmp_path / "out.pcl5"
    MapFile.load(sample, database.map_key).save(target, database.map_key)
    assert crypto.decode(target.read_bytes(), database.map_key).startswith(b"<?xml")


def test_compare_finds_a_changed_table_cell(database, sample):
    a = MapFile.load(sample, database.map_key)
    b = MapFile.load(sample, database.map_key)
    b.entries[2365].values[5] += 10
    found = compare(a, b)
    assert [(d.data_id, d.cells) for d in found] == [(2365, 1)]


def test_compare_ignores_live_values(database, sample):
    a = MapFile.load(sample, database.map_key)
    b = MapFile.load(sample, database.map_key)
    b.entries[1000].values[0] = 7000
    assert compare(a, b) == []


def test_offline_edit_then_save_keeps_the_change(database, sample, tmp_path):
    offline = OfflineEcu(MapFile.load(sample, database.map_key), sample)
    offline.set(2365, 77.0, 3)
    assert offline.has_unstored_changes
    target = tmp_path / "edited.pcl5"
    offline.save(target, database.map_key)
    assert not offline.has_unstored_changes
    assert MapFile.load(target, database.map_key).entries[2365].values[3] == 77.0


def test_offline_refuses_live_values_and_secrets(database, sample):
    offline = OfflineEcu(MapFile.load(sample, database.map_key), sample)
    with pytest.raises(WriteRefused):
        offline.set(1000, 1)
    with pytest.raises(EcuProtected):
        offline.get(2294)


def test_capturing_from_an_ecu_sends_no_writes_and_hides_passwords(database, sample, tmp_path):
    xml = crypto.decode(sample.read_bytes(), database.map_key)
    sim = SimEcu(xml)
    server = PtyServer(sim)
    session = Ecu(SerialTransport(server.path), database)
    session.connect()
    captured = MapFile.from_ecu(session, database)
    assert sim.writes == []
    assert not any(v for v in captured.entries[2294].values)
    target = tmp_path / "from-ecu.pcl5"
    captured.save(target, database.map_key)
    reread = MapFile.load(target, database.map_key)
    # Parameters from 10000 live on the G5 comms processor, which this client does not read.
    assert [d for d in compare(MapFile.parse(xml), reread) if d.data_id < 10000] == []
    session.close()
    server.close()


def test_protected_ecu_cannot_be_captured(database, sample):
    sim = SimEcu()
    sim.memory[sim.offsets[2294] : sim.offsets[2294] + 2] = bytes([0, 0x61])
    server = PtyServer(sim)
    session = Ecu(SerialTransport(server.path), database)
    session.connect()
    with pytest.raises(EcuProtected):
        MapFile.from_ecu(session, database)
    session.close()
    server.close()


def test_log_round_trip(tmp_path):
    logger = PcLogger(tmp_path / "log.csv", [(1, "RPM"), (2, "MAP")])
    logger.sample(lambda data_id: data_id * 10.0)
    logger.sample(lambda data_id: data_id * 20.0)
    logger.close()
    log = read_log(tmp_path / "log.csv")
    assert log.names == ["RPM", "MAP"]
    assert log.channels == [[10.0, 20.0], [20.0, 40.0]]


def test_log_cut_off_mid_line_still_opens(tmp_path):
    path = tmp_path / "cut.csv"
    path.write_text("Time (s),RPM\n0.1,800\n0.2")
    assert read_log(path).channels == [[800.0]]


@pytest.mark.parametrize("value,expected", [(-5, 0), (0, 0), (900, 1), (1100, 2), (4000, 4), (9000, 4)])
def test_axis_cell_finds_nearest_breakpoint(value, expected):
    assert axis_cell(value, [0, 800, 1000, 2000, 4000, 0, 0]) == expected


def test_heat_runs_from_cold_to_hot():
    assert heat_colour(0, 0, 10)[2] > heat_colour(0, 0, 10)[0]
    assert heat_colour(10, 0, 10)[0] > heat_colour(10, 0, 10)[2]
    assert heat_colour(5, 5, 5)
