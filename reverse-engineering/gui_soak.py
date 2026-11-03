"""Read-only: count cut-short polls while the real GUI runs with gauges on screen."""
import logging, os, sys, time
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen"); os.environ["OPENPCLINK_READ_ONLY"] = "1"
interval = float(sys.argv[3])
if interval: sys.setswitchinterval(interval)
from PySide6 import QtWidgets
from openpclink.database import Database
from openpclink.gui import MainWindow
class Count(logging.Handler):
    n = 0; sizes = []
    def emit(self, r):
        m = r.getMessage()
        if "0x09" in m:
            Count.n += 1; Count.sizes.append(m.split("got ")[-1].split(" bytes")[0])
logging.getLogger("openpclink").addHandler(Count()); logging.getLogger("openpclink").setLevel(logging.INFO)
app = QtWidgets.QApplication([]); w = MainWindow(Database.load()); w.resize(1360, 840); w.show(); w.connect_to(sys.argv[1])
def pump(s):
    t = time.time()
    while time.time() - t < s:
        app.processEvents(); time.sleep(.005)
polls = [0]; w.worker.runtime.connect(lambda: polls.__setitem__(0, polls[0] + 1))
t0 = time.time()
while w.status_label.text() != "ONLINE" and time.time() - t0 < 40:
    pump(0.1)
print("status", w.status_label.text(), "connect s", round(time.time() - t0, 1), "rules", w.worker.rules is not None, w.statusBar().currentMessage()[:80])
if w.status_label.text() != "ONLINE": sys.exit(2)
item = next(w.tree.topLevelItem(i) for i in range(w.tree.topLevelItemCount()) if w.tree.topLevelItem(i).text(0) == "Fuel")
it = next(item.child(i) for i in range(item.childCount()) if item.child(i).text(0) == "Fuel Table 1"); w.tree.setCurrentItem(it)
w.right_tabs.setCurrentWidget(w.gauge_panel)
polls[0] = 0; Count.n = 0; Count.sizes = []; start = time.time()
while time.time() - start < float(sys.argv[2]):
    pump(0.05); w.grab()  # Forces repainting, as a visible window would.
print(f"switchinterval={sys.getswitchinterval()} polls={polls[0]} cut-short={Count.n} sizes={Count.sizes[:6]} rpm={w.worker.ecu.get(1000)} unstored={w.worker.ecu.has_unstored_changes}")
w.worker.post("quit"); w.worker.wait(3000)
