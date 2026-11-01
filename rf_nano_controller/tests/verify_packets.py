#!/usr/bin/env python3
"""Check the actual new C++ encoder against the earlier passive decoder/evidence."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "rf_nano_receiver_probe"))
from analyze_raw import RawRecord
from decode_hs6200 import decode_record

CODES = {
    "TL": "tower_rotate_left", "TR": "tower_rotate_right",
    "BU": "main_boom_raise", "BD": "main_boom_lower",
    "LF": "left_track_forward", "LB": "left_track_backward",
    "RF": "right_track_forward", "RB": "right_track_backward",
    "FF": "both_tracks_forward",
}

HARNESS = r'''
#include <cstdio>
#include "rf_nano_controller/tx_frame_builder.h"
int main() {
  const unsigned starts[] = {0xf6, 0xfa, 0xff};
  const unsigned elapsed[] = {0, 127, 128, 896};
  const unsigned queued[] = {0, 1, 2, 197};
  for (unsigned action = 0; action < CONTROLLER_ACTION_COUNT; ++action)
    for (unsigned start : starts) for (unsigned ms : elapsed)
      for (unsigned count : queued) {
        uint8_t a[22], b[22];
        if (!buildControllerPair(action, start, ms, count, a, b)) return 1;
        std::printf("%s,%u,%u,%u,%u,", CONTROLLER_ACTIONS[action].code,
                    CONTROLLER_ACTIONS[action].channel, start, ms, count);
        for (unsigned i = 0; i < 22; ++i) std::printf("%02x", a[i]);
        std::printf(",");
        for (unsigned i = 0; i < 22; ++i) std::printf("%02x", b[i]);
        std::printf("\n");
      }
}
'''


class ControllerPackets(unittest.TestCase):
    def test_all_actions_match_observed_catalogue_and_independent_decoder(self):
        catalogue = json.loads((ROOT / "rf_nano_tx_test/control_catalogue.json").read_text())
        controls = {row["control"]: row for row in catalogue["controls"]}
        with tempfile.TemporaryDirectory(prefix="tractor-packets-") as folder:
            source, binary = Path(folder) / "encoder.cpp", Path(folder) / "encoder"
            source.write_text(HARNESS)
            subprocess.run(["clang++", "-std=c++11", "-Wall", "-Wextra", "-Werror",
                            "-I", str(ROOT), str(source), "-o", str(binary)], check=True)
            output = subprocess.check_output([str(binary)], text=True)
        rows = output.splitlines()
        self.assertEqual(len(rows), 9 * 3 * 4 * 4)
        seen = set()
        for line in rows:
            code, channel, start, elapsed, queued, a, b = line.split(",")
            channel, start, elapsed, queued = map(int, (channel, start, elapsed, queued))
            control = controls[CODES[code]]
            template = control.get("full_observed_payload_template_hex",
                                   control.get("full_tested_payload_template_hex"))
            # A catalogue combination/reverse may retain its seed separately.
            if template is None:
                report = json.loads((ROOT / control["computer_tests"][-1]["profile_report"]).read_text())
                profile = report.get("selected_profile", report)
                template = profile.get("payload_template_hex", profile["frame_a"]["payload_hex"])
            expected = bytearray.fromhex(template)
            expected[4] = (start + elapsed // 128) & 255
            expected[10] = (sum(expected[:10]) + 2) & 255
            self.assertEqual(channel, control["rf_channel"])
            for offset, hex_frame in enumerate((a, b)):
                raw = bytes.fromhex(hex_frame)
                self.assertEqual(raw[:7].hex(), "8d5fb5cb757575")
                matches = [f for f in decode_record(
                    RawRecord(channel, 0, raw + b"\xff" * 10),
                    bytes.fromhex("8d5fb5cb75"), max_missing_address_bits=0)
                    if f.bit_offset == 0 and f.crc_valid]
                self.assertEqual(len(matches), 1, line)
                frame = matches[0]
                self.assertEqual(frame.payload, bytes(expected), line)
                self.assertEqual(frame.pid, (queued + offset) & 3, line)
                self.assertEqual(frame.no_ack, 1)
            seen.add(code)
        self.assertEqual(seen, set(CODES))


if __name__ == "__main__":
    unittest.main()
