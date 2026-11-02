from pathlib import Path

import pytest

from openpclink.ecu import Ecu
from openpclink.sim import PtyServer, SimEcu, UdpServer
from openpclink.transports import SerialTransport, UdpTransport

ROOT = Path(__file__).parent.parent
PROGRAM = ROOT / "work" / "extracted" / "app" / "PCLink G5"


@pytest.fixture
def program_dir():
    if not (PROGRAM / "ECU.cfgr").exists():
        pytest.skip("needs an extracted PCLink in work/")
    return PROGRAM


@pytest.fixture
def sim():
    return SimEcu()


@pytest.fixture
def ecu(sim):
    server = PtyServer(sim)
    session = Ecu(SerialTransport(server.path))
    session.connect()
    yield session
    session.close()
    server.close()


@pytest.fixture
def udp_ecu(sim):
    server = UdpServer(sim)
    session = Ecu(UdpTransport("127.0.0.1", server.port))
    session.connect()
    yield session
    session.close()
    server.close()
