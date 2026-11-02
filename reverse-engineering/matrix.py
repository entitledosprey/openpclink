"""Read-only: which USB settings deliver the ECU's small config read intact?"""
import os, sys, time
os.environ["OPENPCLINK_READ_ONLY"] = "1"
from openpclink import protocol as p
from openpclink.transports import FtdiTransport
pid = int(sys.argv[1], 16)
REQ = p.build_request(0x5B, p.CMD_READ_CONFIG, (56520).to_bytes(2, "big") + (50).to_bytes(2, "big"))
def raw_collect(d, want, limit):
    """Everything that arrives, with the size of each delivery."""
    got, sizes, end = bytearray(), [], time.monotonic() + limit
    while time.monotonic() < end and len(got) < want:
        b = d.read_data(4096)
        if b:
            got += b; sizes.append(len(b))
    return bytes(got), sizes
for latency in (16, 2, 1):
  for purge in ("rx", "none"):
    t = FtdiTransport(pid); d = t._ftdi
    d.set_latency_timer(latency)
    ok = bad = 0; shape = {}; lens = {}
    for i in range(40):
        if purge == "rx":
            d.purge_rx_buffer()
        d.write_data(REQ)
        got, sizes = raw_collect(d, 58, 0.4)
        good = len(got) == 58 and p.crc16(got) == 0
        ok += good; bad += not good
        if not good:
            lens[len(got)] = lens.get(len(got), 0) + 1
            shape[tuple(sizes)] = shape.get(tuple(sizes), 0) + 1
        time.sleep(0.02)
    print(f"latency={latency:2} purge={purge:4} ok={ok:2} bad={bad:2} lens={lens} shapes={list(shape.items())[:3]}", flush=True)
    t.close(); time.sleep(0.2)
