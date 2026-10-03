#!/usr/bin/env python3
"""
flare_marker_scan.py - Targeted reader for scenery flares in Carbon:
geometry::position_marker records inside solids (STREAML5RA.BUN).

Layout source: hyperlinked assets/geometry.hpp, ASSERT_SIZE(position_marker, 0x50):
  +0x00 key u32   +0x04 iparam i32   +0x08 fparam f32   +0x0C tint (4 bytes)
  +0x10 matrix4x4 (row-major, row 3 = translation; w column = 0,0,0,1)

Flare type rule (hyperlinked culling.cpp, commit_flares):
  fparam <= 0 : iparam == 6 -> lamppost, else catseye_orange + iparam
  fparam >  0 : sun_flare + int(fparam)

The chunk ID is not in hyperlinked's chunk.hpp, so:
  DISCOVER (no --chunk-id): walk the chunk tree, list leaf chunks that are an array of
            0x50-byte records (optional 4/8 byte prefix) whose matrices all have
            w column (0,0,0,1) and finite values.
  SCAN (--chunk-id 0x........): decode every record, show the parent container path
            and the sibling chunk IDs (to locate the solid header that names the object).
"""

import argparse
import json
import math
import mmap
import struct
import sys
from collections import Counter, defaultdict

REC = 0x50
SHIFTS = (0x00, 0x04, 0x08)
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
LAMPPOST, CATSEYE_ORANGE, SUN_FLARE = 13, 14, 21


def walk(mm, start, end, visit, anc=(), stats=None):
    off = start
    while off + 8 <= end:
        cid, size = struct.unpack_from("<II", mm, off)
        body = off + 8
        if body + size > end:
            if stats is not None:
                stats["bad"] += 1
            return
        if cid & 0x80000000:
            if len(anc) < MAX_DEPTH:
                walk(mm, body, body + size, visit, anc + ((off, cid, size),), stats)
        else:
            visit(off, cid, size, anc)
        off = body + size


def rec_ok(mm, p):
    """Cheap signature test for one record starting at absolute offset p."""
    if mm[p + 0x1C: p + 0x20] != b"\x00\x00\x00\x00":
        return False
    if mm[p + 0x2C: p + 0x30] != b"\x00\x00\x00\x00":
        return False
    if mm[p + 0x3C: p + 0x40] != b"\x00\x00\x00\x00":
        return False
    if mm[p + 0x4C: p + 0x50] != b"\x00\x00\x80\x3f":
        return False
    vals = struct.unpack_from("<12f", mm, p + 0x10)  # includes w slots, all finite anyway
    if not all(math.isfinite(v) and abs(v) < 1e7 for v in vals):
        return False
    iparam, fparam = struct.unpack_from("<if", mm, p + 4)
    return -1000 <= iparam <= 1000 and math.isfinite(fparam) and abs(fparam) < 1000


def find_shift(mm, off, size):
    payload = off + 8
    for shift in SHIFTS:
        body = size - shift
        if body < REC or body % REC:
            continue
        n = body // REC
        if all(rec_ok(mm, payload + shift + i * REC) for i in range(n)):
            return shift, n
    return None


def resolve_type(iparam, fparam):
    if fparam <= 0.0:
        t = LAMPPOST if iparam == 6 else CATSEYE_ORANGE + iparam
    else:
        t = SUN_FLARE + int(fparam)
    return t


def type_name(t):
    return FLARE_TYPES[t] if 0 <= t < len(FLARE_TYPES) else f"?{t}"


def decode(mm, off, shift, n):
    base = off + 8 + shift
    out = []
    for i in range(n):
        p = base + i * REC
        key, iparam, fparam = struct.unpack_from("<Iif", mm, p)
        tint = bytes(mm[p + 0x0C: p + 0x10]).hex()
        m = struct.unpack_from("<16f", mm, p + 0x10)
        t = resolve_type(iparam, fparam)
        out.append({
            "index": i, "key": key, "iparam": iparam, "fparam": fparam, "tint": tint,
            "matrix": list(m), "translation": [m[12], m[13], m[14]],
            "type": t, "type_name": type_name(t),
        })
    return out


def children(mm, start, end):
    res, off = [], start
    while off + 8 <= end:
        cid, size = struct.unpack_from("<II", mm, off)
        if off + 8 + size > end:
            break
        res.append((off, cid, size))
        off += 8 + size
    return res


def run_discover(mm, args):
    leaves = Counter()
    fits = defaultdict(list)
    stats = {"bad": 0}

    def visit(off, cid, size, anc):
        leaves[cid] += 1
        if size >= REC and rec_ok_first(mm, off, size):
            r = find_shift(mm, off, size)
            if r:
                fits[cid].append((off, r[0], r[1]))

    def rec_ok_first(mm, off, size):
        for shift in SHIFTS:
            if size - shift >= REC and (size - shift) % REC == 0 and rec_ok(mm, off + 8 + shift):
                return True
        return False

    walk(mm, 0, len(mm), visit, (), stats)
    print(f"Walked chunk tree. Leaf IDs seen: {len(leaves)}. Out-of-bounds chunks skipped: {stats['bad']}\n")

    if not fits:
        print("No leaf chunk is an array of 0x50-byte records with w column (0,0,0,1).")
        print("The marker chunk may use another record size on disk, or an extra header.")
        return

    print("Candidate position_marker chunk IDs:")
    print(f"{'ChunkID':<12} {'Fits':<7} {'Leaves':<8} {'Markers':<9} {'Shift':<8} Example offset / count")
    ranked = sorted(fits.items(), key=lambda kv: -len(kv[1]))
    for cid, lst in ranked:
        total = sum(x[2] for x in lst)
        shifts = ",".join(hex(s) for s in sorted({x[1] for x in lst}))
        off, sh, n = lst[0]
        print(f"0x{cid:08X}   {len(lst):<7} {leaves[cid]:<8} {total:<9} {shifts:<8} 0x{off:08X} / {n}")
    print("\nNext: python flare_marker_scan.py <file> --chunk-id 0x........")


def run_scan(mm, args):
    target = int(args.chunk_id, 16)
    hits, misses = [], []
    stats = {"bad": 0}

    def visit(off, cid, size, anc):
        if cid != target:
            return
        r = find_shift(mm, off, size)
        if not r:
            misses.append((off, size))
            return
        hits.append((off, size, r[0], r[1], anc))

    walk(mm, 0, len(mm), visit, (), stats)
    print(f"Chunk 0x{target:08X}: {len(hits)} chunks decoded as marker arrays, {len(misses)} did not fit.\n")
    for off, size in misses[:5]:
        print(f"  misfit @0x{off:08X} size={size} size/0x50={size / REC:.2f}")

    types, iparams, fparams, total = Counter(), Counter(), Counter(), 0
    decoded = []
    for off, size, shift, n, anc in hits:
        recs = decode(mm, off, shift, n)
        decoded.append((off, shift, anc, recs))
        for r in recs:
            total += 1
            types[r["type_name"]] += 1
            iparams[r["iparam"]] += 1
            fparams[round(r["fparam"], 3)] += 1

    print(f"\nTotal markers: {total}")
    print("Resolved flare types:")
    for k, v in types.most_common():
        print(f"  {k:<26} {v}")
    print("iparam values: " + ", ".join(f"{k}={v}" for k, v in sorted(iparams.items())[:40]))
    print("fparam values: " + ", ".join(f"{k}={v}" for k, v in sorted(fparams.items())[:40]))

    print(f"\n{'Offset':<12} {'Count':<6} {'Shift':<6} Parent container path")
    print("-" * 90)
    for off, shift, anc, recs in decoded[: args.max_chunks]:
        path = " > ".join(f"0x{c:08X}@0x{o:08X}" for o, c, _ in anc[-3:])
        print(f"0x{off:08X}   {len(recs):<6} 0x{shift:02X}   {path}")
    if len(decoded) > args.max_chunks:
        print(f"... {len(decoded) - args.max_chunks} more (use --max-chunks)")

    for off, shift, anc, recs in decoded[: args.max_detail]:
        print(f"\n--- Marker chunk @0x{off:08X} ({len(recs)} markers) ---")
        if anc:
            po, pc, ps = anc[-1]
            print(f"Parent 0x{pc:08X}@0x{po:08X}; sibling chunks:")
            for so, sc, ss in children(mm, po + 8, po + 8 + ps)[:20]:
                tag = " <== markers" if so == off else ""
                print(f"    0x{sc:08X} size={ss:<8} @0x{so:08X}{tag}")
        raw = bytes(mm[off + 8: off + 8 + 0x60])
        print("First bytes:")
        for i in range(0, len(raw), 16):
            print(f"  +0x{i:02X}: {raw[i:i + 16].hex(' ')}")
        print(f"{'#':<3} {'Key':<12} {'iP':<4} {'fP':<8} {'Type':<22} {'Tint':<9} Translation")
        for r in recs[: args.max_records]:
            tr = ",".join(f"{v:.3f}" for v in r["translation"])
            print(f"{r['index']:<3} 0x{r['key']:08X}   {r['iparam']:<4} {r['fparam']:<8.3f} {r['type_name']:<22} {r['tint']:<9} ({tr})")

    if args.json:
        out = [{"offset": off, "shift": sh, "path": [[o, c] for o, c, _ in anc], "markers": recs}
               for off, sh, anc, recs in decoded]
        with open(args.json, "w") as fh:
            json.dump(out, fh, indent=1)
        print(f"\nWrote {args.json}")


def main():
    ap = argparse.ArgumentParser(description="Scan stream file for scenery flare position markers.")
    ap.add_argument("file")
    ap.add_argument("--chunk-id", help="Hex chunk ID of the marker array. Omit to run discovery.")
    ap.add_argument("--max-chunks", type=int, default=30)
    ap.add_argument("--max-detail", type=int, default=3, help="Chunks to print in detail")
    ap.add_argument("--max-records", type=int, default=12)
    ap.add_argument("--json", help="Write all decoded markers to this JSON file")
    args = ap.parse_args()

    with open(args.file, "rb") as f:
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
    try:
        (run_scan if args.chunk_id else run_discover)(mm, args)
    finally:
        mm.close()


if __name__ == "__main__":
    main()
