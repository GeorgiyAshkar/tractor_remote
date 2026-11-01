"""Host-only pseudo-terminal checks for the single-burst TX handshake."""

from contextlib import redirect_stderr, redirect_stdout
import argparse
import io
import os
from pathlib import Path
import pty
import select
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import tx_once as tx


PROFILE = "a1b2c3d4"
READY = ("READY,protocol=tractortx-v1,radio=ready,profile=a1b2c3d4,channel=15,"
         "duration_ms={},max_frames={},armed=no,spent=no".format(tx.DURATION_MS, tx.MAX_FRAMES))


class TxSimulator:
    def __init__(self, ready=READY, missing_start=False, missing_done=False,
                 disconnect_on_fire=False, reason="complete", done_changes=None,
                 missing_arm=False):
        self.master, self.slave = pty.openpty()
        self.port = os.ttyname(self.slave)
        self.ready = ready
        self.missing_start = missing_start
        self.missing_done = missing_done
        self.disconnect_on_fire = disconnect_on_fire
        self.reason = reason
        self.done_changes = done_changes or {}
        self.missing_arm = missing_arm
        self.commands = []
        self.token = None
        self.active = False
        self.finished = False
        self.started_at = None
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def send(self, line):
        os.write(self.master, (line + "\n").encode("ascii"))

    def done(self, reason=None):
        fields = {"token": self.token, "reason": reason or self.reason,
                  "frames": "8", "elapsed_ms": "100", "radio": "off"}
        fields.update(self.done_changes)
        self.send("DONE," + ",".join("{}={}".format(key, value) for key, value in fields.items()))
        self.active = False
        self.finished = True

    def handle(self, command):
        self.commands.append(command)
        if command == "HELLO":
            self.send(self.ready)
        elif command.startswith("ARM "):
            _, profile, self.token = command.split(" ")
            if not self.missing_arm:
                self.send("ARMED,profile={},token={}".format(profile, self.token))
        elif command.startswith("FIRE "):
            self.active = True
            self.started_at = time.monotonic()
            if self.disconnect_on_fire:
                os.close(self.master)
                self.master = None
                self.stopping.set()
                return
            if not self.missing_start:
                self.send("START,token={},duration_ms={}".format(self.token, tx.DURATION_MS))
        elif command.startswith("KEEP "):
            pass
        elif command == "STOP":
            if self.active:
                self.done("stop")
            self.send("OK,stopped=1")

    def run(self):
        buffer = bytearray()
        while not self.stopping.is_set():
            if self.active and not self.missing_done and time.monotonic() - self.started_at >= 0.1:
                self.done()
            readable, _, _ = select.select([self.master], [], [], 0.005)
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
                self.handle(line.rstrip(b"\r").decode("ascii", errors="replace"))
                if self.stopping.is_set():
                    return

    def __exit__(self, *args):
        self.stopping.set()
        self.thread.join(timeout=1)
        if self.master is not None:
            os.close(self.master)
        os.close(self.slave)


class TxOnceTests(unittest.TestCase):
    def setUp(self):
        self.patches = [
            patch.object(tx, "STARTUP_SETTLE_SECONDS", 0.01),
            patch.object(tx, "ACK_TIMEOUT_SECONDS", 0.15),
            patch.object(tx, "EVENT_TIMEOUT_SECONDS", 0.3),
            patch.object(tx, "STOP_TIMEOUT_SECONDS", 0.1),
        ]
        for item in self.patches:
            item.start()
        self.directory = tempfile.TemporaryDirectory()
        self.output = str(Path(self.directory.name) / "tx.log")

    def tearDown(self):
        self.directory.cleanup()
        for item in reversed(self.patches):
            item.stop()

    def test_one_fire_with_matching_nonce_keep_and_fresh_timestamped_log(self):
        with TxSimulator() as device, redirect_stdout(io.StringIO()):
            result = tx.run_once(PROFILE, device.port, self.output)
            fire = [command for command in device.commands if command.startswith("FIRE ")]
            self.assertEqual(len(fire), 1)
            token = fire[0].split(" ")[1]
            self.assertRegex(token, r"^[0-9a-f]{8}$")
            self.assertIn("ARM {} {}".format(PROFILE, token), device.commands)
            self.assertGreaterEqual(device.commands.count("KEEP " + token), 2)
            self.assertEqual(device.commands[-1], "STOP")
            self.assertEqual((result["reason"], result["radio"]), ("complete", "off"))
        log = Path(self.output).read_text()
        self.assertIn("PROFILE,requested=" + PROFILE, log)
        self.assertIn("PROFILE,verified=" + PROFILE, log)
        self.assertIn("HOST,end=success", log)
        self.assertRegex(log.splitlines()[0], r"T\d\d:\d\d:\d\d\.\d{3}[+-]\d\d:\d\d ")

    def test_wrong_profile_unknown_protocol_and_unready_radio_abort_before_arm(self):
        for ready in (READY.replace(PROFILE, "deadbeef"), READY.replace("tractortx-v1", "other-v1"),
                      READY.replace("radio=ready", "radio=off"), READY.replace("spent=no", "spent=yes"),
                      READY.replace("armed=no", "armed=yes"),
                      READY.replace("duration_ms={}".format(tx.DURATION_MS), "duration_ms=5000"),
                      READY.replace("max_frames={}".format(tx.MAX_FRAMES), "max_frames=1000")):
            with self.subTest(ready=ready), tempfile.TemporaryDirectory() as directory:
                with TxSimulator(ready=ready) as device, redirect_stdout(io.StringIO()):
                    with self.assertRaises(tx.ProtocolError):
                        tx.run_once(PROFILE, device.port, str(Path(directory) / "tx.log"))
                    self.assertEqual(device.commands, ["HELLO", "STOP"])

    def test_missing_start_never_retries_fire_and_sends_stop(self):
        with TxSimulator(missing_start=True) as device, redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(tx.ProtocolError, "without START"):
                tx.run_once(PROFILE, device.port, self.output)
            self.assertEqual(sum(command.startswith("FIRE ") for command in device.commands), 1)
            self.assertEqual(device.commands[-1], "STOP")
            self.assertFalse(any(command.startswith("KEEP ") for command in device.commands))

    def test_missing_arm_ack_never_fires(self):
        with TxSimulator(missing_arm=True) as device, redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "No ARMED"):
                tx.run_once(PROFILE, device.port, self.output)
            self.assertEqual(sum(command.startswith("ARM ") for command in device.commands), 1)
            self.assertFalse(any(command.startswith("FIRE ") for command in device.commands))
            self.assertEqual(device.commands[-1], "STOP")

    def test_keep_crossing_normal_completion_does_not_fire_again(self):
        # Completion can cross a host heartbeat in flight. The device ignores
        # KEEP while idle; its already queued DONE still completes this run.
        with TxSimulator(missing_done=True) as device, redirect_stdout(io.StringIO()):
            original_write = tx.SerialSession.write_command

            def crossing_keep(session, command):
                if command.startswith("KEEP ") and not device.finished:
                    device.done()
                return original_write(session, command)

            with patch.object(tx.SerialSession, "write_command", crossing_keep):
                result = tx.run_once(PROFILE, device.port, self.output)
            self.assertEqual(result["reason"], "complete")
            self.assertTrue(any(command.startswith("KEEP ") for command in device.commands))
            self.assertEqual(sum(command.startswith("FIRE ") for command in device.commands), 1)
            self.assertEqual(device.commands[-1], "STOP")

    def test_missing_done_has_bounded_timeout_and_stops(self):
        with TxSimulator(missing_done=True) as device, redirect_stdout(io.StringIO()):
            started = time.monotonic()
            with self.assertRaisesRegex(RuntimeError, "Timed out waiting for DONE"):
                tx.run_once(PROFILE, device.port, self.output)
            self.assertLess(time.monotonic() - started, 1)
            self.assertEqual(sum(command.startswith("FIRE ") for command in device.commands), 1)
            self.assertEqual(device.commands[-1], "STOP")

    def test_delayed_done_fits_duration_plus_two_second_response_window(self):
        # Model a response delivered after two seconds, carrying a valid
        # one-second device elapsed time, without sleeping or opening a device.
        clock = {"now": 0.0, "start_sent": False}
        token = "1234abcd"
        session = tx.SerialSession(-1, io.StringIO())
        session.token = token

        def read_line(deadline):
            if not clock["start_sent"]:
                clock["start_sent"] = True
                clock["now"] = 0.001
                return "START,token={},duration_ms={}".format(token, tx.DURATION_MS)
            clock["now"] = min(deadline, 2.5)
            if clock["now"] >= 2.5:
                return "DONE,token={},reason=complete,frames={},elapsed_ms={},radio=off".format(
                    token, tx.MAX_FRAMES, tx.DURATION_MS)
            return None

        event_timeout = tx.DURATION_MS / 1000.0 + 2.0
        self.assertEqual(event_timeout, 3.0)
        with patch.object(tx, "time", SimpleNamespace(monotonic=lambda: clock["now"])):
            with patch.object(tx, "EVENT_TIMEOUT_SECONDS", event_timeout):
                with patch.object(session, "read_line", side_effect=read_line):
                    with patch.object(session, "write_command") as write:
                        result = session.fire_once(token)
        commands = [call.args[0] for call in write.call_args_list]
        self.assertEqual(commands.count("FIRE " + token), 1)
        self.assertGreaterEqual(commands.count("KEEP " + token), 80)
        self.assertEqual((result["reason"], result["elapsed_ms"]), ("complete", str(tx.DURATION_MS)))

    def test_disconnect_after_fire_does_not_retry(self):
        with TxSimulator(disconnect_on_fire=True) as device, redirect_stdout(io.StringIO()):
            with self.assertRaises((OSError, RuntimeError)):
                tx.run_once(PROFILE, device.port, self.output)
            self.assertEqual(sum(command.startswith("FIRE ") for command in device.commands), 1)
            self.assertIn("HOST,command=STOP", Path(self.output).read_text())

    def test_noncomplete_reasons_are_failures(self):
        for reason in ("heartbeat", "radio_timeout", "stop", "deadline", "radio_error", "serial_error"):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as directory:
                with TxSimulator(reason=reason) as device, redirect_stdout(io.StringIO()):
                    with self.assertRaisesRegex(tx.ProtocolError, "reason=" + reason):
                        tx.run_once(PROFILE, device.port, str(Path(directory) / "tx.log"))
                    self.assertEqual(device.commands[-1], "STOP")

    def test_done_token_reason_bounds_and_radio_state_are_validated(self):
        token = "1234abcd"
        valid = "DONE,token=1234abcd,reason=complete,frames={},elapsed_ms={},radio=off".format(tx.MAX_FRAMES, tx.DURATION_MS)
        self.assertEqual(tx.validate_done(valid, token)["frames"], str(tx.MAX_FRAMES))
        for bad in (valid.replace(token, "deadbeef"), valid.replace("reason=complete", "reason=unknown"),
                    valid.replace("frames={}".format(tx.MAX_FRAMES), "frames={}".format(tx.MAX_FRAMES + 1)),
                    valid.replace("elapsed_ms={}".format(tx.DURATION_MS), "elapsed_ms={}".format(tx.DURATION_MS + 1)),
                    valid.replace("radio=off", "radio=on"), valid.replace("frames={}".format(tx.MAX_FRAMES), "frames=-1"),
                    valid + ",token=1234abcd"):
            with self.subTest(bad=bad), self.assertRaises(tx.ProtocolError):
                tx.validate_done(bad, token)

    def test_existing_output_does_not_open_or_reset_device(self):
        Path(self.output).write_text("earlier experiment\n")
        with patch.object(tx.os, "open") as open_device:
            with self.assertRaises(FileExistsError):
                tx.run_once(PROFILE, "/dev/not-opened", self.output)
            open_device.assert_not_called()
        self.assertEqual(Path(self.output).read_text(), "earlier experiment\n")

    def test_stop_bytes_attempted_even_when_logging_fails(self):
        session = tx.SerialSession(-1, io.StringIO())
        with patch.object(session, "emit", side_effect=OSError("log disk full")):
            with patch.object(session, "write_bytes") as write:
                with self.assertRaisesRegex(OSError, "log disk full"):
                    session.write_command("STOP")
                write.assert_called_once_with("STOP")

    def test_cli_requires_profile_and_fresh_output_and_offers_no_repeat(self):
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            tx.main(["--profile", PROFILE])
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            tx.main(["--profile", PROFILE, "--output", self.output, "--repeat", "2"])
        self.assertEqual(tx.hex8("A1B2C3D4"), PROFILE)
        for value in ("", "aabb", "aabbccdd00", "abcdabcd\nFIRE", "gggggggg"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                tx.hex8(value)

    def test_autodetection_requires_exactly_one_serial_port(self):
        with patch.object(tx.glob, "glob", side_effect=[["/dev/cu.usbserial-X"], []]):
            self.assertEqual(tx.resolve_port(None), "/dev/cu.usbserial-X")
        with patch.object(tx.glob, "glob", return_value=[]), self.assertRaisesRegex(RuntimeError, "--port"):
            tx.resolve_port(None)
        with patch.object(tx.glob, "glob", side_effect=[["/dev/a", "/dev/b"], []]), self.assertRaisesRegex(RuntimeError, "--port"):
            tx.resolve_port(None)


if __name__ == "__main__":
    unittest.main()
