import sys,re
want=set(a.lower().rjust(8,'0') for a in sys.argv[1:])
on=False
noise=re.compile(r'^\s*(undefined\d?|uint|int|char|short|bool|byte|ushort|code|float|double|longdouble|unkbyte\d+|ulonglong|longlong|HANDLE|DWORD)\s+\*{0,2}\w+(\s*\[\w+\])?;$|^\s*local_\w+ = local_\w+ [-+] 1;$|^\s*iStack\w+ = iStack\w+ [-+] 1;$|\*in_FS_OFFSET|FUN_008d4e3c\(\);|FUN_008d0798|^\s*local_\w+ = 0x[0-9a-f]+;$|/\* WARNING|^\s*(\{|\}|return;)$|^\s*FUN_008d4b7c\(\);$|^\s*FUN_00403f4c\(\);$|^\s*$')
for line in open('PCLink.c',errors='replace'):
    m=re.match(r'// ==== ([0-9a-f]+) ',line)
    if m: on=m.group(1) in want
    if on and not noise.search(line): sys.stdout.write(line[:110].rstrip()+'\n')
