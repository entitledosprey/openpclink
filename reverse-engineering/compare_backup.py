"""Read-only: compare the tune with the newest backup, ignoring the ECU's own statistics counters.

Exit 0 only if every other byte is identical and the engine is stopped."""
import glob, os, sys
os.environ["OPENPCLINK_READ_ONLY"] = "1"
from openpclink.database import Database
from openpclink.ecu import Ecu
from openpclink.transports import open_transport
d = Database.load(); e = Ecu(open_transport(sys.argv[1]), d); i = e.connect(); rt = i.runtime_size
b = open(sorted(glob.glob(os.path.expanduser("~/openpclink-dev/backup-*.bin")))[-1], "rb").read()
total = 0
def owners(o):
    return [k for k, l in e.table.items()
            if l.offset <= o < l.offset + max(1 if l.is_bitfield else l.width * max(l.count, 1), 1)]
def is_statistic(o):
    found = owners(o)
    return bool(found) and all(d.known(k) and d.parameter(k).key.endswith("(STATISTICS)") for k in found)
for n in range(3):
    e.reload(); now = bytes(e.memory[rt:])
    diff = [(rt + x, b[x], now[x]) for x in range(len(b)) if b[x] != now[x]]
    tune = [item for item in diff if not is_statistic(item[0])]
    total += len(tune)
    print(f"read {n}: tune bytes differing {len(tune)}, statistics bytes {len(diff) - len(tune)}")
    for o, a, c in tune[:12]:
        print("   ", o, hex(a), hex(c), [(k, d.parameter(k).key if d.known(k) else "?") for k in owners(o)])
e.poll_runtime(); rpm = e.get(1000)
print("rpm", rpm, "batt", round(e.get(1081), 2), "description", repr(e.get_string(698)), "cut decay", e.get(2788))
e.close()
sys.exit(0 if total == 0 and rpm == 0 else 1)
