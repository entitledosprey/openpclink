# OpenPCLink

An unofficial macOS and Linux tuning client for Link G4X and G5 ECUs, written from a
reverse-engineered reading of PCLink 7.8.2. Not made or supported by Link Engine Management.

**Status: every menu item in PCLink now exists, but this is not yet at parity, and much of it is unproven.** Reading a
real G4X and a single reverted change on v0.2.0 are the only things that have run on real hardware. Everything added
since, including firmware update, factory reset, Quick Tune and all the new writes, has only run against a simulator, much
of it compared with real PCLink under Wine. A wrong setting can damage an engine. To run with writes blocked at the
protocol layer, start it with `OPENPCLINK_READ_ONLY=1` in the environment.

See [what is done and what is left](#what-is-done-and-what-is-left), [the full checklist](docs/PARITY.md) and
[Verification](docs/VERIFICATION.md).

## Install

Download from this repo's [v0.5.0 release](https://github.com/entitledosprey/openpclink/releases/tag/v0.5.0),
or with the GitHub CLI: `gh release download v0.5.0 -R entitledosprey/openpclink -p '<file>'`.

| System | File | Install |
|---|---|---|
| macOS (Apple Silicon) | `OpenPCLink-0.5.0-macos-arm64.dmg` | Open it, drag to Applications, then see below |
| Debian/Ubuntu x86-64 | `openpclink_0.5.0_amd64.deb` | `sudo apt install ./openpclink_0.5.0_amd64.deb` |
| Debian/Ubuntu ARM64 | `openpclink_0.5.0_arm64.deb` | `sudo apt install ./openpclink_0.5.0_arm64.deb` |
| Other Linux | `OpenPCLink-0.5.0-linux-*.tar.gz` | Unpack, run `OpenPCLink`, install `packaging/70-link-ecu.rules` to `/etc/udev/rules.d/` |

**macOS:** the app is not notarised, so a downloaded copy is blocked until you clear its quarantine flag.
This has not been tested on a second Mac:

```sh
xattr -dr com.apple.quarantine /Applications/OpenPCLink.app
```

**Intel Macs** need their own build; see below. The release also has the PCLink installer used for these
builds (`PCLink-EN-7.8.2.exe`) and the decompile workspace.

To rebuild, or to build on an Intel Mac:

```sh
brew install libusb innoextract p7zip   # innoextract is only for building from the installer; 7z unpacks the manual and its pictures
packaging/build_macos.sh /path/to/PCLink-folder-or-installer
packaging/build_linux.sh path/inside/repo/to/PCLink-folder amd64   # needs Docker
```

The build bakes in PCLink's layouts, CAN setups, firmware package, view layouts, parameter config, logging
options and manual pictures, and stops if any are missing. `OpenPCLink check --bundled` reports what a build holds.
Data imported by an older OpenPCLink lacks the firmware, CAN keys and layouts: the app offers to import again at
start-up, or run `openpclink import <installer or folder>` and then `openpclink check`.

No driver is needed. G5 ECUs are USB serial devices; G4X ECUs use libusb, which is bundled.

## First connection

Do this on the bench or with the engine off, and with a copy of the tune saved from Windows PCLink.

1. Run `openpclink info`, or open the app and press **F3**. Compare firmware and serial with Windows PCLink.
2. Back up: **ECU Controls > Back Up Settings** or `openpclink backup before.json`.
3. Compare several familiar values with Windows PCLink, **including a negative one and a table**.
   If any differ, stop and do not write anything.
4. Change one harmless value and read it back. Do not store. Power-cycle: it should revert.
5. Change it again, press **F4**, power-cycle: it should remain.

## Use

On macOS the terminal command is inside the app:
`alias openpclink=/Applications/OpenPCLink.app/Contents/MacOS/OpenPCLink`

In the app: pick a menu entry, edit a value, press Enter or Tab. Changes are live in ECU RAM until
**F4** stores them. From a terminal:

```sh
openpclink ports
openpclink check
openpclink search "RPM Limit"
openpclink get "RPM Limit Mode"
openpclink set "RPM Limit Mode" 1
openpclink store
openpclink monitor "Engine Speed" MAP
```

Try it with no ECU: `openpclink sim` prints a connection to use with `--port` or the app's manual
address box. It is a simulator, so it says nothing about your ECU.

## Safety rules enforced in code

- **Read-only mode** (`OPENPCLINK_READ_ONLY=1`) blocks everything that can change an ECU, including all the commands below.
- **Firmware and factory-reset commands go through one separate path**, and only the updater and factory reset call it.
  Unidentified commands cannot be sent at all, and the code that disables an ECU's features is refused.
- **Firmware files are checked before the ECU is touched.** A file failing any check is refused; PCLink ignores failed checks.
- **Password locks are respected.** A locked ECU shows live data only until someone enters its password. There is no way
  round that here, and a file locked to another ECU's serial stays closed.
- **Writes are validated first:** limits from Link's database, read-only flags, range of the stored type.
- **Writes are verified:** a value is only reported as set once the ECU confirms it.
- **The memory layout comes from the ECU itself**, and a malformed one refuses the connection.
- Number of Cylinders, Engine Type and Custom TDC, calibrations and factory reset are refused while the engine runs.

## What is done and what is left

Nothing in this section has run on a real ECU unless it says so.

### Done

**Run on a real G4X:** connecting, reading settings and live values, the settings menus, statistics, and tables (display only).
A text setting was changed in RAM and reverted on v0.2.0. Store has never been run on real hardware.

**Compared byte for byte with real PCLink, against the simulator:**
- Log files in both directions, and CSV export of a full log
- ECU log download commands, with its decoded channels and times
- ECU logging setup, and log set files in both directions
- Firmware update on a G5-family ECU, and the factory reset order
- Passwords, ECU unlock and memo
- Table edits and axis changes, for the keys and sizes compared
- Trigger Scope, TPS span, MAP calibration and base timing
- CAN setup writes, stream data and custom CAN files
- G5 connection order

**Compared by figures or by eye with real PCLink:** log statistics, Quick Trim, Mixture Map (two cells), Compare, pages and
layouts (all 8 shipped layouts load), gauges, the Options dialog, colour themes (Black only), event log, fault banner.

**Simulator only:** Save as Encrypted, baro calibration, WiFi selection.

### Left to do

**Needs a real ECU, and the owner's say-so:**
- A read-only check of this version on a G4X. Its connection code has changed since it last ran on a car
- Store, and writes through the new tools
- How an ECU really behaves in boot mode, and firmware timings. Firmware update and factory reset will not be tested on the
  owner's car
- Anything at all on a G5 or with a Mac

**Unproven against real PCLink:**
- Quick Tune: real PCLink never reached Ready under Wine, so the values it writes have not been compared
- Imperial units: factors are unit-tested only
- Loading a map: real PCLink rewrote more cells than this does (334 against 6 changed), unexplained
- E-throttle 2 and APS calibration flows, CAN device search onwards, cancelling an ECU log download
- Blue, White and Gray themes, and the Help window

**Missing or different:**
- CAN: most Link device settings (their definition file is not in this install), device names, keypad and lambda details are partly inferred
- Knock Audio: FFT window, scaling and draw rates are guesses
- G5: logging pages and log info, and the Network page's layout
- Tables: Compare in table cells, PCLink's export format, Ctrl+Z beyond one edit, setting an axis to None, Shift+F and Shift+I
- Table Axis Setup shows base units
- Logging: live plot while recording, the Data Rate label, some parameter tree groups, old `.txt` log import
- Dial and Curve gauges are fitted to pictures, and pages scale to fit instead of scrolling
- Live values update at about 10 Hz here, against about 100 Hz in real PCLink

**Rules:** dependent-setting rules run natively from a data table generated at build time from your copy of PCLink.
They cover hiding, warnings, unit changes and menu moves. Related-setting changes are not included, and accuracy on a
fully configured tune is lower than on the sample maps, so **keep making mode and function changes in official PCLink**.

See [docs/PROTOCOL.md](docs/PROTOCOL.md) for the protocol.
