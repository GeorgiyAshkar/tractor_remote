"""Offline profile export verifies captured bits and refuses implicit selection."""

from contextlib import redirect_stdout, redirect_stderr
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import make_profile as profile
from analyze_raw import RawRecord
from decode_hs6200 import decode_record


AIR_ADDRESS = "8d5fb5cb75"
FRAME_A = "8d5fb5cb7575752ecaf31d428a157ade28c73a1cf480"
FRAME_B = "8d5fb5cb7575752fcaf31d428a157ade28c73a1f8180"
LEFT = "8d5fb5cb7575752dcaf31d42da157ede28c76e608b80"
# Another independently captured CRC-valid address/setup candidate.
OTHER = "529c546e5c5c5c2cfa9f47297015f657a8036c9463182046614ca80a04c41554"


def raw_line(encoded, channel=15):
    data = bytes.fromhex(encoded)
    data += b"\xff" * (32 - len(data))
    return ("2026-11-01T12:00:00+03:00 RAW,channel={},mhz={},pipe=1,bytes=32,hex={}\n"
            .format(channel, 2400 + channel, data.hex()))


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "fresh_capture.log"
        self.path.write_text(raw_line(FRAME_A) + raw_line(FRAME_B))

    def test_exact_captured_pid_pair_and_input_hash(self):
        capture = profile.load_capture(self.path)
        selected = profile.select_profile(capture, AIR_ADDRESS, 1)
        self.assertEqual(selected["frame_a"]["encoded_hex"], FRAME_A)
        self.assertEqual(selected["frame_b"]["encoded_hex"], FRAME_B)
        self.assertEqual(selected["selection"], "captured_different_pid_pair")
        self.assertEqual(selected["frame_a"]["pid"], 2)
        self.assertEqual(selected["frame_b"]["pid"], 3)
        self.assertEqual(selected["frame_a"]["payload_hex"], "1513018879000c00000038")
        self.assertEqual(selected["source_sha256"], hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual(selected["radio_preamble_address_hex"], "5555555555")
        self.assertEqual(len(selected["profile_id"]), 8)
        self.assertEqual(selected["frame_a"]["address_bits_reconstructed"], 0)

    def test_crc_corruption_is_not_a_candidate(self):
        damaged = bytearray.fromhex(FRAME_A)
        damaged[20] ^= 1
        self.path.write_text(raw_line(damaged.hex()) + raw_line(FRAME_B))
        capture = profile.load_capture(self.path)
        self.assertEqual(len(capture["observations"]), 1)
        self.assertEqual(profile.eligible_groups(capture), {})
        with self.assertRaises(ValueError):
            profile.select_profile(capture, AIR_ADDRESS, 2)

    def test_same_frame_is_duplicated_without_inventing_fields(self):
        self.path.write_text(raw_line(LEFT, 31) + raw_line(LEFT, 15))
        selected = profile.select_profile(profile.load_capture(self.path), AIR_ADDRESS, 2)
        self.assertEqual(selected["channel"], 15)
        self.assertEqual(selected["selection"], "duplicate_exact_frame")
        self.assertEqual(selected["frame_a"], selected["frame_b"])
        self.assertEqual(selected["frame_a"]["encoded_hex"], LEFT)
        self.assertEqual(selected["frame_a"]["payload_hex"], "15130188d9000400000090")
        self.assertEqual(selected["frame_a"]["pid"], 1)

    def test_multiple_addresses_are_reviewed_without_auto_selection_or_writes(self):
        self.path.write_text(self.path.read_text() + raw_line(OTHER, 50) * 2)
        with redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(profile.main([str(self.path)]), 0)
        report = json.loads(stdout.getvalue())
        self.assertEqual({row["air_address_hex"] for row in report["addresses"]},
                         {AIR_ADDRESS, "529c546e5c"})
        self.assertNotIn("selected_profile", report)
        self.assertFalse(report["header_written"])
        self.assertEqual(sorted(path.name for path in self.path.parent.iterdir()), [self.path.name])
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            profile.main([str(self.path), "--write-header", str(self.path.parent / "profile.h")])

    def test_write_requires_explicit_address_and_line_and_header_has_fixed_constants(self):
        destination = self.path.parent / "profile.h"
        with redirect_stdout(io.StringIO()) as stdout:
            profile.main([str(self.path), "--address", AIR_ADDRESS, "--line", "1",
                          "--write-header", str(destination)])
        report = json.loads(stdout.getvalue())
        self.assertTrue(report["header_written"])
        header = destination.read_text()
        for expected in ("PROFILE_READY = true", "PROFILE_FRAME_A[22]", "PROFILE_FRAME_B[22]",
                         "PROFILE_FRAME_SIZE = 22", "PROFILE_CHANNEL = 15", "PROFILE_SOURCE"):
            self.assertIn(expected, header)
        self.assertIn(report["source_sha256"], header)
        self.assertNotIn("Serial", header)
        self.assertNotIn("writeFast", header)

    def test_difficult_source_filename_cannot_inject_header(self):
        dangerous = self.path.parent / 'bad"capture.log'
        dangerous.write_bytes(self.path.read_bytes())
        selected = profile.select_profile(profile.load_capture(dangerous), AIR_ADDRESS, 1)
        with self.assertRaisesRegex(ValueError, "ASCII"):
            profile.header_text(selected)
        # Review remains valid JSON even for an unsafe source label.
        with redirect_stdout(io.StringIO()) as stdout:
            profile.main([str(dangerous)])
        self.assertEqual(json.loads(stdout.getvalue())["source"], str(dangerous))

    def test_header_cannot_overwrite_input(self):
        before = self.path.read_bytes()
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            profile.main([str(self.path), "--address", AIR_ADDRESS, "--line", "1",
                          "--write-header", str(self.path)])
        self.assertEqual(self.path.read_bytes(), before)

    def test_first_channel_is_used_even_if_pair_was_seen_elsewhere(self):
        self.path.write_text(raw_line(FRAME_A, 31) + raw_line(FRAME_B, 63))
        selected = profile.select_profile(profile.load_capture(self.path), AIR_ADDRESS, 1)
        self.assertEqual((selected["channel"], selected["frame_b"]["channel"]), (31, 63))

    def test_reconstructed_bits_are_reported_and_complete_partner_is_preferred(self):
        raw = "".join(format(byte, "08b") for byte in bytes.fromhex(FRAME_B)) + "1" * 100
        cropped = raw[2:258]
        data = bytes(int(cropped[index:index + 8], 2) for index in range(0, 256, 8))
        self.path.write_text(raw_line(FRAME_A) + raw_line(data.hex()) + raw_line(FRAME_B))
        capture = profile.load_capture(self.path)
        selected = profile.select_profile(capture, AIR_ADDRESS, 1)
        self.assertEqual(selected["frame_b"]["line"], 3)
        partial = profile.select_profile(capture, AIR_ADDRESS, 2)
        self.assertEqual(partial["frame_a"]["address_bits_reconstructed"], 2)
        self.assertIn("not captured", partial["frame_a"]["reconstruction_assumption"])

    def test_second_frame_cannot_change_payload_or_counter(self):
        self.path.write_text(self.path.read_text() + raw_line(LEFT))
        with self.assertRaisesRegex(ValueError, "same captured"):
            profile.select_profile(profile.load_capture(self.path), AIR_ADDRESS, 1, 3)

    def test_new_capture_address_and_four_byte_prefix_are_not_hardcoded(self):
        # Use another actual captured envelope: its header is not 15130188.
        self.path.write_text(raw_line(OTHER, 50) * 2)
        selected = profile.select_profile(profile.load_capture(self.path), "529c546e5c", 1)
        self.assertEqual(selected["frame_a"]["payload_hex"], "75cbb55f8d011513018895")
        encoded = bytes.fromhex(selected["frame_a"]["encoded_hex"])
        decoded = decode_record(RawRecord(50, 0, encoded + b"\xff" * 10),
                                bytes.fromhex("529c546e5c"), max_missing_address_bits=0)
        self.assertTrue(decoded[0].crc_valid)
        self.assertEqual(selected["radio_preamble_address_hex"], "aaaaaaaaaa")

    def test_constant_noise_and_empty_payloads_do_not_qualify(self):
        self.path.write_text(raw_line("00" * 32) + raw_line("ff" * 32))
        capture = profile.load_capture(self.path)
        self.assertEqual(capture["observations"], [])
        self.assertEqual(profile.review_capture(capture)["addresses"], [])

    def test_advance_mode_is_explicit_and_changes_profile_id(self):
        capture = profile.load_capture(self.path)
        exact = profile.select_profile(capture, AIR_ADDRESS, 1)
        advancing = profile.select_profile(capture, AIR_ADDRESS, 1, advance_counters=True)
        self.assertFalse(exact["advance_counters"])
        self.assertEqual(exact["mode"], "exact_captured_replay")
        self.assertIsNone(exact["counter_transformation"])
        self.assertTrue(advancing["advance_counters"])
        self.assertEqual(advancing["mode"], "counter_advance_hypothesis")
        self.assertIn("not exact replay", advancing["counter_transformation"]["status"])
        self.assertNotEqual(exact["profile_id"], advancing["profile_id"])
        self.assertEqual(advancing["frame_a"]["encoded_hex"], FRAME_A)
        self.assertEqual(advancing["frame_b"]["encoded_hex"], FRAME_B)
        self.assertEqual(advancing["frame_a"]["payload_hex"], "1513018879000c00000038")
        self.assertEqual(advancing["counter_start"], 0x7A)
        self.assertEqual(advancing["counter_period_ms"], 128)

    def test_counter_comes_from_last_same_address_frame_not_selected_or_other_address(self):
        initial = profile.select_profile(profile.load_capture(self.path), AIR_ADDRESS, 1,
                                         advance_counters=True)
        self.path.write_text(raw_line(FRAME_A) + raw_line(FRAME_B)
                             + raw_line(LEFT) + raw_line(OTHER, 50))
        selected = profile.select_profile(profile.load_capture(self.path), AIR_ADDRESS, 1,
                                          advance_counters=True)
        self.assertEqual(selected["counter_start"], 0xDA)
        self.assertEqual(selected["counter_source"]["line"], 3)
        self.assertNotEqual(selected["profile_id"], initial["profile_id"])
        self.assertEqual(selected["frame_a"]["payload_hex"], "1513018879000c00000038")
        self.assertEqual(selected["counter_transformation"]["unchanged_payload_indices"],
                         [0, 1, 2, 3, 5, 6, 7, 8, 9])

    def test_counter_wraps_after_last_crc_valid_ff_fixture(self):
        # Synthetic offline boundary fixture, CRC16 c27b, counter ff. This is
        # test input only and is never selected for the physical TX profile.
        boundary = "8d5fb5cb7575752ecaf31d42c9157ade28c779613d80"
        self.path.write_text(self.path.read_text() + raw_line(boundary))
        selected = profile.select_profile(profile.load_capture(self.path), AIR_ADDRESS, 1,
                                          advance_counters=True)
        self.assertEqual(selected["counter_source"]["crc16"], "c27b")
        self.assertEqual(selected["counter_source"]["payload_hex"], "15130188ff000c000000be")
        self.assertEqual(selected["counter_start"], 0)
        self.assertEqual(selected["frame_a"]["encoded_hex"], FRAME_A)

    def test_counter_period_is_included_in_profile_identity(self):
        capture = profile.load_capture(self.path)
        initial = profile.select_profile(capture, AIR_ADDRESS, 1, advance_counters=True)
        with patch.object(profile, "COUNTER_PERIOD_MS", 129):
            changed = profile.select_profile(capture, AIR_ADDRESS, 1, advance_counters=True)
        self.assertNotEqual(initial["profile_id"], changed["profile_id"])

    def test_both_header_modes_expose_counter_inputs_and_exact_reference_arrays(self):
        capture = profile.load_capture(self.path)
        for advancing in (False, True):
            with self.subTest(advancing=advancing):
                selected = profile.select_profile(capture, AIR_ADDRESS, 1,
                                                  advance_counters=advancing)
                header = profile.header_text(selected)
                self.assertIn("PROFILE_ADVANCE_COUNTERS = " + str(advancing).lower(), header)
                self.assertIn("PROFILE_AIR_ADDRESS[5]", header)
                self.assertIn("PROFILE_PAYLOAD[11]", header)
                self.assertIn("PROFILE_COUNTER_START = 0x7a", header)
                self.assertIn("PROFILE_COUNTER_PERIOD_MS = 128", header)
                self.assertIn("PROFILE_FRAME_A[22]", header)
                self.assertIn("PROFILE_FRAME_B[22]", header)

    def test_advance_cli_requires_selection_and_reports_transformation_without_auto_write(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            profile.main([str(self.path), "--advance-counters"])
        with redirect_stdout(io.StringIO()) as stdout:
            profile.main([str(self.path), "--address", AIR_ADDRESS, "--line", "1", "--advance-counters"])
        report = json.loads(stdout.getvalue())
        self.assertFalse(report["header_written"])
        self.assertEqual(report["selected_profile"]["selection"], "transformed_counter_hypothesis")
        self.assertIn("unverified", report["selected_profile"]["counter_transformation"]["status"])

    def test_counter_start_override_changes_identity_and_preserves_captured_command(self):
        capture = profile.load_capture(self.path)
        derived = profile.select_profile(capture, AIR_ADDRESS, 1, advance_counters=True)
        override = profile.select_profile(capture, AIR_ADDRESS, 1, advance_counters=True,
                                           counter_start_override=0x30)
        self.assertEqual(override["counter_start"], 0x30)
        self.assertEqual(override["derived_counter_start"], derived["counter_start"])
        self.assertEqual(override["counter_start_source"], "explicit_override")
        self.assertNotEqual(override["profile_id"], derived["profile_id"])
        self.assertEqual(override["frame_a"], derived["frame_a"])
        self.assertIn("PROFILE_COUNTER_START = 0x30", profile.header_text(override))

    def test_counter_override_accepts_decimal_and_hex_without_writing_header(self):
        profiles = []
        for value in ("48", "0x30"):
            with redirect_stdout(io.StringIO()) as stdout:
                profile.main([str(self.path), "--address", AIR_ADDRESS, "--line", "1",
                              "--advance-counters", "--counter-start", value])
            report = json.loads(stdout.getvalue())
            self.assertFalse(report["header_written"])
            self.assertEqual(report["selected_profile"]["counter_start"], 48)
            profiles.append(report["selected_profile"])
        self.assertEqual(profiles[0]["profile_id"], profiles[1]["profile_id"])

    def test_counter_override_requires_advance_mode_and_rejects_invalid_values(self):
        for arguments in (["--counter-start", "48"],
                          ["--advance-counters", "--counter-start", "256"],
                          ["--advance-counters", "--counter-start", "-1"],
                          ["--advance-counters", "--counter-start", "0x100"],
                          ["--advance-counters", "--counter-start", "3.5"]):
            with self.subTest(arguments=arguments):
                with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    profile.main([str(self.path), "--address", AIR_ADDRESS, "--line", "1"] + arguments)
        with self.assertRaisesRegex(ValueError, "advancement"):
            profile.select_profile(profile.load_capture(self.path), AIR_ADDRESS, 1,
                                   counter_start_override=48)


if __name__ == "__main__":
    unittest.main()
