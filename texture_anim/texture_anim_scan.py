#!/usr/bin/env python3
"""
texture_anim_scan.py - brute-force candidate scanner for Carbon/MW's
TextureAnimPack chunk family (the texture frame-swap "playlist" system -
NOT UV scrolling, which is a separate, already-confirmed set of fields
inside the ordinary TextureStruct and needs no discovery work).

Chunk IDs below are REAL - found directly in MaxHwoy/hyperlinked's
chunk.hpp, not guessed:
    texture_pack_anim_header = 0x33312001  (leaf)
    texture_pack_anim_frames = 0x33312002  (leaf)
    texture_pack_anim_pack   = 0xB3312000  (container, high bit set)
    texture_pack_anim_inst   = 0xB3312004  (container, high bit set)

What's NOT confirmed is the on-disk byte layout - hyperlinked only gives
the runtime shape of the resulting texture::animation object (name[16],
key u32, frame_count u8, fps u8, time_base, then a frame table). The
per-field offsets this script prints for texture_pack_anim_header are a
HYPOTHESIS based on that runtime shape, not a verified struct - same
"print raw hex + a labeled guess" approach used for world_anim's rtnode,
which is what actually caught that struct's real layout.

These are Carbon/MW TPK (texture package) files, not the STREAM/region
files world_anim and the emitter system live in - point this at a .TPK
(or wherever your project's texture pack data actually lives), not at
STREAML5RA.BUN.

Usage:
    python texture_anim_scan.py some_pack.tpk
    python texture_anim_scan.py some_pack.tpk --max-per-type 5
    python texture_anim_scan.py some_pack.tpk --out dump.txt
    python texture_anim_scan.py some_pack.tpk --offset 0x1234 --length 32
"""

import argparse
import mmap
import struct
import sys

CHUNK_IDS = {
    0x33312001: "texture_pack_anim_header",
    0x33312002: "texture_pack_anim_frames",
    0xB3312000: "texture_pack_anim_pack",
    0xB3312004: "texture_pack_anim_inst",
}

# Plausible payload-length ranges - loose, since nothing here is confirmed
# yet. Containers get a wide range; header is guessed around name(16) +
# key(4) + frame_count(1) + fps(1) + time_base(1..4) + padding, so
# somewhere in the 24-32 byte neighborhood; frames is frame_count entries
# of unknown per-entry size, so left wide open.
PLAUSIBLE_LENGTH = {
    0x33312001: (16, 128),
    0x33312002: (4, 4_000_000),
    0xB3312000: (8, 500_000_000),
    0xB3312004: (8, 500_000_000),
}

# CONFIRMED via two independent real hex samples (SGN_SEOTECH, TRS_WATERA)
# whose frame hashes matched real DDS filenames exactly. The two 8/12-byte
# gaps are zero in both samples - real but still unidentified fields, or
# genuinely unused; either way they're not needed to extract playback data.
HEADER_FIELD_GUESS = [
    ("name",       0x08, "16s"),
    ("key",        0x18, "u32"),
    ("frame_count",0x1C, "u8"),
    ("fps",        0x1D, "u8"),
    ("time_base",  0x1E, "u8"),
    ("pad",        0x1F, "u8"),
]

FMT = {"u32": "<I", "u16": "<H", "u8": "<B", "16s": "16s"}
SIZE = {"u32": 4, "u16": 2, "u8": 1, "16s": 16}


def read_field(buf, base, off, kind):
    size = SIZE[kind]
    if base + off + size > len(buf):
        return None
    if kind == "16s":
        raw = struct.unpack_from(FMT[kind], buf, base + off)[0]
        return raw.split(b"\x00", 1)[0].decode("latin-1", errors="replace")
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


def print_header_detail(p, mm, offset, length, context_before, context_after):
    payload_start = offset + 8
    payload_end = payload_start + length
    p(f"texture_pack_anim_header candidate @ 0x{offset:08X}")
    p(f"payload_length=0x{length:X} ({length})  payload_start=0x{payload_start:08X} payload_end=0x{payload_end:08X}")
    p()
    p("Fields (CONFIRMED layout, two independent real samples verified):")
    frame_count = None
    for name, off, kind in HEADER_FIELD_GUESS:
        val = read_field(mm, payload_start, off, kind)
        if val is None:
            continue
        if name == "frame_count":
            frame_count = val
        if isinstance(val, int):
            p(f"  +0x{off:02X} {name:<12}: {val} (0x{val:X})")
        else:
            p(f"  +0x{off:02X} {name:<12}: {val!r}")
    p()

    # The frames chunk is confirmed to sit immediately after the header
    # with no gap - check for it and flag a mismatch as the strongest
    # available sign this header candidate is a false positive.
    if payload_end + 8 <= len(mm):
        next_id = struct.unpack_from("<I", mm, payload_end)[0]
        if next_id == 0x33312002:
            frames_len = struct.unpack_from("<I", mm, payload_end + 4)[0]
            frames_start = payload_end + 8
            expected_len = (frame_count or 0) * 12
            match = "OK" if frames_len == expected_len else "MISMATCH - likely a false-positive header candidate"
            p(f"Companion frames chunk @ 0x{payload_end:08X}: length={frames_len} "
              f"(expected {expected_len} = frame_count*12) -> {match}")
            if frames_len == expected_len and frame_count:
                p("Frame hashes:")
                for i in range(frame_count):
                    entry_off = frames_start + i * 12
                    if entry_off + 4 > len(mm):
                        break
                    key = struct.unpack_from("<I", mm, entry_off)[0]
                    p(f"  [{i}] 0x{key:08X}")
        else:
            p(f"No companion frames chunk found immediately after (got chunk_id=0x{next_id:08X} instead) - "
              f"likely a false-positive header candidate.")
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
    ap.add_argument("file", help="Path to the TPK (texture pack) file")
    ap.add_argument("--max-per-type", type=int, default=3, help="Detailed hex dumps to print per chunk type (default 3)")
    ap.add_argument("--summary-limit", type=int, default=100, help="Rows in the condensed chunk-order table (default 100)")
    ap.add_argument("--context-before", type=int, default=32, help="Bytes of context before a candidate's header")
    ap.add_argument("--context-after", type=int, default=32, help="Bytes of context after a candidate's payload")
    ap.add_argument("--offset", type=lambda s: int(s, 0), default=None, help="Skip scanning, dump one specific offset (hex, e.g. 0x1234)")
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
            if chunk_id == 0x33312001:
                print_header_detail(p, mm, offset, length, args.context_before, args.context_after)
            else:
                print_generic_detail(p, mm, offset, length, name, args.context_before, args.context_after)
            mm.close()
            if args.out:
                out_fh.close()
                print(f"Wrote output to {args.out}")
            return

        p(f"# Scanning {args.file} ({len(mm):,} bytes) for texture_pack_anim chunk family\n")

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
        p(f"{'offset':<12} {'chunk':<28} {'length':<8}")
        for offset, chunk_id, length in all_hits[:args.summary_limit]:
            p(f"0x{offset:08X}   {CHUNK_IDS[chunk_id]:<28} {length}")
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
                if chunk_id == 0x33312001:
                    print_header_detail(p, mm, offset, length, args.context_before, args.context_after)
                else:
                    print_generic_detail(p, mm, offset, length, name, args.context_before, args.context_after)

        mm.close()

    if args.out:
        out_fh.close()
        print(f"Wrote output to {args.out}")


if __name__ == "__main__":
    main()
