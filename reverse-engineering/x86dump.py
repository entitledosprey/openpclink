import sys,pefile,capstone
pe=pefile.PE("extracted/app/PCLink G5/PCLink.exe")
start=int(sys.argv[1],16); n=int(sys.argv[2],0)
data=pe.get_data(start-0x400000,n)
md=capstone.Cs(capstone.CS_ARCH_X86,capstone.CS_MODE_32)
for i in md.disasm(data,start): print(f"{i.address:08x} {i.mnemonic} {i.op_str}")
