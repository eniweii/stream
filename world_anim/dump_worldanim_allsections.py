#!/usr/bin/env python3
"""
dump_worldanim_allsections.py - Whole-stream rtnode diagnostic, bucketed
by (is_library_anim, use_library_anim, use_parent_anim).

Section 2600 is confirmed correct already - do not touch that path.
This is purely to characterize what a UseLibraryAnim / UseParentAnim
node ("library animation" instance, as opposed to a library SOURCE
node) actually carries on its own record, since WorldAnimReader.cs
currently never resolves the borrow (ReadNode() skips attaching a
Frames chunk to these nodes, and nothing else fills Frames in for
them afterward) - so GetOrBakeFrames() falls through to the raw
axis-spin bake using THIS node's own fields, which is suspected to be
the actual bug.

Struct is the confirmed-correct rt_node layout (see
Common/WorldAnimReader.cs / WorldAnimNode.cs), not the earlier
parse_2400_anims.py field table.
"""

import argparse
import mmap
import struct
import sys
from collections import defaultdict

RTNODE_CHUNK_ID = 0x00037250
SENTINEL = 0x11111111

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
    rec["bits_frame_count_field"] = bits & 0x3FFFFFFF
    rec["is_ping_pong"] = bool(bits & 0x40000000)
    rec["is_looping"] = bool(bits & 0x80000000)

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

            records.append((idx, rec))
        return records


def bucket_key(rec):
    return (rec["is_library_anim"], rec["use_library_anim"], rec["use_parent_anim"])


def bucket_label(key):
    lib, use_lib, use_parent = key
    parts = []
    parts.append("is_library_anim=" + str(lib))
    parts.append("use_library_anim=" + str(use_lib))
    parts.append("use_parent_anim=" + str(use_parent))
    return ", ".join(parts)


def summarize_bucket(recs):
    def uniq(key):
        return sorted({r[key] for r in recs})

    sections = uniq("section_number")
    print(f"    count = {len(recs)}")
    print(f"    section_number values = {sections}")
    print(f"    key_frame_count values = {uniq('key_frame_count')}")
    print(f"    is_ping_pong values = {uniq('is_ping_pong')}")
    print(f"    is_looping values = {uniq('is_looping')}")
    print(f"    initial_angle values = {uniq('initial_angle')}")
    print(f"    rot_speed_x values = {uniq('rot_speed_x')}")
    print(f"    solid_key0 sample (first 5) = "
          f"{[f'0x{v:08X}' for v in [r['solid_key0'] for r in recs[:5]]]}")
    print(f"    UNCONFIRMED unknown_0x6e values = {uniq('unknown_0x6e')}")
    print(f"    UNCONFIRMED use_parent_frames values = {uniq('use_parent_frames')}")
    print()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("file", help="Path to stream file (e.g. STREAML5RA.BUN)")
    ap.add_argument("--dump-records", action="store_true",
                     help="Print every individual record inside each bucket")
    args = ap.parse_args()

    try:
        records = scan(args.file)
    except OSError as e:
        print(f"Error opening file '{args.file}': {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(records)} total rtnode records in {args.file}\n")

    buckets = defaultdict(list)
    for offset, rec in records:
        buckets[bucket_key(rec)].append(rec)

    print("=" * 70)
    print("BUCKETS BY (is_library_anim, use_library_anim, use_parent_anim)")
    print("=" * 70)
    for key in sorted(buckets, key=lambda k: (-len(buckets[k]),)):
        print(f"[{bucket_label(key)}]")
        summarize_bucket(buckets[key])

    if args.dump_records:
        for key, recs in buckets.items():
            print(f"\n### Full record dump for [{bucket_label(key)}] ###")
            for r in recs:
                print(
                    f"  key=0x{r['key']:08X} guid=0x{r['scenery_guid']:08X} "
                    f"section={r['section_number']} "
                    f"solid_keys=(0x{r['solid_key0']:08X},0x{r['solid_key1']:08X},"
                    f"0x{r['solid_key2']:08X}) "
                    f"kfc={r['key_frame_count']} "
                    f"ping_pong={r['is_ping_pong']} looping={r['is_looping']} "
                    f"init_angle={r['initial_angle']} rot_speed="
                    f"({r['rot_speed_x']},{r['rot_speed_y']},{r['rot_speed_z']}) "
                    f"unk_0x6e={r['unknown_0x6e']} use_parent_frames="
                    f"{r['use_parent_frames']}"
                )


if __name__ == "__main__":
    main()
