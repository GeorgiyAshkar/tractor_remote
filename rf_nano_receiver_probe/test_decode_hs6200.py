"""Offline HS6200 framing tests using captured right/left control fragments."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

import decode_hs6200 as decoder
from analyze_raw import RawRecord


# Seven captured tower-right fragments, with independently observed CRC fields.
# Negative offsets reconstruct only the two missing leading known-address bits.
RIGHT_FRAMES = [
    (31, -2, 3, "357ed72dd5d5d4bf2bcc750b2e54af78a31c2efcb475007a36431a32da5c2b2a", "15130188fa00ae0000005b", 0x7E5A),
    (15, -2, 0, "357ed72dd5d5d4b32bcc750a9854af78a31d9c4ae3651a523ae1611944551125", "151301882100ae00000082", 0x2571),
    (31, -2, 1, "357ed72dd5d5d4b72bcc750abe54af78a31dbf9ce4408a285ef4d6858a94a546", "151301883200ae00000093", 0xCE72),
    (15, 0, 3, "8d5fb5cb7575752fcaf31d4285952bde28c745f9430aa5a555af557a5bf5852c", "151301886600ae000000c7", 0xF286),
    (47, 0, 0, "8d5fb5cb7575752ccaf31d428b952bde28c74ba78ad615a81d51232ca97adcb5", "151301887a00ae000000db", 0x4F15),
    (63, -2, 1, "357ed72dd5d5d4b72bcc750a2254af78a31d23103852d5cc8a24aa90a9226ca4", "151301887c00ae000000dd", 0x881C),
    (47, 0, 2, "8d5fb5cb7575752ecaf31d42f2952bde28c7528dd5d5042516b9149a7375558a", "151301888800ae000000e9", 0x1BAB),
]
LEFT_FRAME = (31, "8d5fb5cb7575752dcaf31d428b957ede28c73ebca8d00a986690028b8912c5a9", "151301887a000400000031", 0x7951)


def raw_record(data_hex, channel=15):
    return RawRecord(channel, 1, bytes.fromhex(data_hex))


def flip_bit(data_hex, index):
    data = bytearray.fromhex(data_hex)
    data[index // 8] ^= 1 << (7 - index % 8)
    return data.hex()


class Hs6200Tests(unittest.TestCase):
    def test_crc16_standard_check_value(self):
        bits = "".join(format(byte, "08b") for byte in b"123456789")
        self.assertEqual(decoder.crc16_bits(bits), 0x29B1)

    def test_all_seven_captured_right_frames_validate(self):
        for channel, offset, pid, data, payload, expected_crc in RIGHT_FRAMES:
            with self.subTest(channel=channel, payload=payload):
                frames = decoder.decode_record(raw_record(data, channel))
                self.assertEqual(len(frames), 1)
                frame = frames[0]
                self.assertTrue(frame.crc_valid)
                self.assertEqual(frame.bit_offset, offset)
                self.assertEqual(frame.reconstructed_address_bits, max(0, -offset))
                self.assertEqual((frame.payload_length, frame.pid, frame.no_ack), (11, pid, 1))
                self.assertEqual(frame.payload.hex(), payload)
                self.assertEqual(frame.received_crc, expected_crc)
                self.assertEqual(frame.calculated_crc, expected_crc)
                self.assertEqual((frame.payload[-1] - sum(frame.payload[:-1])) & 255, 2)

    def test_captured_left_has_same_framing_with_different_observed_byte(self):
        channel, data, payload, crc = LEFT_FRAME
        frame = decoder.decode_record(raw_record(data, channel))[0]
        self.assertTrue(frame.crc_valid)
        self.assertEqual((frame.payload.hex(), frame.received_crc), (payload, crc))
        self.assertEqual(frame.payload[6], 0x04)
        self.assertEqual(decoder.decode_record(raw_record(RIGHT_FRAMES[4][3]))[0].payload[6], 0xAE)

    def test_full_address_requirement_rejects_reconstructed_frames(self):
        self.assertEqual(decoder.decode_record(raw_record(RIGHT_FRAMES[0][3]), max_missing_address_bits=0), [])
        frames = decoder.decode_record(raw_record(RIGHT_FRAMES[3][3]), max_missing_address_bits=0)
        self.assertEqual(len(frames), 1)
        self.assertTrue(frames[0].crc_valid)

    def test_incorrect_reconstructed_address_bits_fail_crc(self):
        # This changes only one of the two uncaptured leading address bits.
        wrong_address = bytes.fromhex("4d5fb5cb75")
        frames = decoder.decode_record(raw_record(RIGHT_FRAMES[0][3]), address=wrong_address)
        self.assertEqual(len(frames), 1)
        self.assertFalse(frames[0].crc_valid)

    def test_payload_and_crc_damage_rejected_but_trailing_noise_ignored(self):
        data = RIGHT_FRAMES[3][3]
        for bit in (65 + 12, 65 + 11 * 8 + 5):
            with self.subTest(bit=bit):
                frames = decoder.decode_record(raw_record(flip_bit(data, bit)))
                self.assertEqual(len(frames), 1)
                self.assertFalse(frames[0].crc_valid)
        for bit in (0, 48):
            with self.subTest(prefix_bit=bit):
                self.assertEqual(decoder.decode_record(raw_record(flip_bit(data, bit))), [])
        frames = decoder.decode_record(raw_record(flip_bit(data, 240)))
        self.assertEqual(len(frames), 1)
        self.assertTrue(frames[0].crc_valid)

    def test_nonzero_full_address_bit_offset(self):
        original = "".join(format(byte, "08b") for byte in bytes.fromhex(RIGHT_FRAMES[3][3]))
        shifted = ("111" + original)[:256]
        data = bytes(int(shifted[start:start + 8], 2) for start in range(0, 256, 8))
        frame = decoder.decode_record(RawRecord(15, 1, data))[0]
        self.assertEqual(frame.bit_offset, 3)
        self.assertTrue(frame.crc_valid)
        self.assertEqual(frame.payload.hex(), RIGHT_FRAMES[3][4])

    def test_discovery_recovers_known_frames_without_the_known_address(self):
        for channel, offset, pid, data, payload, expected_crc in RIGHT_FRAMES:
            with self.subTest(offset=offset, crc=expected_crc):
                matches = decoder.discover_record(raw_record(data, channel))
                self.assertEqual(len(matches), 1)
                frame = matches[0]
                self.assertEqual(frame.address, decoder.OBSERVED_AIR_ADDRESS)
                self.assertEqual(frame.payload.hex(), payload)
                self.assertTrue(frame.crc_valid)
                if offset < 0:
                    self.assertEqual(frame.reconstruction_basis, "enumerated_leading_bits_crc_checked")

    def test_discovery_finds_actual_new_address_with_complete_crc(self):
        # Observed ON-idle-repeat log line2789,18:35:20.374,channel50.
        # Its payload carries the known address in reverse byte order. This is
        # evidence for a setup/binding hypothesis, not a verified command map.
        data = "529c546e5c5c5c2cfa9f47297015f657a8036c9463182046614ca80a04c41554"
        raw = RawRecord(50, 0, bytes.fromhex(data))
        self.assertEqual(decoder.decode_record(raw), [])
        matches = decoder.discover_record(raw)
        self.assertEqual(len(matches), 1)
        frame = matches[0]
        self.assertEqual(frame.address.hex(), "529c546e5c")
        self.assertEqual(frame.payload.hex(), "75cbb55f8d011513018895")
        self.assertEqual(frame.payload[:5], decoder.OBSERVED_AIR_ADDRESS[::-1])
        self.assertEqual((frame.bit_offset, frame.received_crc, frame.crc_valid), (0, 0x28C6, True))
        self.assertEqual((frame.payload[-1] - sum(frame.payload[:-1])) & 255, 2)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "setup.log"
            path.write_text("RAW,channel=50,mhz=2450,pipe=0,bytes=32,hex=" + data + "\n")
            with redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(decoder.main([str(path), "--discover-addresses", "--json"]), 0)
            result = json.loads(stdout.getvalue())[0]
            self.assertIsNone(result["known_air_address_hex"])
            self.assertEqual(result["discovered_air_addresses"], ["529c546e5c"])
            self.assertEqual(result["crc_valid_frames"], 1)

    def test_discovery_rejects_damaged_crc_and_constant_noise(self):
        data = RIGHT_FRAMES[3][3]
        self.assertEqual(decoder.discover_record(raw_record(flip_bit(data, 65 + 20))), [])
        self.assertEqual(decoder.discover_record(raw_record("00" * 32)), [])
        self.assertEqual(decoder.discover_record(raw_record("ff" * 32)), [])
        self.assertEqual(decoder.discover_record(raw_record(RIGHT_FRAMES[0][3]), max_missing_address_bits=0), [])

    def test_summary_preserves_provenance_and_excludes_crc_failures_by_default(self):
        channel, offset, pid, data, payload, crc = RIGHT_FRAMES[0]
        damaged = flip_bit(data, 65 - 2 + 12)
        def line(hex_data):
            return "2026-10-31T18:36:51.941+03:00 RAW,channel=31,mhz=2431,pipe=1,bytes=32,hex=" + hex_data
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.log"
            path.write_text(line(data) + "\n" + line(damaged) + "\n")
            result = decoder.analyze_log(path)
            self.assertEqual((result["raw_records"], result["matching_prefix_candidates"], result["crc_valid_frames"]), (2, 2, 1))
            self.assertEqual(len(result["frames"]), 1)
            row = result["frames"][0]
            self.assertEqual((row["file"], row["line"], row["channel"]), (str(path), 1, 31))
            self.assertEqual(row["known_address_bits_reconstructed"], 2)
            self.assertIn("previously observed", row["reconstruction_assumption"])
            self.assertEqual(row["payload_bytes"], list(bytes.fromhex(payload)))
            self.assertEqual(len(decoder.analyze_log(path, include_crc_failures=True)["frames"]), 2)
            with redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(decoder.main([str(path), "--json"]), 0)
            self.assertEqual(json.loads(stdout.getvalue())[0]["crc_valid_frames"], 1)


if __name__ == "__main__":
    unittest.main()
