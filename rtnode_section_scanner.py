#!/usr/bin/env python3
"""
rtnode_section_scanner.py - Targeted reader for world_anim_rtnode chunks
filtering specifically across section 2400 (Global Anim Library) and
section 2600 (Always-Loaded Persistent Objects).
"""

import argparse
import mmap
import struct
import sys

RTNODE_CHUNK_ID = 0x00037250
TARGET_SECTIONS = {
    2400: "0x0960 (Global Anim Library)",
    2600: "0x0A28 (Always-Loaded Pool)",
}

# Offsets relative to payload start (+0x04 preamble shift included)
FIELDS = [
    ("key", 0x44, "<I"),
    ("parent_index", 0x48, "<b"),
    ("timescale", 0x49, "<B"),
    ("flags", 0x4A, "<B"),
    ("solid_keys_0", 0x4C, "<I"),
    ("scenery_guid", 0x5C, "<I"),
    ("rot_x", 0x60, "<h"),
    ("rot_y", 0x62, "<h"),
    ("rot_z", 0x64, "<h"),
    ("section_number", 0x66, "<H"),
    ("key_frame_count", 0x68, "<I"),
    ("bits", 0x6C, "<I"),
]


def parse_rtnode(payload):
    if len(payload) < 0x78:
        return None
    data = {}
    for name, off, fmt in FIELDS:
        size = struct.calcsize(fmt)
        if off + size <= len(payload):
            data[name] = struct.unpack_from(fmt, payload, off)[0]
    return data


def scan_file(filepath, allowed_sections, max_results):
    with open(filepath, "rb") as f:
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)

    pattern = struct.pack("<I", RTNODE_CHUNK_ID)
    pos = 0
    hits = []

    while True:
        idx = mm.find(pattern, pos)
        if idx == -1:
            break
        pos = idx + 1

        if idx + 8 > len(mm):
            continue

        length = struct.unpack_from("<I", mm, idx + 4)[0]
        if length != 0x78 or idx + 8 + length > len(mm):
            continue

        payload = mm[idx + 8 : idx + 8 + length]
        parsed = parse_rtnode(payload)

        if not parsed:
            continue

        sec = parsed.get("section_number")
        if allowed_sections and sec not in allowed_sections:
            continue

        hits.append((idx, parsed))

    mm.close()
    return hits


def main():
    ap = argparse.ArgumentParser(description="Scan STREAM files for section 2400/2600 rtnode entries.")
    ap.add_argument("file", help="Path to stream file (e.g., STREAML5RA.BUN)")
    ap.add_argument("--sections", nargs="+", type=int, default=[2400, 2600], help="Sections to display (default: 2400 2600)")
    ap.add_argument("--max", type=int, default=50, help="Max entries to print per section")
    args = ap.parse_args()

    print(f"Scanning {args.file} for rtnodes in sections: {args.sections}...\n")
    results = scan_file(args.file, set(args.sections), args.max)

    if not results:
        print("No matching rtnode entries found for specified sections.")
        return

    print(f"{'Offset':<12} {'Sec':<6} {'Key (Hash)':<12} {'GUID':<12} {'Frames':<8} {'RotSpeed (X,Y,Z)':<20} {'Flags':<8}")
    print("-" * 85)

    counts = {sec: 0 for sec in args.sections}
    for offset, d in results:
        sec = d['section_number']
        if counts.get(sec, 0) >= args.max:
            continue
        counts[sec] = counts.get(sec, 0) + 1

        rot_str = f"({d['rot_x']},{d['rot_y']},{d['rot_z']})"
        print(f"0x{offset:08X}   {sec:<6} 0x{d['key']:08X}   0x{d['scenery_guid']:08X}   {d['key_frame_count']:<8} {rot_str:<20} 0x{d['flags']:02X}")

    print("\nSection hit counts:")
    for sec in args.sections:
        label = TARGET_SECTIONS.get(sec, "Custom Section")
        print(f"  Section {sec} [{label}]: {counts.get(sec, 0)} printed")


if __name__ == "__main__":
    main()