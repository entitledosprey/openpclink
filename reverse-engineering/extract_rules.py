"""Feasibility probe: explore D_menu.dll's branches per setting by running it in the emulator.

Each run answers the DLL's reads from an assignment; each new read is a branch to explore.
The output is plain data for a native evaluator. This tool is never shipped.
"""
import collections, gzip, json, sys, time
from pathlib import Path
from openpclink.database import Database
from openpclink.rules import FLAG_USER_CHANGE, RulesEngine

db = Database.load()
dll = Path("work/extracted/app/PCLink G5/D_menu.dll").read_bytes()
MAX_PATHS = int(sys.argv[2]) if len(sys.argv) > 2 else 1500
MAX_DEPTH = 1
NUMBERS = (0.0, 1.0)


class Probe(RulesEngine):
    def _property(self, data_id, prop, index=0):
        if prop == 1:
            return data_id if db.known(data_id) else -1
        key = (data_id, prop, index)
        if key in self.written:
            return self.written[key]
        if key in self.assign:
            value = self.assign[key]
        else:
            value = self.candidates(key)[0]
            self.assign[key] = value
            self.fresh.append(key)
        return value

    compared = frozenset()
    domains = {}

    def candidates(self, key):
        data_id, prop, _ = key
        if prop == 2 and db.known(data_id) and db.parameter(data_id).options and db.parameter(data_id).path[:1] != ("Table Allocation",):
            count = len(db.parameter(data_id).options)
            near = {w for v in self.compared for w in (v - 1, v, v + 1)}
            return self.remember(data_id, tuple(sorted(({0, 1, count - 1} | near) & set(range(count)))))
        # Table slots multiply the paths without changing what shows. Pinned to OFF.
        if db.known(data_id) and db.parameter(data_id).path[:1] == ("Table Allocation",):
            return (0,)
        if prop == 10:
            return (1, 0)
        if prop == 2 and db.known(data_id) and db.parameter(data_id).options:
            return tuple(range(len(db.parameter(data_id).options)))
        if prop == 2:
            near = sorted({float(w) for v in self.compared if abs(v) <= 255 for w in (v - 1, v, v + 1)} | set(NUMBERS))
            return self.remember(data_id, tuple(near[:24]))
        return NUMBERS

    def remember(self, data_id, values):
        self.domains[data_id] = values
        return values

    def _set_int(self):
        data_id, prop = self._ids()
        value = self._arg(1)
        self.written[(data_id, prop, 0)] = value
        self.effects.append(("int", data_id, prop, value))
        self._return_int(0, 2)

    def _set_double(self):
        import struct
        data_id, prop = self._ids()
        index = self._arg(0)
        value = struct.unpack("<d", self._uc.mem_read(self._sp() + 8, 8))[0]
        self.written[(data_id, prop, index)] = value
        self.effects.append(("dbl", data_id, prop, index, value))
        self._return_int(0, 3)


probe = Probe(dll, lambda *_: 0.0, lambda _: True, lambda _: True)
probe.assign, probe.fresh, probe.written, probe.effects = {}, [], {}, []


TEXT_START, TEXT_END = 0x401000, 0x523000


def trace_compares(trigger, new_value):
    """Record constants the rules compare against, so only those values are tried."""
    from unicorn import UC_HOOK_CODE

    seen = set()
    uc = probe._uc

    def on_code(_uc, address, size, _data):
        code = bytes(uc.mem_read(address, min(size, 7)))
        if code[0] == 0x3D and size == 5:  # cmp eax, imm32
            seen.add(int.from_bytes(code[1:5], "little", signed=True))
        elif code[0] == 0x83 and size >= 3 and (code[1] >> 3) & 7 == 7:  # cmp r/m, imm8
            seen.add(int.from_bytes(code[size - 1:size], "little", signed=True))
        elif code[0] == 0x81 and size >= 6 and (code[1] >> 3) & 7 == 7:  # cmp r/m, imm32
            seen.add(int.from_bytes(code[size - 4:size], "little", signed=True))

    hook = uc.hook_add(UC_HOOK_CODE, on_code, begin=TEXT_START, end=TEXT_END)
    probe.compared = frozenset()
    run(trigger, new_value, {})
    uc.hook_del(hook)
    probe.compared = frozenset(seen)


def run(trigger, new_value, assign):
    probe.assign, probe.fresh, probe.written, probe.effects = dict(assign), [], {}, []
    probe.value_changed(trigger, 0, new_value, FLAG_USER_CHANGE)
    shown = {e[1]: e[3] for e in probe.effects if e[0] == "int" and e[2] == 10}
    return dict(probe.assign), tuple(sorted(shown.items()))


def explore(trigger, new_value):
    """Keep a branch only where changing one read changes what shows."""
    probe.domains = {}
    trace_compares(trigger, new_value)
    reads, outcome = run(trigger, new_value, {})
    rules = [({}, outcome)]
    queue = [({}, reads, outcome, 0)]
    seen = set()
    cut = False
    while queue:
        fixed, reads, outcome, depth = queue.pop()
        for key, current in reads.items():
            if key in fixed:
                continue
            for alternative in probe.candidates(key):
                if alternative == current:
                    continue
                conditions = {**fixed, key: alternative}
                name = frozenset(conditions.items())
                if name in seen:
                    continue
                seen.add(name)
                if len(seen) > MAX_PATHS:
                    return rules, True
                new_reads, new_outcome = run(trigger, new_value, conditions)
                if new_outcome != outcome:
                    rules.append((conditions, new_outcome))
                    if depth < MAX_DEPTH:
                        queue.append((conditions, new_reads, new_outcome, depth + 1))
    return rules, cut


shard, shards = (int(x) for x in sys.argv[4].split("/")) if len(sys.argv) > 4 else (0, 1)
everything = db.ids if sys.argv[1] == "all" else [int(x) for x in sys.argv[1].split(",")]
ids = everything[shard::shards]
target = Path(sys.argv[3]) if len(sys.argv) > 3 else Path("work/rules.json.gz")
start = time.time(); table = {}; stats = collections.Counter(); truncated = []
for n, data_id in enumerate(ids):
    parameter = db.parameter(data_id)
    values = [0, 1]  # The rules read the setting's value themselves, so these two are enough.
    entry = {}
    for value in values:
        rules, cut = explore(data_id, value)
        stats["rules"] += len(rules)
        if cut:
            truncated.append((data_id, value))
        kept = [
            {"when": [[k[0], k[1], k[2], v] for k, v in conditions.items()], "visible": [list(x) for x in outcome]}
            for conditions, outcome in rules
            if outcome or conditions
        ]
        if kept:
            entry[str(value)] = {"rules": kept, "domains": {str(k): list(v) for k, v in probe.domains.items()}}
    if entry:
        table[str(data_id)] = entry
    print(shard, n, len(ids), data_id, dict(stats), "with rules", len(table), "truncated", len(truncated), round(time.time() - start), "s", flush=True)
    if n % 25 == 0:
        with gzip.open(target, "wt") as out:
            json.dump({"complete": False, "rules": table, "truncated": truncated}, out)
with gzip.open(target, "wt") as out:
    json.dump({"complete": True, "rules": table, "truncated": truncated}, out)
print("DONE", shard, dict(stats), "with rules", len(table), "truncated", len(truncated), round(time.time() - start), "s", flush=True)
