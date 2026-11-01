#!/usr/bin/env python3
"""Review three uncaptured track hypotheses offline; never opens USB or sends RF.

Backward direction is inferred from the packed-nibble model. The original
reverse buttons are desoldered, so no stock-remote reverse capture establishes
these meanings. Both-forward combines two separately observed templates.
"""

import argparse
import copy
import hashlib
import json
from pathlib import Path
import zlib

import make_profile as profiles
from analyze_raw import RawRecord
from decode_hs6200 import decode_record


HYPOTHESES = {"left-backward": (0x0F, ("left",)),
              "right-backward": (0xF0, ("right",)),
              "both-forward": (0x77, ("left", "right"))}
PEER = "8d5fb5cb75"
IDENTITY_PREFIX = bytes.fromhex("15130188")


def read_seed(path, side):
    raw = Path(path).read_bytes()
    document = json.loads(raw)
    seed = document.get("selected_profile", document)
    expected = 0x07 if side == "left" else 0x70
    if seed.get("advance_counters") is not True or seed.get("air_address_hex") != PEER:
        raise ValueError("seed must advance counters and belong to the observed peer")
    if seed.get("payload_template_hex") is not None:
        raise ValueError("a transformed hypothesis cannot serve as an observed forward seed")
    if not 0 <= seed.get("channel", -1) <= 83 or seed.get("counter_period_ms") != 128:
        raise ValueError("seed channel or counter period is invalid")
    for name in ("frame_a", "frame_b"):
        row = seed[name]
        encoded, payload = bytes.fromhex(row["encoded_hex"]), bytes.fromhex(row["payload_hex"])
        if payload[:4] != IDENTITY_PREFIX:
            raise ValueError("seed payload identity must match the currently observed peer/session")
        if (len(encoded) != 22 or len(payload) != 11 or row["air_address_hex"] != PEER
                or payload[5:10] != bytes([expected, 0, 0, 0, 0])
                or ((payload[10] - sum(payload[:10])) & 255) != 2):
            raise ValueError("seed must be a clean forward template with checksum sum+2")
        matches = decode_record(RawRecord(seed["channel"], 0, encoded + b"\xff" * 10),
                                bytes.fromhex(PEER), max_missing_address_bits=0)
        if not any(f.bit_offset == 0 and f.crc_valid and f.payload == payload
                   and f.pid == row["pid"] and f.no_ack == 1
                   and format(f.received_crc, "04x") == row["crc16"].lower() for f in matches):
            raise ValueError("captured seed frame fails the existing decoder/CRC check")
    if seed["frame_a"]["payload_hex"] != seed["frame_b"]["payload_hex"]:
        raise ValueError("seed A/B must describe the same captured application payload")
    provenance = {"profile_json": str(Path(path)), "profile_json_sha256": hashlib.sha256(raw).hexdigest(),
                  "profile_id": seed["profile_id"], "capture": seed["source"],
                  "capture_sha256": seed["source_sha256"],
                  "captured_frame_a": copy.deepcopy(seed["frame_a"]),
                  "captured_frame_b": copy.deepcopy(seed["frame_b"])}
    return seed, provenance


def make_candidate(hypothesis, counter_start, left_profile=None, right_profile=None):
    if hypothesis not in HYPOTHESES or type(counter_start) is not int or not 0 <= counter_start <= 255:
        raise ValueError("select a named hypothesis and an explicit counter start from 0 to 255")
    new_slot, sides = HYPOTHESES[hypothesis]
    paths = {"left": left_profile, "right": right_profile}
    if any(paths[side] is None for side in sides):
        raise ValueError("the hypothesis requires its applicable observed forward profile(s)")
    loaded = [read_seed(paths[side], side) for side in sides]
    base = loaded[0][0]
    prefix = bytes.fromhex(base["frame_a"]["payload_hex"])[:4]
    if any(bytes.fromhex(seed["frame_a"]["payload_hex"])[:4] != prefix
           or seed["air_address_hex"] != base["air_address_hex"]
           or seed["channel"] != base["channel"] for seed, _ in loaded):
        raise ValueError("forward seeds must match peer, identity prefix, and channel")
    payload = bytearray.fromhex(base["frame_a"]["payload_hex"])
    payload[5] = new_slot
    payload[10] = (sum(payload[:10]) + 2) & 255
    candidate = copy.deepcopy(base)
    candidate.update(mode="uncaptured_track_hypothesis", hypothesis=hypothesis,
                     semantic_status="NOT_CONFIRMED", advance_counters=True,
                     counter_start=counter_start, counter_start_source="explicit_override",
                     payload_template_hex=payload.hex(), selection="uncaptured_track_hypothesis",
                     source_provenance=[provenance for _, provenance in loaded],
                     action_transformation={"payload_indices_changed": [5, 10],
                                            "new_slot5_hex": format(new_slot, "02x"),
                                            "model": "unverified packed-nibble direction/magnitude model"},
                     limits=["Uncaptured action hypothesis; direction and combined motion are NOT confirmed.",
                             "Reverse buttons are desoldered; no reverse capture validates this interpretation.",
                             "Preserved A/B arrays are captured references, not the new action packets.",
                             "Counter/PID/checksum/CRC are generated by the existing bounded firmware.",
                             "This review tool performs no transmission and assigns no universal receiver identity."])
    pair_bytes = b"".join(bytes.fromhex(seed[name]["encoded_hex"])
                          for seed, _ in loaded for name in ("frame_a", "frame_b"))
    identity = (b"track-candidate-v1\0" + candidate["mode"].encode() + hypothesis.encode()
                + payload + bytes([base["channel"], counter_start])
                + base["counter_period_ms"].to_bytes(2, "big") + pair_bytes)
    candidate["profile_id"] = format(zlib.crc32(identity) & 0xFFFFFFFF, "08x")
    return candidate


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hypothesis", required=True, choices=HYPOTHESES)
    parser.add_argument("--left-profile", type=Path)
    parser.add_argument("--right-profile", type=Path)
    parser.add_argument("--counter-start", required=True, type=profiles.parse_counter_start)
    parser.add_argument("--write-header", type=Path, help="Create a new candidate header; refuses an existing file")
    args = parser.parse_args(argv)
    try:
        candidate = make_candidate(args.hypothesis, args.counter_start, args.left_profile, args.right_profile)
        report = {"mode": "offline_review", "header_written": False, "selected_profile": candidate}
        if args.write_header is not None:
            text = profiles.header_text(candidate)
            with args.write_header.open("x", encoding="ascii") as stream:
                stream.write(text)
            report.update(header_written=True, header_path=str(args.write_header))
        print(json.dumps(report, indent=2))
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, "Error: {}\n".format(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
