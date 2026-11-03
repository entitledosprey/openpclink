# Feature parity with PCLink 7.8.2

Taken from the real program's menus, source modules and strings. Status means what exists here, and how it was checked:
**car** = on the real ECU, **PCLink** = compared with real PCLink under Wine, **sim** = simulator or tests only.

## Real PCLink's menus, item by item

Read from real PCLink 7.8.2 under Wine. ✅ exists here, 🟡 partial, ❌ missing. A tick says it exists, not that it has run on a real ECU; the sections below say how each was checked.

| Menu | Items |
|---|---|
| File | ✅ Open, ❌ Save, ✅ Save as, ❌ Save as Encrypted, ✅ Compare, ❌ Compare Close, ❌ Show Memo, ❌ Exit |
| Options | 🟡 Connection, ❌ Preferences, ❌ Directories, ❌ Colour Theme, ❌ Units, ❌ Language, ❌ Setup ECU Parameters |
| ECU Controls | ✅ Disconnect, ✅ Search, ❌ Update Firmware, ✅ Clear Fault Codes, ❌ ECU Log File Setup, ❌ ECU Log File Download, ❌ CAN Setup, ✅ Show Statistics, ✅ Clear Statistics, ✅ Store, ❌ ECU Unlock, ❌ Restore to Factory Settings, ❌ Setup Password, ❌ Unlock Password, 🟡 TPS Setup (no electronic throttle), ✅ MAP Sensor Calibration, ❌ Trigger Scope |
| Display Mode | ✅ ECU, 🟡 Log Analysis, ❌ Full Screen with Menus, ❌ Full Screen |
| Layout | ❌ New, ❌ Load, ❌ Save As, ❌ New Page, ❌ Delete Page, ❌ Rename Page, ❌ Configuration, ❌ Tuning, ❌ Logging, ❌ Mixture Map |
| View | ✅ ECU Settings, ❌ Log File Manager, ✅ Parameters, ❌ Event Log, ❌ Digital Gauge, ❌ Bar Gauge, ❌ Curve Gauge, ✅ Dial Gauge, ❌ Target Gauge, ✅ Runtime Value List, 🟡 Help Browser, ❌ Vehicle Plan |
| Logging | 🟡 Open Log File, ❌ Save Log File, ❌ Save As, ❌ Save All, ❌ Close, ❌ Close All, ❌ Log File Details, ❌ Export, ❌ Play, ✅ Record, ✅ Stop, ❌ Setup Logging, ❌ ECU Log File Download, ❌ Navigator, ✅ Time Plot, ❌ Logged Values List, 🟡 XY Plot, ❌ Vehicle Plan, ❌ Parameter List, 🟡 Histogram, ❌ Statistics, ❌ GPS Plot |
| Tuning | ✅ Runtime Values, ❌ Quick Tune, ❌ Mixture Map, ❌ Knock Audio, ❌ Reset Tuning Markers, ❌ Undo Short Term Changes |
| Help | ✅ Contents, ❌ Keyboard Shortcuts, ❌ Wiring Information, ❌ ECU Fault Codes, ❌ ECU Information, ❌ About |

## ECU controls

| Feature | Status |
|---|---|
| Connect, disconnect, search (G4X USB) | Done: car |
| Connect (G5 USB, serial, WiFi, WiFi selection) | Built: sim only; WiFi selection missing |
| Change settings live | Done: car (text, number); sim and PCLink bytes (option, table) |
| Store to ECU | Built: PCLink capture; not run on the car |
| Undo Short Term Changes, Load from ECU, Download to ECU | Missing |
| ECU status and fault codes window | Done: sim |
| Fault code in the status bar | Done: sim |
| Clear Fault Codes, Clear Statistics | Built: commands captured from PCLink; not run on the car |
| Update Statistics | Missing |
| ECU log setup and download | Missing |
| CAN setup dialog, CAN device, keypad and lambda forms | Missing (settings are in the menus) |
| TPS setup (non-electronic throttle), MAP sensor calibration | Built: commands captured from PCLink; not run on the car |
| E-throttle calibration, Baro calibration, base timing, injection setup | Missing |
| Trigger scope | Missing |
| Password setup and unlock, ECU and feature unlock | Missing: refused on purpose; a locked ECU stays locked |
| Update Firmware, Restore to Factory Settings | Missing: blocked at protocol level; cannot be tested on the owner's car |

## Settings

| Feature | Status |
|---|---|
| Menu tree, order, per-ECU hiding | Done: PCLink (top level and sampled forms) |
| Dependent hiding | Native, from a generated table: 242 of 252 forms match real PCLink on one sample map (6 differ, 4 missing) |
| Warnings and cross-field checks from the rules | Native: 6 messages from the DLL; not yet compared with real PCLink on screen |
| Units and menu moves from the rules | Native: from the DLL; not yet compared with real PCLink |
| Related changes from the rules | Missing: real PCLink did not make the one tested |
| Search | Done: sim (name search only; PCLink's keywords not used) |
| Tables: heat map, live cell, bulk edit, copy and paste, 3D, axis editing | Done: car (display), sim (edits) |
| Axis setup form, Quick Tune, Quick Trim, snake trails, tuning markers | Missing |
| Bit-field picker, memo editor | Missing |

## Files

| Feature | Status |
|---|---|
| Open, save, compare maps; offline editing | Done: PCLink opened a saved map |
| Save as Encrypted, encrypted file editor | Missing; encrypted maps refused |
| Load a map into the ECU | Missing |

## Views and display

| Feature | Status |
|---|---|
| Live list, runtime tree, statistics, dial gauges | Done: car |
| Digital, bar, curve, target gauges | Missing |
| Layouts, pages, pop-outs, colour themes, language | Missing |
| Mixture map, vehicle plan, engine timing diagram, table graph, GPS | Missing |
| Knock audio | Missing |
| Help browser | Text only, without images |
| Unit options, Preferences | Missing (metric only) |

## Logging

| Feature | Status |
|---|---|
| PC logging to CSV, time plot | Done: sim |
| XY plot, histogram | Partial: no colour channel, manual ranges or mixture map |
| PCLink log file format, log file manager | Missing |
| Navigator, parameter list, drag and section tools, export | Missing |

## Other

| Feature | Status |
|---|---|
| Dyno comms | Missing |
| G5 comms-processor settings (from parameter 10000) | Missing |
