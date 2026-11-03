# OpenPCLink

An unofficial macOS and Linux tuning client for Link G4X and G5 ECUs, written from a
reverse-engineered reading of PCLink 7.8.2. Not made or supported by Link Engine Management.

**Status: reads and changes parameters on a real G4X ECU in RAM; Store has not been run on real hardware.** See
[Verification](docs/VERIFICATION.md) and follow the first-connection steps below. A wrong setting can
damage an engine. To run with writes blocked at the protocol layer, start it with
`OPENPCLINK_READ_ONLY=1` in the environment.

## Install

Download from this repo's [v0.2.1 release](https://github.com/entitledosprey/openpclink/releases/tag/v0.2.1),
or with the GitHub CLI: `gh release download v0.2.1 -R entitledosprey/openpclink -p '<file>'`.

The packages contain Link's parameter data, so **keep this repo private and do not share them**;
they are not licensed for redistribution.

| System | File | Install |
|---|---|---|
| macOS (Apple Silicon) | `OpenPCLink-0.2.1-macos-arm64.dmg` | Open it, drag to Applications, then see below |
| Debian/Ubuntu x86-64 | `openpclink_0.2.1_amd64.deb` | `sudo apt install ./openpclink_0.2.1_amd64.deb` |
| Debian/Ubuntu ARM64 | `openpclink_0.2.1_arm64.deb` | `sudo apt install ./openpclink_0.2.1_arm64.deb` |
| Other Linux | `OpenPCLink-0.2.1-linux-*.tar.gz` | Unpack, run `OpenPCLink`, install `packaging/70-link-ecu.rules` to `/etc/udev/rules.d/` |

**macOS:** the app is not notarised, so a downloaded copy is blocked until you clear its quarantine flag.
This has not been tested on a second Mac:

```sh
xattr -dr com.apple.quarantine /Applications/OpenPCLink.app
```

**Intel Macs** need their own build; see below. The release also has the PCLink installer used for these
builds (`PCLink-EN-7.8.2.exe`) and the decompile workspace.

To rebuild, or to build on an Intel Mac:

```sh
brew install libusb innoextract   # innoextract is only for building from the installer
packaging/build_macos.sh /path/to/PCLink-folder-or-installer
packaging/build_linux.sh path/inside/repo/to/PCLink-folder amd64   # needs Docker
```

No driver is needed. G5 ECUs are USB serial devices; G4X ECUs use libusb, which is bundled.

## First connection

Do this on the bench or with the engine off, and with a copy of the tune saved from Windows PCLink.

1. Run `openpclink info`, or open the app and press **F3**. Compare firmware and serial with Windows PCLink.
2. Back up: **ECU > Back Up Settings** or `openpclink backup before.json`.
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
openpclink search "RPM Limit"
openpclink get "RPM Limit Mode"
openpclink set "RPM Limit Mode" 1
openpclink store
openpclink monitor "Engine Speed" MAP
```

Try it with no ECU: `openpclink sim` prints a connection to use with `--port` or the app's manual
address box. It is a simulator, so it says nothing about your ECU.

## Safety rules enforced in code

- **Firmware, bootloader and factory-reset commands cannot be sent.** The protocol layer has an allowlist.
- **Tuner password locks are respected.** A locked ECU shows live data only and refuses all writes.
  Password parameters cannot be read or written, and tuner-encrypted map files are refused.
- **Writes are validated first:** limits from Link's database, read-only flags, range of the stored type.
- **Writes are verified:** a value is only reported as set once the ECU confirms it.
- **The memory layout comes from the ECU itself**, and a malformed one refuses the connection.
- Number of Cylinders, Engine Type and Custom TDC are refused while the engine runs.

## Compared with PCLink

**Included:** ECU connection (G4X USB tested; G5 USB and WiFi untested), settings menus in PCLink's order,
per-ECU hiding, live editing, Store, runtime values tree, live list and dial gauges, statistics, tables with heat map, live
position, axis editing, bulk edits, copy/paste and 3D view, map files (open, save, compare, offline editing),
PC logging to CSV with a plot viewer, and Link's manual as text.

**Deliberately excluded, because they can brick or wipe an ECU and cannot be tested safely:** firmware
updates, Restore to Factory Settings, setting or removing passwords, ECU unlock codes.

**Not built yet:** most of PCLink's calibration, logging and display tools. [docs/PARITY.md](docs/PARITY.md) is the
full checklist.

**Rules:** the app runs PCLink's own `D_menu.dll` from your copy in a CPU emulator, for dependent hiding and messages.
When the rules want a related setting changed, the app asks you first. That has only been compared with real PCLink on
one form, so until wider comparison is done, **keep making mode and function changes in official PCLink**.

See [docs/PROTOCOL.md](docs/PROTOCOL.md) for the protocol.
