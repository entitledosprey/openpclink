import sys,re,html
for f in sys.argv[1:]:
    s=open(f,'rb').read().decode('utf8','replace')
    s=re.sub(r'(?is)<(style|script|head)\b.*?</\1>',' ',s)
    s=re.sub(r'(?i)<(br|p|tr|li|div|h\d)\b[^>]*>','\n',s)
    s=re.sub(r'<[^>]+>',' ',s); s=html.unescape(s)
    s=re.sub(r'[ \t\xa0]+',' ',s); s=re.sub(r'\n\s*\n+','\n',s)
    print('=====',f); print(s.strip())
