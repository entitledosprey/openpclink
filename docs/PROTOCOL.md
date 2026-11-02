# Link ECU protocol

Recovered from `PCLink.exe` 7.8.2 (Ghidra 12.2), then checked by running the real program against a
simulator. Addresses are virtual addresses in that build. Integers are big-endian unless noted.

## Transports

| Link | Identification | Settings |
|---|---|---|
| G5 USB | CDC-ACM, `0483:A451` (`MI_00` ECU, `MI_02` COMS) | baud ignored |
| G4X USB | FTDI `0403:7068`–`706F` | 8N1; FT232H 4,700,000 baud, X-series 1,171,875 |
| Serial | RS-232 | default 1,171,875, fallback 585,937 |
| G5 WiFi | UDP port 10470 | text `Discover`/`Connect`/`Disconnect`, then frames |

## Frames (`0x426200`, `0x426598`)

```
Request  5A addr cmd:u16 len:u16 payload crc:u16     len = payload + 2
Reply    addr 5A|01 cmd:u16 len:u16 payload crc:u16
```

CRC-16/XMODEM over the frame (`0x47D754`). Reply command `0x19` carries a u16 error code.
`addr` selects a processor: `0x5B` main, `0x7D` comms (G5).

## Commands

| Cmd | Purpose | Request | Used here |
|---|---|---|---|
| 00 | Read config | offset:u16 size:u16 | yes |
| 01 / 0D | Read descriptor (legacy / chunked) | 0D: offset:u16 size:u16 | yes |
| 02 / 08 | Write config / section 2 | offset:u16 size:u16 data | yes |
| 03 | State, u16: 1 running, 3 bootloader | | yes |
| 09 | Poll runtime block | | yes |
| 0B | Sizes: `[4]` config, `[6]` runtime, `[8]` section 2, `[0xB]` log2 buffer, `[0x12]` max id | | yes |
| 16 / 18 | Firmware / boot info block, CRC-protected | | yes |
| 1A | Identity | | yes |
| **20** | **Store to flash** (seen from real PCLink) | | yes |
| **21** | **Restore to factory settings** (seen from real PCLink) | | **forbidden** |
| 32 | Read section 2 | | yes |
| 10, 11, 12, 14, 15, 22 | Firmware update | | **forbidden** |

Memory is addressed as `[runtime][config][section 2]`. Config and section 2 offsets are relative to
the start of their own section.

## Connect sequence

`03 → 18 → 16 → 1A → 0B → 0D… → 00… → 32 → 09`, then `09` polled. G5 adds `7D` queries that this
client does not need. Observed unchanged from real PCLink.

## Descriptor (`0x42B678`)

A stream of entries assigned consecutive addresses by a cursor.

```
id_word:u16  id = word >> 2, bit 1 = read-only
info:u8      type = info >> 4
```

| Type | Rest of entry | Meaning |
|---|---|---|
| 3, 5 | options:u8 | Bitfield of `info & 0xF` bits, allocated from the MSB, sharing bytes |
| 8 | count:u8 | Text or bytes, width `info & 0xF` |
| 9 | one ignored byte | Unscaled scalar, width from bits 2–3 (0 = 4), signed bit 1 |
| 6, 7, ≥10 | none | No storage |
| others | count:u8, then if `info & 1 == 0`: scale:f32 add:f32; type 1 with count 0: count:u32 | Scalar/array. `info & 1` = float (width 4) |

ID 0 is padding. IDs at or above the ECU's max id are skipped without advancing the cursor. Any
non-bitfield entry first advances past a partly-used bitfield byte.

`value = raw × scale + add`. Tables are row-major, `index = row × columns + column`.

## Passwords

Client-side in PCLink. The password is stored as u16 characters **in plain text** in parameter 2294,
with the entered password in 2295. The ECU counts as locked when any character of 2294 is above zero
and differs from 2295 (`0x43359C`). 2303 and 2324 hold related state.

## Data files (`0x4516D4`, `0x451988`)

XXTEA over the whole file. Plaintext header, little-endian: timestamp, size, tag. Non-zero tag means
tuner-encrypted, which this project refuses to open. Keys are read from the user's `PCLink.exe`.

`ECU.cfgr` is XML describing each DataId. `Languages.cfgr` is UTF-16 strings. `Parent` is string IDs
joined by `\_`, and `CopyFromID` inherits properties from another parameter.
