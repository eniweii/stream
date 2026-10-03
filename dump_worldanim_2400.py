#!/usr/bin/env python3
"""
dump_worldanim_2400.py - Section 2400 rtnode diagnostic dump

Rebuilt against the CONFIRMED-CORRECT rt_node struct (see
Common/WorldAnimReader.cs / WorldAnimNode.cs in NFS-ModTools), not the
earlier parse_2400_anims.py field table, which is known wrong: it
collapsed ParentIndex+TimeScale+Flags+Pad0 into a bogus "node_flags"
and RotationSpeedX+Y into "param_extra".

Goal: for every section-2400 (procedural/library) rtnode, print every
field -- including the still-unconfirmed/unused ones (Unknown0x6E,
UseParentFrames, Pad1, Pad2, and the low 30 "frame_count" bits inside
Bits, which should be redundant/zero for these nodes since they have
no real keyframes) -- and group the dump by whether IsPingPong is set,
so a real correlation (a field that only varies/only-nonzero on
ping-pong nodes) is visible directly in the output instead of guessed.
"""

import argparse
import mmap
import struct
import sys

RTNODE_CHUNK_ID = 0x00037250
TARGET_SECTION = 2400
SENTINEL = 0x11111111

# sentinel(I) + matrix(16f) + Key(I) + ParentIndex(b) + TimeScale(B) +
# Flags(B) + Pad0(B) + SolidKey0-2(III) + SmackableKey(I) +
# SceneryGuid(I) + RotSpeedX,Y,Z(hhh) + SectionNumber(H) +
# KeyFrameCount(I) + Bits(I) + InitialAngle(h) + Unknown0x6E(H) +
# UseParentFrames(B) + Pad1(B) + Pad2(h)  == 120 bytes, matches
# confirmed chunk payload size.
STRUCT_FMT = "<I16fIbBBBIIIIIhhhHIIhHBBh"
STRUCT_SIZE = struct.calcsize(STRUCT_FMT)
assert STRUCT_SIZE == 0x78, STRUCT_SIZE

FIELD_NAMES = [
    "sentinel",
    *[f"m{i}" for i in range(16)],
    "key", "parent_index", "time_scale", "flags", "pad0",
    "solid_key0", "solid_key1", "solid_key2",
    "smackable_key", "scenery_guid",
    "rot_speed_x", "rot_speed_y", "rot_speed_z",
    "section_number", "key_frame_count", "bits",
    "initial_angle", "unknown_0x6e", "use_parent_frames",
    "pad1", "pad2",
]


def parse_record(payload):
    vals = struct.unpack(STRUCT_FMT, payload)
    rec = dict(zip(FIELD_NAMES, vals))

    bits = rec["bits"]
    rec["bits_frame_count_field"] = bits & 0x3FFFFFFF  # low 30 bits
    rec["is_ping_pong"] = bool(bits & 0x40000000)       # bit 30
    rec["is_looping"] = bool(bits & 0x80000000)         # bit 31

    rec["is_library_anim"] = bool(rec["flags"] & 0x01)
    rec["use_library_anim"] = bool(rec["flags"] & 0x02)
    rec["use_parent_anim"] = bool(rec["flags"] & 0x04)

    return rec


def scan(path):
    with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        pattern = struct.pack("<I", RTNODE_CHUNK_ID)
        pos = 0
        records = []
        while True:
            idx = mm.find(pattern, pos)
            if idx == -1:
                break
            pos = idx + 1

            if idx + 8 > len(mm):
                continue
            chunk_size = struct.unpack_from("<I", mm, idx + 4)[0]
            if chunk_size != STRUCT_SIZE:
                continue

            payload = mm[idx + 8: idx + 8 + chunk_size]
            if len(payload) != STRUCT_SIZE:
                continue

            rec = parse_record(payload)
            if rec["sentinel"] != SENTINEL:
                continue
            if rec["section_number"] != TARGET_SECTION:
                continue

            records.append((idx, rec))
        return records


def print_record(offset, rec):
    print(f"--- rtnode @ 0x{offset:08X} key=0x{rec['key']:08X} "
          f"guid=0x{rec['scenery_guid']:08X} ---")
    print(f"  flags=0x{rec['flags']:02X} "
          f"(is_library_anim={rec['is_library_anim']}, "
          f"use_library_anim={rec['use_library_anim']}, "
          f"use_parent_anim={rec['use_parent_anim']})")
    print(f"  key_frame_count={rec['key_frame_count']}")
    print(f"  bits=0x{rec['bits']:08X} -> "
          f"frame_count_bits={rec['bits_frame_count_field']}, "
          f"is_ping_pong={rec['is_ping_pong']}, "
          f"is_looping={rec['is_looping']}")
    print(f"  rotation_speed=({rec['rot_speed_x']}, {rec['rot_speed_y']}, "
          f"{rec['rot_speed_z']})  initial_angle={rec['initial_angle']}")
    print(f"  time_scale={rec['time_scale']}  parent_index={rec['parent_index']}")
    print(f"  solid_keys=(0x{rec['solid_key0']:08X}, 0x{rec['solid_key1']:08X}, "
          f"0x{rec['solid_key2']:08X})  smackable_key=0x{rec['smackable_key']:08X}")
    print(f"  UNCONFIRMED: unknown_0x6e={rec['unknown_0x6e']} "
          f"(0x{rec['unknown_0x6e']:04X})  "
          f"use_parent_frames={rec['use_parent_frames']}  "
          f"pad1={rec['pad1']}  pad2={rec['pad2']}")
    print()


def summarize(records):
    def bucket(pred):
        return [r for _, r in records if pred(r)]

    pp = bucket(lambda r: r["is_ping_pong"])
    non_pp = bucket(lambda r: not r["is_ping_pong"])

    def stats(name, key):
        vals_pp = sorted({r[key] for r in pp})
        vals_non = sorted({r[key] for r in non_pp})
        print(f"  {name}:")
        print(f"    ping_pong=True  -> unique values: {vals_pp}")
        print(f"    ping_pong=False -> unique values: {vals_non}")

    print("=" * 60)
    print(f"SUMMARY: {len(pp)} ping-pong nodes, {len(non_pp)} non-ping-pong nodes "
          f"(of {len(records)} total section-2400 nodes)")
    print("=" * 60)
    for name, key in [
        ("bits_frame_count_field (low 30 bits of Bits)", "bits_frame_count_field"),
        ("unknown_0x6e", "unknown_0x6e"),
        ("use_parent_frames", "use_parent_frames"),
        ("pad1", "pad1"),
        ("pad2", "pad2"),
        ("time_scale", "time_scale"),
        ("initial_angle", "initial_angle"),
        ("is_looping", "is_looping"),
    ]:
        stats(name, key)
    print()
    print("Look for any field above where ping_pong=True and ping_pong=False")
    print("show clearly DIFFERENT (non-overlapping) value ranges - that's the")
    print("strongest candidate for an amplitude/range/period parameter.")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("file", help="Path to stream file (e.g. STREAML5RA.BUN)")
    ap.add_argument("--dump-records", action="store_true",
                     help="Print full per-record field dump (verbose)")
    args = ap.parse_args()

    try:
        records = scan(args.file)
    except OSError as e:
        print(f"Error opening file '{args.file}': {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(records)} section-2400 rtnode records in {args.file}\n")

    if args.dump_records:
        for offset, rec in records:
            print_record(offset, rec)

    summarize(records)


if __name__ == "__main__":
    main()
