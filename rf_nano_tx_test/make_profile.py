#!/usr/bin/env python3
"""Review captured HS6200 candidates and explicitly export one finite TX profile.

Offline only: no serial device, network, or radio access. By default a profile
replays captured frames exactly. Explicit --advance-counters instead requests an
unverified counter/PID advancement experiment; command and identity bytes stay
captured. The firmware must recalculate the application checksum and radio CRC
for that mode. CRC-valid framing alone does not prove device or action identity.
"""

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import sys
import zlib


PROBE_DIRECTORY = Path(__file__).resolve().parents[1] / "rf_nano_receiver_probe"
if str(PROBE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(PROBE_DIRECTORY))

from analyze_raw import RawRecord, parse_raw_line  # noqa: E402
from decode_hs6200 import decode_record, discover_record, Hs6200Frame  # noqa: E402


FRAME_SIZE = 22
MIN_ADDRESS_RECORDS = 2
COUNTER_PERIOD_MS = 128


@dataclass(frozen=True)
class Observation:
    record: RawRecord
    frame: Hs6200Frame


def has_candidate_entropy(frame):
    """Exclude obvious constant/noise envelopes, without assigning semantics."""
    return (frame.payload_length == 11 and frame.payload is not None
            and len(set(frame.address)) >= 3 and len(set(frame.payload)) >= 3)


def load_capture(path):
    raw_bytes = Path(path).read_bytes()
    observations = []
    valid_observations = []
    records = 0
    for number, line in enumerate(raw_bytes.decode("utf-8", errors="replace").splitlines(), 1):
        record = parse_raw_line(line, number)
        if record is None:
            continue
        records += 1
        for frame in discover_record(record):
            if frame.crc_valid and frame.payload_length == 11 and frame.payload is not None:
                observation = Observation(record, frame)
                valid_observations.append(observation)
                if has_candidate_entropy(frame):
                    observations.append(observation)
    return {
        "source": str(Path(path)),
        "source_sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "raw_records": records,
        "observations": observations,
        "valid_observations": valid_observations,
    }


def eligible_groups(capture):
    groups = defaultdict(list)
    for observation in capture["observations"]:
        groups[observation.frame.address.hex()].append(observation)
    return {
        address: rows for address, rows in groups.items()
        if len({row.record.line_number for row in rows}) >= MIN_ADDRESS_RECORDS
    }


def encode_observation(observation):
    """Pack observed bits; preserve the captured CRC instead of recomputing it."""
    frame = observation.frame
    if not frame.crc_valid or not has_candidate_entropy(frame):
        raise ValueError("only CRC-valid, nonconstant eleven-byte candidates can be exported")
    prefix = frame.address + frame.address[-1:] * 2
    bits = "".join(format(byte, "08b") for byte in prefix)
    bits += "{:06b}{:02b}{:01b}".format(frame.payload_length, frame.pid, frame.no_ack)
    bits += "".join(format(byte, "08b") for byte in frame.scrambled_payload)
    bits += format(frame.received_crc, "016b")
    bits += "0" * (-len(bits) % 8)
    encoded = bytes(int(bits[index:index + 8], 2) for index in range(0, len(bits), 8))
    if len(encoded) != FRAME_SIZE:
        raise ValueError("unexpected captured frame size")

    # Cross-check the independent packing with the existing decoder. Supplying
    # a complete encoded address must not use its missing-address reconstruction.
    record = RawRecord(observation.record.channel, 0,
                       encoded + b"\xff" * (32 - len(encoded)))
    matches = decode_record(record, frame.address, max_missing_address_bits=0)
    if not any(match.bit_offset == 0 and match.crc_valid
               and match.address == frame.address
               and match.pid == frame.pid and match.no_ack == frame.no_ack
               and match.scrambled_payload == frame.scrambled_payload
               and match.received_crc == frame.received_crc for match in matches):
        raise ValueError("encoded frame failed the existing decoder round-trip")
    return encoded


def observation_row(observation):
    frame, record = observation.frame, observation.record
    return {
        "line": record.line_number,
        "timestamp": record.timestamp,
        "channel": record.channel,
        "mhz": 2400 + record.channel,
        "air_address_hex": frame.address.hex(),
        "bit_offset": frame.bit_offset,
        "address_bits_reconstructed": frame.reconstructed_address_bits,
        "reconstruction_basis": frame.reconstruction_basis,
        "reconstruction_assumption": (
            "Leading address bits were enumerated and selected by CRC; they were not captured"
            if frame.reconstructed_address_bits else None),
        "pid": frame.pid,
        "no_ack": frame.no_ack,
        "payload_hex": frame.payload.hex(),
        "scrambled_payload_hex": frame.scrambled_payload.hex(),
        "crc16": format(frame.received_crc, "04x"),
        "encoded_hex": encode_observation(observation).hex(),
    }


def same_payload(first, second):
    return (first.frame.address == second.frame.address
            and first.frame.scrambled_payload == second.frame.scrambled_payload
            and first.frame.no_ack == second.frame.no_ack)


def select_profile(capture, address, line, line_b=None, advance_counters=False,
                   counter_start_override=None):
    if counter_start_override is not None:
        if not advance_counters:
            raise ValueError("counter start override requires counter advancement mode")
        if not isinstance(counter_start_override, int) or not 0 <= counter_start_override <= 255:
            raise ValueError("counter start must be an integer from 0 to 255")
    groups = eligible_groups(capture)
    if address not in groups:
        raise ValueError("selected address needs at least two qualifying capture records")
    rows = groups[address]
    selected = [row for row in rows if row.record.line_number == line]
    if len(selected) != 1:
        raise ValueError("selected line must identify exactly one qualifying frame for the address")
    first = selected[0]
    if line_b is not None:
        second_rows = [row for row in rows if row.record.line_number == line_b]
        if len(second_rows) != 1 or not same_payload(first, second_rows[0]):
            raise ValueError("second line must contain the same captured address, payload, and no_ack")
        second = second_rows[0]
        if second.frame.pid == first.frame.pid and second != first:
            raise ValueError("use the first line itself to duplicate it, or select a different captured PID")
    else:
        alternatives = [row for row in rows if same_payload(first, row)
                        and row.frame.pid != first.frame.pid]
        alternatives.sort(key=lambda row: (
            row.frame.reconstructed_address_bits,
            row.record.channel != first.record.channel,
            abs(row.record.line_number - first.record.line_number),
            row.record.line_number))
        second = alternatives[0] if alternatives else first
    encoded_a = encode_observation(first)
    encoded_b = encode_observation(second)
    channel = first.record.channel
    counter_rows = [row for row in capture["valid_observations"]
                    if row.frame.address.hex() == address]
    last_observation = max(counter_rows, key=lambda row: row.record.line_number)
    derived_counter_start = (last_observation.frame.payload[4] + 1) & 0xFF
    counter_start = derived_counter_start if counter_start_override is None else counter_start_override
    if advance_counters and any(
            ((row.frame.payload[-1] - sum(row.frame.payload[:-1])) & 0xFF) != 2
            for row in rows):
        raise ValueError("counter advance requires the captured sum(payload[:10])+2 checksum relation")
    identifier_material = (b"hs6200-profile-v2\0"
                           + bytes([channel, int(advance_counters), counter_start])
                           + COUNTER_PERIOD_MS.to_bytes(2, "big") + encoded_a + encoded_b)
    profile_id = format(zlib.crc32(identifier_material) & 0xFFFFFFFF, "08x")
    return {
        "profile_id": profile_id,
        "source": capture["source"],
        "source_sha256": capture["source_sha256"],
        "air_address_hex": address,
        "channel": channel,
        "mhz": 2400 + channel,
        "frame_size": FRAME_SIZE,
        "mode": "counter_advance_hypothesis" if advance_counters else "exact_captured_replay",
        "advance_counters": advance_counters,
        "counter_start": counter_start,
        "counter_start_source": "last_captured_counter_plus_one" if counter_start_override is None else "explicit_override",
        "derived_counter_start": derived_counter_start,
        "counter_period_ms": COUNTER_PERIOD_MS,
        "counter_source": observation_row(last_observation),
        "counter_transformation": ({
            "status": "unverified hypothesis, not exact replay",
            "counter_payload_index": 4,
            "checksum_payload_index": 10,
            "checksum_rule": "sum(payload[:10])+2 modulo 256",
            "pid_rule": "generated by the packet builder",
            "radio_crc_rule": "recomputed by the packet builder",
            "unchanged_payload_indices": [0, 1, 2, 3, 5, 6, 7, 8, 9],
        } if advance_counters else None),
        "selection": ("transformed_counter_hypothesis" if advance_counters else
                      "duplicate_exact_frame" if first == second else "captured_different_pid_pair"),
        "captured_frame_selection": "duplicate_exact_frame" if first == second else "captured_different_pid_pair",
        "frame_a": observation_row(first),
        "frame_b": observation_row(second),
        "radio_preamble_address_hex": "5555555555" if first.frame.address[0] & 0x80 else "aaaaaaaaaa",
        "hardware_crc": "off",
        "auto_ack": "off",
        "limits": [
            ("Counter/PID advancement is an unverified transformation, not exact replay"
             if advance_counters else "Exact captured frames; counter/PID/checksum are not changed"),
            "Command meaning, neutral, and binding are not established",
            "The input hash identifies the capture but does not prove freshness or device identity",
            "Generating this header performs no transmission",
        ],
    }


def review_capture(capture):
    eligible = eligible_groups(capture)
    report = {
        "mode": "offline_review",
        "source": capture["source"],
        "source_sha256": capture["source_sha256"],
        "raw_records": capture["raw_records"],
        "qualifying_frames": len(capture["observations"]),
        "minimum_address_records": MIN_ADDRESS_RECORDS,
        "addresses": [],
        "header_written": False,
    }
    for address, rows in sorted(eligible.items()):
        full = [row for row in rows if row.frame.reconstructed_address_bits == 0]
        preview = max(full or rows, key=lambda row: row.record.line_number)
        report["addresses"].append({
            "air_address_hex": address,
            "frames": len(rows),
            "full_address_frames": len(full),
            "channels": dict(sorted(Counter(row.record.channel for row in rows).items())),
            "candidates": [observation_row(row) for row in rows],
            "suggested_line": preview.record.line_number,
        })
    return report


def header_text(profile, payload_template_hex=None):
    # Never interpolate unrestricted filenames/timestamps into C++ or comments.
    label = Path(profile["source"]).name
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", label):
        raise ValueError("header source filename must use only ASCII letters, digits, '.', '_', or '-'")
    payload_hex = (payload_template_hex if payload_template_hex is not None else
                   profile.get("payload_template_hex", profile["frame_a"]["payload_hex"]))
    if not isinstance(payload_hex, str) or not re.fullmatch(r"[0-9a-fA-F]{22}", payload_hex):
        raise ValueError("payload template must contain exactly eleven bytes in hexadecimal")
    def values(row):
        return ", ".join("0x{:02x}".format(byte) for byte in bytes.fromhex(row["encoded_hex"]))
    def hex_values(value):
        return ", ".join("0x{:02x}".format(byte) for byte in bytes.fromhex(value))
    return "\n".join([
        "// Generated offline from captured CRC-valid frames; this file sends no RF.",
        "// Mode: " + profile["mode"],
        "// Source: {} ; lines A={} B={}".format(label, profile["frame_a"]["line"], profile["frame_b"]["line"]),
        "// Input SHA256: " + profile["source_sha256"],
        "// PROFILE_SOURCE is a label, not proof of device identity or freshness.",
        "#pragma once",
        "#include <stdint.h>",
        "static const bool PROFILE_READY = true;",
        'static const char PROFILE_ID[] = "{}";'.format(profile["profile_id"]),
        'static const char PROFILE_SOURCE[] = "{}";'.format(label),
        "static const uint8_t PROFILE_CHANNEL = {};".format(profile["channel"]),
        "static const bool PROFILE_ADVANCE_COUNTERS = {};".format("true" if profile["advance_counters"] else "false"),
        "static const uint8_t PROFILE_AIR_ADDRESS[5] = {" + hex_values(profile["air_address_hex"]) + "};",
        "static const uint8_t PROFILE_PAYLOAD[11] = {" + hex_values(payload_hex) + "};",
        "static const uint8_t PROFILE_COUNTER_START = 0x{:02x};".format(profile["counter_start"]),
        "static const uint16_t PROFILE_COUNTER_PERIOD_MS = {};".format(profile["counter_period_ms"]),
        "static const uint8_t PROFILE_FRAME_SIZE = 22;",
        "static const uint8_t PROFILE_FRAME_A[22] = {" + values(profile["frame_a"]) + "};",
        "static const uint8_t PROFILE_FRAME_B[22] = {" + values(profile["frame_b"]) + "};",
        "",
    ])


def parse_address(value):
    if not re.fullmatch(r"[0-9a-fA-F]{10}", value):
        raise argparse.ArgumentTypeError("address must contain exactly ten hexadecimal digits")
    return value.lower()


def parse_counter_start(value):
    try:
        counter = int(value, 16) if re.fullmatch(r"0[xX][0-9a-fA-F]+", value) else int(value, 10)
    except ValueError:
        raise argparse.ArgumentTypeError("counter start must be decimal or 0x-prefixed hexadecimal")
    if not 0 <= counter <= 255:
        raise argparse.ArgumentTypeError("counter start must be from 0 to 255")
    return counter


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path, help="Existing RAW capture; default is JSON review only")
    parser.add_argument("--address", type=parse_address, help="Explicit on-air address from the review")
    parser.add_argument("--line", type=int, help="Explicit first captured frame line from the review")
    parser.add_argument("--line-b", type=int, help="Optional second frame line; otherwise choose or duplicate")
    parser.add_argument("--advance-counters", action="store_true",
                        help="Explicitly request unverified counter/PID advancement instead of exact replay")
    parser.add_argument("--counter-start", type=parse_counter_start,
                        help="Override first counter (0..255, decimal or 0x hex); requires --advance-counters")
    parser.add_argument("--write-header", type=Path, help="Write a C++ header after explicit address and line selection")
    args = parser.parse_args(argv)
    if bool(args.address) != (args.line is not None):
        parser.error("select both --address and --line, or omit both for review")
    if args.write_header is not None and args.address is None:
        parser.error("--write-header requires explicit --address and --line")
    if args.line_b is not None and args.address is None:
        parser.error("--line-b requires explicit --address and --line")
    if args.advance_counters and args.address is None:
        parser.error("--advance-counters requires explicit --address and --line")
    if args.counter_start is not None and not args.advance_counters:
        parser.error("--counter-start requires --advance-counters")
    if any(line is not None and line < 1 for line in (args.line, args.line_b)):
        parser.error("line numbers must be positive")
    try:
        capture = load_capture(args.log)
        report = review_capture(capture)
        if args.address is not None:
            profile = select_profile(capture, args.address, args.line, args.line_b,
                                     args.advance_counters, args.counter_start)
            report["selected_profile"] = profile
            if args.write_header is not None:
                if args.write_header.resolve() == args.log.resolve():
                    raise ValueError("header destination cannot overwrite the source capture")
                content = header_text(profile)
                args.write_header.write_text(content, encoding="ascii")
                report["header_written"] = True
                report["header_path"] = str(args.write_header)
        print(json.dumps(report, indent=2))
    except (OSError, ValueError) as error:
        parser.exit(1, "Error: {}\n".format(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
