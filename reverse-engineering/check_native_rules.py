"""Score the native rules against the real D_menu.dll in the emulator, on every sample map.

For each map: run startup both ways, then apply random option changes, comparing what shows.
Never shipped, and it never touches an ECU.
"""
import random, sys
from pathlib import Path
from openpclink.database import Database
from openpclink.mapfile import MapFile
from openpclink.native_rules import NativeRules, RulesTable
from openpclink.rules import FLAG_STARTUP, RulesEngine

db = Database.load()
folder = Path("work/extracted/app/PCLink G5")
dll = (folder / "D_menu.dll").read_bytes()
pinned = lambda i: db.known(i) and db.parameter(i).path[:1] == ("Table Allocation",)
table = RulesTable.load(Path(sys.argv[1]), pinned)
maps = [Path(sys.argv[2])] if len(sys.argv) > 2 and sys.argv[2].endswith("x") else sorted((folder / "Sample Maps").glob("*.pcl*"))[:: int(sys.argv[2]) if len(sys.argv) > 2 else 8]
rng = random.Random(7)
total = wrong = 0
controlled = controlled_wrong = 0
examples = {}
for path in maps:
    tune = MapFile.load(path, db.map_key)
    entries = tune.entries
    def read(i, index=0):
        values = entries[i].values
        return values[index] if index < len(values) else 0.0
    exists = lambda i: i in entries
    default = lambda i: db.known(i) and db.parameter(i).visible
    real = RulesEngine(dll, read, exists, default)
    native = NativeRules(table, read, exists, default)
    ids = [i for i, e in sorted(entries.items()) if e.values and e.kind not in (1, 8) and db.known(i)]
    for i in ids:
        real.value_changed(i, entries[i].values[0], entries[i].values[0], FLAG_STARTUP)
        native.value_changed(i, entries[i].values[0])
    def score(stage):
        global total, wrong, controlled, controlled_wrong
        for i in entries:
            if db.known(i) and not db.parameter(i).path[:1] == ("Table Allocation",):
                total += 1
                if bool(real.visible(i)) != bool(default(i)):  # The rules matter for this one.
                    controlled += 1
                    controlled_wrong += bool(real.visible(i)) != bool(native.visible(i))
                if bool(real.visible(i)) != bool(native.visible(i)):
                    wrong += 1
                    examples.setdefault((stage, i), (path.name[:28], db.parameter(i).caption, real.visible(i), native.visible(i)))
    score("startup")
    options = [i for i in ids if db.parameter(i).options and not entries[i].read_only]
    for _ in range(60):
        i = rng.choice(options); new = rng.randrange(len(db.parameter(i).options)); old = entries[i].values[0]
        entries[i].values[0] = float(new)
        real.value_changed(i, old, new); native.value_changed(i, new)
    score("changes")
print("maps", len(maps), "checks", total, "wrong", wrong, f"agreement {100 * (1 - wrong / max(total, 1)):.2f}%")
print(f"where the rules change the answer: {controlled} checks, native wrong on {controlled_wrong} ({100 * (1 - controlled_wrong / max(controlled, 1)):.1f}% right)")
for key, value in list(examples.items())[:15]:
    print(key, value)
