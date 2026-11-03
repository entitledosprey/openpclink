"""Compare each form real PCLink showed (OCR captures) with what this app shows.

Usage: compare_forms.py <real-forms.json> <map> <out.json>. Runs on the simulator; never touches an ECU.
"""
import difflib, json, os, sys, time
from pathlib import Path
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6 import QtWidgets
from openpclink import crypto
from openpclink.database import Database
from openpclink.gui import MainWindow
from openpclink.sim import PtyServer, SimEcu

real = json.loads(Path(sys.argv[1]).read_text())
app = QtWidgets.QApplication([])
db = Database.load()
xml = crypto.decode(Path(sys.argv[2]).read_bytes(), db.map_key)
sim = SimEcu(xml, hardware_family=1000); sim.engine_running = False
server = PtyServer(sim)
w = MainWindow(db); w.connect_to(f"serial:{server.path}")
end = time.time() + 30
while w.status_label.text() != "ONLINE" and time.time() < end:
    app.processEvents(); time.sleep(.01)
assert w.worker.rules is not None, "rules did not start"
mine = {}
tables = set()
stack = [(w.tree.topLevelItem(i), ()) for i in range(w.tree.topLevelItemCount())]
while stack:
    item, parents = stack.pop()
    ids = w._form_ids(item)
    shown = [i for i in ids if w.parameter_visible(i)]
    if item.data(0, 0x0100) and len(item.data(0, 0x0100)) == 1 and db.parameter(item.data(0, 0x0100)[0]).is_table:
        tables.add(item.text(0))
    mine.setdefault(item.text(0), []).append((bool(item.childCount()), [db.parameter(i).caption for i in sorted(shown, key=lambda i: -db.parameter(i).priority)]))
    stack.extend((item.child(i), parents + (item.text(0),)) for i in range(item.childCount()))
def norm(s): return "".join(ch for ch in s.lower() if ch.isalnum())
def same(a, b): return norm(a) == norm(b) or difflib.SequenceMatcher(None, norm(a), norm(b)).ratio() > 0.8
seen = {}
for frame, form in real.items():
    seen.setdefault(form["title"], form["rows"])
result = {"match": [], "tables": [], "folders": [], "differ": [], "missing_here": []}
for title, rows in seen.items():
    best = None
    # An exact title first: similar names such as "GP PWM 1" and "GP Temp 1" are different forms.
    candidates = next((v for k, v in mine.items() if norm(k) == norm(title)), None)
    if candidates is None:
        candidates = max(
            (v for k, v in mine.items() if same(k, title)),
            key=lambda v: 0,
            default=None,
        )
    if candidates is None:
        result["missing_here"].append(title); continue
    if any(same(t, title) for t in tables):
        result["tables"].append(title); continue  # A grid, which OCR cannot read as rows.
    if all(folder for folder, _ in candidates):
        result["folders"].append(title); continue  # Real folders add sub-headings; scored by their leaf forms.
    for _folder, captions in candidates:
        extra = [c for c in captions[:16] if not any(same(c, r) for r in rows)]
        lost = [r for r in rows if not any(same(c, r) for c in captions)]
        # Forms can share a name; a form is matched by the one it resembles most.
        if best is None or len(extra) + len(lost) < len(best[0]) + len(best[1]):
            best = (extra, lost)
    (result["match"].append(title) if not best[0] and not best[1] else result["differ"].append({"form": title, "only_here": best[0], "only_real": best[1]}))
result["only_here_forms"] = [k for k in mine if not any(same(k, t) for t in seen)]
Path(sys.argv[3]).write_text(json.dumps(result, indent=1))
print("real forms", len(seen), "tables", len(result["tables"]), "folders", len(result["folders"]), "match", len(result["match"]), "differ", len(result["differ"]), "missing here", len(result["missing_here"]), "only here", len(result["only_here_forms"]))
w.worker.ecu and setattr(w.worker.ecu, "_dirty", False); w.close(); server.close()
