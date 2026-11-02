"""One careful RAM write test on a real ECU, always reverted and verified. Never stores."""
import os, sys, struct
from pathlib import Path
os.environ.pop("OPENPCLINK_READ_ONLY", None)
from openpclink import protocol as p
from openpclink.database import Database
from openpclink.ecu import Ecu
from openpclink.transports import open_transport

TEXT, NUMBER, MODE = 698, 2788, 2757
READS = {p.CMD_READ_CONFIG, p.CMD_STATE, p.CMD_POLL_RUNTIME, p.CMD_MEMORY_INFO, p.CMD_READ_DESCRIPTOR,
         p.CMD_FIRMWARE_INFO, p.CMD_BOOT_INFO, p.CMD_IDENTITY, p.CMD_READ_SECTION2}
d = Database.load(); e = Ecu(open_transport(sys.argv[1]), d); info = e.connect(); rt = info.runtime_size
allowed = []
real = e.link.request
log = []
def guarded(cmd, payload=b"", timeout=p.DEFAULT_TIMEOUT):
    if cmd == p.CMD_WRITE_CONFIG:
        off, n = struct.unpack(">HH", payload[:4])
        if not any(a <= off and off + n <= b for a, b in allowed):
            raise SystemExit(f"BLOCKED write outside allowed range: {off}+{n}")
        body = real(cmd, payload, timeout)
        log.append((payload.hex(), body.hex()))
        return body
    if cmd not in READS:
        raise SystemExit(f"BLOCKED command 0x{cmd:02X}")
    return real(cmd, payload, timeout)
e.link.request = guarded
def stopped():
    e.poll_runtime()
    rpm = e.get(1000)
    if rpm != 0:
        raise RuntimeError(f"engine speed {rpm}: not writing")
if e.is_protected: raise SystemExit("locked; aborting")
stopped()
if int(e.get(MODE)) != 0: raise SystemExit("GP RPM Limit 2 is not OFF; aborting")
e.reload()
baseline = bytes(e.memory[rt:])
backup = sorted(Path(os.path.expanduser("~/openpclink-dev")).glob("backup-*.bin"))[-1].read_bytes()
if baseline != backup: raise SystemExit("config differs from the backup; aborting")
tl, nl = e.table[TEXT], e.table[NUMBER]
allowed += [(tl.offset - rt, tl.offset - rt + tl.count), (nl.offset - rt, nl.offset - rt + nl.width)]
text0, num0 = e.get_string(TEXT), e.get(NUMBER)
print("before:", repr(text0), num0, "rpm 0, batt", round(e.get(1081), 2))
failure = None
try:
    stopped(); r = e.set_string(TEXT, "OpenPCLink test"); e.reload()
    print("text after write, read from ECU:", repr(e.get_string(TEXT)), "(returned", repr(r), ")")
    assert e.get_string(TEXT) == "OpenPCLink test"
    stopped(); r = e.set(NUMBER, num0 + 10); e.reload()
    print("number after write, read from ECU:", e.get(NUMBER), "(returned", r, ")")
    assert e.get(NUMBER) == num0 + 10
    changed = [i for i, (a, b) in enumerate(zip(baseline, e.memory[rt:])) if a != b]
    inside = all(any(a <= i < b for a, b in allowed) for i in changed)
    print("bytes differing while changed:", len(changed), "all inside the two settings:", inside)
    assert inside
except BaseException as exc:
    failure = exc
    print("TEST STEP FAILED:", repr(exc))
finally:
    e.reload()
    if e.get_string(TEXT) != text0: e.set_string(TEXT, text0)
    if e.get(NUMBER) != num0: e.set(NUMBER, num0)
    e.reload()
    now = bytes(e.memory[rt:])
    print("after revert:", repr(e.get_string(TEXT)), e.get(NUMBER))
    print("REVERT VERIFIED byte-for-byte:", now == baseline, "| differing bytes:", sum(a != b for a, b in zip(now, baseline)))
for sent, got in log: print("wire write", sent[:36], "reply", got[:36])
print("store sent: no; writes:", len(log), "; rpm", e.get(1000))
e.close()
sys.exit(1 if failure else 0)
