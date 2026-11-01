"""Clock/serial simulations; this suite never opens a USB device or emits RF."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

try:
    from .controller import Controller, DemoSerial, CounterStore, BusyError, ControlError
except ImportError:
    from controller import Controller, DemoSerial, CounterStore, BusyError, ControlError


CLIENT = "a" * 32
OTHER = "b" * 32


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeSerial(DemoSerial):
    def __init__(self, clock):
        super().__init__(clock)
        self.commands = []
        self.hold_arm = False
        self.fail_fire = False
        self.on_fire = None
        self.old_firmware = False

    def write(self, command):
        self.commands.append(command)
        if command.startswith("FIRE"):
            if self.on_fire:
                self.on_fire()
            if self.fail_fire:
                raise OSError("simulated uncertain FIRE write")
        if self.old_firmware and command == "HELLO":
            self.lines.append("READY,protocol=tractortx-v1,radio=ready")
        elif self.hold_arm and command.startswith("ARM"):
            self.armed = tuple(command.split()[1:])
        else:
            super().write(command)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "runtime/state.json"
        self.clock = Clock()
        self.ports = ["/fake/cu.usbserial-test"]
        self.instances = []
        self.nonce = 0
        self.controls = []

    def make(self, **kwargs):
        def factory(port):
            serial = FakeSerial(self.clock)
            self.instances.append(serial)
            return serial
        def nonce():
            self.nonce += 1
            return format(self.nonce, "08x")
        defaults = dict(clock=self.clock, port_finder=lambda: self.ports[:], transport_factory=factory,
                        store_factory=lambda: CounterStore(self.path, 0xF6),
                        nonce_factory=nonce, startup_delay=0)
        defaults.update(kwargs)
        control = Controller(**defaults)
        self.controls.append(control)
        self.addCleanup(control.close)
        return control

    def ready(self, control):
        for _ in range(4):
            control.step()
        self.assertEqual(control.status()["board"]["state"], "ready")
        return control.status()["board"]["token"]

    def hold(self, control, seq=1, action="tower_right"):
        token = control.status()["board"]["token"]
        control.set_power(True, token)
        control.control(CLIENT, seq, action, token)
        for _ in range(3):
            control.step()
        self.assertTrue(control.status()["control"]["transmitting"])

    def test_connect_and_status_do_not_arm_or_fire_and_confirmation_starts_false(self):
        control = self.make()
        for _ in range(5):
            control.status()
        self.assertEqual(self.instances, [])
        self.ready(control)
        self.assertFalse(control.status()["tractor"]["confirmed"])
        self.assertEqual(self.instances[0].commands, ["HELLO"])
        self.assertFalse(self.path.exists())

    def test_commit_and_fsync_happen_before_fire_even_if_fire_write_fails(self):
        control = self.make()
        self.ready(control)
        serial = self.instances[0]
        serial.fail_fire = True
        def check_commit():
            state = json.loads(self.path.read_text())
            self.assertEqual(state["next_counter"], 0xFE)
            self.assertEqual(state["reservations"][-1]["token"], "00000001")
        serial.on_fire = check_commit
        token = control.status()["board"]["token"]
        control.set_power(True, token)
        control.control(CLIENT, 1, "tower_right", token)
        control.step()
        control.step()
        self.assertEqual(control.status()["board"]["state"], "error")
        self.assertFalse(control.status()["tractor"]["confirmed"])
        self.assertEqual(sum(command.startswith("FIRE") for command in serial.commands), 1)
        reopened = CounterStore(self.path)
        try:
            self.assertEqual(reopened.reserve("TR", "00000002"), 0xFE)
        finally:
            reopened.close()

    def test_durable_write_failure_prevents_arm_and_fire(self):
        control = self.make()
        token = self.ready(control)
        control.set_power(True, token)
        control.control(CLIENT, 1, "boom_up", token)
        module = Controller.__module__
        with patch(module + ".os.fsync", side_effect=OSError("disk failed")):
            control.step()
        self.assertFalse(any(command.startswith(("ARM", "FIRE")) for command in self.instances[0].commands))
        self.assertFalse(control.status()["tractor"]["confirmed"])

    def test_lease_expiry_stops_without_another_keep(self):
        control = self.make()
        self.ready(control)
        self.hold(control)
        serial = self.instances[0]
        old = len(serial.commands)
        self.clock.advance(0.351)
        control.step()
        control.step()
        self.assertIn("STOP", serial.commands[old:])
        self.assertFalse(any(line.startswith("KEEP") for line in serial.commands[old:]))
        self.assertFalse(control.status()["control"]["transmitting"])
        self.assertIsNone(control.status()["control"]["owner"])

    def test_expired_lease_cannot_be_revived_by_a_delayed_higher_sequence(self):
        control = self.make()
        token = self.ready(control)
        self.hold(control)
        self.clock.advance(0.351)
        # No status/worker iteration has expired it yet. The request itself
        # must expire the old lease before checking its connection fence.
        with self.assertRaises(ControlError):
            control.control(CLIENT, 100, "tower_right", token)
        control.step()
        self.assertIsNone(self.instances[0].active)
        self.assertIsNone(control.action)

    def test_another_tab_stop_fences_owner_renewals_but_idle_stop_does_not_rotate(self):
        control = self.make()
        token = self.ready(control)
        self.hold(control)
        control.stop(OTHER, 1)
        fenced = control.status()["board"]["token"]
        self.assertNotEqual(token, fenced)
        self.assertTrue(control.confirmed)
        with self.assertRaises(ControlError):
            control.control(CLIENT, 100, "tower_right", token)
        control.step()
        control.step()
        self.assertIsNone(self.instances[0].active)
        control.stop(OTHER, 2)
        self.assertEqual(fenced, control.status()["board"]["token"])

    def test_owner_stop_uses_sequence_fence_and_allows_a_fresh_gesture(self):
        control = self.make()
        token = self.ready(control)
        self.hold(control)
        control.stop(CLIENT, 3)
        self.assertEqual(control.status()["board"]["token"], token)
        with self.assertRaises(ControlError):
            control.control(CLIENT, 2, "tower_right", token)
        control.control(CLIENT, 4, "boom_up", token)
        for _ in range(5):
            control.step()
        self.assertEqual(control.action, "boom_up")
        self.assertTrue(control.status()["control"]["transmitting"])
        self.assertEqual(control.status()["board"]["token"], token)

    def test_release_immediately_stops_active_chunk(self):
        control = self.make()
        token = self.ready(control)
        self.hold(control)
        control.control(CLIENT, 2, None, token)
        control.step()
        self.assertIsNone(self.instances[0].active)
        control.step()
        self.assertEqual(control.status()["control"]["state"], "idle")

    def test_delayed_armed_reply_after_release_cannot_fire(self):
        control = self.make()
        token = self.ready(control)
        serial = self.instances[0]
        serial.hold_arm = True
        control.set_power(True, token)
        control.control(CLIENT, 1, "boom_down", token)
        control.step()
        control.control(CLIENT, 2, None, token)
        # An in-flight ARMED response arrives after the release.
        serial.lines.append("ARMED,action=BD,counter=f6,token=00000001")
        control.step()
        control.step()
        self.assertFalse(any(command.startswith("FIRE") for command in serial.commands))
        self.assertEqual(control.phase, "idle")

    def test_sequence_fences_reordered_release_and_start_and_owner_is_exclusive(self):
        control = self.make()
        token = self.ready(control)
        control.set_power(True, token)
        control.control(CLIENT, 4, "left_forward", token)
        with self.assertRaises(ControlError):
            control.control(OTHER, 1, "right_forward", token)
        control.control(CLIENT, 6, None, token)
        with self.assertRaises(ControlError):
            control.control(CLIENT, 5, "left_forward", token)
        control.step()
        self.assertFalse(any(command.startswith("FIRE") for command in self.instances[0].commands))

    def test_disconnect_and_reconnect_clear_confirmation_and_fence_old_requests(self):
        control = self.make()
        old_token = self.ready(control)
        self.hold(control)
        self.ports.clear()
        self.clock.advance(0.26)
        control.step()
        self.assertFalse(control.status()["tractor"]["confirmed"])
        self.assertEqual(control.status()["board"]["state"], "missing")
        self.assertIsNone(control.status()["control"]["owner"])
        self.ports.append("/fake/cu.usbserial-test")
        self.clock.advance(1.1)
        new_token = self.ready(control)
        self.assertNotEqual(old_token, new_token)
        control.set_power(True, new_token)
        with self.assertRaises(ControlError):
            control.control(CLIENT, 100, "tower_left", old_token)
        self.assertFalse(any(command.startswith("FIRE") for command in self.instances[-1].commands))
        with self.assertRaises(ControlError):
            control.set_power(True, old_token)

    def test_live_hold_renews_with_fresh_tokens_and_persistent_counter_ranges(self):
        control = self.make()
        board_token = self.ready(control)
        self.hold(control)
        for seq in range(2, 14):
            self.clock.advance(0.1)
            control.control(CLIENT, seq, "tower_right", board_token)
            for _ in range(3):
                control.step()
        state = json.loads(self.path.read_text())
        self.assertEqual([record["counter_hex"] for record in state["reservations"]], ["f6", "fe"])
        arm = [line for line in self.instances[0].commands if line.startswith("ARM")]
        self.assertEqual(arm, ["ARM TR f6 00000001", "ARM TR fe 00000002"])
        # No idle HELLO/STATUS/PING was sent during motion.
        self.assertEqual([line for line in self.instances[0].commands if line in ("HELLO", "PING", "STATUS")], ["HELLO"])

    def test_old_firmware_is_rejected_before_counter_store_or_rf(self):
        control = self.make(store_factory=lambda: self.fail("store opened for old firmware"))
        control.step()
        self.instances[0].old_firmware = True
        for _ in range(3):
            control.step()
        self.assertEqual(control.status()["board"]["state"], "firmware_required")
        self.assertFalse(any(line.startswith(("ARM", "FIRE")) for line in self.instances[0].commands))

    def test_old_probe_bare_error_is_firmware_required(self):
        control = self.make()
        control.step()
        self.instances[0].lines.append("ERR,unknown_command")
        control.step()
        self.assertEqual(control.status()["board"]["state"], "firmware_required")

    def test_incompatible_limits_are_rejected_before_store_or_rf(self):
        control = self.make(store_factory=lambda: self.fail("store opened for incompatible limits"))
        control.step()
        control.step()
        self.instances[0].lines.clear()
        self.instances[0].lines.append("READY,protocol=tractorctl-v1,radio=ready,armed=no,active=no,max_duration_ms=5000,heartbeat_ms=100,max_frames=1000")
        control.step()
        self.assertEqual(control.status()["board"]["state"], "firmware_required")
        self.assertEqual(self.instances[0].commands, ["HELLO"])

    def test_corrupt_ledger_blocks_ready_and_does_not_transmit(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("broken ledger")
        control = self.make()
        for _ in range(4):
            control.step()
        self.assertEqual(control.status()["board"]["state"], "error")
        self.assertFalse(control.status()["control"]["enabled"])
        self.assertEqual(self.instances[0].commands, ["HELLO"])
        self.assertEqual(self.path.read_text(), "broken ledger")

    def test_multiple_ports_and_busy_port_do_not_open_counter_store(self):
        control = self.make(store_factory=lambda: self.fail("store opened without serial exclusivity"))
        self.ports.append("/fake/other")
        control.step()
        self.assertEqual(control.status()["board"]["state"], "error")
        self.assertEqual(self.instances, [])
        self.ports.pop()
        control.transport_factory = lambda port: (_ for _ in ()).throw(BusyError("busy"))
        self.clock.advance(1)
        control.step()
        self.assertEqual(control.status()["board"]["state"], "busy")

    def test_demo_never_discovers_or_opens_devices_and_does_not_touch_physical_state(self):
        poison = lambda *args: self.fail("demo attempted hardware access")
        control = Controller(mode="demo", clock=self.clock, port_finder=poison, transport_factory=poison)
        self.addCleanup(control.close)
        module = Controller.__module__
        with patch(module + ".os.open", side_effect=AssertionError("demo opened a file/device")):
            token = self.ready(control)
            self.hold(control)
            control.set_demo_board(False)
            control.step()
            self.assertFalse(control.status()["tractor"]["confirmed"])
            control.set_demo_board(True)
            self.ready(control)
            self.assertNotEqual(token, control.status()["board"]["token"])

    def test_demo_explicit_state_cannot_target_the_physical_ledger(self):
        physical = Path(__file__).resolve().parent / "runtime/state.json"
        with self.assertRaisesRegex(ValueError, "independent"):
            Controller(mode="demo", state_file=physical)

    def test_fresh_clone_uses_public_config_without_private_experiment_history(self):
        clone = Path(self.temp.name) / "clone"
        config = clone / "web_control/config.json"
        config.parent.mkdir(parents=True)
        config.write_bytes((Path(__file__).resolve().parent / "config.json").read_bytes())
        self.assertFalse((clone / "rf_nano_tx_test/experiment_state.json").exists())
        control = self.make(store_factory=None)
        with patch(Controller.__module__ + ".ROOT", clone):
            self.ready(control)
            self.hold(control)
        self.assertIn("ARM TR f6 00000001", self.instances[-1].commands)
        ledger = clone / "web_control/runtime/state.json"
        self.assertEqual(json.loads(ledger.read_text())["next_counter"], 0xFE)

    def test_persisted_ledger_takes_precedence_over_public_bootstrap(self):
        clone = Path(self.temp.name) / "clone"
        config = clone / "web_control/config.json"
        config.parent.mkdir(parents=True)
        config.write_text('{"initial_counter_hex":"00"}')
        ledger = clone / "web_control/runtime/state.json"
        store = CounterStore(ledger, 0xFE)
        store.reserve("LF", "previous")
        store.close()
        control = self.make(store_factory=None)
        with patch(Controller.__module__ + ".ROOT", clone):
            self.ready(control)
            self.hold(control)
        self.assertIn("ARM TR 06 00000001", self.instances[-1].commands)
        self.assertEqual(json.loads(ledger.read_text())["next_counter"], 0x10E)

    def test_missing_or_invalid_public_config_blocks_ready_and_arm(self):
        clone = Path(self.temp.name) / "clone"
        config = clone / "web_control/config.json"
        config.parent.mkdir(parents=True)
        invalid = (None, "broken JSON", "[]", "{}", '{"initial_counter_hex":246}',
                   '{"initial_counter_hex":"F6"}', '{"initial_counter_hex":"0xf6"}',
                   '{"initial_counter_hex":"f6 "}', '{"initial_counter_hex":"f6","extra":0}')
        for content in invalid:
            with self.subTest(config=content):
                if config.exists():
                    config.unlink()
                if content is not None:
                    config.write_text(content)
                control = self.make(store_factory=None, state_file=self.path)
                with patch(Controller.__module__ + ".ROOT", clone):
                    for _ in range(4):
                        control.step()
                self.assertEqual(control.status()["board"]["state"], "error")
                self.assertIn("web_control/config.json", control.status()["board"]["message"])
                self.assertFalse(control.status()["control"]["enabled"])
                self.assertEqual(self.instances[-1].commands, ["HELLO"])
                self.assertFalse(self.path.exists())


class CounterStoreTests(unittest.TestCase):
    def test_reservation_survives_restart_and_wraps_only_the_transmitted_byte(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            store = CounterStore(path, 0xFE)
            self.assertEqual(store.reserve("LF", "00000001"), 0xFE)
            store.close()
            store = CounterStore(path, 0)
            try:
                self.assertEqual(store.reserve("RF", "00000002"), 0x06)
                self.assertEqual(json.loads(path.read_text())["next_counter"], 0x10E)
                with self.assertRaises(BusyError):
                    CounterStore(path)
            finally:
                store.close()

    def test_corrupt_or_rolled_back_ledger_is_not_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            for content in ("not json", '{"schema_version":1,"next_counter":0,"reservations":[{"ordinal":246,"size":8,"counter_hex":"f6"}]}'):
                path.write_text(content)
                with self.assertRaises(ValueError):
                    CounterStore(path, 0xF6)
                self.assertEqual(path.read_text(), content)


if __name__ == "__main__":
    unittest.main()
