"""Read-only preparation for a write test: backup, volatility, candidates."""
import hashlib, os, sys, time
os.environ["OPENPCLINK_READ_ONLY"] = "1"
from pathlib import Path
from openpclink.database import Database
from openpclink.ecu import Ecu, SECRET_IDS, ENGINE_LOCKED_IDS
from openpclink.mapfile import MapFile
from openpclink.transports import open_transport
d = Database.load(); e = Ecu(open_transport(sys.argv[1]), d); i = e.connect()
print("state ok; fw", i.firmware, "locked", e.is_protected, "rpm", e.get(1000), "batt", round(e.get(1081), 2))
rt = i.runtime_size; snaps = []
for _ in range(4):
    e.reload(); snaps.append(bytes(e.memory[rt:])); time.sleep(0.7)
vol = [n for n in range(len(snaps[0])) if len({s[n] for s in snaps}) > 1]
print("config+sec2 bytes", len(snaps[0]), "self-changing", len(vol), [rt + v for v in vol][:20])
Path(os.path.expanduser("~/openpclink-dev")).mkdir(exist_ok=True)
stamp = time.strftime("%Y%m%d-%H%M%S")
MapFile.from_ecu(e, d).save(Path(os.path.expanduser(f"~/openpclink-dev/backup-{stamp}.pclx")), d.map_key)
Path(os.path.expanduser(f"~/openpclink-dev/backup-{stamp}.bin")).write_bytes(snaps[-1])
print("backup", stamp, hashlib.sha256(snaps[-1]).hexdigest()[:16])
for t in (698, 2250, 1123, 2246):
    if t in e.table: print("text", t, d.parameter(t).caption, e.table[t].count, e.table[t].width, repr(e.get_string(t)) if e.table[t].width == 1 else "", "offset", e.table[t].offset, "ro", e.table[t].read_only)
def opt(n):
    p=[x for x in d.search(n) if x.caption==n and x.data_id in e.table]; return [(x.data_id,x.key,int(e.get(x.data_id)),x.options[int(e.get(x.data_id))] if x.options else "") for x in p]
for n in ("RPM Limit Mode","GP RPM Limit 1 Mode","GP RPM Limit 2 Mode","Speed Limit Mode","MAP Limit Mode","GP PWM 1 Mode","GP PWM 2 Mode"):
    print(n,opt(n))
for name in ("GP RPM Limit 2","GP RPM Limit 1"):
    ids=[x for x in d.ids if x in e.table and d.parameter(x).path==("Engine Protection",name)]
    print(name,[(x,d.parameter(x).caption,round(e.get(x),3) if not e.table[x].is_bitfield and e.table[x].count==1 else None,e.table[x].width,e.table[x].read_only,d.parameter(x).minimum,d.parameter(x).maximum,d.parameter(x).priority) for x in ids][:14])
e.close()
