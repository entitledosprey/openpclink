"""Improve the rules table from counterexamples, using the DLL in the emulator. Never shipped.

For each tune and setting: compare the DLL's outcome with the table's. On a difference, shrink what
the DLL read to the smallest set that still gives its outcome, and add that as a rule.
Usage: refine_rules.py <table> <out> <shard>/<shards> <tune>...
"""
import gzip, json, random, sys
from pathlib import Path
from extract_rules import db, probe, run
from openpclink.mapfile import MapFile
from openpclink.native_rules import NativeRules, RulesTable

source, target = Path(sys.argv[1]), Path(sys.argv[2])
shard, shards = (int(x) for x in sys.argv[3].split("/"))
tunes = [Path(p) for p in sys.argv[4:]][shard::shards]
raw = json.load(gzip.open(source, "rt"))
pinned = lambda i: db.known(i) and db.parameter(i).path[:1] == ("Table Allocation",)
ids = [i for i in db.ids]
rng = random.Random(shard + 11)
additions = []
compared = found = 0


def outcome_of(data_id, new, reader):
    """The DLL's outcome with reads answered from a tune, and what it read."""
    original = probe.candidates

    def tune_first(key):
        got = reader(key)
        return (got,) if got is not None else original(key)

    probe.candidates = tune_first
    try:
        probe.domains = {}
        return run(data_id, new, {})
    finally:
        probe.candidates = original


def shrink(data_id, new, reads, wanted):
    keep = {k: v for k, v in reads.items() if not pinned(k[0])}
    for key in sorted(keep, key=lambda k: (k[1] != 2, k[0])):
        trial = {k: v for k, v in keep.items() if k != key}
        if run(data_id, new, trial)[1] == wanted:
            keep = trial
    return keep


def table_outcome(table, data_id, new, entries):
    def read(i, index=0):
        values = entries[i].values if i in entries else []
        return values[index] if index < len(values) else 0.0

    rules = NativeRules(table, read, lambda i: i in entries, lambda i: db.known(i) and db.parameter(i).visible)
    rules.value_changed(data_id, new)
    return rules


for variant, path in enumerate(tunes):
    for attempt in range(4):
        entries = MapFile.load(path, db.map_key).entries
        if attempt:  # Random option changes reach combinations no single tune has.
            options = [i for i, e in entries.items() if e.values and db.known(i) and db.parameter(i).options and not e.read_only]
            for i in rng.sample(options, min(40, len(options))):
                entries[i].values[0] = float(rng.randrange(len(db.parameter(i).options)))
        table = RulesTable(raw, pinned)

        def reader(key):
            data_id, prop, index = key
            if prop == 2 and data_id in entries and not pinned(data_id):
                values = entries[data_id].values
                return values[index] if index < len(values) else 0.0
            return None

        for data_id in ids:
            entry = entries.get(data_id)
            if entry is None or not entry.values or entry.kind in (1, 8):
                continue
            new = 1 if entry.values[0] > 0 else 0
            reads, wanted = outcome_of(data_id, new, reader)
            if not any(wanted):
                continue
            compared += 1
            native = table_outcome(table, data_id, new, entries)
            shown = dict(wanted[0])
            if all(bool(native.shown.get(t, not v)) == bool(v) for t, v in shown.items()):
                continue
            found += 1
            keep = shrink(data_id, new, reads, wanted)
            rule = {
                "when": [[k[0], k[1], k[2], v] for k, v in keep.items()],
                "visible": [list(x) for x in wanted[0]],
                "captions": [list(x) for x in wanted[1]],
                "messages": list(wanted[2]),
                "parents": [list(x) for x in wanted[3]],
            }
            additions.append([data_id, new, rule])
            group = raw["rules"].setdefault(str(data_id), {}).setdefault(str(new), {"rules": [], "domains": {}})
            group["rules"].append(rule)
            for key, value in keep.items():
                domain = group["domains"].setdefault(str(key[0]), [])
                if value not in domain:
                    domain.append(value)
            table = RulesTable(raw, pinned)
        print(shard, path.name[:30], attempt, "compared", compared, "fixed", found, flush=True)
    with gzip.open(target, "wt") as out:
        json.dump(additions, out)
print("DONE", shard, "compared", compared, "fixed", found, flush=True)
