#!/usr/bin/env python3
"""Decode an observed HS6200-style envelope in existing RAW logs, offline only.

The known on-air address 8d5fb5cb75 and guards 7575 were observed in full in
control-held captures. Frames beginning at bit offset -1 or -2 reconstruct
those missing leading address bits from that known address; this assumption
is explicitly reported. CRC validation includes the reconstructed address.
By default, no unknown address is discovered or guessed. Opt-in
--discover-addresses searches five-byte addresses followed by two copies of
that address's last byte and requires a matching CRC16. Missing leading bits
are then enumerated (one or two bits) and explicitly marked as reconstructed;
CRC-consistent discovery remains an envelope candidate without source or
application semantics. Empty-payload candidates can be noise artifacts.

Framing, scramble bytes and CRC provenance:
https://raw.githubusercontent.com/pascallanger/DIY-Multiprotocol-TX-Module/master/Multiprotocol/HS6200_EMU.ino
This primary emulation implementation documents address, two guard bytes,
9-bit PCF, XOR-scrambled payload, and CRC over address+PCF+scrambled payload.
CRC16 uses polynomial 0x1021 and initial value 0xffff, MSB first. Guard bytes
are excluded. This decoder independently processes the resulting bit stream;
it does not reproduce the emulation source's byte-packing implementation.

The emitted payload bytes are observations, not an established command map.
No transmission, serial device access, or control of the excavator occurs.
Only the first 15 scramble bytes are known from the cited implementation;
longer payloads retain their scrambled form and are not silently truncated.
"""

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
from typing import List, Optional

from analyze_raw import RawRecord, parse_raw_line


OBSERVED_AIR_ADDRESS = bytes.fromhex("8d5fb5cb75")
HS6200_SCRAMBLE = bytes.fromhex("80f53b0d6d2af9bc518e4cfdc165d0")


@dataclass(frozen=True)
class Hs6200Frame:
    bit_offset: int
    address: bytes
    payload_length: int
    pid: int
    no_ack: int
    scrambled_payload: bytes
    payload: Optional[bytes]
    received_crc: int
    calculated_crc: int
    reconstruction_basis: Optional[str] = None

    @property
    def crc_valid(self) -> bool:
        return self.received_crc == self.calculated_crc

    @property
    def reconstructed_address_bits(self) -> int:
        return max(0, -self.bit_offset)


def crc16_bits(bits: str) -> int:
    state = 0xFFFF
    for bit in bits:
        feedback = ((state >> 15) & 1) ^ int(bit)
        state = (state << 1) & 0xFFFF
        if feedback:
            state ^= 0x1021
    return state


def decode_record(record: RawRecord, address: bytes = OBSERVED_AIR_ADDRESS,
                  max_missing_address_bits: int = 2) -> List[Hs6200Frame]:
    if len(address) != 5:
        raise ValueError("a known five-byte on-air address is required")
    if not 0 <= max_missing_address_bits <= 2:
        raise ValueError("at most two missing leading address bits may be reconstructed")
    prefix = address + address[-1:] * 2
    prefix_bits = "".join(format(byte, "08b") for byte in prefix)
    raw_bits = "".join(format(byte, "08b") for byte in record.data)
    offsets = []
    position = raw_bits.find(prefix_bits)
    while position >= 0:
        offsets.append(position)
        position = raw_bits.find(prefix_bits, position + 1)
    for missing in range(1, max_missing_address_bits + 1):
        if raw_bits[:len(prefix_bits) - missing] == prefix_bits[missing:]:
            offsets.append(-missing)
    frames = []
    for offset in offsets:
        missing = max(0, -offset)
        bits = prefix_bits[:missing] + raw_bits[max(0, offset):]
        frame = decode_frame_bits(bits, address, offset, "previously_observed_address" if missing else None)
        if frame is not None:
            frames.append(frame)
    return frames


def decode_frame_bits(bits: str, address: bytes, offset: int,
                      reconstruction_basis: Optional[str] = None) -> Optional[Hs6200Frame]:
    if len(bits) < 65:
        return None
    payload_length = int(bits[56:62], 2)
    if payload_length > 32:
        return None
    crc_start = 65 + 8 * payload_length
    if len(bits) < crc_start + 16:
        return None
    scrambled = bytes(int(bits[start:start + 8], 2) for start in range(65, crc_start, 8))
    payload = None
    if payload_length <= len(HS6200_SCRAMBLE):
        payload = bytes(byte ^ HS6200_SCRAMBLE[index] for index, byte in enumerate(scrambled))
    return Hs6200Frame(
        offset, address, payload_length, int(bits[62:64], 2), int(bits[64]),
        scrambled, payload, int(bits[crc_start:crc_start + 16], 2),
        crc16_bits(bits[:40] + bits[56:crc_start]), reconstruction_basis)


def discover_record(record: RawRecord, max_missing_address_bits: int = 2) -> List[Hs6200Frame]:
    """Return CRC-valid five-byte-address envelope candidates, without a known address.

    Guard matching contributes 16 constrained bits. CRC contributes 16 more;
    these checks greatly limit random matches but do not prove device identity.
    Zero-length or low-entropy candidates especially need repetition/context.
    """
    if not 0 <= max_missing_address_bits <= 2:
        raise ValueError("at most two missing leading address bits may be reconstructed")
    raw = "".join(format(byte, "08b") for byte in record.data)
    candidates = [(offset, raw[offset:]) for offset in range(len(raw) - 80)]
    candidates.extend(
        (-missing, format(leading, "0{}b".format(missing)) + raw)
        for missing in range(1, max_missing_address_bits + 1)
        for leading in range(1 << missing))
    frames = []
    for offset, bits in candidates:
        if bits[32:40] != bits[40:48] or bits[32:40] != bits[48:56]:
            continue
        # The public scramble reference only covers fifteen bytes. Bound the
        # discovery search accordingly, rather than guessing unknown scrambling.
        if int(bits[56:62], 2) > len(HS6200_SCRAMBLE):
            continue
        address = bytes(int(bits[start:start + 8], 2) for start in range(0, 40, 8))
        frame = decode_frame_bits(bits, address, offset, "enumerated_leading_bits_crc_checked" if offset < 0 else None)
        if frame is not None and frame.crc_valid:
            frames.append(frame)
    return frames


def frame_row(path: Path, record: RawRecord, frame: Hs6200Frame) -> dict:
    return {
        "file": str(path),
        "line": record.line_number,
        "timestamp": record.timestamp,
        "channel": record.channel,
        "mhz": 2400 + record.channel,
        "pipe": record.pipe,
        "bit_offset": frame.bit_offset,
        "air_address_hex": frame.address.hex(),
        "known_address_bits_reconstructed": frame.reconstructed_address_bits,
        "reconstruction_basis": frame.reconstruction_basis,
        "reconstruction_assumption": (
            ("Missing leading bits were supplied from the previously observed full address"
             if frame.reconstruction_basis == "previously_observed_address" else
             "Missing leading bits were enumerated; this assignment matches the captured CRC")
            if frame.reconstructed_address_bits else None),
        "payload_length": frame.payload_length,
        "pid": frame.pid,
        "no_ack": frame.no_ack,
        "scrambled_payload_hex": frame.scrambled_payload.hex(),
        "payload_hex": frame.payload.hex() if frame.payload is not None else None,
        "payload_bytes": list(frame.payload) if frame.payload is not None else None,
        "last_byte_minus_prior_sum_mod256": (
            (frame.payload[-1] - sum(frame.payload[:-1])) & 0xFF if frame.payload else None),
        "crc_received": format(frame.received_crc, "04x"),
        "crc_calculated": format(frame.calculated_crc, "04x"),
        "crc_valid": frame.crc_valid,
    }


def analyze_log(path: Path, address: bytes = OBSERVED_AIR_ADDRESS,
                max_missing_address_bits: int = 2, include_crc_failures: bool = False,
                discover_addresses: bool = False) -> dict:
    raw_count = 0
    candidates = 0
    valid = 0
    rows = []
    with Path(path).open(encoding="utf-8", errors="replace") as stream:
        for number, line in enumerate(stream, 1):
            record = parse_raw_line(line, number)
            if record is None:
                continue
            raw_count += 1
            frames = (discover_record(record, max_missing_address_bits) if discover_addresses else
                      decode_record(record, address, max_missing_address_bits))
            for frame in frames:
                candidates += 1
                valid += int(frame.crc_valid)
                if frame.crc_valid or include_crc_failures:
                    rows.append(frame_row(path, record, frame))
    return {
        "file": str(path),
        "known_air_address_hex": None if discover_addresses else address.hex(),
        "address_discovery": discover_addresses,
        "discovered_air_addresses": sorted(set(row["air_address_hex"] for row in rows if row["crc_valid"])),
        "raw_records": raw_count,
        "matching_prefix_candidates": candidates,
        "crc_valid_frames": valid,
        "crc_invalid_candidates": candidates - valid,
        "frames": rows,
    }


def parse_address(value: str) -> bytes:
    try:
        address = bytes.fromhex(value)
    except ValueError:
        raise argparse.ArgumentTypeError("address must contain five bytes in hex")
    if len(address) != 5:
        raise argparse.ArgumentTypeError("address must contain five bytes in hex")
    return address


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("logs", nargs="+", type=Path, help="Existing RAW capture logs")
    parser.add_argument("--address", type=parse_address, default=OBSERVED_AIR_ADDRESS,
                        help="Previously observed on-air address (default: 8d5fb5cb75)")
    parser.add_argument("--discover-addresses", action="store_true",
                        help="Find CRC-valid five-byte-address/guard candidates without the known address")
    parser.add_argument("--require-full-address", action="store_true",
                        help="Reject fragments missing leading address bits")
    parser.add_argument("--include-crc-failures", action="store_true",
                        help="Also emit matching-prefix candidates that fail CRC")
    parser.add_argument("--json", action="store_true", help="Print structured summaries and frame rows")
    args = parser.parse_args(argv)
    if args.discover_addresses and args.include_crc_failures:
        parser.error("discovery emits only CRC-valid candidates; --include-crc-failures applies to the known address")
    try:
        results = [analyze_log(path, args.address, 0 if args.require_full_address else 2,
                               args.include_crc_failures, args.discover_addresses) for path in args.logs]
    except OSError as error:
        parser.exit(1, "Error: {}\n".format(error))
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for result in results:
            print("{}: RAW={}, prefix candidates={}, CRC-valid frames={}".format(
                result["file"], result["raw_records"], result["matching_prefix_candidates"], result["crc_valid_frames"]))
            for row in result["frames"]:
                print("  line={line}, time={timestamp}, channel={channel}, bit_offset={bit_offset}, "
                      "address={air_address_hex}, reconstructed_address_bits={known_address_bits_reconstructed}, len={payload_length}, "
                      "pid={pid}, no_ack={no_ack}, payload={payload_hex}, crc_valid={crc_valid}".format(**row))
        print("Payload bytes are observations; no command map or RF transmission is implemented.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
