# Captured-frame transmitter experiment

The RF-Nano now runs the reusable [web-controller firmware](../rf_nano_controller/README.md).
Use the [local web interface](../web_control/README.md) for normal operation.
This directory preserves the earlier one-shot experiments and their evidence.
Full capture/TX logs are excluded from publication; their filenames and hashes
remain provenance labels. Curated source frames are included under
`rf_nano_controller/tests/fixtures/` for independent verification.
The web service owns USB while running and keeps its live counter ledger in
`web_control/runtime/state.json`; historical profile counters are not current.

On 2026-11-01 the user authorized observed transmitting tests against their
RC excavator. Computer pulses have now been physically confirmed for tower
rotation in both directions, main-boom raise/lower, both tracks forward, and
both tracks in reverse, and both tracks forward together. Each observation
included self-stop. The preferred boom-lower `70` template was confirmed to
move only the main boom down. Historical `72` may coactivate another function.

The [control catalogue](control_catalogue.json) records full templates,
capture provenance, profiles, counter ranges, logs, physical observations,
and limits. The values apply to this observed device pair and setup.

## Confirmed movements and remaining limits

| Movement | Profile / RF channel | Computer observation |
|---|---|---|
| Tower right | `9c77d879` and three subsequent profiles / 15 | Four confirmed right pulses and self-stop |
| Tower left | `14a11a61` / 15 | Left and self-stop |
| Only main boom lower | `f470d358` / 31 | Only main boom down and self-stop; preferred `70` template |
| Main boom raise | `dcd5d68e` / 31 | Raise and self-stop |
| Left track forward | `0b9b4c89` / 31 | Only left forward, right stationary, self-stop |
| Right track forward | `844f1254` / 31 | Only right forward, left stationary, self-stop |
| Left track backward | `b5d11eff` / 31 | Only left backward, right stationary, self-stop |
| Right track backward | `a0930a40` / 31 | Only right backward, left stationary, self-stop |
| Both tracks forward | `2f6f18cc` / 31 | Both forward and self-stop |

The forward-track tests each queued 198 frames over 1000 ms, powered down,
and acknowledged STOP. Both tracks were suspended and the original remote
was off. Left-forward used counter `c6..cd`; right-forward used `ce..d5`.
Their source payloads were `15130188b207000000006c` and
`15130188b67000000000d9`. Byte 5 was `07` and `70`, with bytes 6-9 zero.
The right-forward source retains the historical filename
`2026-11-01_left_track_backward_raw.log`; the operator confirmed **right
forward**, so it must not be cited as a reverse capture.

Left reverse was initially an uncaptured `0f` hypothesis derived from the
left-forward template. Profile `b5d11eff`, counters `d6..dd`, then produced
only left reverse and self-stop: 198 frames / 1000 ms / radio off / STOP ACK.
Its pre-test profile still says hypothesis; the
[physical result](control_mapping_2026-11-01/left_track_backward_hypothesis_01_result.json)
records the subsequent confirmation. The stock reverse switches are unavailable,
so this is computer-test evidence, not a captured stock-remote reverse command.

Right reverse was also inferred before testing. Profile `a0930a40`, byte 5=`f0`,
counters `de..e5`, produced only right reverse and self-stop, confirmed by the
user. Its log records 198 frames / 1000 ms / radio off / STOP ACK. The
[separate result](control_mapping_2026-11-01/right_track_backward_hypothesis_01_result.json)
records physical confirmation after the pre-test hypothesis report.

Combined forward `77` was inferred from the two forward templates, then
separately tested as profile `2f6f18cc`, counters `e6..ed`. The user confirmed
both tracks forward and self-stop; its log records 198 frames / 1000 ms / radio
off / STOP ACK. Other combinations and ground driving remain untested.

The preferred boom-lower test used `151301883f000070000062`, byte 7=`70`,
profile `f470d358`, counters `ee..f5`. The user confirmed **only the main boom
down and self-stop**. The log records 198 frames / 1000 ms / radio off / STOP
ACK. Source A is line 3814 with the full address; source B is line 3813 with
two leading address bits reconstructed and CRC checked. Both provenance
records remain in the saved profile report. The
[physical result](control_mapping_2026-11-01/boom_lower_clean_01_result.json)
supersedes its pre-test unverified annotation.

The historical lower test `37248156` used `151301886d000072000092`, byte 7=`72`.
It also lowered the boom and stopped, but its low nibble `02` may coactivate
another function. It is retained as historical evidence; prefer `70` for
isolated lowering. Main-boom raising used byte 7=`d0` in its full tested template. The bucket attachment is not mounted; bucket labels remain
unverified. Physical stop delay and a dedicated neutral packet are unresolved.

## Earlier exact-replay experiments

| Test | Mode | Window / queued frames | User observation |
|---|---|---|---|
| 1 | Exact captured frame replay | 196 ms / 40 | No reaction noticed |
| 2 | Same exact replay | 997 ms / 200 | Definite buzz; part unclear |
| 3 | Same exact replay | 5,000 ms / 998 | No reaction |
| 4 | Advancing counter and PID | 1,000 ms / 198 | Tower right, then self-stop |

These early logs remain in `2026-11-01_test_01.log` through
`2026-11-01_test_04_counters.log`. Subsequent evidence is under
`repeatability_2026-11-01/` and `control_mapping_2026-11-01/`.
Local `experiment_state.json` (not published) records individual attempts;
[working control](working_control.json) retains the initial tower-right result.
The exact-replay buzz is not counted as a confirmed directional success.

## Frame preparation and counter provenance

The on-air address is `8d5fb5cb75`, guards `7575`, 1 Mbps, minimum hardware PA
(-18 dBm). Confirmed profiles use RF channel 15 (2415 MHz) or 31 (2431 MHz).
The original tower-right source was the fresh capture at line 1109, payload
`1513018822000d000000e2`, PID 3, CRC `5edd`. All controls retain their own full
source template and evidence; one action-slot value is not a universal command.

Exact-replay mode preserves captured 22-byte frames. With
`--advance-counters`, the packet builder copies the selected 11 application
bytes, updates byte 4 once per 128 ms, updates byte 10 to
`(sum(first 10 bytes) + 2) & 255`, and cycles the two-bit PID. CRC16 is recomputed
and the 169-bit envelope is padded to 22 bytes. Other application fields remain
unchanged. Inferred action profiles explicitly record any prior transformation
of a control byte, keeping those transformations distinct from captured bytes.

Counter starts must follow the current stock-remote epoch or the last committed
Nano range. Historical counters in catalogue templates are not live starts to
reuse after reset. Enabling the original remote can supersede a Nano-only
sequence. Preparing a header performs no RF transmission; its ID covers the
selected mode, counter start and period. Saved profile reports remain pre-test
artifacts; physical outcomes belong in separate result records.

The nRF uses legacy ShockBurst (`EN_AA=0`, `ARC=0`), with hardware CRC,
automatic ACK/retry, and dynamic payloads disabled. Hardware address
`5555555555` extends the preamble. The software payload contains address,
guards, manual PCF, scrambling and manual CRC. References:

- [Nordic specification, section 7.9](https://devzone.nordicsemi.com/cfs-file/__key/communityserver-discussions-components-files/4/nRF24L01P_5F00_PS_5F00_v1.0.pdf)
- [DeviationTX HS6200 emulation](https://github.com/DeviationTX/deviation/blob/master/src/protocol/spi/nrf24l01.c)
- [DeviationTX E015 double-packet experiment](https://github.com/DeviationTX/deviation/blob/master/src/protocol/e015_nrf24l01.c)

## Bounded host and firmware operation

Firmware permits one explicitly armed burst up to 1000 ms / 200 queued frames
per reset. Uploading or opening USB never fires. A matching compiled profile
and fresh nonce are required for ARM/FIRE. FIRE is never retried automatically.
KEEP runs every 25 ms; heartbeat loss for 100 ms stops the burst. STOP, malformed
input, radio error and completion lower CE, flush TX and power down. Deadline
and heartbeat are rechecked after encoding; late matching KEEP after completion
is harmless. Radio completion alone does not establish received movement.

`tx_once.py --profile PROFILE_ID --output FRESH_LOG` really arms and fires the
compiled profile. The log must be new. Match the profile to the intended
observed experiment with the original remote off and the physical setup
prepared for that movement. Restoring receive-only measurements requires
uploading the original sketch from `rf_nano_receiver_probe/`.

## Local verification

```sh
/usr/bin/python3 -m unittest discover -s rf_nano_tx_test -p 'test_*.py'

clang++ -std=c++11 -Wall -Wextra -Werror \
  rf_nano_tx_test/tests/test_tx_firmware.cpp -o /private/tmp/traktor-tx-firmware-test
/private/tmp/traktor-tx-firmware-test

clang++ -std=c++11 -Wall -Wextra -Werror \
  rf_nano_tx_test/tests/test_tx_encoding.cpp -o /private/tmp/traktor-tx-encoding-test
/private/tmp/traktor-tx-encoding-test

~/.local/bin/arduino-cli compile \
  --fqbn arduino:avr:nano:cpu=atmega328 \
  --build-path /private/tmp/traktor-tx-build rf_nano_tx_test
```

The host tests cover the one-FIRE handshake, limits, tokens, heartbeats,
completion races, disconnects and STOP cleanup. Encoder fixtures compare
captured tower, boom and track frames bit for bit, including a non-byte-aligned
track frame; counter tests preserve application fields while changing only
counter/checksum/PID/CRC. Native engine tests use a mocked radio; physical
results are recorded separately in the catalogue and observation files.

## Saved milestone

The initial mapping milestone was saved before the user requested the web
interface. That interface is now implemented separately. The last one-shot profile was
`f470d358`; it completed with radio off and STOP acknowledged. Counter `f6` is
the initial web-controller start in the recorded epoch; its runtime ledger is
authoritative after web operation. The catalogue has nine control
entries; boom lowering retains both successful templates, with `70` preferred
for its confirmed isolated movement.
