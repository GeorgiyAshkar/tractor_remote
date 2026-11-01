#!/usr/bin/env python3
"""POSIX serial console for the RF-Nano receive-only probe (firmware v2)."""

import argparse
from collections import Counter, deque
from contextlib import contextmanager
from datetime import datetime
import fcntl
import glob
import hashlib
import json
import os
import re
import select
import sys
import tempfile
import termios
import time
import tty
from typing import Optional


ALLOWED_COMMANDS = (
    "PING", "STATUS", "SCAN ON", "SCAN OFF", "HELP", "MODE SCAN", "MODE DWELL", "MODE RAW"
)
STARTUP_SETTLE_SECONDS = 1.5
STARTUP_TIMEOUT_SECONDS = 5.0
ACK_TIMEOUT_SECONDS = 3.0
FINISH_TIMEOUT_SECONDS = 1.5


def channel_number(value: str) -> int:
    if not re.fullmatch(r"[0-9]+", value) or not 0 <= int(value) <= 83:
        raise argparse.ArgumentTypeError("channel must be an integer from 0 to 83")
    return int(value)


def diagnostic_command(value: str) -> str:
    command = " ".join(value.upper().split())
    if command in ALLOWED_COMMANDS:
        return command
    if command.startswith("CHANNEL "):
        return "CHANNEL {}".format(channel_number(command[8:]))
    raise argparse.ArgumentTypeError(
        "command must be PING, STATUS, SCAN ON/OFF, HELP, MODE SCAN/DWELL/RAW, or CHANNEL 0..83"
    )


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="Serial port; otherwise detect exactly one /dev/cu.usbserial-* device")
    parser.add_argument("--seconds", type=int, default=5, choices=range(1, 301), metavar="1..300",
                        help="Capture duration after startup and command acknowledgements (default: %(default)s)")
    parser.add_argument("--command", type=diagnostic_command, help="Optional receive-only diagnostic command")
    parser.add_argument("--channel", type=channel_number, help="Listening channel, 0..83 (2400 + channel MHz)")
    parser.add_argument("--mode", choices=("scan", "dwell", "raw"), help="Full-band RPD scan, fixed-channel RPD sampling, or raw fragments")
    parser.add_argument("--summary", action="store_true", help="Print scan, dwell and raw-fragment totals")
    parser.add_argument("--output", help="Also save timestamped output and summaries to this file")
    parser.add_argument("--label", default="unknown", help="Remote state recorded with the capture (default: %(default)s)")
    return parser.parse_args(argv)


def resolve_port(port: Optional[str]) -> str:
    if port:
        return port
    ports = sorted(glob.glob("/dev/cu.usbserial-*"))
    if len(ports) == 1:
        return ports[0]
    if not ports:
        raise RuntimeError("No /dev/cu.usbserial-* device found. Connect the RF-Nano or specify --port /dev/cu.<device>.")
    raise RuntimeError("Several serial devices found: {}. Select the RF-Nano with --port.".format(", ".join(ports)))


@contextmanager
def port_lock(port: str):
    # A separate advisory lock works on device nodes that do not support flock.
    # Keep the file after release: unlinking it could let two processes lock different inodes.
    digest = hashlib.sha256(os.path.realpath(port).encode("utf-8")).hexdigest()[:20]
    path = os.path.join(tempfile.gettempdir(), "rfprobe-{}.lock".format(digest))
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("This serial port is already in use by another rfprobe console: {}".format(port))
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
            return None
        key, value = part.split("=", 1)
        if not key or key in fields:
            return None
        fields[key] = value
    return parts[0], fields


def integer_field(fields: dict, name: str, minimum: int = 0, maximum=None) -> int:
    value = fields.get(name, "")
    if not re.fullmatch(r"[0-9]+", value):
        raise ValueError(name)
    result = int(value)
    if result < minimum or (maximum is not None and result > maximum):
        raise ValueError(name)
    return result


class CaptureStats:
    """Count measurement records; RPD hits and raw fragments are not validated packets.

    Scan HIT records are provisional until a consistent ROUND closes their
    round. Every completed v2 round contributes samples for all 84 channels,
    including channels for which the firmware correctly emitted no HIT.
    """

    def __init__(self):
        self.channel_hits = Counter()
        self.rounds = 0
        self.active_rounds = 0
        self.hits = 0
        self.scan_samples_per_channel = 0
        self.scan_rpd_hits = Counter()
        self.raw_records = Counter()
        self.dwell = {}
        self._closed_round = 0
        self._latest_round = 0
        self._pending_scan = {}
        self._invalid_rounds = set()

    def _begin_scan_round(self, number: int) -> bool:
        if number <= self._closed_round or number < self._latest_round:
            return False
        if number > self._latest_round:
            # Firmware emits rounds in order. A newer round makes any unclosed
            # older HIT records orphaned, and keeps pending memory bounded.
            self._pending_scan.clear()
            self._invalid_rounds.clear()
            self._latest_round = number
        self._pending_scan.setdefault(number, {})
        return True

    def _invalidate_scan_record(self, line: str) -> None:
        key = "number" if line.startswith("ROUND,") else "round"
        numbers = re.findall(r"(?:^|,)" + key + r"=([0-9]+)(?=,|$)", line)
        if not numbers:
            numbers = [str(self._latest_round)] if self._latest_round else []
        for value in numbers:
            try:
                number = int(value)
            except ValueError:
                continue
            if number > 0 and self._begin_scan_round(number):
                self._invalid_rounds.add(number)
                if key == "number":
                    self._close_scan_round(number)

    def _close_scan_round(self, number: int) -> None:
        self._closed_round = max(self._closed_round, number)
        self._pending_scan.pop(number, None)
        self._invalid_rounds.discard(number)

    def _scan_hit(self, fields: dict) -> None:
        number = integer_field(fields, "round", minimum=1)
        if not self._begin_scan_round(number):
            return
        channel = integer_field(fields, "channel", maximum=83)
        if integer_field(fields, "mhz") != 2400 + channel:
            raise ValueError("mhz")
        samples = hits = None
        if "samples" in fields or "hits" in fields:
            samples = integer_field(fields, "samples", minimum=1)
            hits = integer_field(fields, "hits", minimum=1, maximum=samples)
        pending = self._pending_scan[number]
        if channel in pending:
            raise ValueError("duplicate HIT channel")
        pending[channel] = (samples, hits)

    def _scan_round(self, fields: dict) -> None:
        number = integer_field(fields, "number", minimum=1)
        if not self._begin_scan_round(number):
            return
        active = integer_field(fields, "active_channels", maximum=84)
        samples = None
        if "samples_per_channel" in fields:
            samples = integer_field(fields, "samples_per_channel", minimum=1)
        pending = self._pending_scan[number]
        valid = number not in self._invalid_rounds and len(pending) == active
        # A v2 round requires v2 HIT records with matching denominators. Legacy
        # rounds require legacy HIT records and never affect sample-rate totals.
        valid = valid and all(sample_count == samples for sample_count, _ in pending.values())
        if valid:
            self.rounds += 1
            self.active_rounds += int(active > 0)
            self.hits += active
            self.channel_hits.update(pending.keys())
            if samples is not None:
                self.scan_samples_per_channel += samples
                for channel, (_, hit_count) in pending.items():
                    self.scan_rpd_hits[channel] += hit_count
        self._close_scan_round(number)

    def consume(self, line: str) -> None:
        record = record_fields(line)
        if record is None:
            if line.startswith(("HIT,", "ROUND,")):
                self._invalidate_scan_record(line)
            return
        kind, fields = record
        try:
            if kind == "HIT":
                self._scan_hit(fields)
            elif kind == "ROUND":
                self._scan_round(fields)
            elif kind == "RAW":
                channel = integer_field(fields, "channel", maximum=83)
                if integer_field(fields, "mhz") != 2400 + channel:
                    return
                integer_field(fields, "pipe", maximum=5)
                integer_field(fields, "bytes", minimum=32, maximum=32)
                if not re.fullmatch(r"[0-9a-fA-F]{64}", fields.get("hex", "")):
                    return
                self.raw_records[channel] += 1
            elif kind == "DWELL":
                channel = integer_field(fields, "channel", maximum=83)
                if integer_field(fields, "mhz") != 2400 + channel:
                    return
                samples = integer_field(fields, "samples", minimum=1)
                hits = integer_field(fields, "hits", maximum=samples)
                elapsed = integer_field(fields, "elapsed_ms", minimum=1)
                totals = self.dwell.setdefault(channel, {"samples": 0, "hits": 0, "elapsed_ms": 0})
                totals["samples"] += samples
                totals["hits"] += hits
                totals["elapsed_ms"] += elapsed
        except (ValueError, OverflowError):
            # Preserve the original line in the log, but never count malformed data.
            if kind in ("HIT", "ROUND"):
                self._invalidate_scan_record(line)
            return

    def summary_lines(self):
        channels = ",".join("{}:{}".format(ch, self.channel_hits[ch]) for ch in sorted(self.channel_hits))
        yield "SUMMARY,rounds={},active_rounds={},hits={},channel_hits={}".format(
            self.rounds, self.active_rounds, self.hits, channels or "none")
        for channel in sorted(self.scan_rpd_hits):
            hits = self.scan_rpd_hits[channel]
            if hits:
                yield "SCAN_SUMMARY,channel={},samples={},hits={},hit_rate={:.6f}".format(
                    channel, self.scan_samples_per_channel, hits, hits / self.scan_samples_per_channel)
        for channel in sorted(self.raw_records):
            yield "RAW_SUMMARY,channel={},records={}".format(channel, self.raw_records[channel])
        for channel in sorted(self.dwell):
            totals = self.dwell[channel]
            yield "DWELL_SUMMARY,channel={},samples={},hits={},hit_rate={:.6f},elapsed_ms={}".format(
                channel, totals["samples"], totals["hits"], totals["hits"] / totals["samples"], totals["elapsed_ms"])


class SerialSession:
    def __init__(self, fd: int, output=None):
        self.fd = fd
        self.output = output
        self.buffer = bytearray()
        self.lines = deque()
        self.stats = CaptureStats()
        self.collecting = False

    def emit(self, text: str) -> None:
        line = "{} {}".format(datetime.now().astimezone().isoformat(timespec="milliseconds"), text)
        print(line, flush=True)
        if self.output is not None:
            self.output.write(line + "\n")
            self.output.flush()

    def read_line(self, deadline: float):
        while not self.lines:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            readable, _, _ = select.select([self.fd], [], [], min(0.2, remaining))
            if not readable:
                continue
            try:
                data = os.read(self.fd, 4096)
            except BlockingIOError:
                continue
            if not data:
                raise RuntimeError("Serial device disconnected")
            self.buffer.extend(data)
            while b"\n" in self.buffer:
                line, _, remainder = self.buffer.partition(b"\n")
                self.buffer[:] = remainder
                self.lines.append(line.rstrip(b"\r").decode("utf-8", errors="replace"))
        line = self.lines.popleft()
        self.emit(line)
        if self.collecting:
            self.stats.consume(line)
        return line

    def write_command(self, command: str) -> None:
        self.emit("HOST,command={}".format(command))
        pending = memoryview((command + "\n").encode("ascii"))
        deadline = time.monotonic() + ACK_TIMEOUT_SECONDS
        while pending:
            if time.monotonic() >= deadline:
                raise RuntimeError("Timed out writing command: {}".format(command))
            _, writable, _ = select.select([], [self.fd], [], 0.1)
            if writable:
                try:
                    pending = pending[os.write(self.fd, pending):]
                except BlockingIOError:
                    pass

    def request(self, command: str, timeout=None) -> None:
        self.write_command(command)
        deadline = time.monotonic() + (ACK_TIMEOUT_SECONDS if timeout is None else timeout)
        while time.monotonic() < deadline:
            line = self.read_line(deadline)
            if line is None:
                break
            if line.startswith("ERR,"):
                raise RuntimeError("Probe rejected {}: {}".format(command, line))
            if acknowledged(command, line):
                return
        raise RuntimeError("No acknowledgement for {} (check firmware v2 and serial connection)".format(command))

    def startup(self) -> None:
        deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
        settle = time.monotonic() + STARTUP_SETTLE_SECONDS
        # Opening a Nano usually resets it. Drain startup output before sending one
        # STATUS request, rather than sending commands into its bootloader.
        while time.monotonic() < settle:
            line = self.read_line(min(settle, deadline))
            if line and line.startswith("ERR,"):
                raise RuntimeError("Probe startup failed: {}".format(line))
        self.request("STATUS", max(0.01, deadline - time.monotonic()))

    def flush_partial(self) -> None:
        if self.buffer:
            self.emit(bytes(self.buffer).rstrip(b"\r").decode("utf-8", errors="replace"))
            self.buffer.clear()


def acknowledged(command: str, line: str) -> bool:
    if command == "PING":
        return line == "PONG"
    if command == "HELP":
        return line.startswith("COMMANDS,")
    if command == "STATUS":
        record = record_fields(line)
        if not record or record[0] != "STATUS":
            return False
        fields = record[1]
        if fields.get("radio") != "ready":
            raise RuntimeError("Probe radio is not ready: {}".format(line))
        if fields.get("mode") not in ("scan", "dwell", "raw") or fields.get("scan") not in ("on", "off"):
            raise RuntimeError("Firmware v2 is required (STATUS is missing mode/scan fields)")
        try:
            integer_field(fields, "channel", maximum=83)
            integer_field(fields, "sample_us", minimum=170)
            integer_field(fields, "samples_per_channel", minimum=1)
        except ValueError:
            raise RuntimeError("Firmware v2 returned an invalid STATUS: {}".format(line))
        return True
    prefix, value = command.split(" ", 1)
    return line == "OK,{}={}".format(prefix.lower(), value.lower())


def monitor(port: str, seconds: int, command: Optional[str], summary: bool,
            mode: Optional[str] = None, channel: Optional[int] = None,
            output_path: Optional[str] = None, label: str = "unknown") -> CaptureStats:
    if not 1 <= seconds <= 300:
        raise ValueError("seconds must be from 1 to 300")
    if channel is not None:
        channel = channel_number(str(channel))
    if command is not None:
        command = diagnostic_command(command)
    if mode not in (None, "scan", "dwell", "raw"):
        raise ValueError("mode must be scan, dwell or raw")
    with port_lock(port):
        output = open(output_path, "w", encoding="utf-8") if output_path else None
        fd = None
        session = None
        pending_error = None
        try:
            fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
            configure_serial(fd)
            session = SerialSession(fd, output)
            session.emit("CAPTURE,label={},requested_seconds={},port={}".format(json.dumps(label, ensure_ascii=False), seconds, port))
            session.startup()
            session.request("SCAN OFF")
            if mode:
                session.request("MODE " + mode.upper())
            if channel is not None:
                session.request("CHANNEL {}".format(channel))
            if command:
                session.request(command)
            if command not in ("PING", "STATUS", "HELP", "SCAN OFF", "SCAN ON"):
                session.request("SCAN ON")
            session.collecting = True
            session.emit("CAPTURE_START,label={}".format(json.dumps(label, ensure_ascii=False)))
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                session.read_line(deadline)
        except BaseException as error:
            pending_error = error
        finally:
            if session is not None:
                try:
                    # Include the firmware's final partial dwell report before ACK.
                    session.request("SCAN OFF", FINISH_TIMEOUT_SECONDS)
                except (OSError, RuntimeError) as stop_error:
                    session.emit("HOST,error={}".format(stop_error))
                    if pending_error is None:
                        pending_error = stop_error
                session.collecting = False
                session.flush_partial()
                session.emit("CAPTURE_END")
                if summary or output is not None:
                    for line in session.stats.summary_lines():
                        session.emit(line)
            if fd is not None:
                os.close(fd)
            if output is not None:
                output.close()
        if pending_error is not None:
            raise pending_error
        return session.stats


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        monitor(resolve_port(args.port), args.seconds, args.command, args.summary,
                args.mode, args.channel, args.output, args.label)
    except KeyboardInterrupt:
        print("Capture interrupted; stop command attempted.", file=sys.stderr)
        return 130
    except (OSError, RuntimeError, ValueError) as error:
        print("Error: {}".format(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
