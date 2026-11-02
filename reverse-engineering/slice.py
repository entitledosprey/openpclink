import sys,re
lo,hi=int(sys.argv[1],16),int(sys.argv[2],16)
on=False
noise=re.compile(r'^\s*(undefined\d?|uint|int|char|short|bool|byte|ushort|code|float|double|longdouble|unkbyte\d+|ulonglong|longlong)\s+\*{0,2}\w+(\s*\[\w+\])?;$|local_[0-9a-f]+ = local_[0-9a-f]+ [-+] 1;|\*in_FS_OFFSET|FUN_008d4e3c\(\);|FUN_008d0798|local_\w+ = 0x[0-9a-f]+;$|\/\* WARNING')
for line in open('PCLink.c',errors='replace'):
    m=re.match(r'// ==== ([0-9a-f]+) ',line)
    if m:
        a=int(m.group(1),16); on=lo<=a<hi
    if on and line.strip() and not noise.search(line): sys.stdout.write(line)
