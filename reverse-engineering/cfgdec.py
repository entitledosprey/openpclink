import struct,sys,os,glob,math,collections
import pefile
EXE="extracted/app/PCLink G5/PCLink.exe"
pe=pefile.PE(EXE,fast_load=True)
KEYS={n:struct.unpack('<4I',pe.get_data(a-0x400000,16)) for n,a in dict(k90=0x8eda90,ka0=0x8edaa0,kb0=0x8edab0).items()}
D=0x9e3779b9; M=0xffffffff
def xxdec(v,k):
    n=len(v); rounds=6+52//n; s=(rounds*D)&M; y=v[0]
    while s:
        e=(s>>2)&3
        for p in range(n-1,0,-1):
            z=v[p-1]
            v[p]=(v[p]-(((z>>5^y<<2)+(y>>3^z<<4))^((s^y)+(k[(p&3)^e]^z))))&M; y=v[p]
        z=v[n-1]
        v[0]=(v[0]-(((z>>5^y<<2)+(y>>3^z<<4))^((s^y)+(k[e]^z))))&M; y=v[0]
        s=(s-D)&M
    return v
def dec(b,k):
    n=len(b)//4; v=list(struct.unpack('<%dI'%n,b[:n*4])); xxdec(v,k); return struct.pack('<%dI'%n,*v)
if __name__=='__main__':
    root="extracted/app/PCLink G5/"
    fs=[root+x for x in ['DTV.cfgr','ECU.cfgr','Languages.cfgr','DefaultParameterConfig.rtcr','RuntimeViewLayout','TSViewlayout','Sample Maps/Xtreme default map.pclx','Sample Maps/Voodoo Neo 4 default map.pcl5','Layouts/DefaultLayout.llf5','Calibrations/Boost Solenoid Deadtime 500kpa.lte','Firmware/firmware_package_1.9.2.lfp']]+glob.glob(root+'CAN/*.lcs')[:1]
    for f in fs:
        b=open(f,'rb').read()
        res=[]
        for kn,k in KEYS.items():
            p=dec(b,k); ts,sz,tag=struct.unpack('<3I',p[:12])
            ok= 0<sz<=len(b)-12 and len(b)-12-sz<32
            res.append(f"{kn}:{'OK' if ok else '--'} sz={sz} tag={tag:#x} {p[12:44]!r}" if ok else f"{kn}:--")
        print(os.path.basename(f),len(b),' | '.join(res), '' if any('OK' in r for r in res) else 'head='+repr(b[:32]))
