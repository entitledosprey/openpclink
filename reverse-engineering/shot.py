import os,sys,time
os.environ['QT_QPA_PLATFORM']='offscreen'
from PySide6 import QtWidgets
from openpclink import crypto
from openpclink.database import Database
from openpclink.gui import MainWindow
from openpclink.sim import SimEcu,PtyServer
app=QtWidgets.QApplication([])
d=Database.load()
xml=crypto.decode(open('work/extracted/app/PCLink G5/Sample Maps/Voodoo Neo 4 default map.pcl5','rb').read(),d.map_key)
sim=SimEcu(xml); s=PtyServer(sim)
w=MainWindow(d); w.resize(1280,800); w.show(); w.connect_to('serial:'+s.path)
def pump(sec):
    t=time.time()
    while time.time()-t<sec: app.processEvents(); time.sleep(.01)
pump(3)
def item(*path):
    items=[w.tree.topLevelItem(i) for i in range(w.tree.topLevelItemCount())]
    for n in path:
        it=next(c for c in items if c.text(0)==n); it.setExpanded(True); items=[it.child(i) for i in range(it.childCount())]
    return it
w.tree.setCurrentItem(item('Engine Protection','RPM Limit')); pump(1); w.grab().save(sys.argv[1]+'_a.png')
print([ (w.tree.topLevelItem(0).text(0)) ], [w.tree.topLevelItem(i).text(0) for i in range(w.tree.topLevelItemCount())][:12])
f=[p for p in d.search('Fuel Table 1') if p.caption=='Fuel Table 1' and p.data_id in w.worker.ecu.table]
print([(p.data_id,p.path,p.rows,p.columns,w.worker.ecu.table[p.data_id].count) for p in f])
w.form.show_parameters(f[:1]); w.centre.setCurrentIndex(1); pump(1); w.grab().save(sys.argv[1]+'_b.png')
w.worker.ecu._dirty=False; w.close()
