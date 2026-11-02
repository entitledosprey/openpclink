"""Exercises the importer and database against a real PCLink. Skipped without one."""
import random
from pathlib import Path

import pytest

from openpclink import crypto
from openpclink.database import Database
from openpclink.ecu import Ecu
from openpclink.importer import find_keys, import_pclink
from openpclink.sim import PtyServer, SimEcu
from openpclink.transports import SerialTransport


@pytest.fixture(scope="module")
def database(tmp_path_factory):
    from tests.conftest import PROGRAM

    if not (PROGRAM / "ECU.cfgr").exists():
        pytest.skip("needs an extracted PCLink in work/")
    return Database.load(import_pclink(PROGRAM, tmp_path_factory.mktemp("db") / "db.json.gz"))


def test_parameter_inherits_through_copyfromid(database):
    assert database.parameter(21).units == database.parameter(20).units
    assert database.parameter(21).caption


def test_caption_and_options_resolve_to_text(database):
    item = database.parameter(2324)
    assert item.caption == "Password Protection"
    assert item.options[0] == "OFF"


def test_table_shape_is_known(database):
    assert database.parameter(278).rows * database.parameter(278).columns > 1 or database.known(278)


def test_range_check(database):
    item = next(p for p in map(database.parameter, database.ids) if p.maximum is not None)
    with pytest.raises(ValueError):
        database.check_range(item.data_id, item.maximum + 1)
    database.check_range(item.data_id, item.maximum)


def test_xxtea_round_trip_with_real_key(database):
    payload = b"hello ecu" * 100
    assert crypto.decode(crypto.encode(payload, database.map_key), database.map_key) == payload


def test_protected_files_are_refused(database):
    import struct

    payload = b"x" * 64
    words = list(struct.unpack("<20I", struct.pack("<3I", 1, len(payload), 0xDEAD) + payload + bytes(4)))
    crypto._encrypt_words(words, database.map_key)
    with pytest.raises(crypto.ProtectedFile):
        crypto.decode(struct.pack("<20I", *words), database.map_key)


def _maps(program_dir: Path):
    return sorted((program_dir / "Sample Maps").glob("*.pcl*"))


def test_every_sample_map_decodes(program_dir, database):
    maps = _maps(program_dir)
    assert len(maps) > 60
    for path in maps:
        assert crypto.decode(path.read_bytes(), database.map_key).lstrip().startswith(b"<?xml")


@pytest.mark.parametrize("name", ["Voodoo Neo 4 default map.pcl5", "Xtreme default map.pclx"])
def test_full_session_against_real_map_image(program_dir, database, name):
    xml = crypto.decode((program_dir / "Sample Maps" / name).read_bytes(), database.map_key)
    sim = SimEcu(xml)
    server = PtyServer(sim)
    session = Ecu(SerialTransport(server.path))
    session.connect()
    assert len(session.table) > 3000
    assert not session.is_protected
    assert session.info.config_size > 10000

    rng = random.Random(1)
    numeric = [
        loc for loc in session.table.values()
        if not (loc.read_only or loc.is_bitfield or loc.is_string or loc.is_float)
        and loc.data_id not in (2294, 2295, 2303, 2324) and loc.scale and loc.offset >= session.info.runtime_size
    ]
    assert len(numeric) > 500
    for loc in rng.sample(numeric, 150):
        index = rng.randrange(loc.count)
        current = session.get(loc.data_id, index)
        target = current + loc.scale
        try:
            session.set(loc.data_id, target, index)
        except Exception:
            target = current - loc.scale
            session.set(loc.data_id, target, index)
        assert session.get(loc.data_id, index) == pytest.approx(target, abs=abs(loc.scale) / 2)

    expected = bytes(session.memory[session.info.runtime_size:])
    session.reload()
    assert bytes(session.memory[session.info.runtime_size:]) == expected
    session.close()
    server.close()


def _session_for(program_dir, database, name="Xtreme default map.pclx"):
    xml = crypto.decode((program_dir / "Sample Maps" / name).read_bytes(), database.map_key)
    sim = SimEcu(xml)
    server = PtyServer(sim)
    session = Ecu(SerialTransport(server.path), database)
    session.connect()
    return sim, server, session


def test_write_is_byte_identical_to_real_pclink_capture(program_dir, database):
    """Real PCLink 7.8.2 set RPM Limit Mode to "Ignition %cut" on this same image
    and sent cmd 0x02 with payload 37b3000120 (see docs/VERIFICATION.md)."""
    sim, server, session = _session_for(program_dir, database)
    data_id = next(p.data_id for p in database.search("RPM Limit Mode") if p.caption == "RPM Limit Mode")
    session.set(data_id, 1)
    assert sim.writes == [bytes.fromhex("37b3000120")]
    session.close()
    server.close()


def test_numeric_write_is_byte_identical_to_real_pclink_capture(program_dir, database):
    """Real PCLink wrote the u16 value 123 to parameter 2657: payload 378e0002007b."""
    sim, server, session = _session_for(program_dir, database)
    session.set(2657, 123)
    assert sim.writes == [bytes.fromhex("378e0002007b")]
    session.close()
    server.close()


def test_padlocked_setting_is_refused_while_engine_runs(program_dir, database):
    from openpclink.ecu import WriteRefused

    sim, server, session = _session_for(program_dir, database)
    cylinders = 2419
    sim.engine_running = True
    with pytest.raises(WriteRefused, match="engine is running"):
        session.set(cylinders, 6)
    assert sim.writes == []
    sim.engine_running = False
    assert session.set(cylinders, 6) == pytest.approx(6)
    session.close()
    server.close()


def test_out_of_range_value_is_refused_by_session(program_dir, database):
    from openpclink.ecu import WriteRefused

    sim, server, session = _session_for(program_dir, database)
    item = next(
        p for p in map(database.parameter, database.ids)
        if p.maximum is not None and p.data_id in session.table and not session.table[p.data_id].read_only
        and not session.table[p.data_id].is_bitfield and session.table[p.data_id].offset >= session.info.runtime_size
        and p.data_id not in (2294, 2295, 2303, 2324, 2419, 2428, 2377)
    )
    with pytest.raises(WriteRefused):
        session.set(item.data_id, item.maximum + 1000)
    assert sim.writes == []
    session.close()
    server.close()
