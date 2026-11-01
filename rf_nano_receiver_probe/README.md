# RF-Nano receive-only radio probe

> Update 2026-11-02: the [local web controller](../web_control/README.md) is ready.
> The reusable [controller firmware](../rf_nano_controller/README.md) is now
> installed on the RF-Nano; its USB handshake was verified without motion.
> Computer tests now confirm tower rotation both ways, main-boom raise/lower,
> and each track forward/reverse plus both forward together with self-stop.
> The preferred boom-lower `70` moves only the main boom down; historical
> `72` retains possible coactuation uncertainty. The web service detects USB
> readiness and separately asks the user to confirm tractor power.
> This page retains the passive investigation and receive-only source. Full
> radio captures and unrelated traffic analyses stay in the local archive; the
> public repository includes [curated target frames](../rf_nano_controller/tests/fixtures/hs6200_captured_frames.json).
> See [current status](PROGRESS_RU.md) before using the board.

Firmware `rfprobe-v2` measures strong 2.4 GHz energy with the nRF24L01+ RPD
bit. It has a full-band scan, fixed-channel energy sampling, and an
experimental RAW fragment capture. An offline decoder validates an observed
HS6200-style frame envelope. Later transmitting tests established the limited
control mappings recorded in the [catalogue](../rf_nano_tx_test/control_catalogue.json).
This receive-only sketch has no RF-transmit implementation. Wi-Fi and other nearby radios can trigger RPD.

On 2026-10-31 the connected CH340 device was `/dev/cu.usbserial-14220`.
The RF-Nano answered `radio=ready`; firmware v2 was compiled and uploaded.
Serial port names can change after reconnecting. The host console detects a
single `/dev/cu.usbserial-*` automatically; use `--port` when several exist.

The photographed older micro-USB RF-Nano uses CE=D10, CSN=D9. RF-Nano V3
boards use different pins. This board uses the Nano `atmega328` new bootloader;
the `atmega328old` upload profile did not synchronize in the earlier session.

## Build and upload

With the Arduino AVR core and RF24 library installed:

```sh
~/.local/bin/arduino-cli compile \
  --fqbn arduino:avr:nano:cpu=atmega328 \
  --build-path /private/tmp/traktor-rfprobe-v2-build \
  rf_nano_receiver_probe

~/.local/bin/arduino-cli board list

# Choose the CH340 port shown above; this is the port observed on 2026-10-31.
~/.local/bin/arduino-cli upload \
  --fqbn arduino:avr:nano:cpu=atmega328 \
  --port /dev/cu.usbserial-14220 \
  --input-dir /private/tmp/traktor-rfprobe-v2-build \
  rf_nano_receiver_probe
```

Firmware v2 starts **stopped**. The console waits for startup, verifies STATUS,
sets mode/channel with acknowledgements, and starts a capture explicitly. The
capture duration excludes startup. On exit it requests `SCAN OFF` and waits for
the final partial dwell report and acknowledgement. Labels and timestamped raw
records are saved when `--output` is supplied. Create the output directory first.

```sh
mkdir -p rf_nano_receiver_probe/captures

# Check the board without starting radio measurements.
/usr/bin/python3 rf_nano_receiver_probe/serial_console.py \
  --command STATUS --seconds 1

# Listen repeatedly on the previous candidate, 2403 MHz.
/usr/bin/python3 rf_nano_receiver_probe/serial_console.py \
  --mode dwell --channel 3 --seconds 30 --summary \
  --label remote_on_idle \
  --output rf_nano_receiver_probe/captures/on_idle_ch3.log

# Scan all 84 channels, 2400-2483 MHz.
/usr/bin/python3 rf_nano_receiver_probe/serial_console.py \
  --mode scan --seconds 30 --summary --label remote_on_idle
```

## Measurement method and interpretation

The old v1 scanner waited 10 ms and read RPD near the end of each channel
visit. That wait does not integrate energy over the entire 10 ms. Nordic
specifies an instantaneous RPD reading during RX, with a latched value at the
end of the receive period when CE goes low. RPD becomes valid after
130 + 40 microseconds. See section 6.4, printed page 25 of the
[Nordic product specification](https://devzone.nordicsemi.com/cfs-file/__key/communityserver-discussions-components-files/4/nRF24L01P_5F00_PS_5F00_v1.0.pdf).

V2 repeats 250-microsecond CE-high receive intervals and reads RPD immediately
before and after lowering CE. A positive result from either read counts as one
positive sample. Scan mode takes 32 samples per channel and reports only
completed sweeps. Dwell mode stays on one selected channel and reports
`samples`, `hits`, and measured `elapsed_ms` about once a second. SPI, serial
output, and loop overhead affect the actual sampling frequency. These are
intermittent energy samples, not continuous coverage or packet counts.

Auto-ACK is disabled and the TX FIFO is flushed. `PRIM_RX` remains set while
CE is toggled. In the energy modes all receive pipes are closed. No transmit API is called. Closing
pipes keeps packet reception out of the energy measurement. The firmware has
no movement or radio-transmit command. Serial commands are `PING`, `STATUS`,
`SCAN ON`, `SCAN OFF`, `MODE SCAN`, `MODE DWELL`, `MODE RAW`, `CHANNEL 0..83`,
and `HELP`. `CHANNEL` selects the dwell channel. In RAW mode it switches to
fixed-channel listening; without `CHANNEL`, RAW scans the full range. Sending
`MODE RAW` again restores full-band RAW scanning.
The host validates commands and requires firmware v2.

For a comparison, keep the RF-Nano and remote in the same position, around
5-10 cm apart. Take equal-length captures for remote off, on with controls
released, and one held control; repeat the off/on comparison. Record the
excavator's power state too, since its own radio could be a source. Keep wheels
and moving parts clear when operating the original controller.

Compare positive samples divided by total samples, rather than absolute hit
counts from captures of different lengths. `SUMMARY` counts positive channel
visits per completed sweep; `SCAN_SUMMARY` counts individual positive RPD
samples with their denominator. Malformed or incomplete sweeps are excluded. A repeatable power-state-dependent
change makes a frequency a candidate. It does not establish the transmitter
address, packet contents, timing of individual packets, binding, or command
values. Consecutive samples may be correlated. New v2 counts are not directly
comparable with the old v1 measurements below.

## Experimental RAW fragment capture

```sh
/usr/bin/python3 rf_nano_receiver_probe/serial_console.py \
  --mode raw --seconds 30 --summary --label remote_on_idle \
  --output rf_nano_receiver_probe/captures/on_idle_raw.log
```

RAW scans channels 0-83 at 1 Mbps with nominal 20 ms visits. Add `--channel 15`
to listen continuously on 2415 MHz instead. Fixed-channel RAW does not emit
scan-round markers. It uses the
[RF24 scanner's short-address tactic](https://github.com/nRF24/RF24/blob/master/examples/scanner/scanner.ino):
2-byte preamble-like addresses, CRC disabled, auto-ACK disabled, and six
receive pipes. Each FIFO read emits `RAW,channel=...,mhz=...,pipe=...,bytes=32,hex=...`.
The 32 bytes are an unvalidated fragment; they are not an identified address,
a verified packet, or the actual payload length. Noise can produce records.
A negative result does not prove no signal exists or exclude this radio family.

`RAW_ROUND` marks a completed scan and `RAW_SUMMARY` counts records by channel.
Serial output, RX restarts, and FIFO flushes consume time and may drop data;
counts are not packet rates or equal-duration occupancy measurements. Channel
changes and command processing always lower CE; there is no transmit call.
Returning to scan/dwell restores normal address width/CRC and closes all pipes.
The mode can help look for repeatable bit patterns, but identifying commands
still requires packet-framing and integrity checks plus controlled comparisons.

## Local checks

```sh
/usr/bin/python3 -m unittest discover -s rf_nano_receiver_probe -p 'test_*.py'

clang++ -std=c++11 -Wall -Wextra -Werror \
  -I rf_nano_receiver_probe/tests/support \
  rf_nano_receiver_probe/tests/test_firmware.cpp \
  -o /private/tmp/traktor-firmware-test
/private/tmp/traktor-firmware-test
```

The host tests emulate a serial device using a pseudo-terminal. The native
firmware tests check commands, complete/interrupted sweeps, dwell reports,
missing hardware, RX/CE ordering, RAW scanning, and return to energy mode
with a fake radio. Actual RF behavior
requires board measurements; an Arduino build verifies the real RF24 API.

## Historical v1 measurements

These earlier results were captured using the old end-of-window RPD method.
They remain observations, but are not reliable evidence of total activity
inside the listening window.

### Initial measurement (2.5 ms per channel)

With the RF-Nano near the remote, three 20-second captures gave:

| Remote state | Scan rounds | Rounds with activity | Channel hits |
|---|---:|---:|---:|
| Idle | 69 | 33 | 37 |
| Crane rotation held | 69 | 29 | 33 |
| Crane rotation held (repeat) | 69 | 27 | 31 |

These captures show no repeatable channel or activity increase associated with
the held control. They are inconclusive: RPD only reports strong energy
(approximately -64 dBm or greater), and the full-band scan samples each
channel briefly. The hits may be unrelated nearby 2.4 GHz traffic. Do not use
them to infer the tractor's channel or packet protocol.

The later v1 sketch used a longer 10 ms listening window per channel to reduce
the chance of missing short, repeated transmissions. The AVR core's
`delayMicroseconds()` overflows above approximately 16 ms at 16 MHz, so the
window is deliberately kept below that limit. A full scan round takes about
1 second.

At 10 ms per channel, a 20-second idle capture reported 5 hits on 5 channels.
A 20-second capture in which crane rotation was held reported 6 hits on 4
channels; the control was started after capture began. This small difference
does not identify the remote's signal and may be background traffic.

Two additional 20-second captures compared remote power state:

| Remote state | Scan rounds | Rounds with activity | Channel hits |
|---|---:|---:|---:|
| Remote off | 20 | 5 | 8 |
| Remote on, idle | 20 | 5 | 5 |

These short captures are statistically inconclusive and show no clear
remote-dependent increase. The scanner is stopped manually with `SCAN OFF`
after a capture; opening the serial console resets the Nano, so scanning starts
again on the next connection.

At a closer, unchanged position (RF-Nano about 5-10 cm from the remote), the
10 ms/channel scanner produced these captures:

| Remote state | Rounds | Channel 3 (2403 MHz) hits | Total channel hits |
|---|---:|---:|---:|
| Movement button held | 20 | 4 | 12 |
| Button released, remote on | 20 | 4 | 12 |
| Remote off | 20 | 1 | 8 |
| Remote on, idle | 20 | 4 | 9 |
| Remote on, idle (repeat) | 20 | 2 | 10 |

This makes channel 3 (2403 MHz) a candidate for periodic remote activity:
three of four powered-on captures showed 2-4 hits there, versus one hit while
powered off. It is not a confirmed carrier or command channel: the control
press did not increase activity over idle, the samples are short, and RPD can
report unrelated strong energy. The next comparison should repeat remote-off,
remote-idle, and button-held captures at the same distance. If the distinction
persists, dwell on channel 3 to characterize timing; this still will not reveal
packet contents or prove nRF24 compatibility.

## Model and protocol research

The Huina Toys product listing describes the 1594 as a 1:14 RC excavator,
updated from the 1593, with 2.4 GHz control and a 22-channel remote. It lists
one paper manual in the box but does not provide a downloadable user manual.
The listing currently labels its product as a 2026 model, so its details are
not proof that every 1594 revision has the same electronics. The 22-channel
description does not establish that the radio uses 22 frequencies or an
nRF24-compatible protocol:
[Huina 1594 product listing](https://huina-toys.com/products/huina-1594-excavator-alloy-engineering-vehicle-toy).

The PDF in this project is the RW1608P radio-chip datasheet, not the excavator
user manual. It documents the radio family and modulation characteristics but
does not disclose the model's over-the-air packet format, addressing, binding,
or command values. A third-party manual listing was not accessible for
verification; do not treat an unverified mirror or search snippet as the
manufacturer's instructions. Photos/scans of the paper booklet—especially the
pairing and controller-operation pages—would be needed to study those details.

No Huina 1594, Huina, or RW1608P entry was found in the checked
[Multiprotocol TX supported-protocol table](https://github.com/pascallanger/DIY-Multiprotocol-TX-Module/blob/master/Protocols_Details.md).
That project does list model-specific nRF24 protocols, including KN for
WLtoys, and explains that protocols and binding are model-specific. The
[Arduino KN receiver project](https://github.com/donkwt/Arduino_nRF24l01_KN_Receiver)
is a concrete example for a different WLtoys model; its protocol must not be
assumed to work with Huina. These searches do not prove that nobody has ever
reverse-engineered the 1594, only that no verified public implementation was
found in the checked sources.

## Protocol recovery: current direction

The supplied RW1608P datasheet describes an integrated MCU and GFSK radio.
Its pin table does not document a separate external SPI interface to the radio;
there is no established MCU-to-radio SPI bus on this photographed remote to
sniff. This is a different architecture from an MCU wired to an nRF24 module.
The datasheet alone does not disclose packet framing, binding, or commands.

A useful lead comes from goebish's primary reverse-engineering account for
**another model**, Eachine E015: its RW1608-based transmitter was investigated
using SDR and found to use HS6200-like framing. Later implementations emulate
that radio family. This makes HS6200 framing a hypothesis to check in a future
Huina capture, not a confirmed Huina protocol. E015 frequencies, binding
addresses, and control values must not be transferred to this excavator.

Sources:

- [Author's E015 reverse-engineering account](https://www.deviationtx.com/forum/protocol-development/8099-eachine-e015-flight-boat-car?screenMode=desktop)
- [DeviationTX E015 implementation](https://github.com/DeviationTX/deviation/blob/master/src/protocol/e015_nrf24l01.c)
- [Multiprotocol HS6200 framing implementation](https://github.com/pascallanger/DIY-Multiprotocol-TX-Module/blob/master/Multiprotocol/HS6200_EMU.ino)

RPD alone did not establish a repeatable remote-off/on difference. The
subsequent RAW work below recovered CRC-valid frame envelopes through the
RF-Nano, providing a basis for controlled comparisons of individual movements.
RPD itself cannot establish individual packet timing or recover packet bytes. At the end of those RPD-only tests, no Huina channel or packet format was
identified. The later RAW work below recovered an observed envelope with
valid CRCs. Binding remains unresolved; later limited computer-confirmed
control mappings are recorded in the separate TX catalogue.

## 2026-10-31 v2 board measurement

The user confirmed each remote state and that the excavator stayed powered on
and responded to the original remote before the measurements. The off capture
kept the same placement as the on capture. The user subsequently confirmed the RF-Nano-to-remote distance was about
5-10 cm. Fixed-channel 30-second captures at 2403 MHz
produced:

| Remote state | Elapsed time | RPD samples | Positive samples | Positive fraction |
|---|---:|---:|---:|---:|
| On, idle | 30.006 s | 93,494 | 3,348 | 3.581% |
| Off | 30.005 s | 93,475 | 7,145 | 7.644% |

Raw records: remote on, idle — `captures/2026-10-31_remote_on_idle_ch3.log` (local archive, not published),
remote off — `captures/2026-10-31_remote_off_ch3.log` (local archive, not published). Both captures ended with
`OK,scan=off`. The off capture had more than twice the positive sample fraction
of the on capture. This does not support identifying 2403 MHz as the remote
signal from these measurements. RPD sees varying background energy; it cannot
exclude a remote signal hidden by that energy. The next step is a same-position
off/on comparison across the full band, rather than assuming channel 3.

A 30-second full-band remote-off scan — `captures/2026-10-31_remote_off_scan.log` (local archive, not published)
completed 34 sweeps, or 1,088 RPD samples per channel. The largest positive
fractions were 2403 MHz (64/1,088, 5.88%), 2418 MHz (53/1,088, 4.87%),
and 2432 MHz (45/1,088, 4.14%). All of these observations were made with the
remote off, so they must not be used to choose the remote's carrier.
The corresponding remote-on scan — `captures/2026-10-31_remote_on_idle_scan.log` (local archive, not published)
also completed 34 sweeps. Its largest increases over the off scan were
2449 MHz (39 vs 8 positive samples), 2408 MHz (31 vs 3), 2452 MHz (48 vs 23),
and 2450 MHz (24 vs 1), each out of 1,088 samples. These differences are
unconfirmed candidates; background can change between sequential captures.
A second on/off comparison also completed 34 sweeps per state. All four scans
have 1,088 samples per channel. Selected frequencies show the temporal drift:

| MHz | Off 1 | On 1 | On 2 | Off 2 |
|---|---:|---:|---:|---:|
| 2403 | 64 | 22 | 50 | 3 |
| 2408 | 3 | 31 | 9 | 1 |
| 2449 | 8 | 39 | 8 | 12 |
| 2450 | 1 | 24 | 7 | 8 |
| 2452 | 23 | 48 | 79 | 98 |
| 2462 | 22 | 19 | 42 | 63 |

The first 2449/2450 MHz peaks did not persist in the second on capture. The
2452 MHz increase also continued after turning the remote off. None of these
frequencies is a confirmed remote channel. The tractor stayed on, so the off
records include its possible radio activity as well as other local sources.

Repeat logs: on 2 — `captures/2026-10-31_remote_on_idle_scan_repeat.log` (local archive, not published),
off 2 — `captures/2026-10-31_remote_off_scan_repeat.log` (local archive, not published).
Machine-readable comparison — `captures/2026-10-31_scan_comparison.json` (local archive, not published) contains
all 84 channels. Individual samples are correlated; no independent-sample
significance claim is made.

### Additional FCC research lead (not matched to this board)

Indexed excerpts of report AGC08169220303FE03 for FCC ID
`2AT93HUINA-1550` list GFSK, six channels, hardware `HN350-A RX-2.4G`,
and software `HN-350R V1.0` (printed page 6). A page-7 indexed excerpt lists
2410, 2415, 2427, 2437, 2445, and 2473 MHz. The report's model list includes
1594, but this does not establish the photographed board's hardware revision.

The full report could not be opened for page-level verification during this
session (HTTP 403). Treat these as preliminary research targets, not the
frequency plan of this excavator. The full-band captures already include them.
Source: [FCC report copy](https://fcc.report/FCC-ID/2AT93HUINA-1550/5861170.pdf).

## RAW framing recovery and controlled movements

A full-band RAW comparison collected 2,884 unvalidated records with the remote
off, 2,882 on idle, and 2,910 on idle in a repeat. Repeated fragments on
2426/2480 MHz had Bluetooth-like signatures. `analyze_raw.py` distinguishes
full advertising access-address + dewhitening + CRC24 validation from weaker
partial-address CRC candidates, keeping the latter separate. A valid Bluetooth
fragment does not identify its device or establish a tractor command.

Following the user's suggestion, one movement was held at a time. This was
more informative than remote power-state comparisons:

| Capture | CRC-valid observed HS6200-style frames | Observed payload byte 6 |
|---|---:|---|
| Tower right, full-band 20 s | 7 | AE |
| Tower left, full-band 20 s | 5 | 04 |
| Tower right repeat, fixed 2415 MHz 8 s | 34 | 0C |

Frame observations: on-air address `8d5fb5cb75`, two guard bytes `7575`,
11-byte scrambled payload, 9-bit PCF, PID 0-3, no-ACK=1, and CRC16 polynomial
0x1021 initialized to 0xFFFF. CRC includes address and PCF/payload and excludes
guards. Valid frames appeared on channels 15, 31, 47, and 63 (2415, 2431,
2447, 2463 MHz). `decode_hs6200.py` independently validates CRC and reports
every reconstructed leading address bit; it can require a full address with
`--require-full-address`. The known XOR mask comes from the linked primary
HS6200 emulation implementation.

The varying byte 6 in repeated right-turn captures means **AE/04 must not be
treated as an established right/left command map**. Payload byte 4 changes
throughout a hold, suggesting a counter; the final payload byte was observed
to equal `(sum(previous bytes) + 2) & 0xff`. These are observations to verify,
not a complete transmitter specification. The later 2026-11-01 controlled
transmitting results are recorded in the separate [TX project](../rf_nano_tx_test/README.md);
this passive sketch retains no RF-transmit command.

```sh
/usr/bin/python3 rf_nano_receiver_probe/decode_hs6200.py \
  rf_nano_receiver_probe/captures/2026-10-31_tower_right_held_raw.log \
  rf_nano_receiver_probe/captures/2026-10-31_tower_left_held_raw.log

/usr/bin/python3 rf_nano_receiver_probe/analyze_raw.py \
  rf_nano_receiver_probe/captures/2026-10-31_remote_on_idle_raw_repeat.log
```

Raw captures and decoder outputs remain under `captures/`. The receive-only
firmware starts stopped and the console requests `SCAN OFF` at the end of
every capture. Next steps are controlled repeated actions and release
transitions to identify neutral/stop behavior and distinguish direction fields
from counters or other changing state.

### Confirmed idle and timing limits

The 15-second `tower_right_release_transition` capture contains 77 CRC-valid
frames, but the user's release confirmation arrived after its end. Actual
release could have occurred before or after the final frame. It must not be
used as a verified post-release record or evidence of continuous status traffic.

A subsequent confirmed-idle fixed-channel capture — `captures/2026-10-31_confirmed_idle_fixed_raw.log` (local archive, not published)
started after the user confirmed all buttons released. It collected 1,452 RAW
records at 2415 MHz and zero matching CRC-valid frames. The user also confirmed
that the tower had moved in the indicated directions and stopped on release.
This supports a connection between the observed stream and active controls,
while leaving the exact command fields and any brief stop packet unresolved.

Generic offline discovery (`--discover-addresses`) additionally found one
CRC-valid 11-byte envelope at on-air address `529c546e5c`. Its payload begins
with the reverse of the observed data address, followed by the four-byte
`15130188` identifier from the data frames. This is a tentative setup/binding
interpretation, not a recovered binding procedure. An empty low-entropy
candidate is separately flagged as a possible artifact.

- Decoded known-address frame rows — `captures/2026-10-31_hs6200_decoded.json` (local archive, not published)
- Address discovery and reproduction command — `captures/2026-10-31_hs6200_address_discovery.json` (local archive, not published)
- BLE classification analysis — `captures/2026-10-31_ble_analysis.json` (local archive, not published)

Final local checks: 39 Python tests and 7 native firmware scenarios pass;
the real Nano build uses 7,144 bytes flash and 485 bytes SRAM. Fixed-channel
RAW was uploaded and verified with real captures. All captures ended with
`SCAN OFF` acknowledged.
