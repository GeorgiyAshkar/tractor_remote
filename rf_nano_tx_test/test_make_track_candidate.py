"""Offline transformations preserve observed templates and remain unconfirmed."""

from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import unittest

import make_profile
import make_track_candidate as candidate


MAPPING = Path(__file__).resolve().parent / "control_mapping_2026-11-01"


class TrackCandidateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.left = Path(self.temp.name) / "left.json"
        self.right = Path(self.temp.name) / "right.json"
        self.left.write_bytes((MAPPING / "left_track_forward_01_profile.json").read_bytes())
        self.right.write_bytes((MAPPING / "right_track_forward_01_profile.json").read_bytes())

    def build(self, hypothesis="both-forward", start=0xD6):
        return candidate.make_candidate(hypothesis, start, self.left, self.right)

    def test_whitelist_preserves_id_other_actions_counter_seed_and_reference_frames(self):
        ids = set()
        for hypothesis, slot, side in (("left-backward", 0x0F, self.left),
                                       ("right-backward", 0xF0, self.right),
                                       ("both-forward", 0x77, self.left)):
            with self.subTest(hypothesis=hypothesis):
                seed = json.loads(side.read_text())
                result = self.build(hypothesis)
                old = bytes.fromhex(seed["frame_a"]["payload_hex"])
                new = bytes.fromhex(result["payload_template_hex"])
                self.assertEqual([i for i in range(11) if old[i] != new[i]], [5, 10])
                self.assertEqual(new[:5], old[:5])
                self.assertEqual(new[5:10], bytes([slot, 0, 0, 0, 0]))
                self.assertEqual(new[10], (sum(new[:10]) + 2) & 255)
                self.assertEqual(result["frame_a"], seed["frame_a"])
                self.assertEqual(result["frame_b"], seed["frame_b"])
                self.assertEqual(result["semantic_status"], "NOT_CONFIRMED")
                self.assertEqual(result["mode"], "uncaptured_track_hypothesis")
                ids.add(result["profile_id"])
        self.assertEqual(len(ids), 3)
        self.assertNotEqual(self.build(start=0xD6)["profile_id"], self.build(start=0xDE)["profile_id"])

    def test_header_uses_new_payload_and_preserves_reference_arrays(self):
        result = self.build()
        header = make_profile.header_text(result)
        payload_line = next(line for line in header.splitlines() if "PROFILE_PAYLOAD[11]" in line)
        self.assertIn("0x77", payload_line)
        self.assertIn("PROFILE_COUNTER_START = 0xd6", header)
        self.assertIn("PROFILE_FRAME_A[22]", header)
        for invalid in ("00", "zz" * 11, "00" * 12):
            with self.assertRaises(ValueError):
                make_profile.header_text(result, payload_template_hex=invalid)

    def test_bad_seed_crc_peer_or_action_is_rejected(self):
        original = json.loads(self.left.read_text())
        for field, value in (("advance_counters", False), ("air_address_hex", "1111111111")):
            bad = dict(original, **{field: value})
            self.left.write_text(json.dumps(bad))
            with self.assertRaises(ValueError):
                self.build()
        bad = json.loads(json.dumps(original))
        bad["frame_a"]["encoded_hex"] = "00" + bad["frame_a"]["encoded_hex"][2:]
        self.left.write_text(json.dumps(bad))
        with self.assertRaises(ValueError):
            self.build()
        self.left.write_bytes(self.right.read_bytes())
        with self.assertRaisesRegex(ValueError, "clean forward"):
            self.build()

    def test_mismatched_identity_prefix_with_valid_crc_is_rejected(self):
        # Synthetic offline frames have correct radio/application checksums,
        # but a different session prefix. They are never a physical profile.
        bad = json.loads(self.right.read_text())
        for key, encoded, crc in (
            ("frame_a", "8d5fb5cb7575752cd2f31d42edad7cde28c752913680", "226d"),
            ("frame_b", "8d5fb5cb7575752dd2f31d42edad7cde28c752924380", "2487"),
        ):
            bad[key].update(encoded_hex=encoded, payload_hex="25130188b67000000000e9", crc16=crc)
        self.right.write_text(json.dumps(bad))
        with self.assertRaisesRegex(ValueError, "identity"):
            self.build()

    def test_default_cli_dry_review_and_explicit_counter_requirements(self):
        args = ["--hypothesis", "both-forward", "--left-profile", str(self.left),
                "--right-profile", str(self.right), "--counter-start", "0xd6"]
        before = sorted(Path(self.temp.name).iterdir())
        with redirect_stdout(io.StringIO()) as stdout:
            candidate.main(args)
        self.assertFalse(json.loads(stdout.getvalue())["header_written"])
        self.assertEqual(sorted(Path(self.temp.name).iterdir()), before)
        for extra in ([], ["--counter-start", "256"], ["--counter-start", "-1"]):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                candidate.main(args[:-2] + extra)
        with self.assertRaises(ValueError):
            candidate.make_candidate("both-forward", 0xD6, self.left, None)

    def test_header_creation_cannot_overwrite_an_existing_file(self):
        destination = Path(self.temp.name) / "existing.h"
        destination.write_text("keep me")
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            candidate.main(["--hypothesis", "left-backward", "--left-profile", str(self.left),
                            "--counter-start", "0xd6", "--write-header", str(destination)])
        self.assertEqual(destination.read_text(), "keep me")


if __name__ == "__main__":
    unittest.main()
