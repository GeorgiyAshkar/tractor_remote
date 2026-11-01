#!/usr/bin/env python3
"""Offline analysis of RF-Nano RAW logs; no hardware access or extra packages.

A record is classified as BLE only when a complete 32-bit advertising Access
Address, a complete plausible advertising PDU, channel-specific dewhitening,
and CRC24 all match. A CRC-consistent PDU with a truncated Access Address is
reported separately and remains unclassified. Unclassified does not mean
non-BLE: a 32-byte fragment often does not contain a complete packet.

Primary references:
https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/Core_v6.3/out/en/low-energy-controller/link-layer-specification.html
  Sections 1.2, 1.4.1, 2.1.2, 2.3, 3.1.1 and 3.2 specify bit order, primary
  advertising channels, Access Address, PDU lengths, CRC and whitening.
https://devzone.nordicsemi.com/cfs-file/__key/communityserver-discussions-components-files/4/nRF24L01P_5F00_PS_5F00_v1.0.pdf
  Section 8.3.1 documents SPI byte/bit order and RX FIFO reads. The RAW sketch
  intentionally uses an experimental address configuration and disables CRC.

This tool identifies BLE evidence in captured fragments. It does not identify
which device sent a fragment or infer any Huina radio protocol or commands.
"""

import argparse
from collections import Counter
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import List, Optional


ADVERTISING_CHANNELS = {2: 37, 26: 38, 80: 39}
# 0x8E89BED6 is transmitted least-significant octet and bit first. The RF-Nano
# RAW bytes expose the captured sequence most-significant bit first per byte.
ADVERTISING_AA_BITS = format(0x6B7D9171, "032b")
CRC24_POLYNOMIAL = 0x00065B
CRC24_ADVERTISING_INIT = 0x555555


@dataclass(frozen=True)
class RawRecord:
    channel: int
    pipe: int
    data: bytes
    timestamp: Optional[str] = None
    line_number: Optional[int] = None


@dataclass(frozen=True)
class BleMatch:
    aa_bit_offset: int
    aa_bits_captured: int
    pdu: bytes
    crc24: int

    @property
    def complete_access_address(self) -> bool:
        return self.aa_bit_offset >= 0


def parse_raw_line(line: str, line_number: Optional[int] = None) -> Optional[RawRecord]:
    line = line.strip()
    timestamp = None
    if not line.startswith("RAW,"):
        timestamp, separator, line = line.partition(" ")
        if not separator or not line.startswith("RAW,"):
            return None
    fields = {}
    for part in line.split(",")[1:]:
        if "=" not in part:
            return None
        key, value = part.split("=", 1)
        if key in fields:
            return None
        fields[key] = value
    try:
        for key in ("channel", "mhz", "pipe", "bytes"):
            if not re.fullmatch(r"[0-9]+", fields.get(key, "")):
                return None
        channel = int(fields["channel"])
        pipe = int(fields["pipe"])
        if not 0 <= channel <= 83 or not 0 <= pipe <= 5:
            return None
        if int(fields["mhz"]) != 2400 + channel or int(fields["bytes"]) != 32:
            return None
        if not re.fullmatch(r"[0-9a-fA-F]{64}", fields.get("hex", "")):
            return None
        return RawRecord(channel, pipe, bytes.fromhex(fields["hex"]), timestamp, line_number)
    except (KeyError, ValueError):
        return None


def dewhiten_bits(bits: str, physical_channel_index: int) -> List[int]:
    """Reverse whitening in transmitted bit order (Core Vol 6 Part B 3.2)."""
    if not 0 <= physical_channel_index <= 39:
        raise ValueError("BLE physical channel index must be from 0 to 39")
    state = physical_channel_index | 0x40
    output = []
    for bit in bits:
        feedback = state & 1
        output.append(int(bit) ^ feedback)
        if feedback:
            state ^= 0x88
        state >>= 1
    return output


def lsb_first_bytes(bits: List[int]) -> bytes:
    return bytes(sum(bit << index for index, bit in enumerate(bits[start:start + 8]))
                 for start in range(0, len(bits) - 7, 8))


def crc24(pdu: bytes) -> int:
    """Return the advertising CRC register in its transmitted MSB-first order."""
    state = CRC24_ADVERTISING_INIT
    for byte in pdu:
        for index in range(8):
            feedback = ((state >> 23) & 1) ^ ((byte >> index) & 1)
            state = (state << 1) & 0xFFFFFF
            if feedback:
                state ^= CRC24_POLYNOMIAL
    return state


def plausible_advertising_header(header: int, length: int) -> bool:
    # Header bit 4 and PDU types 8..15 are reserved. The remaining two-bit
    # address flags and ChSel bit depend on PDU type and need no interpretation
    # to validate the CRC of an intact fragment.
    if header & 0x10:
        return False
    pdu_type = header & 0x0F
    if pdu_type in (0, 2, 4, 6):
        return 6 <= length <= 37
    if pdu_type in (1, 3):
        return length == 12
    if pdu_type == 5:
        return length == 34
    if pdu_type == 7:
        return 1 <= length <= 255
    return False


def find_ble_matches(record: RawRecord) -> List[BleMatch]:
    """Find CRC-valid PDUs; negative AA offsets are weaker, separate candidates.

    Partial-AA searches require at least 24 exact Access Address bits and a
    complete matching CRC. They never qualify for the confirmed BLE count.
    """
    physical_channel = ADVERTISING_CHANNELS.get(record.channel)
    if physical_channel is None:
        return []
    bits = "".join(format(byte, "08b") for byte in record.data)
    offsets = []
    position = bits.find(ADVERTISING_AA_BITS)
    while position >= 0:
        offsets.append(position)
        position = bits.find(ADVERTISING_AA_BITS, position + 1)
    for offset in range(-8, 0):
        if bits[:32 + offset] == ADVERTISING_AA_BITS[-offset:]:
            offsets.append(offset)
    matches = []
    for offset in offsets:
        decoded_bits = dewhiten_bits(bits[offset + 32:], physical_channel)
        decoded = lsb_first_bytes(decoded_bits)
        if len(decoded) < 2 or not plausible_advertising_header(decoded[0], decoded[1]):
            continue
        pdu_length = 2 + decoded[1]
        crc_start = pdu_length * 8
        if len(decoded_bits) < crc_start + 24:
            continue
        pdu = decoded[:pdu_length]
        received_crc = 0
        for bit in decoded_bits[crc_start:crc_start + 24]:
            received_crc = (received_crc << 1) | bit
        if crc24(pdu) == received_crc:
            matches.append(BleMatch(offset, 32 if offset >= 0 else 32 + offset, pdu, received_crc))
    return matches


def match_details(record: RawRecord, match: BleMatch) -> dict:
    return {
        "line": record.line_number,
        "timestamp": record.timestamp,
        "channel": record.channel,
        "mhz": 2400 + record.channel,
        "ble_physical_channel_index": ADVERTISING_CHANNELS[record.channel],
        "pipe": record.pipe,
        "aa_bit_offset": match.aa_bit_offset,
        "aa_bits_captured": match.aa_bits_captured,
        "pdu_hex": match.pdu.hex(),
        "crc24": format(match.crc24, "06x"),
    }


def analyze_log(path: Path, prefix_limit: int = 20) -> dict:
    records = []
    malformed = 0
    with Path(path).open(encoding="utf-8", errors="replace") as stream:
        for line_number, line in enumerate(stream, 1):
            record = parse_raw_line(line, line_number)
            if record is not None:
                records.append(record)
            elif line.startswith("RAW,") or " RAW," in line:
                malformed += 1
    confirmed = []
    partial_aa = []
    unclassified_prefixes = Counter()
    confirmed_record_count = 0
    partial_record_count = 0
    for record in records:
        matches = find_ble_matches(record)
        complete_matches = [match for match in matches if match.complete_access_address]
        partial_matches = [match for match in matches if not match.complete_access_address]
        if complete_matches:
            confirmed_record_count += 1
            confirmed.extend(match_details(record, match) for match in complete_matches)
        else:
            unclassified_prefixes[(record.channel, record.data[:4].hex())] += 1
            if partial_matches:
                partial_record_count += 1
                partial_aa.extend(match_details(record, match) for match in partial_matches)
    repeats = [(channel, prefix, count) for (channel, prefix), count in unclassified_prefixes.items() if count > 1]
    repeats.sort(key=lambda item: (-item[2], item[0], item[1]))
    return {
        "file": str(path),
        "raw_records": len(records),
        "malformed_raw_lines": malformed,
        "confirmed_ble_records": confirmed_record_count,
        "remaining_unclassified_records": len(records) - confirmed_record_count,
        "crc_consistent_partial_aa_records": partial_record_count,
        "confirmed_ble_matches": confirmed,
        "crc_consistent_partial_aa_matches": partial_aa,
        "repeated_unclassified_prefix_groups": len(repeats),
        "repeated_unclassified_prefixes": [
            {"channel": channel, "prefix_hex": prefix, "records": count}
            for channel, prefix, count in repeats[:prefix_limit]
        ],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("logs", nargs="+", type=Path, help="RAW capture logs to analyze offline")
    parser.add_argument("--json", action="store_true", help="Print structured JSON")
    parser.add_argument("--prefix-limit", type=int, default=20, help="Maximum repeated-prefix groups per file (default: 20)")
    args = parser.parse_args(argv)
    if not 0 <= args.prefix_limit <= 1000:
        parser.error("--prefix-limit must be from 0 to 1000")
    try:
        summaries = [analyze_log(path, args.prefix_limit) for path in args.logs]
    except OSError as error:
        parser.exit(1, "Error: {}\n".format(error))
    if args.json:
        print(json.dumps(summaries, indent=2))
    else:
        for result in summaries:
            print("{}: RAW={}, confirmed BLE={}, remaining unclassified={}, partial-AA CRC matches={}".format(
                result["file"], result["raw_records"], result["confirmed_ble_records"],
                result["remaining_unclassified_records"], result["crc_consistent_partial_aa_records"]))
            for prefix in result["repeated_unclassified_prefixes"]:
                print("  repeated prefix: channel={channel}, hex={prefix_hex}, records={records}".format(**prefix))
        print("Partial-AA matches remain unclassified. No transmitting device or Huina protocol is identified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
