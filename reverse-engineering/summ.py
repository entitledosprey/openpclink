import xml.etree.ElementTree as ET,collections,sys
r=ET.parse(sys.argv[1]).getroot()
print(r.tag,r.attrib)
def walk(e,d=0):
    c=collections.Counter(x.tag for x in e)
    for t,n in c.items():
        x=e.find(t)
        print('  '*d+f"{t} x{n} attrs={list(x.attrib)[:14]} sample={dict(list(x.attrib.items())[:4])}"[:220])
        if d<3: walk(x,d+1)
walk(r)
if r.find('Parameters') is not None:
    ps=collections.Counter();vals=collections.defaultdict(collections.Counter)
    for p in r.find('Parameters'):
        for q in p.iter('Property'):
            ps[q.get('Name')]+=1; vals[q.get('Name')][q.get('Value')]+=1
    for n,c in ps.most_common(70): print(n,c,vals[n].most_common(6) if len(vals[n])<40 else list(vals[n].most_common(3)))
