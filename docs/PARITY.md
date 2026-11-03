# Feature parity with PCLink 7.8.2

Taken from the real program's menus, source modules and strings. Status means what exists here, and how it was checked:
**car** = on the real ECU, **PCLink** = compared with real PCLink under Wine, **sim** = simulator or tests only.

## ECU controls

| Feature | Status |
|---|---|
| Connect, disconnect, search (G4X USB) | Done: car |
| Connect (G5 USB, serial, WiFi, WiFi selection) | Built: sim only; WiFi selection missing |
| Change settings live | Done: car (text, number); sim and PCLink bytes (option, table) |
| Store to ECU | Built: PCLink capture; not run on the car |
| Undo Short Term Changes, Load from ECU, Download to ECU | Missing |
| Clear Fault Codes, Update and Clear Statistics | Missing (viewing is done) |
| ECU log setup and download | Missing |
| CAN setup dialog, CAN device, keypad and lambda forms | Missing (settings are in the menus) |
| TPS setup, MAP sensor and Baro calibration, base timing, injection setup | Missing |
| Trigger scope | Missing |
| Password setup and unlock, ECU and feature unlock | Missing: refused on purpose; a locked ECU stays locked |
| Update Firmware, Restore to Factory Settings | Missing: blocked at protocol level; cannot be tested on the owner's car |

## Settings

| Feature | Status |
|---|---|
| Menu tree, order, per-ECU hiding | Done: PCLink (top level and sampled forms) |
| Dependent hiding | Native, from a generated table: 208 of 224 forms match real PCLink on one sample map |
| Warnings and cross-field checks from the rules | Missing |
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
| PCLink log file format, log file manager | Missing |
| Navigator, XY plot, histogram, parameter list, drag and section tools, export | Missing |

## Other

| Feature | Status |
|---|---|
| Dyno comms | Missing |
| G5 comms-processor settings (from parameter 10000) | Missing |
