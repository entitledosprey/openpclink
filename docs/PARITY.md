# Feature parity with PCLink 7.8.2

Every item in real PCLink's menus now exists here. That is not the same as proven. The tables say how each area was checked:

- **Bytes**: the frames or file bytes were compared with real PCLink under Wine, against the simulator.
- **Numbers**: decoded results or displayed figures were compared with real PCLink.
- **Look**: compared by eye with real PCLink or its manual's pictures.
- **Sim**: run against the simulator and tests only, so it rests on the decoded code.

**Nothing added since v0.4.0 has run on a real ECU.** Only reading a G4X, and one reverted label change on v0.2.0, have. The simulator encodes this project's reading of the protocol.

## Menus

| Menu | Items |
|---|---|
| File | Open, Save, Save as, Save as Encrypted, Compare, Compare Close, Show Memo, Exit |
| Options | Connection, Preferences, Directories, Colour Theme, Units, Language (English is all that ships), Setup ECU Parameters |
| ECU Controls | Connect, Search, Update Firmware, Clear Fault Codes, ECU Log File Setup and Download, CAN Setup, Statistics, Store, ECU Unlock, Restore to Factory Settings, Setup and Unlock Password, TPS Setup, MAP Calibration, Trigger Scope |
| Display Mode | ECU, Log Analysis, Full Screen with Menus, Full Screen |
| Layout | New, Load, Save As, New, Delete and Rename Page, page list |
| View | ECU Settings, Log File Manager, Parameters, Event Log, Digital, Bar, Curve, Dial and Target Gauge, Runtime Value List, Help Browser, Vehicle Plan |
| Logging | Open, Save, Save As, Save All, Close, Close All, Details, Export, Play, Record, Stop, Setup, ECU Download, Navigator, Time Plot, Logged Values, XY Plot, Vehicle Plan, Parameter List, Histogram, Statistics, GPS Plot |
| Tuning | Runtime Values, Quick Tune, Mixture Map, Knock Audio, Reset Tuning Markers, Undo Short Term Changes |
| Help | Contents, Keyboard Shortcuts, Wiring Information, ECU Fault Codes, ECU Information, About |

Extra here, not in PCLink's menus: Baro Calibration, APS and E-Throttle Calibration (PCLink reaches these from settings), Back Up Settings.

## How each area was checked

| Area | Checked | Still different or unproven |
|---|---|---|
| Log files (.llg5, .llgx) | Bytes both ways: a real log read here, a log written here opened in real PCLink | `.txt` import |
| CSV export | Bytes on Full Log | Section and Zoom ranges |
| Log statistics | Numbers | |
| ECU log download | Bytes for commands; Numbers for channels, times, markers | Cancel prompts; `ECU` detail field blank |
| Logging setup, .lls5 | Bytes: 507 of 507 frames; files exchanged both ways | Data Rate label formula; no live plot while recording; some tree groups |
| Firmware update | Bytes: 1739 of 1739 frames on G5 family | Real boot-mode replies and timings unknown. Stops at the first engine-running refusal, where PCLink retries about 30 times |
| Factory reset | Bytes for order | No hardware test, by the owner's choice |
| Passwords, ECU Unlock, memo | Bytes | Temporary unlock always ends on reconnecting |
| Load a map | Bytes for cells that differ | Real rewrote more cells (334 against 6 changed): unexplained, may be a simulator effect |
| Save as Encrypted | Sim and spec | Locks to a serial; not strong encryption, and PCLink's isn't either |
| Compare | Look | Single values only; tables show a count |
| Table edits, axis changes | Bytes for keys and sizes compared | Copy and export use this project's format; Ctrl+Z undoes one edit; an axis cannot be set to None; Shift+F and Shift+I missing |
| 3D view | Look, from manual pictures | Wine has no OpenGL |
| Gauges, value list, pages | Look; all 8 shipped layouts load | Dial and Curve geometry fitted to pictures; pages scale to fit instead of scrolling |
| Trigger Scope | Bytes and Numbers | |
| TPS span, MAP, base timing | Bytes | Base timing skips PCLink's brief write of 0 on opening, on purpose |
| E-throttle, APS | Bytes for what was reached | Second throttle and APS flows unchecked |
| Baro | Sim | |
| CAN setup | Bytes for Apply, stream data and .lcf | Device field lists missing (their definition file is not in this install); keypad and lambda partly inferred; devices shown by code, not name |
| Quick Trim | Numbers | |
| Quick Tune | Look, statuses up to lockouts | **Real PCLink never reached Ready under Wine, so written values are not compared.** Formula from decoded code |
| Mixture Map | Numbers in two cells | Filters, AFR display |
| Knock Audio | Look | FFT window, scaling and draw rates are guesses; not tried with a microphone signal |
| Options, themes | Look (Black theme) | Blue, White, Gray not compared side by side. Windows Font applies at once |
| Units | Unit tests of the decoded factors | **Not compared with real PCLink in Imperial.** Table Axis Setup shows base units |
| Help, event log, fault banner | Look | Real Help window never opened under Wine |
| G5 connection, comms settings | Bytes for connect order | Value types guessed; G5 logging pages and its log info not done; Network page nests differently |
| WiFi selection | Sim | Reply format unverified |
| Packaging | v0.5.0: macOS, Linux amd64 and arm64 builds each connected to the simulator; Linux .debs installed in Docker | Not installed on a real Linux desktop or a second Mac |
| Live update rate | | About 10 Hz here, about 100 Hz in real PCLink |
| Mac or G5 with a real ECU | Never | No hardware |

## Deliberately stricter than PCLink

- Loading a map never overwrites an ECU's password settings; a locked ECU refuses loads and saves.
- A file locked to another serial stays closed and shows no values.
- Damaged firmware files are refused before any traffic; PCLink ignores failed checks.
- The Target gauge's pointer accounts for its minimum, which PCLink's code omits.

## Not built

PCLink's dead code, which has no way in: Injection Setup and Dyno Comms forms. Dyno data arrives through ordinary CAN streams. Internal tools, such as the unlock code generator and the feature lock, are left out on purpose.
