"""Offline BLE analyzer checks with captured data and deliberately damaged frames."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

import analyze_raw as analyzer


# Actual 32-byte capture, 2026-10-31 18:34:57.340+03:00, RF channel 80.
# Its Access Address starts two bits BEFORE the fragment: only 30 bits exist.
PARTIAL_AA_CAPTURE = "adf645c6ea7159f7954558898e5244893369ed1ec69a9f4e72b5295dd69a9bb0"
# The first two Access Address bits are restored for this synthetic fixture.
# It must never be represented as an observed complete-AA capture.
COMPLETE_AA_FIXTURE = "6b7d9171ba9c567de5515622639491224cda7b47b1a6a7d39cad4a5775a6a6ec"
EXPECTED_PDU = bytes.fromhex("420e20e1227cf6de07ff4c0012020003")
EXPECTED_CRC24 = 0x407E02
# Full Access Address with a 33-byte PDU length that exceeds the fragment;
# signature, plausible header and frequency alone do not validate it.
TRUNCATED_PDU_CAPTURE = "56b7d9171292771b74e48eeeb9825adaa2140b396c78e240a21773d3bab23fa8"


def record(data_hex, channel=80, pipe=0):
    return analyzer.RawRecord(channel, pipe, bytes.fromhex(data_hex))


def raw_line(data_hex, channel=80, pipe=0):
    return "RAW,channel={},mhz={},pipe={},bytes=32,hex={}".format(channel, 2400 + channel, pipe, data_hex)


def flip_bit(data_hex, index):
    data = bytearray.fromhex(data_hex)
    data[index // 8] ^= 1 << (7 - index % 8)
    return data.hex()


class BleAnalysisTests(unittest.TestCase):
    def test_crc_matches_independent_captured_crc(self):
        self.assertEqual(analyzer.crc24(EXPECTED_PDU), EXPECTED_CRC24)
        damaged_pdu = bytes([EXPECTED_PDU[0] ^ 1]) + EXPECTED_PDU[1:]
        self.assertNotEqual(analyzer.crc24(damaged_pdu), EXPECTED_CRC24)

    def test_full_access_address_and_complete_crc_qualify(self):
        matches = analyzer.find_ble_matches(record(COMPLETE_AA_FIXTURE))
        self.assertEqual(len(matches), 1)
        match = matches[0]
        self.assertTrue(match.complete_access_address)
        self.assertEqual((match.aa_bit_offset, match.aa_bits_captured), (0, 32))
        self.assertEqual(match.pdu, EXPECTED_PDU)
        self.assertEqual(match.crc24, EXPECTED_CRC24)

    def test_observed_complete_access_address_and_crc(self):
        # Actual idle-after-tower capture at18:37:50.293+03:00, channel26,
        # line248. Unlike the reconstructed fixture, all32AA bits are captured.
        data = "956d6fb22e312262539e3a3f254a342fc8d3d8d672531ebfcd5bb65ae6f5dae5"
        matches = analyzer.find_ble_matches(record(data, channel=26, pipe=1))
        self.assertEqual(len(matches), 1)
        match = matches[0]
        self.assertTrue(match.complete_access_address)
        self.assertEqual((match.aa_bit_offset, match.aa_bits_captured), (11, 32))
        self.assertEqual(match.pdu.hex(), "470d0c19d6557edb9120d120022d00")
        self.assertEqual(match.crc24, 0x944FB3)

    def test_observed_partial_access_address_is_only_a_candidate(self):
        matches = analyzer.find_ble_matches(record(PARTIAL_AA_CAPTURE))
        self.assertEqual(len(matches), 1)
        match = matches[0]
        self.assertFalse(match.complete_access_address)
        self.assertEqual((match.aa_bit_offset, match.aa_bits_captured), (-2, 30))
        self.assertEqual((match.pdu, match.crc24), (EXPECTED_PDU, EXPECTED_CRC24))

    def test_aa_signature_without_complete_pdu_and_crc_is_insufficient(self):
        self.assertEqual(analyzer.find_ble_matches(record(TRUNCATED_PDU_CAPTURE, channel=26)), [])
        self.assertEqual(analyzer.find_ble_matches(record("6b7d9171" + "00" * 28)), [])

    def test_damaged_access_address_pdu_and_crc_are_rejected(self):
        for bit in (0, 32 + 35, 32 + len(EXPECTED_PDU) * 8 + 5):
            with self.subTest(bit=bit):
                self.assertEqual(analyzer.find_ble_matches(record(flip_bit(COMPLETE_AA_FIXTURE, bit))), [])

    def test_wrong_whitening_channel_and_nonadvertising_frequency_rejected(self):
        for channel in (2, 26, 79):
            with self.subTest(channel=channel):
                self.assertEqual(analyzer.find_ble_matches(record(COMPLETE_AA_FIXTURE, channel)), [])

    def test_record_validation(self):
        line = raw_line(PARTIAL_AA_CAPTURE)
        parsed = analyzer.parse_raw_line("2026-10-31T18:34:57.340+03:00 " + line, 7)
        self.assertEqual((parsed.channel, parsed.pipe, parsed.line_number), (80, 0, 7))
        self.assertEqual(parsed.data.hex(), PARTIAL_AA_CAPTURE)
        self.assertTrue(parsed.timestamp.endswith("+03:00"))
        for malformed in (
            line.replace("channel=80", "channel=84"),
            line.replace("mhz=2480", "mhz=2479"),
            line.replace("pipe=0", "pipe=6"),
            line.replace("bytes=32", "bytes=31"),
            line.replace("hex=", "hex=0"),
            line.replace("hex=", "hex=z"),
            line.replace("pipe=0", "pipe=0,pipe=1"),
            "RAW,broken",
        ):
            with self.subTest(malformed=malformed):
                self.assertIsNone(analyzer.parse_raw_line(malformed))

    def test_summary_counts_candidates_separately_and_bounds_prefix_output(self):
        lines = [
            raw_line(COMPLETE_AA_FIXTURE),
            raw_line(PARTIAL_AA_CAPTURE),
            raw_line(PARTIAL_AA_CAPTURE),
            raw_line(TRUNCATED_PDU_CAPTURE, channel=26),
            raw_line(TRUNCATED_PDU_CAPTURE, channel=26),
            "RAW,broken",
            "RAW_ROUND,number=1,records=5",
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.log"
            path.write_text("\n".join(lines) + "\n")
            result = analyzer.analyze_log(path, prefix_limit=1)
            self.assertEqual(result["raw_records"], 5)
            self.assertEqual(result["malformed_raw_lines"], 1)
            self.assertEqual(result["confirmed_ble_records"], 1)
            self.assertEqual(result["remaining_unclassified_records"], 4)
            self.assertEqual(result["crc_consistent_partial_aa_records"], 2)
            self.assertEqual(result["repeated_unclassified_prefix_groups"], 2)
            self.assertEqual(len(result["repeated_unclassified_prefixes"]), 1)
            self.assertEqual(result["repeated_unclassified_prefixes"][0], {
                "channel": 26, "prefix_hex": "56b7d917", "records": 2})
            with redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(analyzer.main([str(path), "--json"]), 0)
            self.assertEqual(json.loads(stdout.getvalue())[0]["confirmed_ble_records"], 1)


if __name__ == "__main__":
    unittest.main()
