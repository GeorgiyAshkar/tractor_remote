# Reusable RF-Nano controller

This firmware serves the local web controller over USB at 115200 baud. It uses
the previously tested radio configuration and HS6200 framing. Uploading,
opening USB, inspecting status, and arming never transmit a radio packet.

Each operation requires an explicit `ARM` and matching `FIRE`. Completion,
STOP, an error, or a missing heartbeat disarms the operation. The next operation
requires another ARM with a token different from the last fired token. There
is no automatic rearming.

## Serial contract

Commands are ASCII lines ending in LF; CR before LF is accepted.

```text
HELLO
STATUS
PING
ARM LF f6 1234abcd
FIRE 1234abcd
KEEP 1234abcd
STOP
```

- Action codes contain exactly two uppercase letters from the table below.
- The starting counter is exactly two lowercase hexadecimal digits.
- Tokens are exactly eight lowercase hexadecimal digits.
- ARM expires after 5 seconds. FIRE can consume an ARM only once.
- Send KEEP approximately every 25 ms while active; 100 ms without a valid
  heartbeat stops RF and disarms.
- During an active operation, only KEEP and STOP are accepted. Other complete
  commands stop RF and return `ERR,busy`.
- Invalid bytes, an oversized line, or a mismatched KEEP token stop RF and
  disarm. Invalid/oversized lines are discarded completely through LF.
- A matching late KEEP from the last completed operation is ignored while
  idle. It never arms or starts transmission.

Startup emits BOOT and READY with the same fields:

```text
READY,protocol=tractorctl-v1,firmware=web-control-v1,radio=ready,armed=no,active=no,max_duration_ms=1000,heartbeat_ms=100,max_frames=200
ARMED,action=LF,counter=f6,token=1234abcd
START,token=1234abcd,duration_ms=1000,action=LF
DONE,token=1234abcd,reason=complete,frames=200,elapsed_ms=996,radio=off,action=LF
OK,stopped=1
```

`radio=error` means initialization failed. PING replies PONG. STOP replies
`OK,stopped=1`, with a DONE record first if an operation was active. DONE frame
count and elapsed time report the actual result; the example values are not
guaranteed. Reasons include `complete`, `stop`, `heartbeat`, `serial_error`,
`radio_error`, and `radio_timeout`. Errors begin with `ERR,`.

## Fixed action whitelist

| Code | Action | Channel | Application payload slot/value |
|---|---|---:|---|
| TL | Tower left | 15 | 6 / `04` |
| TR | Tower right | 15 | 6 / `0d` |
| BU | Boom up | 31 | 7 / `d0` |
| BD | Boom down | 31 | 7 / `70` |
| LF | Left track forward | 31 | 5 / `07` |
| LB | Left track backward | 31 | 5 / `0f` |
| RF | Right track forward | 31 | 5 / `70` |
| RB | Right track backward | 31 | 5 / `f0` |
| FF | Both tracks forward | 31 | 5 / `77` |

The other action slots 5–9 remain zero. The known peer address is
`8d5fb5cb75` and the application prefix is `15130188`. The application counter
advances every 128 ms from ARM's starting value. PID, application checksum and
radio CRC are regenerated for every pair.

RF is limited to one second and 200 queued frames. Packets are paired at 10 ms
intervals with PA_MIN, acknowledgements and retries disabled. Each pair has a
2 ms radio completion timeout. A 5 ms reserve plus a post-build deadline and
heartbeat check prevents frame construction from bypassing transmission
limits. STOP lowers CE, flushes TX and powers down.

## Verification and build

```sh
clang++ -std=c++11 -Wall -Wextra -Werror \
  rf_nano_controller/tests/test_controller_firmware.cpp \
  -o /private/tmp/traktor-controller-test
/private/tmp/traktor-controller-test

~/.local/bin/arduino-cli compile \
  --fqbn arduino:avr:nano:cpu=atmega328 \
  --build-path /private/tmp/traktor-web-controller-build \
  rf_nano_controller
```

Native checks exercise command/state/timing boundaries using the real pure
encoder and an instrumented radio substitute. They do not access USB or emit
RF. The independent packet check is `tests/verify_packets.py`.
