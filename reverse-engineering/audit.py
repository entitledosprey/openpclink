"""Read-only: connect, then check every decoded value against Link's database."""
import collections, json, os, sys, time
os.environ["OPENPCLINK_READ_ONLY"] = "1"
from openpclink.database import Database
from openpclink.ecu import Ecu, SECRET_IDS
from openpclink.transports import open_transport
db = Database.load()
t0 = time.monotonic()
e = Ecu(open_transport(sys.argv[1]), db)
info = e.connect()
print("connect s", round(time.monotonic() - t0, 2), info)
print("locked", e.is_protected)
kinds = collections.Counter(l.kind for l in e.table.values())
print("kinds", sorted(kinds.items()))
print("float", sum(l.is_float for l in e.table.values()), "signed", sum(l.signed for l in e.table.values()), "readonly", sum(l.read_only for l in e.table.values()))
end = max(l.offset + max(l.width, 1) * max(l.count, 1) for l in e.table.values())
print("layout end", end, "memory", len(e.memory))
print("unknown to db", [i for i in e.table if not db.known(i)][:20], sum(not db.known(i) for i in e.table))
rt = [l for l in e.table.values() if l.offset < info.runtime_size]; print("in runtime", len(rt), "config", len(e.table) - len(rt))
out = n = 0; bad = []; opts = 0; optbad = []
for i, l in sorted(e.table.items()):
    if i in SECRET_IDS or not db.known(i) or l.is_string or i == 0: continue
    p = db.parameter(i)
    if l.is_bitfield:
        v = int(e.get(i)); opts += 1
        if p.options and v >= len(p.options): optbad.append((i, p.key, v, len(p.options)))
        continue
    for v in e.get_all(i):
        n += 1
        if (p.minimum is not None and v < p.minimum - abs(l.scale)) or (p.maximum is not None and v > p.maximum + abs(l.scale)):
            out += 1; bad.append((i, p.key, round(v, 3), p.minimum, p.maximum, "rt" if l.offset < info.runtime_size else "cfg"))
print("values", n, "out of range", out, "ids", len({b[0] for b in bad})); print("bitfields", opts, "bad option", len(optbad))
print(bad[:25]); print(optbad[:15])
print("strings", [(i, e.get_string(i)[:24]) for i, l in sorted(e.table.items()) if l.is_string and l.width == 1 and i not in SECRET_IDS and e.get_string(i)][:14])
for c in ("Engine Speed", "Batt Voltage", "MAP", "ECT", "IAT", "ECU Temperature"):
    m = [p for p in db.search(c) if p.caption == c and p.data_id in e.table]
    print(c, [(p.data_id, round(e.get(p.data_id), 2), p.units) for p in m][:3])
print("Fuel Table 1", e.table.get(2365), [round(v, 1) for v in e.get_all(2365)[:12]], "x", [round(v) for v in e.get_all(3074)[:12]])
print("Number of Cylinders", e.get(2419), db.parameter(2419).options[int(e.get(2419))] if db.parameter(2419).options else "")
e.close()
