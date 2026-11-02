import sys,time,struct
sys.path.insert(0,'/project/src')
from openpclink.sim import SimEcu,PtyServer
xml=open(sys.argv[1],'rb').read()
fam=int(sys.argv[2])
class Log(SimEcu):
    def handle(self,frame):
        r=super().handle(frame)
        cmd=struct.unpack('>H',frame[2:4])[0] if len(frame)>=4 else -1
        if cmd!=9 or Log.n9<3:
            print(f"{time.time()%1000:8.2f} addr={frame[1]:02x} cmd={cmd:02x} req={frame[6:-2][:24].hex()} -> rcmd={r[3]:02x} len={len(r)-8}",flush=True)
        if cmd==9: Log.n9+=1
        return r
Log.n9=0
e=Log(xml,hardware_family=fam)
import os
e.engine_running=os.environ.get('ENGINE','run')=='run'
s=PtyServer(e)
print('runtime',e.runtime_size,'config',e.config_size,'desc',len(e.descriptor),'maxid',e.max_id,flush=True)
open('/tmp/pty','w').write(s.path)
while True:
    time.sleep(1)
