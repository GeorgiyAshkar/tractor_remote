#!/usr/bin/env python3
"""Send one explicitly requested, firmware-bounded tractor TX test.

The compiled profile selects the radio contents. This host accepts no payload,
repeat count, or duration override. It sends FIRE exactly once, then services
short-lived heartbeats and always attempts STOP before closing the port.
"""

import argparse
from collections import deque
from contextlib import contextmanager
from datetime import datetime
import fcntl
import glob
import hashlib
import os
import re
import secrets
import select
import sys
import tempfile
import termios
import time
import tty
from typing import Optional


PROTOCOL = "tractortx-v1"
DURATION_MS = 1000
MAX_FRAMES = 200
STARTUP_SETTLE_SECONDS = 1.8
ACK_TIMEOUT_SECONDS = 1.0
EVENT_TIMEOUT_SECONDS = DURATION_MS / 1000.0 + 2.0
STOP_TIMEOUT_SECONDS = 0.5
KEEP_INTERVAL_SECONDS = 0.025
DONE_REASONS = {"complete", "stop", "heartbeat", "radio_timeout", "deadline", "radio_error", "serial_error"}


class ProtocolError(RuntimeError):
    pass


def hex8(value: str) -> str:
    if not re.fullmatch(r"[0-9a-fA-F]{8}", value):
        raise argparse.ArgumentTypeError("profile must be exactly eight hexadecimal characters")
    return value.lower()


def resolve_port(port: Optional[str]) -> str:
    if port:
        return port
    ports = sorted(set(glob.glob("/dev/cu.usbserial-*") + glob.glob("/dev/cu.wchusbserial*")))
    if len(ports) == 1:
        return ports[0]
    if not ports:
        raise RuntimeError("No CH340-style serial port found. Connect the RF-Nano or specify --port /dev/cu.<device>.")
    raise RuntimeError("Several serial devices found: {}. Select the RF-Nano with --port.".format(", ".join(ports)))


@contextmanager
def port_lock(port: str):
    # Match the receive-only console's advisory lock, so our own tools cannot
    # share a device. Keep the lock file after closing to avoid inode races.
    digest = hashlib.sha256(os.path.realpath(port).encode("utf-8")).hexdigest()[:20]
    path = os.path.join(tempfile.gettempdir(), "rfprobe-{}.lock".format(digest))
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Serial port is already in use by another tractor console: {}".format(port))
        yield
    finally:
        os.close(fd)


def configure_serial(fd: int) -> None:
    tty.setraw(fd)
    settings = termios.tcgetattr(fd)
    settings[4] = termios.B115200
    settings[5] = termios.B115200
    settings[2] &= ~(termios.CSIZE | termios.PARENB | termios.CSTOPB)
    settings[2] |= termios.CS8 | termios.CLOCAL | termios.CREAD
    termios.tcsetattr(fd, termios.TCSANOW, settings)


def record_fields(line: str):
    parts = line.split(",")
    fields = {}
    for part in parts[1:]:
        if "=" not in part:
            raise ProtocolError("Malformed device response: {}".format(line))
        key, value = part.split("=", 1)
        if not key or key in fields:
            raise ProtocolError("Malformed device response: {}".format(line))
        fields[key] = value
    return parts[0], fields


def integer_field(fields: dict, name: str, minimum: int = 0, maximum=None) -> int:
    value = fields.get(name, "")
    if not re.fullmatch(r"[0-9]+", value):
        raise ProtocolError("Missing or invalid integer field: {}".format(name))
    result = int(value)
    if result < minimum or (maximum is not None and result > maximum):
        raise ProtocolError("Device field outside allowed bounds: {}={}".format(name, value))
    return result


def validate_ready(line: str, profile: str) -> dict:
    kind, fields = record_fields(line)
    if kind != "READY" or fields.get("protocol") != PROTOCOL:
        raise ProtocolError("Expected READY protocol={}".format(PROTOCOL))
    if fields.get("profile") != profile:
        raise ProtocolError("Compiled profile does not match requested profile {}".format(profile))
    if fields.get("radio") != "ready":
        raise ProtocolError("Device radio is not ready")
    if fields.get("armed") != "no" or fields.get("spent") != "no":
        raise ProtocolError("Device must be disarmed and unspent before this single test")
    integer_field(fields, "channel", maximum=83)
    if integer_field(fields, "duration_ms") != DURATION_MS or integer_field(fields, "max_frames") != MAX_FRAMES:
        raise ProtocolError("Compiled limits must be duration_ms={} and max_frames={}".format(DURATION_MS, MAX_FRAMES))
    return fields


def validate_start(line: str, token: str) -> None:
    kind, fields = record_fields(line)
    if kind != "START" or fields.get("token") != token:
        raise ProtocolError("START token does not match this test")
    if integer_field(fields, "duration_ms") != DURATION_MS:
        raise ProtocolError("START duration does not match the compiled {} ms limit".format(DURATION_MS))


def validate_done(line: str, token: str) -> dict:
    kind, fields = record_fields(line)
    if kind != "DONE" or fields.get("token") != token:
        raise ProtocolError("DONE token does not match this test")
    if fields.get("reason") not in DONE_REASONS:
        raise ProtocolError("Unknown DONE reason: {}".format(fields.get("reason")))
    if fields.get("radio") != "off":
        raise ProtocolError("DONE did not confirm that the radio is off")
    integer_field(fields, "frames", maximum=MAX_FRAMES)
    integer_field(fields, "elapsed_ms", maximum=DURATION_MS)
    return fields


class SerialSession:
    def __init__(self, fd: int, output):
        self.fd = fd
        self.output = output
        self.buffer = bytearray()
        self.lines = deque()
        self.fire_sent = False
        self.started = False
        self.done = None
        self.token = None

    def emit(self, text: str) -> None:
        line = "{} {}".format(datetime.now().astimezone().isoformat(timespec="milliseconds"), text)
        print(line, flush=True)
        self.output.write(line + "\n")
        self.output.flush()

    def read_line(self, deadline: float):
        while not self.lines:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            readable, _, _ = select.select([self.fd], [], [], min(0.05, remaining))
            if not readable:
                continue
            try:
                data = os.read(self.fd, 4096)
            except BlockingIOError:
                continue
            if not data:
                raise RuntimeError("Serial device disconnected")
            self.buffer.extend(data)
            if len(self.buffer) > 16384:
                raise ProtocolError("Device emitted an oversized serial line")
            while b"\n" in self.buffer:
                line, _, rest = self.buffer.partition(b"\n")
                self.buffer[:] = rest
                self.lines.append(line.rstrip(b"\r").decode("utf-8", errors="replace"))
        line = self.lines.popleft()
        self.emit(line)
        if line.startswith("ERR,"):
            raise ProtocolError("Device error: {}".format(line))
        return line

    def write_command(self, command: str) -> None:
        if command == "STOP":
            # Even a failed log write must not suppress the stop attempt.
            try:
                self.emit("HOST,command=STOP")
            finally:
                self.write_bytes(command)
            return
        self.emit("HOST,command={}".format(command))
        self.write_bytes(command)

    def write_bytes(self, command: str) -> None:
        pending = memoryview((command + "\n").encode("ascii"))
        deadline = time.monotonic() + ACK_TIMEOUT_SECONDS
        while pending:
            if time.monotonic() >= deadline:
                raise RuntimeError("Timed out writing command: {}".format(command.split(" ", 1)[0]))
            _, writable, _ = select.select([], [self.fd], [], min(0.025, max(0, deadline - time.monotonic())))
            if writable:
                try:
                    pending = pending[os.write(self.fd, pending):]
                except BlockingIOError:
                    pass

    def startup(self, profile: str) -> dict:
        deadline = time.monotonic() + STARTUP_SETTLE_SECONDS
        while time.monotonic() < deadline:
            self.read_line(deadline)
        self.write_command("HELLO")
        deadline = time.monotonic() + ACK_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            line = self.read_line(deadline)
            if line is not None and line.startswith("READY"):
                return validate_ready(line, profile)
        raise RuntimeError("No READY acknowledgement for HELLO; no ARM or FIRE was sent")

    def arm(self, profile: str, token: str) -> None:
        self.token = token
        self.write_command("ARM {} {}".format(profile, token))
        deadline = time.monotonic() + ACK_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            line = self.read_line(deadline)
            if line is not None and line.startswith("ARMED"):
                kind, fields = record_fields(line)
                if kind != "ARMED" or fields.get("profile") != profile or fields.get("token") != token:
                    raise ProtocolError("ARMED profile/token does not match this test")
                return
        raise RuntimeError("No ARMED acknowledgement; FIRE was not sent")

    def fire_once(self, token: str) -> dict:
        if self.fire_sent:
            raise RuntimeError("FIRE has already been sent in this session")
        # Set the latch before writing: even a partial write or missing reply
        # must never cause another FIRE attempt.
        self.fire_sent = True
        self.write_command("FIRE " + token)
        deadline = time.monotonic() + EVENT_TIMEOUT_SECONDS
        next_keep = None
        while time.monotonic() < deadline:
            now = time.monotonic()
            if self.started and now >= next_keep:
                self.write_command("KEEP " + token)
                next_keep = time.monotonic() + KEEP_INTERVAL_SECONDS
            read_deadline = min(deadline, next_keep) if self.started else deadline
            line = self.read_line(read_deadline)
            if line is None:
                continue
            if line.startswith("START"):
                if self.started:
                    raise ProtocolError("Unexpected repeated START")
                validate_start(line, token)
                self.started = True
                next_keep = time.monotonic() + KEEP_INTERVAL_SECONDS
            elif line.startswith("DONE"):
                self.done = validate_done(line, token)
                if not self.started:
                    raise ProtocolError("DONE arrived without START; FIRE will not be retried")
                if self.done["reason"] != "complete":
                    raise ProtocolError("Burst ended with reason={}".format(self.done["reason"]))
                return self.done
        raise RuntimeError("Timed out waiting for {}; FIRE will not be retried".format("DONE" if self.started else "START"))

    def stop(self) -> None:
        self.write_command("STOP")
        deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
        acknowledged = False
        while time.monotonic() < deadline:
            line = self.read_line(deadline)
            if line == "OK,stopped=1":
                acknowledged = True
            elif line and line.startswith("DONE") and self.fire_sent:
                self.done = validate_done(line, self.token)
            if acknowledged and (not self.fire_sent or self.done is not None):
                return
        if not acknowledged:
            raise RuntimeError("No STOP acknowledgement")
        raise RuntimeError("STOP acknowledged without a matching radio-off DONE")


def run_once(profile: str, port: str, output_path: str) -> dict:
    profile = hex8(profile)
    token = secrets.token_hex(4)
    # Exclusive creation happens before opening/resetting the board. An old
    # experiment log cannot silently be overwritten by a fresh run.
    with open(output_path, "x", encoding="utf-8") as output, port_lock(port):
        fd = None
        session = None
        result = None
        pending_error = None
        try:
            fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
            session = SerialSession(fd, output)
            session.emit("PROFILE,requested={},port={},max_duration_ms={},max_frames={}".format(
                profile, port, DURATION_MS, MAX_FRAMES))
            configure_serial(fd)
            ready = session.startup(profile)
            session.emit("PROFILE,verified={},channel={},duration_ms={},max_frames={}".format(
                ready["profile"], ready["channel"], ready["duration_ms"], ready["max_frames"]))
            session.arm(profile, token)
            result = session.fire_once(token)
        except BaseException as error:
            pending_error = error
        finally:
            try:
                if session is not None:
                    try:
                        session.stop()
                    except (OSError, RuntimeError) as stop_error:
                        if pending_error is None:
                            pending_error = stop_error
                        session.emit("HOST,stop_error={}".format(stop_error))
                    if session.buffer:
                        session.emit(bytes(session.buffer).decode("utf-8", errors="replace"))
                    session.emit("HOST,end=success" if pending_error is None else "HOST,end=error")
            finally:
                if fd is not None:
                    os.close(fd)
        if pending_error is not None:
            raise pending_error
        return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, type=hex8, help="Exact eight-hex compiled profile identifier")
    parser.add_argument("--port", help="RF-Nano serial port; otherwise detect one CH340-style port")
    parser.add_argument("--output", required=True, help="Fresh timestamped log file (must not already exist)")
    args = parser.parse_args(argv)
    try:
        result = run_once(args.profile, resolve_port(args.port), args.output)
    except KeyboardInterrupt:
        print("Interrupted; STOP was attempted. FIRE was not retried.", file=sys.stderr)
        return 130
    except (OSError, RuntimeError, ValueError) as error:
        print("Error: {}".format(error), file=sys.stderr)
        return 1
    print("One burst completed: frames={}, elapsed_ms={}, radio=off.".format(result["frames"], result["elapsed_ms"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
