#!/usr/bin/env python3
"""Verify curated actual target frames without accessing the original captures.

The source hashes and original line numbers are archival provenance only. A
whole-source hash cannot be reproduced from these excerpts and is never
compared with the hash of this JSON. All decoding uses only each included RAW
line and the existing independent receive-side decoder.
"""

from collections import Counter
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).resolve().parent / "fixtures/hs6200_captured_frames.json"
sys.path.insert(0, str(ROOT / "rf_nano_receiver_probe"))
from analyze_raw import RawRecord, parse_raw_line
from decode_hs6200 import decode_record


# These are observed full-template variants, not a universal command table.
VARIANTS = {
    "captured_tower_right_0d": ("tower_rotate_right", {5: 0, 6: 0x0D, 7: 0, 8: 0, 9: 0}),
    "captured_tower_left_04": ("tower_rotate_left", {5: 0, 6: 0x04, 7: 0, 8: 0, 9: 0}),
    "captured_boom_up_d0": ("main_boom_raise", {5: 0, 6: 0, 7: 0xD0, 8: 0, 9: 0}),
    "preferred_isolated_lower_70": ("main_boom_lower", {5: 0, 6: 0, 7: 0x70, 8: 0, 9: 0}),
    "historical_lower_72_coactuation_uncertain": ("main_boom_lower", {5: 0, 6: 0, 7: 0x72, 8: 0, 9: 0}),
    "captured_left_forward_07": ("left_track_forward", {5: 0x07, 6: 0, 7: 0, 8: 0, 9: 0}),
    "captured_right_forward_70": ("right_track_forward", {5: 0x70, 6: 0, 7: 0, 8: 0, 9: 0}),
}


class CapturedFrames(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        cls.rows = cls.fixture["frames"]

    def test_provenance_counts_and_known_control_tags(self):
        self.assertEqual(self.fixture["schema_version"], 1)
        self.assertEqual(self.fixture["kind"], "curated_actual_target_radio_frames")
        self.assertEqual(len(self.rows), 12)
        self.assertEqual(len({row["id"] for row in self.rows}), 12)
        self.assertEqual(Counter(row["control_tag"] for row in self.rows), {
            "tower_rotate_right": 1, "tower_rotate_left": 1,
            "main_boom_raise": 2, "main_boom_lower": 4,
            "left_track_forward": 2, "right_track_forward": 2,
        })
        source_hashes = {}
        for row in self.rows:
            with self.subTest(frame=row["id"]):
                source = row["source"]
                filename = source["original_filename"]
                self.assertEqual(Path(filename).name, filename)
                self.assertTrue(filename.endswith("_raw.log"))
                self.assertRegex(source["full_source_sha256"], r"^[0-9a-f]{64}$")
                self.assertIs(type(source["original_line"]), int)
                self.assertGreater(source["original_line"], 0)
                self.assertEqual(source_hashes.setdefault(filename, source["full_source_sha256"]), source["full_source_sha256"])
                self.assertIn(row["variant"], VARIANTS)
                self.assertEqual(row["control_tag"], VARIANTS[row["variant"]][0])
                if filename == "2026-11-01_left_track_backward_raw.log":
                    self.assertEqual(row["control_tag"], "right_track_forward")
                    self.assertIn("RIGHT FORWARD", row["source_label_correction"])
        self.assertEqual(len(source_hashes), 6)

    def check_decoded_row(self, row):
        expected = row["expected"]
        record = parse_raw_line(row["raw_line"], row["source"]["original_line"])
        self.assertIsNotNone(record)
        self.assertEqual(len(record.data), 32)
        self.assertTrue(record.timestamp.startswith(row["source"]["original_filename"][:10]))
        self.assertEqual(record.channel, expected["channel"])
        self.assertEqual(2400 + record.channel, expected["mhz"])
        self.assertEqual(record.pipe, expected["pipe"])
        address = bytes.fromhex(expected["air_address_hex"])
        self.assertEqual(len(address), 5)
        matches = [frame for frame in decode_record(record, address, max_missing_address_bits=2)
                   if frame.crc_valid and frame.bit_offset == expected["bit_offset"]]
        self.assertEqual(len(matches), 1)
        frame = matches[0]
        self.assertEqual(frame.address.hex(), expected["air_address_hex"])
        self.assertEqual(frame.payload_length, expected["payload_length"])
        self.assertEqual(frame.payload_length, 11)
        self.assertEqual(frame.payload.hex(), expected["payload_hex"])
        self.assertEqual(frame.payload[:4], bytes.fromhex("15130188"))
        self.assertEqual(frame.payload[-1], (sum(frame.payload[:-1]) + 2) & 255)
        self.assertEqual(frame.pid, expected["pid"])
        self.assertEqual(frame.no_ack, expected["no_ack"])
        self.assertEqual(frame.no_ack, 1)
        self.assertEqual(format(frame.received_crc, "04x"), expected["crc16"])
        self.assertEqual(frame.calculated_crc, frame.received_crc)
        self.assertEqual(frame.reconstructed_address_bits, expected["missing_leading_address_bits"])
        self.assertEqual(frame.reconstructed_address_bits, max(0, -frame.bit_offset))
        if frame.reconstructed_address_bits:
            self.assertIn("not captured", row["address_reconstruction_note"])
        raw_bits = "".join(format(byte, "08b") for byte in record.data)
        address_bits = "".join(format(byte, "08b") for byte in address)
        missing = frame.reconstructed_address_bits
        aligned = address_bits[:missing] + raw_bits[max(0, frame.bit_offset):]
        guards = bytes([int(aligned[40:48], 2), int(aligned[48:56], 2)])
        self.assertEqual(guards, address[-1:] * 2)
        self.assertEqual(guards.hex(), expected["guards_hex"])
        for index, value in VARIANTS[row["variant"]][1].items():
            self.assertEqual(frame.payload[index], value)

    def test_all_actual_frames_decode_with_file_access_disabled(self):
        # Load only the curated fixture in setUpClass. Decoder checks must not
        # fall back to the private original captures or any other source file.
        with patch.object(Path, "open", side_effect=AssertionError("Decoder attempted file access")):
            with patch("builtins.open", side_effect=AssertionError("Decoder attempted file access")):
                for row in self.rows:
                    with self.subTest(frame=row["id"]):
                        self.check_decoded_row(row)

    def test_crc_or_guard_damage_is_rejected(self):
        row = self.rows[0]
        original = parse_raw_line(row["raw_line"])
        self.assertEqual(row["expected"]["bit_offset"], 0)
        for bit in (40, 65):
            with self.subTest(changed_bit=bit):
                damaged = bytearray(original.data)
                damaged[bit // 8] ^= 1 << (7 - bit % 8)
                matches = decode_record(RawRecord(original.channel, original.pipe, bytes(damaged)),
                                        bytes.fromhex(row["expected"]["air_address_hex"]))
                self.assertFalse(any(frame.crc_valid for frame in matches))


if __name__ == "__main__":
    unittest.main(verbosity=2)
