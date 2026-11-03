# What has and has not been verified

## Verified on a real ECU

A Link G4X (FT232H, firmware 7.8.2, Link ID 23001) in the owner's daily-driven car, on Ubuntu 26.04 over USB.
Everything except the write test below used read-only mode, which refuses writes before they reach the wire.

| Check | Result |
|---|---|
| Connects, identifies, loads the layout in about 1 s | Pass |
| Parsed layout ends exactly at the reported memory size (59,985 bytes, 4,389 parameters) | Pass |
| 18,606 values checked against Link's limits | 6 outside, all live values that look like "no data" placeholders |
| 1,770 bitfields hold a valid option | Pass |
| Soak: 1,644 exchanges unpaced, then 1,087 rounds at GUI pace | No failures after the first command |
| Tune saved from this ECU opened in real PCLink 7.8.2 under Wine, with plausible statistics and I/O names | Pass |

A rare truncated poll (`got 11 of 1954`) was seen once in the GUI and recovered by a retry. Its cause is unknown.

### Write test (owner approved; engine stopped, ignition on, RAM only)

| Check | Result |
|---|---|
| Text setting (Description) changed, read back from the ECU | Pass |
| Number setting (GP RPM Limit 2 Cut Decay Time, limit off) changed, read back from the ECU | Pass |
| Only bytes inside those settings changed | Pass |
| Write replies are an exact echo of the data | Confirmed, as the simulator assumed |
| Reverted: all 58,033 config bytes match the snapshot | Pass, 0 differences |
| Text change repeated through the packaged 0.2.0 Linux build (unpacked `.tar.gz`), reverted | Pass |
| Same change through the `.deb` installed with `apt` on Ubuntu 26.04, reverted | Pass, 0 tune bytes differ |
| Tune unchanged after a power cycle and an engine start | Pass, only the ECU's statistics counters moved |

**Open question:** a full read straight after the unpacked-build test showed 2 bytes differing from the snapshot, and
five further reads showed none. Which bytes they were was not captured, so the cause is unknown. Later, Engine
Running Time and Num of Starts were seen to change by themselves, so comparisons now ignore statistics, but those
changes persisted and that earlier one did not, so this is not a proven explanation.

**Fixed in 0.2.1:** with the GUI running in the same process, about 20% of polls lost bytes, and a shorter thread
switch interval did not help. Running the FTDI link in its own process cut that to under 1% (2-3 of about 350),
with retries recovering those. The cause of the remainder is unknown.

## Not verified

- **Store on real hardware.** It was deliberately not run on this car. It rests on the real PCLink capture.
- Writes to bitfields and tables on real hardware (byte-compared with real PCLink, not run on the car).
- Writing with the engine running, or through the GUI on real hardware.
- G5 USB, serial and WiFi on real hardware, and any Mac with a real ECU.
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

- `D_menu.dll` now runs in an emulator, and its hiding matches real PCLink on the RPM Limit form in both states. Other
  forms have not been compared. It also asks for related changes: 2754 (RPM Limit Mode) set to 1 asks for 3460 = 56. Real
  PCLink under Wine, same map and change, wrote only the mode byte and T1 stayed OFF. Why is not known, so the app logs
  these and never sends them. Any related changes PCLink really makes are therefore missing here.
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
