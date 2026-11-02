"""Read-only link soak test: times exchanges and records how failures look."""
import collections, logging, os, statistics, sys, time
os.environ["OPENPCLINK_READ_ONLY"] = "1"
from openpclink import protocol as p
from openpclink.transports import FtdiTransport

seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 60
logging.getLogger("pyftdi").setLevel(logging.ERROR)
logging.basicConfig(level=logging.ERROR, format="%(name)s %(message)s")
t = FtdiTransport(int(sys.argv[1], 16))
d = t._ftdi
print("chip", d.ic_name, "ver", hex(d.device_version), "H", d.is_H_series, "baud", d.baudrate, "maxpkt", d._max_packet_size, "latency", d.get_latency_timer() if hasattr(d,"get_latency_timer") else "?")
sizes = {}
def x(cmd, payload=b"", timeout=0.3):
    t.purge()
    req = p.build_request(0x5B, cmd, payload)
    t0 = time.monotonic()
    t.write(req)
    h = t.read(6, timeout)
    t1 = time.monotonic()
    if len(h) != 6:
        return ("nohdr", len(h), 0, t1 - t0, None)
    n = int.from_bytes(h[4:6], "big")
    body = t.read(n, timeout)
    t2 = time.monotonic()
    late = b""
    if len(body) != n:
        late = t.read(n - len(body), 1.0)
    crc = p.crc16(h + body + late) == 0
    kind = "ok" if len(body) == n and crc else ("late" if late else "short") if len(body) != n else "crc"
    return (kind, len(body), n, t2 - t0, len(late), h.hex())

results = collections.defaultdict(collections.Counter)
times = collections.defaultdict(list)
fails = []
end = time.monotonic() + seconds
n = 0
cfg_off = 0
print(x(p.CMD_STATE))
while time.monotonic() < end:
    for name, cmd, pl in (("state", p.CMD_STATE, b""), ("poll", p.CMD_POLL_RUNTIME, b""), ("cfg50", p.CMD_READ_CONFIG, (47100+9420).to_bytes(2,"big")+(50).to_bytes(2,"big")), ("cfg9k", p.CMD_READ_CONFIG, (cfg_off).to_bytes(2,"big")+(9420).to_bytes(2,"big"))):
        r = x(cmd, pl)
        results[name][r[0]] += 1
        times[name].append(r[3])
        if r[0] != "ok":
            fails.append((name, round(time.monotonic()-end+seconds, 2)) + r)
    n += 1
    time.sleep(float(sys.argv[3]) if len(sys.argv) > 3 else 0.0)
print("rounds", n)
for k, v in results.items():
    ts = sorted(times[k])
    print(k, dict(v), "ms med %.1f p99 %.1f max %.1f" % (statistics.median(ts)*1e3, ts[int(len(ts)*.99)]*1e3, ts[-1]*1e3))
for f in fails[:25]:
    print(f)
t.close()
