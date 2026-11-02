# What has and has not been verified

## Verified on a real ECU, read-only

A Link G4X (FT232H, firmware 7.8.2, Link ID 23001) on Ubuntu 26.04 over USB. **By the owner's instruction,
nothing has been written to it.** Testing used read-only mode, which refuses writes before they reach the wire.

| Check | Result |
|---|---|
| Connects, identifies, loads the layout in about 1 s | Pass |
| Parsed layout ends exactly at the reported memory size (59,985 bytes, 4,389 parameters) | Pass |
| 18,606 values checked against Link's limits | 6 outside, all live values that look like "no data" placeholders |
| 1,770 bitfields hold a valid option | Pass |
| Soak: 1,644 exchanges unpaced, then 1,087 rounds at GUI pace | No failures after the first command |
| Tune saved from this ECU opened in real PCLink 7.8.2 under Wine, with plausible statistics and I/O names | Pass |

A rare truncated poll (`got 11 of 1954`) was seen once in the GUI and recovered by a retry. Its cause is unknown.

## Not verified

- **Any write or Store on real hardware.** Everything about writing rests on the simulator and on comparing bytes
  with real PCLink. Real write replies have never been seen.
- G5 USB, serial and WiFi on real hardware.
- That displayed values match Windows PCLink on the same ECU, side by side. The Wine check is close to this,
  but used a saved copy.

## Verified against real PCLink.exe 7.8.2

The unmodified program ran under Wine with a simulated ECU on a virtual COM port. Its requests were logged.

| Check | Result |
|---|---|
| Connects and shows Online at about 100 Hz (G4X family mode) | Pass |
| Same in G5 family mode, with the Neo 4 map | Pass |
| Decodes and displays values from the simulator's descriptor | Pass |
| Connect sequence matches the decompile | Pass |
| Enum write payload `37b3000120` identical to this client's | Pass, regression test |
| Numeric write payload `378e0002007b` identical to this client's | Pass, regression test |
| Store is `0x20` | Pass. **Corrected an error from the decompile** |
| `0x21` is Restore to Factory Settings | Pass. **This client's first Store used it** |
| Password storage and lock behaviour | Pass. **Corrected the lock detection** |
| Padlocks disappear with the engine stopped | Pass |

This shows the simulator satisfies real PCLink, and that this client sends what PCLink sends. It does
not show a real ECU replies the way the simulator does. Most likely points of difference: the exact
contents of write replies (handled by reading back if not an echo), timing, and error codes.

## Known gaps

- Dependent-setting rules and cross-field checks in `D_menu.dll` are not ported, so forms can show settings PCLink
  hides, and invalid combinations are not caught.
- Only 3 engine-running locks were observed, on one form. PCLink's rule was not recovered, and other
  settings may be padlocked there but **writable here with the engine running**.
- Parameter limits come from Link's database; PCLink may apply further checks in its forms that were
  not recovered, including warnings about setting combinations.
- WiFi reply format and discovery were not checked against real PCLink. Only the simulator was used.
- Packages were tested on macOS 27 arm64 and clean Debian 12 amd64/arm64 in containers. The GUI there
  ran headless, without a real display.

## Automated tests

`pytest` covers the codec, parser, session, safety rules and GUI, and runs against the real data files
when `work/` holds an extracted PCLink. To repeat the Wine check, see `reverse-engineering/wine/`.
