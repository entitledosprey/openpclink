"""Command-line interface."""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from .database import Database, DatabaseMissing
from .ecu import Ecu, EcuProtected, WriteMismatch, WriteRefused
from .importer import ImportFailed, import_pclink
from .protocol import ProtocolError
from .sim import PtyServer, SimEcu, UdpServer
from .transports import TransportError, list_ports, open_transport


class CliError(Exception):
    pass


def _connect(args: argparse.Namespace) -> Ecu:
    """Connect to --port, or to the first detected port that answers as an ECU.

    A G5 exposes several USB interfaces and only one is the tuning port.
    """
    uris = [args.port] if args.port else [port.uri for port in list_ports()]
    if not uris:
        raise CliError("no Link ECU found; connect one or pass --port (see 'openpclink ports')")
    database = Database.load()
    failure: Exception | None = None
    for uri in uris:
        ecu = None
        try:
            ecu = Ecu(open_transport(uri), database)
            ecu.connect()
            return ecu
        except (ProtocolError, TransportError) as exc:
            failure = exc
            if ecu:
                ecu.close()
    raise failure


def _resolve_id(database: Database, text: str) -> int:
    if text.isdigit():
        return int(text)
    matches = database.search(text)
    exact = [item for item in matches if text.lower() in (item.caption.lower(), item.key.lower())]
    matches = exact or matches
    if len(matches) == 1:
        return matches[0].data_id
    if not matches:
        raise CliError(f"no parameter matches '{text}'")
    listing = "\n".join(f"  {item.data_id}: {item.key}" for item in matches[:20])
    raise CliError(f"'{text}' matches several parameters:\n{listing}")


def _format(database: Database, data_id: int, value: float) -> str:
    if database.known(data_id):
        parameter = database.parameter(data_id)
        if parameter.options and 0 <= int(value) < len(parameter.options):
            return f"{parameter.options[int(value)]} ({int(value)})"
        decimals = parameter.decimals if parameter.decimals is not None else 3
        text = f"{value:.{decimals}f}"
        # Never let display rounding misstate what the ECU holds.
        if abs(float(text) - value) > 1e-3:
            text = f"{value:g}"
        return f"{text} {parameter.units}".strip()
    return f"{value:g}"


def cmd_import(args: argparse.Namespace) -> None:
    print(f"Imported PCLink data to {import_pclink(Path(args.source))}")


def cmd_ports(args: argparse.Namespace) -> None:
    ports = list_ports(all_serial=args.all)
    if not ports:
        print("No Link ECU found.")
    for port in ports:
        print(f"{port.uri}\t{port.description}")


def cmd_info(args: argparse.Namespace) -> None:
    ecu = _connect(args)
    try:
        info = ecu.info
        print(f"Firmware:  {info.firmware}")
        print(f"Boot code: {info.boot}")
        print(f"Serial:    {info.serial}")
        print(f"Link ID:   {info.link_id}")
        print(f"Memory:    runtime {info.runtime_size}, config {info.config_size}, section 2 {info.section2_size} bytes")
        print(f"Params:    {len(ecu.table)}")
        print(f"Locked:    {'yes' if ecu.is_protected else 'no'}")
    finally:
        ecu.close()


def cmd_search(args: argparse.Namespace) -> None:
    for item in Database.load().search(args.text):
        print(f"{item.data_id}\t{item.caption}\t{' > '.join(item.path)}")


def cmd_get(args: argparse.Namespace) -> None:
    database = Database.load()
    data_id = _resolve_id(database, args.parameter)
    ecu = _connect(args)
    try:
        location = ecu.location(data_id)
        if location.is_string and location.width == 1:
            print(ecu.get_string(data_id))
        elif args.index is not None:
            print(_format(database, data_id, ecu.get(data_id, args.index)))
        elif location.count > 1 and not location.is_bitfield:
            print(",".join(f"{value:g}" for value in ecu.get_all(data_id)))
        else:
            print(_format(database, data_id, ecu.get(data_id)))
    finally:
        ecu.close()


def cmd_set(args: argparse.Namespace) -> None:
    database = Database.load()
    data_id = _resolve_id(database, args.parameter)
    ecu = _connect(args)
    try:
        location = ecu.location(data_id)
        if location.is_string and location.width == 1:
            print(f"Now: {ecu.set_string(data_id, args.value)}")
        else:
            value = float(args.value)
            before = ecu.get(data_id, args.index or 0)
            after = ecu.set(data_id, value, args.index or 0)
            print(f"{_format(database, data_id, before)} -> {_format(database, data_id, after)}")
        if args.store:
            ecu.store()
            print("Stored to ECU flash.")
        else:
            print("Changed in ECU RAM only; it is lost at power-off unless stored (run 'openpclink store').")
    finally:
        ecu.close()


def cmd_store(args: argparse.Namespace) -> None:
    ecu = _connect(args)
    try:
        ecu.store()
        print("Stored to ECU flash.")
    finally:
        ecu.close()


def cmd_backup(args: argparse.Namespace) -> None:
    ecu = _connect(args)
    try:
        ecu.backup(Path(args.file))
        print(f"Saved {args.file}")
    finally:
        ecu.close()


def cmd_monitor(args: argparse.Namespace) -> None:
    database = Database.load()
    ids = [_resolve_id(database, item) for item in args.parameters]
    ecu = _connect(args)
    try:
        while True:
            ecu.poll_runtime()
            print("  ".join(f"{database.parameter(i).caption}={_format(database, i, ecu.get(i))}" for i in ids))
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        ecu.close()


def cmd_sim(args: argparse.Namespace) -> None:
    xml = None
    if args.map:
        from . import crypto

        xml = crypto.decode(Path(args.map).read_bytes(), Database.load().map_key)
    ecu = SimEcu(xml)
    pty, udp = PtyServer(ecu), UdpServer(ecu, port=args.udp_port)
    print(f"Simulated ECU ready.\n  serial: serial:{pty.path}\n  wifi:   udp:127.0.0.1:{udp.port}", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        pty.close()
        udp.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="openpclink", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    def add(name: str, handler, help_text: str, port: bool = True) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, help=help_text)
        sub.set_defaults(handler=handler)
        if port:
            sub.add_argument("--port", help="serial:/dev/x, ftdi:<pid> or udp:<host>; default auto-detect")
        return sub

    add("import", cmd_import, "import data from your PCLink installer", False).add_argument("source")
    add("ports", cmd_ports, "list connected ECUs", False).add_argument("--all", action="store_true")
    add("info", cmd_info, "show ECU information")
    add("search", cmd_search, "find parameters by name", False).add_argument("text")
    get = add("get", cmd_get, "read a parameter")
    get.add_argument("parameter")
    get.add_argument("--index", type=int)
    set_ = add("set", cmd_set, "change a parameter")
    set_.add_argument("parameter")
    set_.add_argument("value")
    set_.add_argument("--index", type=int)
    set_.add_argument("--store", action="store_true", help="also make it permanent")
    add("store", cmd_store, "make live changes permanent")
    add("backup", cmd_backup, "save the ECU configuration as JSON").add_argument("file")
    monitor = add("monitor", cmd_monitor, "print live values")
    monitor.add_argument("parameters", nargs="+")
    monitor.add_argument("--interval", type=float, default=0.2)
    sim = add("sim", cmd_sim, "run a simulated ECU for testing", False)
    sim.add_argument("--map", help="build the simulation from a .pclx/.pcl5 map")
    sim.add_argument("--udp-port", type=int, default=0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.handler(args)
    except (
        CliError, DatabaseMissing, EcuProtected, ImportFailed, KeyError, OSError, ProtocolError,
        TransportError, ValueError, WriteMismatch, WriteRefused,
    ) as exc:
        print(f"error: {exc.args[0] if exc.args else exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
