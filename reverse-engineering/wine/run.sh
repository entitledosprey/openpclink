#!/bin/bash
# $1 map xml, $2 hardware family
export DISPLAY=:9
Xvfb :9 -screen 0 1366x900x24 >/dev/null 2>&1 &
sleep 1
wineboot -i >/dev/null 2>&1; sleep 3
python3 -u /wine/harness.py "$1" "$2" > /out/sim.log 2>&1 &
for i in 1 2 3 4 5 6 7 8 9 10; do [ -f /tmp/pty ] && break; sleep 1; done
PTY=$(cat /tmp/pty); echo "pty=$PTY"
wine reg add 'HKLM\Software\Wine\Ports' /v COM1 /t REG_SZ /d "$PTY" /f >/dev/null 2>&1
wine reg add 'HKLM\HARDWARE\DEVICEMAP\SERIALCOMM' /v 'Serial0' /t REG_SZ /d COM1 /f >/dev/null 2>&1
rm -f $WINEPREFIX/dosdevices/com1; ln -s "$PTY" $WINEPREFIX/dosdevices/com1
wineserver -k; sleep 2
cp -r "/pclink" /tmp/app; cd /tmp/app
wine PCLink.exe > /out/wine.log 2>&1 &
shot(){ import -window root /out/$1.png; xdotool search --name '.' getwindowname %@ 2>/dev/null | sort -u | head -12 > /out/$1.txt; }
sleep 40; shot s1
sleep 1
sleep 100000
