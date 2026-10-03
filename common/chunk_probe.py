#!/usr/bin/env python3
"""chunk_probe.py - inspect one chunk ID in a real file, to find its record layout.

For every chunk with the given ID it shows offset, payload size, the 16-byte alignment
pad (the game pads with 0x11), and a hex preview. Across all chunks it shows a size
histogram and stride candidates (common divisors of the aligned data sizes). For a stride
(guessed or given with --stride) it writes a per-column table: each 4-byte column's
distinct values, min/max as u32, and whether it looks like a float, a small int, a
constant, or zero.

Usage:
    python common/chunk_probe.py STREAML5RA.BUN 0x0003410D
    python common/chunk_probe.py STREAML5RA.BUN 0x0013401A --size 92
    python common/chunk_probe.py STREAML5RA.BUN 0x00135002 --stride 0x30 --records 6

Options:
    --size N       only chunks with this payload size, as listed by chunk_registry.py
                   (includes the alignment pad). Example: the 44 size-92 chunks
    --stride N     record size to test (default: best guess from the sizes)
    --records N    records to dump in hex per chunk (default 4)
    --max-chunks N chunks to detail (default 5; statistics use all chunks)
    --hex-bytes N  bytes in the preview of each chunk (default 128)

Output: outputs/chunk_probe/<id>_<file stem>.txt and ..._columns.tsv
"""
# stream bootstrap: make the shared library folders importable
import pathlib as _pl, sys as _sys
_sys.path[:0] = [str(_pl.Path(__file__).resolve().parents[1] / _d) for _d in ['common', 'region', 'scenery', 'solids_materials']]

import argparse
import math
import mmap
import pathlib
import struct
from collections import Counter
from functools import reduce

from nfs_outputs import out_path
from nfs_region_common import walk_chunks

TOOL_NAME = "chunk_probe"
PAD_BYTE = 0x11
COMMON_STRIDES = [0x08, 0x0C, 0x10, 0x14, 0x18, 0x1C, 0x20, 0x24, 0x28, 0x2C, 0x30, 0x40,
                  0x44, 0x48, 0x50, 0x58, 0x5C, 0x60, 0x70, 0x80, 0x90, 0xA0]


def align16(n):
    return n if n % 16 == 0 else n + (16 - n % 16)


def hexdump(buf, base):
    lines = []
    for i in range(0, len(buf), 16):
        row = buf[i:i + 16]
        lines.append("0x%08X  %-47s  %s" % (
            base + i, " ".join("%02X" % b for b in row),
            "".join(chr(b) if 32 <= b < 127 else "." for b in row)))
    return "\n".join(lines)


def collect(mm, chunk_id, size_filter):
    found = []
    for offset, raw_id, _hex, length, is_container, payload_start in walk_chunks(mm):
        if int.from_bytes(raw_id, "little") != chunk_id:
            continue
        if size_filter is not None and length != size_filter:
            continue
        data_start = align16(payload_start)
        pad = data_start - payload_start
        pad_ok = pad == 0 or all(b == PAD_BYTE for b in mm[payload_start:data_start])
        found.append({
            "offset": offset, "length": length, "container": is_container,
            "payload_start": payload_start, "data_start": data_start,
            "data_len": max(0, payload_start + length - data_start),
            "pad": pad, "pad_ok": pad_ok,
        })
    return found


def stride_candidates(chunks):
    """Strides that divide every aligned data size (or at least 90 percent of them)."""
    sizes = [c["data_len"] for c in chunks if c["data_len"] > 0]
    if not sizes:
        return [], 0
    g = reduce(math.gcd, sizes)
    cands = []
    for s in COMMON_STRIDES:
        fit = sum(1 for n in sizes if n % s == 0)
        if fit == len(sizes):
            cands.append((s, fit, len(sizes)))
        elif fit >= 0.9 * len(sizes):
            cands.append((s, fit, len(sizes)))
    return cands, g


def classify(values):
    """Describe one 4-byte column from a list of raw u32 values."""
    distinct = len(set(values))
    if distinct == 1:
        v = values[0]
        return "zero" if v == 0 else "constant"
    floats_ok = True
    for v in values:
        f = struct.unpack("<f", struct.pack("<I", v))[0]
        if v != 0 and (not math.isfinite(f) or abs(f) > 1e7 or abs(f) < 1e-9):
            floats_ok = False
            break
    if floats_ok:
        return "float?"
    if max(values) < 0x10000:
        return "small int"
    return "u32/hash"


def column_table(mm, chunks, stride):
    cols = [[] for _ in range(stride // 4)]
    records = 0
    for c in chunks:
        n = c["data_len"] // stride
        for r in range(n):
            base = c["data_start"] + r * stride
            for k in range(stride // 4):
                cols[k].append(struct.unpack_from("<I", mm, base + 4 * k)[0])
            records += 1
    rows = []
    for k, vals in enumerate(cols):
        if not vals:
            continue
        rows.append({
            "offset": "0x%02X" % (4 * k), "distinct": len(set(vals)),
            "min_u32": "0x%08X" % min(vals), "max_u32": "0x%08X" % max(vals),
            "kind": classify(vals),
            "sample": " ".join("0x%08X" % v for v in vals[:4]),
        })
    return rows, records


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file")
    ap.add_argument("chunk_id", type=lambda s: int(s, 0))
    ap.add_argument("--size", type=lambda s: int(s, 0), default=None)
    ap.add_argument("--stride", type=lambda s: int(s, 0), default=None)
    ap.add_argument("--records", type=int, default=4)
    ap.add_argument("--max-chunks", type=int, default=5)
    ap.add_argument("--hex-bytes", type=int, default=128)
    args = ap.parse_args()

    stem = pathlib.Path(args.file).stem
    tag = "%08X_%s" % (args.chunk_id, stem) + ("_size%d" % args.size if args.size else "")
    report_path = out_path(TOOL_NAME, tag + ".txt")
    columns_path = out_path(TOOL_NAME, tag + "_columns.tsv")

    lines = []
    p = lines.append
    with open(args.file, "rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        chunks = collect(mm, args.chunk_id, args.size)
        p("chunk 0x%08X in %s: %d chunk(s)%s" % (
            args.chunk_id, args.file, len(chunks),
            " (payload size %d only)" % args.size if args.size else ""))
        if not chunks:
            print("\n".join(lines))
            print("No chunk with this ID. Run chunk_registry.py to list the IDs in the file.")
            return
        p("container bit set: %s" % ("yes" if chunks[0]["container"] else "no"))
        p("")
        hist = Counter(c["length"] for c in chunks)
        p("Payload size histogram (size: count):")
        for size, cnt in sorted(hist.items(), key=lambda kv: (-kv[1], kv[0]))[:25]:
            p("  %d (0x%X): %d" % (size, size, cnt))
        bad_pad = sum(1 for c in chunks if not c["pad_ok"])
        p("Alignment pad not all 0x%02X in %d chunk(s)" % (PAD_BYTE, bad_pad))
        p("")

        cands, g = stride_candidates(chunks)
        p("gcd of aligned data sizes: %d (0x%X)" % (g, g))
        p("Stride candidates (divides all or 90 percent of sizes): " +
          (", ".join("0x%X (%d/%d)" % c for c in cands) if cands else "none"))
        stride = args.stride
        if stride is None:
            exact = [c for c in cands if c[1] == c[2]]
            pick = exact[-1][0] if exact else (cands[-1][0] if cands else None)
            stride = pick if pick and pick <= 0x100 else None
            if stride:
                p("Stride used for columns (largest exact candidate): 0x%X. Override with --stride." % stride)
        p("")

        for i, c in enumerate(chunks[:args.max_chunks]):
            p("--- chunk %d at 0x%X: payload %d bytes, data starts 0x%X (pad %d%s), aligned data %d bytes" % (
                i, c["offset"], c["length"], c["data_start"], c["pad"],
                "" if c["pad_ok"] else " NOT 0x11", c["data_len"]))
            p(hexdump(mm[c["data_start"]:c["data_start"] + args.hex_bytes], c["data_start"]))
            if stride and c["data_len"] >= stride:
                n = min(args.records, c["data_len"] // stride)
                p("  first %d record(s), stride 0x%X:" % (n, stride))
                for r in range(n):
                    base = c["data_start"] + r * stride
                    p("  [%d] " % r + " ".join("%08X" % struct.unpack_from("<I", mm, base + k)[0]
                                                for k in range(0, stride, 4)))
            p("")

        if stride:
            rows, nrec = column_table(mm, chunks, stride)
            with open(columns_path, "w", encoding="utf-8") as f:
                f.write("offset\tdistinct\tmin_u32\tmax_u32\tkind\tsample\n")
                for r in rows:
                    f.write("\t".join(str(r[k]) for k in ("offset", "distinct", "min_u32",
                                                          "max_u32", "kind", "sample")) + "\n")
            p("Column table (%d records, stride 0x%X) written to %s" % (nrec, stride, columns_path))
            for r in rows:
                p("  +%s  distinct=%-6d %-9s min=%s max=%s" % (
                    r["offset"], r["distinct"], r["kind"], r["min_u32"], r["max_u32"]))
            if any(c["data_len"] % stride for c in chunks):
                p("WARNING: not every chunk's data size is a multiple of 0x%X. "
                  "The stride may be wrong, or records may have a header." % stride)
        else:
            p("No stride chosen. Give one with --stride to get the column table.")

    text = "\n".join(lines)
    report_path.write_text(text + "\n", encoding="utf-8")
    print(text)
    print("\nWrote %s" % report_path)


if __name__ == "__main__":
    main()
