#!/usr/bin/env python3
"""
flare_scan.py - Targeted reader for Carbon flare packs (flare::pack + flare::instance)
in the stream file (STREAML5RA.BUN).

Layout source: hyperlinked assets/flares.hpp (ASSERT_SIZE pack 0x60, instance 0x30).
The chunk ID is NOT in hyperlinked's chunk.hpp, so this tool has two modes:

  1) DISCOVER (no --chunk-id): walk the chunk tree, test every leaf chunk against the
     pack shape (header + N x 0x30, count field matches), list the IDs that fit.
  2) SCAN (--chunk-id 0x........): decode every pack with that ID and print the records.

Pack header offsets (relative to payload start, plus SHIFT for a possible prefix):
  +0x00 next ptr   +0x04 prev ptr   (runtime, meaningless on disk)
  +0x08 version u32   +0x0C key u32   +0x10 name[0x20]
  +0x30 bbox_min f32[3]+pad   +0x40 bbox_max f32[3]+pad
  +0x50 light_flare_count u16   +0x52 endian_swapped u8   +0x54 section_number u16
  +0x58 flare_list head/tail ptrs (runtime)          header total 0x60
Instance (0x30):
  +0x00 next  +0x04 prev  +0x08 key u32  +0x0C tint (4 bytes)  +0x10 position f32[3]
  +0x1C reflect_pos_z f32  +0x20 direction f32[3]  +0x2C type u8  +0x2D flags u8
  +0x2E section_number u16
"""

import argparse
import json
import math
import mmap
import struct
import sys
from collections import Counter, defaultdict

PACK_HDR = 0x60
INST = 0x30
SHIFTS = (0x00, 0x04, 0x08)  # possible 0x11111111-style prefix, same trick as rtnode
MAX_DEPTH = 32

FLARE_TYPES = [
    "car_headlight", "car_brakelight", "car_traffic_brakelight", "car_reverse_light",
    "car_fog_light", "car_cop_light_red", "car_cop_light_blue", "car_cop_light_white",
    "car_cop_headlight_right", "car_cop_headlight_left", "car_cop_light_bright_red",
    "car_cop_light_bright_blue", "car_cop_light_orange", "lamppost", "catseye_orange",
    "catseye_red", "catseye_blue", "blinking_amber", "blinking_red", "blinking_green",
    "hand_flare", "sun_flare", "generic_1", "generic_2", "generic_3", "generic_4",
    "generic_5", "generic_6", "generic_7", "generic_8", "generic_9", "generic_10",
]
FLAG_NAMES = {0x1: "bi_directional", 0x2: "n_directional", 0x4: "uni_directional"}


# ---------------------------------------------------------------- chunk walker
def walk(mm, start, end, visit, depth=0, stats=None):
    """Walk chunks in [start, end). Containers (high bit set) are entered."""
    off = start
    while off + 8 <= end:
        cid, size = struct.unpack_from("<II", mm, off)
        body = off + 8
        if body + size > end:
            if stats is not None:
                stats["bad"] += 1
            return
        if cid & 0x80000000:
            if depth < MAX_DEPTH:
                walk(mm, body, body + size, visit, depth + 1, stats)
        else:
            visit(off, cid, size)
        off = body + size


# ---------------------------------------------------------------- pack decoding
def find_shift(mm, off, size):
    """Return (shift, count) if the leaf chunk fits header + count*0x30, else None."""
    payload = off + 8
    for shift in SHIFTS:
        h = PACK_HDR + shift
        if size < h + INST:
            continue
        count = struct.unpack_from("<H", mm, payload + 0x50 + shift)[0]
        if count and h + count * INST == size:
            return shift, count
    return None


def finite(v):
    return all(math.isfinite(x) and abs(x) < 1e7 for x in v)


def decode_pack(mm, off, size, shift, count):
    p = off + 8 + shift
    name_raw = bytes(mm[p + 0x10: p + 0x30])
    name = name_raw.split(b"\0")[0].decode("ascii", "replace")
    pack = {
        "offset": off,
        "shift": shift,
        "version": struct.unpack_from("<I", mm, p + 0x08)[0],
        "key": struct.unpack_from("<I", mm, p + 0x0C)[0],
        "name": name,
        "bbox_min": struct.unpack_from("<3f", mm, p + 0x30),
        "bbox_max": struct.unpack_from("<3f", mm, p + 0x40),
        "count": struct.unpack_from("<H", mm, p + 0x50)[0],
        "endian_swapped": mm[p + 0x52],
        "section_number": struct.unpack_from("<H", mm, p + 0x54)[0],
        "flares": [],
    }
    base = off + 8 + PACK_HDR + shift
    for i in range(count):
        q = base + i * INST
        pos = struct.unpack_from("<3f", mm, q + 0x10)
        dr = struct.unpack_from("<3f", mm, q + 0x20)
        ftype = mm[q + 0x2C]
        flags = mm[q + 0x2D]
        pack["flares"].append({
            "index": i,
            "next": struct.unpack_from("<I", mm, q)[0],
            "prev": struct.unpack_from("<I", mm, q + 4)[0],
            "key": struct.unpack_from("<I", mm, q + 0x08)[0],
            "tint": bytes(mm[q + 0x0C: q + 0x10]).hex(),
            "position": pos,
            "reflect_pos_z": struct.unpack_from("<f", mm, q + 0x1C)[0],
            "direction": dr,
            "type": ftype,
            "flags": flags,
            "section_number": struct.unpack_from("<H", mm, q + 0x2E)[0],
            "valid": ftype < len(FLARE_TYPES) and flags in FLAG_NAMES and finite(pos) and finite(dr),
        })
    return pack


def type_name(t):
    return FLARE_TYPES[t] if t < len(FLARE_TYPES) else f"?{t}"


# ---------------------------------------------------------------- modes
def run_discover(mm, args):
    leaf_count = Counter()
    fits = defaultdict(list)
    sizes = defaultdict(lambda: [1 << 62, 0])
    stats = {"bad": 0}

    def visit(off, cid, size):
        leaf_count[cid] += 1
        s = sizes[cid]
        s[0] = min(s[0], size)
        s[1] = max(s[1], size)
        if size >= PACK_HDR + INST:
            r = find_shift(mm, off, size)
            if r:
                fits[cid].append((off, r[0], r[1], size))

    walk(mm, 0, len(mm), visit, 0, stats)
    print(f"Walked chunk tree. Leaf IDs seen: {len(leaf_count)}. Out-of-bounds chunks skipped: {stats['bad']}\n")

    if args.inventory:
        print(f"{'ChunkID':<12} {'Leaves':<9} {'MinSize':<10} {'MaxSize':<10}")
        for cid, n in sorted(leaf_count.items()):
            print(f"0x{cid:08X}   {n:<9} {sizes[cid][0]:<10} {sizes[cid][1]:<10}")
        print()

    if not fits:
        print("No leaf chunk fits 'pack header + N x 0x30'.")
        print("Run with --inventory and look for IDs whose sizes follow N*0x30 + small header.")
        print("The instances may sit in a separate chunk from the pack header.")
        return

    print("Candidate flare pack chunk IDs (leaf chunks whose size fits header + count x 0x30):")
    print(f"{'ChunkID':<12} {'Fits':<7} {'Leaves':<8} {'Shift':<7} Example offset / count")
    ranked = sorted(fits.items(), key=lambda kv: -len(kv[1]))
    for cid, lst in ranked:
        shifts = sorted({x[1] for x in lst})
        off, sh, cnt, _ = lst[0]
        print(f"0x{cid:08X}   {len(lst):<7} {leaf_count[cid]:<8} {','.join(hex(s) for s in shifts):<7} 0x{off:08X} / {cnt}")
    best = ranked[0][0]
    print(f"\nBest candidate: 0x{best:08X}. Next: python flare_scan.py <file> --chunk-id 0x{best:08X}")


def run_scan(mm, args):
    cid_target = int(args.chunk_id, 16)
    packs = []
    misses = []
    stats = {"bad": 0}

    def visit(off, cid, size):
        if cid != cid_target:
            return
        r = find_shift(mm, off, size)
        if not r:
            misses.append((off, size))
            return
        packs.append(decode_pack(mm, off, size, r[0], r[1]))

    walk(mm, 0, len(mm), visit, 0, stats)

    if args.sections:
        packs = [p for p in packs if p["section_number"] in set(args.sections)]

    print(f"Chunk 0x{cid_target:08X}: {len(packs)} packs decoded, {len(misses)} chunks did not fit the pack shape.\n")
    for off, size in misses[:5]:
        print(f"  misfit @0x{off:08X} size={size} (size-0x60={size - PACK_HDR}, /0x30={(size - PACK_HDR) / INST:.2f})")
    if misses:
        print()

    if not packs:
        return

    total = 0
    invalid = 0
    types = Counter()
    flags_seen = Counter()
    for p in packs:
        for f in p["flares"]:
            total += 1
            types[f["type"]] += 1
            flags_seen[f["flags"]] += 1
            if not f["valid"]:
                invalid += 1

    print(f"{'Offset':<12} {'Sec':<6} {'Key':<12} {'Cnt':<5} {'Ver':<4} {'End':<4} {'Name':<20} BBox min -> max")
    print("-" * 110)
    for p in packs[: args.max_packs]:
        bmin = ",".join(f"{v:.0f}" for v in p["bbox_min"])
        bmax = ",".join(f"{v:.0f}" for v in p["bbox_max"])
        print(f"0x{p['offset']:08X}   {p['section_number']:<6} 0x{p['key']:08X}   {p['count']:<5} {p['version']:<4} "
              f"{p['endian_swapped']:<4} {p['name']:<20} ({bmin}) -> ({bmax})")
    if len(packs) > args.max_packs:
        print(f"... {len(packs) - args.max_packs} more packs (use --max-packs)")

    print(f"\nTotals: {total} flares, {invalid} fail sanity (type<32, flags in 1/2/4, finite pos/dir).")
    print("Type histogram:")
    for t, n in sorted(types.items()):
        print(f"  {t:>3} {type_name(t):<26} {n}")
    print("Flags seen: " + ", ".join(f"0x{k:02X}={v}" for k, v in sorted(flags_seen.items())))

    shown = 0
    for p in packs:
        if shown >= args.max_hex_packs:
            break
        shown += 1
        print(f"\n--- Pack @0x{p['offset']:08X} section {p['section_number']} shift 0x{p['shift']:02X} ---")
        start = p["offset"] + 8
        print("Header hex (first 0x70 bytes):")
        raw = bytes(mm[start: start + 0x70])
        for i in range(0, len(raw), 16):
            print(f"  +0x{i:02X}: {raw[i:i + 16].hex(' ')}")
        print(f"{'#':<4} {'Key':<12} {'Type':<24} {'Fl':<5} {'Tint(hex)':<10} {'Position':<30} {'Direction':<28} ReflZ")
        for f in p["flares"][: args.max_records]:
            pos = ",".join(f"{v:.2f}" for v in f["position"])
            dr = ",".join(f"{v:.2f}" for v in f["direction"])
            print(f"{f['index']:<4} 0x{f['key']:08X}   {type_name(f['type']):<24} 0x{f['flags']:02X}  {f['tint']:<10} "
                  f"({pos}){'':<2} ({dr}){'':<2} {f['reflect_pos_z']:.2f}")
        if len(p["flares"]) > args.max_records:
            print(f"  ... {len(p['flares']) - args.max_records} more")

    if args.json:
        out = []
        for p in packs:
            q = dict(p)
            q["bbox_min"] = list(p["bbox_min"])
            q["bbox_max"] = list(p["bbox_max"])
            q["flares"] = [dict(f, position=list(f["position"]), direction=list(f["direction"]),
                                type_name=type_name(f["type"])) for f in p["flares"]]
            out.append(q)
        with open(args.json, "w") as fh:
            json.dump(out, fh, indent=1)
        print(f"\nWrote {args.json}")


def main():
    ap = argparse.ArgumentParser(description="Scan stream file for Carbon flare packs.")
    ap.add_argument("file", help="Path to stream file (e.g., STREAML5RA.BUN)")
    ap.add_argument("--chunk-id", help="Hex chunk ID of the flare pack, e.g. 0x00135200. Omit to run discovery.")
    ap.add_argument("--inventory", action="store_true", help="Discovery mode: also print every leaf chunk ID with size range")
    ap.add_argument("--sections", nargs="+", type=int, help="Only show packs from these section numbers")
    ap.add_argument("--max-packs", type=int, default=40, help="Max packs in the summary table")
    ap.add_argument("--max-hex-packs", type=int, default=2, help="Packs to print with header hex and records")
    ap.add_argument("--max-records", type=int, default=12, help="Records to print per detailed pack")
    ap.add_argument("--json", help="Write all decoded packs to this JSON file")
    args = ap.parse_args()

    with open(args.file, "rb") as f:
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
    try:
        if args.chunk_id:
            run_scan(mm, args)
        else:
            run_discover(mm, args)
    finally:
        mm.close()


if __name__ == "__main__":
    main()
