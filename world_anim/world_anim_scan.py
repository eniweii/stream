#!/usr/bin/env python3
"""
world_anim_scan.py - brute-force candidate scanner for Carbon's
world_anim chunk family (header/frames/rtnode/counts/endptr), for use
against a real stream file (e.g. STREAML5RA.BUN).

Same approach already used for BCHUNK_SPEED_EMITTER_LIBRARY: since these
are leaf chunks (no nested children) we don't need true chunk-tree
traversal - scan the whole file for the raw 4-byte little-endian chunk-ID
pattern, pair it with the following 4-byte declared length, and only
accept it as a candidate if that length falls in a plausible range for
that chunk type.

Usage:
    python world_anim_scan.py STREAML5RA.BUN
    python world_anim_scan.py STREAML5RA.BUN --max-per-type 5
    python world_anim_scan.py STREAML5RA.BUN --out world_anim_dump.txt
    python world_anim_scan.py STREAML5RA.BUN --offset 0x195A0B20 --length 120
"""

import argparse
import mmap
import struct
import sys

CHUNK_IDS = {
    0x00037220: "world_anim_header",
    0x00037240: "world_anim_frames",
    0x00037250: "world_anim_rtnode",
    0x00037260: "world_anim_counts",
    0x00037270: "world_anim_endptr",
}

# Plausible payload-length ranges per chunk type, used only to filter out
# false-positive 4-byte matches - not a claim about the true size.
PLAUSIBLE_LENGTH = {
    0x00037220: (8, 64),           # header - doc says 0x10 (16) bytes
    0x00037240: (32, 50_000_000),  # frames - variable, holds N x 64-byte matrices
    0x00037250: (96, 160),         # rtnode - 0x78 (120) bytes payload
    0x00037260: (1, 64),           # counts - probably a handful of bytes
    0x00037270: (1, 64),           # endptr - probably a handful of bytes
}

# Corrected field offsets including +0x04 preamble shift
RTNODE_FIELD_OFFSETS = [
    ("sentinel",          0x00, "u32"),
    ("key",               0x44, "u32"),
    ("parent_index",      0x48, "i8"),
    ("timescale",         0x49, "u8"),
    ("flags",             0x4A, "u8"),
    ("pad",               0x4B, "u8"),
    ("solid_keys[0]",     0x4C, "u32"),
    ("solid_keys[1]",     0x50, "u32"),
    ("solid_keys[2]",     0x54, "u32"),
    ("smackable_key",     0x58, "u32"),
    ("scenery_guid",      0x5C, "u32"),
    ("rotation_speed[0]", 0x60, "i16"),
    ("rotation_speed[1]", 0x62, "i16"),
    ("rotation_speed[2]", 0x64, "i16"),
    ("section_number",    0x66, "u16"),
    ("key_frame_count",   0x68, "u32"),
    ("bits",              0x6C, "u32"),
    ("initial_angle",     0x70, "i16"),
    ("unknown0x6E",       0x72, "u16"),
    ("use_parent_frames", 0x74, "u8"),
]

FMT = {"u32": "<I", "i32": "<i", "u16": "<H", "i16": "<h", "u8": "<B", "i8": "<b"}
SIZE = {"u32": 4, "i32": 4, "u16": 2, "i16": 2, "u8": 1, "i8": 1}


def read_field(buf, base, off, kind):
    size = SIZE[kind]
    if base + off + size > len(buf):
        return None
    return struct.unpack_from(FMT[kind], buf, base + off)[0]


def hexdump(window, start_addr):
    lines = []
    for i in range(0, len(window), 16):
        chunk = window[i:i + 16]
        addr = start_addr + i
        hex_part = " ".join(f"{b:02X}" for b in chunk)
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"0x{addr:08X}  {hex_part:<47}  {ascii_part}")
    return "\n".join(lines)


def find_candidates(mm, chunk_id, min_len, max_len):
    pattern = struct.pack("<I", chunk_id)
    pos = 0
    results = []
    while True:
        idx = mm.find(pattern, pos)
        if idx == -1:
            break
        pos = idx + 1
        if idx + 8 > len(mm):
            continue
        length = struct.unpack_from("<I", mm, idx + 4)[0]
        if min_len <= length <= max_len and idx + 8 + length <= len(mm):
            results.append((idx, length))
    return results


def print_rtnode_detail(p, mm, offset, length, context_before, context_after):
    payload_start = offset + 8
    payload_end = payload_start + length
    p(f"world_anim_rtnode candidate @ 0x{offset:08X}")
    p(f"chunk_id=0x00037250 payload_length=0x{length:X} ({length})")
    p(f"payload_start=0x{payload_start:08X} payload_end=0x{payload_end:08X}")
    p()
    p("Parsed fields (Corrected +0x04 Preamble Shift):")
    for name, off, kind in RTNODE_FIELD_OFFSETS:
        val = read_field(mm, payload_start, off, kind)
        if val is None:
            continue
        if name == "flags":
            p(f"  +0x{off:02X} {name:<18}: 0x{val:02X}  "
              f"(is_library_anim={val & 1}, use_library_anim={(val >> 1) & 1}, use_parent_anim={(val >> 2) & 1})")
        elif name == "bits":
            p(f"  +0x{off:02X} {name:<18}: 0x{val:08X}  "
              f"(frame_count={val & 0x3FFFFFFF}, is_ping_pong={(val >> 30) & 1}, is_looping={(val >> 31) & 1})")
        else:
            p(f"  +0x{off:02X} {name:<18}: {val} (0x{val:X})")
    p()
    win_start = max(0, offset - context_before)
    win_end = min(len(mm), payload_end + context_after)
    p(f"Raw hex (window 0x{win_start:08X}..0x{win_end:08X}):")
    p(hexdump(mm[win_start:win_end], win_start))
    p()


def print_generic_detail(p, mm, offset, length, chunk_name, context_before, context_after):
    payload_start = offset + 8
    payload_end = payload_start + length
    p(f"{chunk_name} candidate @ 0x{offset:08X}")
    p(f"payload_length=0x{length:X} ({length})  payload_start=0x{payload_start:08X} payload_end=0x{payload_end:08X}")
    win_start = max(0, offset - context_before)
    win_end = min(len(mm), payload_end + context_after)
    p(hexdump(mm[win_start:win_end], win_start))
    p()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", help="Path to the stream file (e.g. STREAML5RA.BUN)")
    ap.add_argument("--max-per-type", type=int, default=3, help="Detailed hex dumps to print per chunk type (default 3)")
    ap.add_argument("--summary-limit", type=int, default=100, help="Rows in the condensed chunk-order table (default 100)")
    ap.add_argument("--context-before", type=int, default=32, help="Bytes of context before a candidate's header")
    ap.add_argument("--context-after", type=int, default=32, help="Bytes of context after a candidate's payload")
    ap.add_argument("--offset", type=lambda s: int(s, 0), default=None, help="Skip scanning, dump one specific offset (hex, e.g. 0x195A0B20)")
    ap.add_argument("--length", type=lambda s: int(s, 0), default=None, help="Payload length for --offset (default: read from file)")
    ap.add_argument("--out", default=None, help="Write output to this file instead of stdout")
    args = ap.parse_args()

    out_fh = open(args.out, "w", encoding="utf-8") if args.out else sys.stdout

    def p(*a, **kw):
        print(*a, file=out_fh, **kw)

    with open(args.file, "rb") as f:
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)

        if args.offset is not None:
            offset = args.offset
            length = args.length if args.length is not None else struct.unpack_from("<I", mm, offset + 4)[0]
            chunk_id = struct.unpack_from("<I", mm, offset)[0]
            name = CHUNK_IDS.get(chunk_id, f"unknown_0x{chunk_id:08X}")
            p(f"# Direct dump requested at 0x{offset:08X} (chunk_id=0x{chunk_id:08X} -> {name})\n")
            if chunk_id == 0x00037250:
                print_rtnode_detail(p, mm, offset, length, args.context_before, args.context_after)
            else:
                print_generic_detail(p, mm, offset, length, name, args.context_before, args.context_after)
            mm.close()
            if args.out:
                out_fh.close()
                print(f"Wrote output to {args.out}")
            return

        p(f"# Scanning {args.file} ({len(mm):,} bytes) for world_anim chunk family\n")

        all_hits = []
        per_type = {}
        for chunk_id, name in CHUNK_IDS.items():
            min_len, max_len = PLAUSIBLE_LENGTH[chunk_id]
            hits = find_candidates(mm, chunk_id, min_len, max_len)
            per_type[chunk_id] = hits
            for offset, length in hits:
                all_hits.append((offset, chunk_id, length))
            p(f"{name} (0x{chunk_id:08X}): {len(hits)} candidate(s) found")
        p()

        all_hits.sort()

        p(f"## Condensed chunk-order table (first {args.summary_limit} hits, sorted by file offset)")
        p(f"{'offset':<12} {'chunk':<20} {'length':<8}")
        for offset, chunk_id, length in all_hits[:args.summary_limit]:
            p(f"0x{offset:08X}   {CHUNK_IDS[chunk_id]:<20} {length}")
        p()

        p("## Detailed hex dumps")
        for chunk_id, name in CHUNK_IDS.items():
            hits = per_type[chunk_id]
            if not hits:
                continue
            to_show = hits[:args.max_per_type]
            if len(hits) > args.max_per_type:
                to_show = to_show + [hits[-1]]
            for offset, length in to_show:
                p("=" * 70)
                if chunk_id == 0x00037250:
                    print_rtnode_detail(p, mm, offset, length, args.context_before, args.context_after)
                else:
                    print_generic_detail(p, mm, offset, length, name, args.context_before, args.context_after)

        mm.close()

    if args.out:
        out_fh.close()
        print(f"Wrote output to {args.out}")


if __name__ == "__main__":
    main()