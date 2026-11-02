# What has and has not been verified

## Not verified: a real ECU

No ECU was available. **Nothing here has talked to real Link hardware**, so connecting and changing a
parameter on a real ECU is unproven. The USB paths have also never run: tests use pseudo-terminals
and UDP, so the G5 CDC and G4X FTDI (including its 4.7 Mbaud setting) are unexercised.

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
