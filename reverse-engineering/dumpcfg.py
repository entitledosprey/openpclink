import sys,struct
sys.path.insert(0,'tools')
from cfgdec import dec,KEYS
root="extracted/app/PCLink G5/"
for f,k,o in [('ECU.cfgr','k90','ECU.xml'),('Languages.cfgr','ka0','Languages.txt'),('Sample Maps/Xtreme default map.pclx','k90','xtreme.xml'),('Sample Maps/Voodoo Neo 4 default map.pcl5','k90','neo4.xml')]:
    p=dec(open(root+f,'rb').read(),KEYS[k]); sz=struct.unpack('<I',p[4:8])[0]; open('decoded/'+o,'wb').write(p[12:12+sz])
