"""Host-only parser and pseudo-terminal protocol tests; no RF hardware is used."""

from contextlib import redirect_stdout
import argparse
import io
import os
from pathlib import Path
import pty
import select
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import serial_console as console


STATUS = "STATUS,radio=ready,scan=off,mode=scan,channel=3,sample_us=250,samples_per_channel=32"


class ProbeSimulator:
    def __init__(self, ignore=None, status=STATUS, on_start=None):
        self.master, self.slave = pty.openpty()
        self.port = os.ttyname(self.slave)
        self.ignore = ignore
        self.status = status
        self.on_start = on_start
        self.commands = []
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def send(self, line):
        os.write(self.master, (line + "\n").encode("utf-8"))

    def run(self):
        buffer = bytearray()
        while not self.stopping.is_set():
            readable, _, _ = select.select([self.master], [], [], 0.02)
            if not readable:
                continue
            try:
                data = os.read(self.master, 4096)
            except OSError:
                return
            buffer.extend(data)
            while b"\n" in buffer:
                line, _, rest = buffer.partition(b"\n")
                buffer[:] = rest
                command = line.rstrip(b"\r").decode("ascii", errors="replace")
                self.commands.append(command)
                if command == self.ignore:
                    continue
                if command == "STATUS":
                    self.send(self.status)
                elif command == "PING":
                    self.send("PONG")
                elif command == "HELP":
                    self.send("COMMANDS,PING|STATUS|SCAN ON|SCAN OFF|HELP|MODE SCAN|MODE DWELL|CHANNEL 0..83")
                else:
                    kind, value = command.split(" ", 1)
                    self.send("OK,{}={}".format(kind.lower(), value.lower()))
                    if command == "SCAN ON" and self.on_start:
                        self.on_start(self)

    def __exit__(self, *args):
        self.stopping.set()
        self.thread.join(timeout=1)
        os.close(self.master)
        os.close(self.slave)


class ParserTests(unittest.TestCase):
    def test_channel_bounds_and_allowlist(self):
        self.assertEqual(console.diagnostic_command(" channel 83 "), "CHANNEL 83")
        self.assertEqual(console.diagnostic_command("mode dwell"), "MODE DWELL")
        self.assertEqual(console.diagnostic_command("mode raw"), "MODE RAW")
        self.assertEqual(console.channel_number("0"), 0)
        for value in ("-1", "84", "1.0", "1 2", "²", ""):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                console.channel_number(value)
        for command in ("WRITE 1", "CHANNEL -1", "CHANNEL 84", "SCAN ON\nSCAN OFF"):
            with self.subTest(command=command), self.assertRaises(argparse.ArgumentTypeError):
                console.diagnostic_command(command)

    def test_cli_short_capture_and_config(self):
        args = console.parse_args(["--seconds", "1", "--mode", "dwell", "--channel", "3", "--command", "PING"])
        self.assertEqual((args.seconds, args.mode, args.channel, args.command), (1, "dwell", 3, "PING"))
        self.assertIsNone(args.port)

    def test_port_detection(self):
        with patch.object(console.glob, "glob", return_value=["/dev/cu.usbserial-NEW"]):
            self.assertEqual(console.resolve_port(None), "/dev/cu.usbserial-NEW")
            self.assertEqual(console.resolve_port("/dev/custom"), "/dev/custom")
        for ports in ([], ["/dev/a", "/dev/b"]):
            with patch.object(console.glob, "glob", return_value=ports), self.assertRaisesRegex(RuntimeError, "--port"):
                console.resolve_port(None)

    def test_malformed_and_dwell_totals(self):
        stats = console.CaptureStats()
        valid = [
            "HIT,round=1,channel=3,mhz=2403,samples=32,hits=2",
            "ROUND,number=1,active_channels=1,samples_per_channel=32",
            "DWELL,channel=3,mhz=2403,samples=100,hits=7,elapsed_ms=1001",
            "DWELL,channel=3,mhz=2403,samples=20,hits=1,elapsed_ms=202",
            "DWELL,channel=4,mhz=2404,samples=50,hits=0,elapsed_ms=500",
        ]
        malformed = [
            "HIT,round=1,channel=nope,mhz=2403",
            "HIT,round=1,channel=3,mhz=2403,samples=32,hits=33",
            "HIT,round=1,channel=3,mhz=2404,samples=32,hits=1",
            "ROUND,number=abc,active_channels=1",
            "ROUND,number=2,active_channels=85",
            "DWELL,channel=84,mhz=2484,samples=100,hits=1,elapsed_ms=1000",
            "DWELL,channel=3,mhz=2403,samples=0,hits=0,elapsed_ms=1000",
            "DWELL,channel=3,mhz=2403,samples=1,hits=2,elapsed_ms=1000",
            "DWELL,channel=3,channel=4,mhz=2403,samples=1,hits=1,elapsed_ms=1000",
            "DWELL,broken",
        ]
        for line in valid + malformed:
            stats.consume(line)
        self.assertEqual((stats.rounds, stats.active_rounds, stats.hits), (1, 1, 1))
        self.assertEqual(stats.dwell[3], {"samples": 120, "hits": 8, "elapsed_ms": 1203})
        self.assertEqual(len(stats.dwell), 2)
        self.assertIn("hit_rate=0.066667", "\n".join(stats.summary_lines()))

    def test_scan_samples_include_zero_hit_rounds_and_keep_event_counts(self):
        stats = console.CaptureStats()
        for line in (
            "HIT,round=1,channel=3,mhz=2403,samples=32,hits=2",
            "HIT,round=1,channel=7,mhz=2407,samples=32,hits=5",
            "ROUND,number=1,active_channels=2,samples_per_channel=32",
            "ROUND,number=2,active_channels=0,samples_per_channel=32",
            "HIT,round=3,channel=3,mhz=2403,samples=32,hits=1",
            "ROUND,number=3,active_channels=1,samples_per_channel=32",
        ):
            stats.consume(line)
        self.assertEqual((stats.rounds, stats.active_rounds, stats.hits), (3, 2, 3))
        self.assertEqual(stats.channel_hits, {3: 2, 7: 1})
        self.assertEqual(stats.scan_samples_per_channel, 96)
        self.assertEqual(stats.scan_rpd_hits, {3: 3, 7: 5})
        summary = list(stats.summary_lines())
        self.assertIn("SCAN_SUMMARY,channel=3,samples=96,hits=3,hit_rate=0.031250", summary)
        self.assertEqual(sum(line.startswith("SCAN_SUMMARY,") for line in summary), 2)
        self.assertNotIn("SCAN_SUMMARY,channel=0,", "\n".join(summary))

    def test_orphan_scan_hits_are_not_committed(self):
        stats = console.CaptureStats()
        stats.consume("HIT,round=1,channel=3,mhz=2403,samples=32,hits=9")
        self.assertEqual((stats.hits, stats.scan_samples_per_channel), (0, 0))
        # Starting round 2 orphans round 1. A delayed round 1 cannot revive it.
        stats.consume("HIT,round=2,channel=4,mhz=2404,samples=32,hits=2")
        stats.consume("ROUND,number=1,active_channels=1,samples_per_channel=32")
        stats.consume("ROUND,number=2,active_channels=1,samples_per_channel=32")
        stats.consume("HIT,round=3,channel=8,mhz=2408,samples=32,hits=10")
        self.assertEqual((stats.rounds, stats.hits), (1, 1))
        self.assertEqual(stats.scan_rpd_hits, {4: 2})
        self.assertEqual(stats.scan_samples_per_channel, 32)

    def test_mismatched_and_duplicate_rounds_never_inflate_counts(self):
        stats = console.CaptureStats()
        for line in (
            "HIT,round=1,channel=3,mhz=2403,samples=32,hits=4",
            "ROUND,number=2,active_channels=1,samples_per_channel=32",
            "ROUND,number=1,active_channels=1,samples_per_channel=32",
            "HIT,round=3,channel=4,mhz=2404,samples=32,hits=2",
            "ROUND,number=3,active_channels=1,samples_per_channel=32",
            "ROUND,number=3,active_channels=1,samples_per_channel=32",
            "HIT,round=3,channel=4,mhz=2404,samples=32,hits=2",
        ):
            stats.consume(line)
        self.assertEqual((stats.rounds, stats.hits, stats.scan_samples_per_channel), (1, 1, 32))
        self.assertEqual(stats.scan_rpd_hits, {4: 2})

    def test_malformed_scan_rounds_are_rejected_as_a_whole(self):
        bad_rounds = (
            ("HIT,round=1,channel=3,mhz=2403,samples=32,hits=2",
             "HIT,round=1,channel=4,mhz=2404,samples=32,hits=33",
             "ROUND,number=1,active_channels=1,samples_per_channel=32"),
            ("HIT,round=1,channel=3,mhz=2403,samples=32,hits=2",
             "HIT,round=1,broken",
             "ROUND,number=1,active_channels=1,samples_per_channel=32"),
            ("HIT,round=1,channel=3,mhz=2403,samples=32,hits=2",
             "HIT,round=1,channel=3,mhz=2403,samples=32,hits=2",
             "ROUND,number=1,active_channels=1,samples_per_channel=32"),
            ("HIT,round=1,channel=3,mhz=2403,samples=32,hits=2",
             "ROUND,number=1,active_channels=1,samples_per_channel=64"),
            ("HIT,round=1,channel=3,mhz=2403,samples=32,hits=2",
             "ROUND,number=1,active_channels=0,samples_per_channel=32"),
            ("ROUND,number=1,active_channels=0,samples_per_channel=0",
             "ROUND,number=1,active_channels=0,samples_per_channel=32"),
        )
        for records in bad_rounds:
            with self.subTest(records=records):
                stats = console.CaptureStats()
                for line in records:
                    stats.consume(line)
                self.assertEqual((stats.rounds, stats.hits, stats.scan_samples_per_channel), (0, 0, 0))
                self.assertEqual(stats.channel_hits, {})
                self.assertEqual(stats.scan_rpd_hits, {})

    def test_legacy_scan_rounds_keep_events_without_sample_rates(self):
        stats = console.CaptureStats()
        stats.consume("HIT,round=1,channel=3,mhz=2403")
        stats.consume("ROUND,number=1,active_channels=1")
        stats.consume("ROUND,number=2,active_channels=0")
        self.assertEqual((stats.rounds, stats.active_rounds, stats.hits), (2, 1, 1))
        self.assertEqual(stats.channel_hits, {3: 1})
        self.assertEqual(stats.scan_samples_per_channel, 0)
        self.assertFalse(any(line.startswith("SCAN_SUMMARY,") for line in stats.summary_lines()))

    def test_raw_fragment_fields_and_summary(self):
        stats = console.CaptureStats()
        hex_data = "0123456789abcdef" * 4
        valid = "RAW,channel=3,mhz=2403,pipe=0,bytes=32,hex=" + hex_data
        stats.consume(valid)
        stats.consume(valid.replace("pipe=0", "pipe=5").replace(hex_data, hex_data.upper()))
        stats.consume(valid.replace("channel=3,mhz=2403", "channel=83,mhz=2483"))
        for malformed in (
            valid.replace("channel=3", "channel=84"),
            valid.replace("channel=3", "channel=-1"),
            valid.replace("mhz=2403", "mhz=2404"),
            valid.replace("pipe=0", "pipe=6"),
            valid.replace("pipe=0", "pipe=-1"),
            valid.replace("bytes=32", "bytes=31"),
            valid.replace("bytes=32", "bytes=33"),
            valid.replace("bytes=32", "bytes=bad"),
            valid.replace(hex_data, hex_data[:-1]),
            valid.replace(hex_data, hex_data + "0"),
            valid.replace(hex_data, "z" * 64),
            valid.replace("hex=", "data="),
            valid.replace("pipe=0", "pipe=0,pipe=1"),
            "RAW,broken",
        ):
            stats.consume(malformed)
        self.assertEqual(stats.raw_records, {3: 2, 83: 1})
        self.assertEqual(stats.hits, 0)
        self.assertEqual(stats.dwell, {})
        summary = list(stats.summary_lines())
        self.assertIn("RAW_SUMMARY,channel=3,records=2", summary)
        self.assertIn("RAW_SUMMARY,channel=83,records=1", summary)
        self.assertNotIn("packet", "\n".join(summary).lower())

    def test_partial_line_is_logged_without_counting(self):
        master, slave = pty.openpty()
        output = io.StringIO()
        try:
            console.configure_serial(slave)
            session = console.SerialSession(slave, output)
            session.collecting = True
            os.write(master, b"DWELL,channel=3,mhz=2403,samples=10,hits=2,elapsed_ms=100")
            with redirect_stdout(io.StringIO()):
                self.assertIsNone(session.read_line(time.monotonic() + 0.03))
                session.flush_partial()
            self.assertIn("DWELL,channel=3", output.getvalue())
            self.assertEqual(session.stats.dwell, {})
        finally:
            os.close(master)
            os.close(slave)

    def test_advisory_lock_excludes_second_console(self):
        with console.port_lock("/dev/test-rfprobe-unittest"):
            with self.assertRaisesRegex(RuntimeError, "already in use"):
                with console.port_lock("/dev/test-rfprobe-unittest"):
                    self.fail("lock was not exclusive")


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.patches = [
            patch.object(console, "STARTUP_SETTLE_SECONDS", 0.02),
            patch.object(console, "STARTUP_TIMEOUT_SECONDS", 0.25),
            patch.object(console, "ACK_TIMEOUT_SECONDS", 0.2),
            patch.object(console, "FINISH_TIMEOUT_SECONDS", 0.2),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()

    def test_one_second_capture_config_ack_logging_and_stop(self):
        def measurements(probe):
            # Deliberately split a serial record over separate reads.
            os.write(probe.master, b"DWELL,channel=3,mhz=2403,samp")
            time.sleep(0.01)
            os.write(probe.master, b"les=80,hits=9,elapsed_ms=900\nHIT,broken\n")

        with tempfile.TemporaryDirectory() as directory, ProbeSimulator(on_start=measurements) as probe:
            log_path = str(Path(directory) / "capture.log")
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                stats = console.monitor(probe.port, 1, None, True, "dwell", 3, log_path, "remote off")
            self.assertEqual(probe.commands, ["STATUS", "SCAN OFF", "MODE DWELL", "CHANNEL 3", "SCAN ON", "SCAN OFF"])
            self.assertEqual(stats.dwell[3]["samples"], 80)
            self.assertEqual(stats.dwell[3]["hits"], 9)
            log = Path(log_path).read_text()
            self.assertIn('label="remote off"', log)
            self.assertIn("HIT,broken", log)
            self.assertIn("DWELL_SUMMARY,channel=3,samples=80,hits=9", log)
            self.assertRegex(log.splitlines()[0], r"T\d\d:\d\d:\d\d\.\d{3}[+-]\d\d:\d\d ")
            self.assertIn("CAPTURE_END", stdout.getvalue())

    def test_one_second_ping_is_sent_and_does_not_start_scan(self):
        with ProbeSimulator() as probe, redirect_stdout(io.StringIO()):
            console.monitor(probe.port, 1, "PING", False)
            self.assertEqual(probe.commands, ["STATUS", "SCAN OFF", "PING", "SCAN OFF"])

    def test_raw_mode_ack_status_and_fragment_logging(self):
        raw_line = "RAW,channel=3,mhz=2403,pipe=1,bytes=32,hex=" + "aa55" * 16

        def fragments(probe):
            probe.send(raw_line)
            probe.send("RAW,channel=3,mhz=2403,pipe=1,bytes=32,hex=broken")
            probe.send("RAW_ROUND,number=1,records=1")

        raw_status = STATUS.replace("mode=scan", "mode=raw")
        with ProbeSimulator(status=raw_status, on_start=fragments) as probe, redirect_stdout(io.StringIO()) as stdout:
            stats = console.monitor(probe.port, 1, None, True, "raw")
            self.assertEqual(probe.commands, ["STATUS", "SCAN OFF", "MODE RAW", "SCAN ON", "SCAN OFF"])
            self.assertEqual(stats.raw_records, {3: 1})
            self.assertIn(raw_line, stdout.getvalue())
            self.assertIn("RAW_SUMMARY,channel=3,records=1", stdout.getvalue())
        self.assertEqual(console.parse_args(["--mode", "raw"]).mode, "raw")

    def test_startup_timeout_is_bounded_and_attempts_stop(self):
        with ProbeSimulator(ignore="STATUS") as probe, redirect_stdout(io.StringIO()):
            started = time.monotonic()
            with self.assertRaisesRegex(RuntimeError, "No acknowledgement for STATUS"):
                console.monitor(probe.port, 1, None, False)
            self.assertLess(time.monotonic() - started, 1)
            self.assertEqual(probe.commands, ["STATUS", "SCAN OFF"])

    def test_missing_config_ack_stops_without_starting_or_retrying(self):
        with ProbeSimulator(ignore="CHANNEL 3") as probe, redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "No acknowledgement for CHANNEL 3"):
                console.monitor(probe.port, 1, None, False, "dwell", 3)
            self.assertEqual(probe.commands.count("CHANNEL 3"), 1)
            self.assertNotIn("SCAN ON", probe.commands)
            self.assertEqual(probe.commands[-1], "SCAN OFF")

    def test_old_firmware_and_radio_error_rejected(self):
        for status in ("STATUS,radio=ready,scan=on", "STATUS,radio=error,scan=off"):
            with self.subTest(status=status), ProbeSimulator(status=status) as probe, redirect_stdout(io.StringIO()):
                with self.assertRaises(RuntimeError):
                    console.monitor(probe.port, 1, None, False)
                self.assertNotIn("SCAN ON", probe.commands)

    def test_stop_ack_timeout_is_reported(self):
        with ProbeSimulator() as probe, redirect_stdout(io.StringIO()):
            calls = []
            original = console.SerialSession.request

            def request(session, command, timeout=None):
                calls.append(command)
                if command == "PING":
                    probe.ignore = "SCAN OFF"
                return original(session, command, timeout)

            with patch.object(console.SerialSession, "request", request), self.assertRaisesRegex(RuntimeError, "No acknowledgement for SCAN OFF"):
                console.monitor(probe.port, 1, "PING", False)
            self.assertEqual(calls[-1], "SCAN OFF")


if __name__ == "__main__":
    unittest.main()
