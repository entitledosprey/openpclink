import sys,re
sys.path.insert(0,'src')
from openpclink.sim import SimEcu
s=SimEcu(open('work/decoded/xtreme.xml','rb').read(),hardware_family=1000)
import xml.etree.ElementTree as ET
names={p.get('DataId'):p.get('Name') for p in ET.parse('work/decoded/ECU.xml').getroot().find('Parameters')}
inv=sorted((o,i) for i,o in s.offsets.items())
import bisect
for line in open(sys.argv[1]):
    m=re.search(r'cmd=02 req=([0-9a-f]{4})([0-9a-f]{4})([0-9a-f]*)',line)
    if m:
        off=int(m.group(1),16)+s.runtime_size
        k=bisect.bisect_right([o for o,_ in inv],off)-1
        print(hex(int(m.group(1),16)),'len',int(m.group(2),16),'data',m.group(3)[:40],'->',inv[k][1],names.get(str(inv[k][1])),'+%d'%(off-inv[k][0]))
    else: print(line.strip()[:60])
