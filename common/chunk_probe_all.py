#!/usr/bin/env python3
"""chunk_probe_all.py - probe every chunk ID of a whole .BUN file in one run.

Does what chunk_probe.py does for one ID, for all of them: a few example chunks per ID
(spread over the file, including the smallest and the largest), hex preview, record dump
as words and floats when a stride is found, a per-column table, ASCII strings, and the
nesting (which container holds which chunk). Output stays small: only --examples chunks
per ID are printed, however many exist.

Written files (outputs/chunk_probe_all/):
    <stem>.txt           the report: summary, index, one section per chunk ID
    <stem>_index.tsv     one row per chunk ID: parents, depth, counts, sizes, padding kinds,
                         stride, and the chunk_registry.tsv label (seed for the ledger)
    <stem>_columns.tsv   per-column table for every ID that has a stride
    <stem>/<id>.txt      one file per ID with --split

Usage:
    python common/chunk_probe_all.py STREAML8R_MW2.BUN
    python common/chunk_probe_all.py L8R_MW2.BUN STREAML8R_MW2.BUN --examples 5
    python common/chunk_probe_all.py FILE.BUN --ids 0x37250,0x33312001 --stride 0x37250=0x88
    python common/chunk_probe_all.py FILE.BUN --exclude 0x134901,0x33320002 --split

Options:
    --examples N        example chunks per ID (default 3)
    --records N         records dumped per example when a stride is known (default 3)
    --hex-bytes N       bytes of hex preview per example (default 96)
    --ids LIST          only these chunk IDs (comma separated, may repeat)
    --exclude LIST      skip the detail section of these IDs (they stay in the index)
    --stride ID=N       record size for one ID, overriding the guess (may repeat)
    --column-chunks N   chunks sampled for the column table (default 300)
    --column-records N  record cap for the column table of one ID (default 4000)
    --split             also write one report file per ID
    --registry PATH     ledger to read labels from (default common/chunk_registry.tsv)
    --no-registry       do not read the ledger

How the data start is found: a leaf payload is padded with 0x11 bytes up to a 16-byte file
boundary in most families, but some (texture pack chunks 0x3331xxxx) are not padded and
start with data. The tool only skips a run of 0x11 that ends on a boundary, and reports
the pad kind per ID (pad11 / aligned / unpadded).
"""
# stream bootstrap: make the shared library folders importable
import pathlib as _pl, sys as _sys
_sys.path[:0] = [str(_pl.Path(__file__).resolve().parents[1] / _d) for _d in ['common', 'region', 'scenery', 'solids_materials']]

import argparse
import mmap
import pathlib
import struct
from array import array
from collections import Counter, defaultdict

from chunk_registry import DEFAULT_REGISTRY, load_registry
from nfs_chunk_walk import (MAX_STRIDE, ascii_strings, choose_stride, classify, float_text,
                            hexdump, iter_chunks, split_payload, stride_analysis)
from nfs_outputs import out_dir, out_path

TOOL_NAME = "chunk_probe_all"
FILLER_ID = 0x00000000
CHILD_PATTERN_CAP = 40


class IdStats:
    __slots__ = ("cid", "container", "count", "total", "min_len", "max_len", "min_idx", "max_idx",
                 "offsets", "lengths", "len_hist", "data_hist", "parents", "depths", "pads",
                 "patterns", "filler")

    def __init__(self, cid, container):
        self.cid = cid
        self.container = container
        self.count = 0
        self.total = 0
        self.min_len = None
        self.max_len = None
        self.min_idx = 0
        self.max_idx = 0
        self.offsets = array("Q")
        self.lengths = array("Q")
        self.len_hist = Counter()
        self.data_hist = Counter()
        self.parents = Counter()
        self.depths = set()
        self.pads = Counter()
        self.patterns = Counter()
        self.filler = Counter()


def scan(mm):
    """One pass over the file. Returns (stats by id, problems, top_end, max_depth)."""
    stats = {}
    problems = []
    children = defaultdict(list)       # container offset -> child ids
    container_ids = {}                 # container offset -> id
    filler_mixed = []
    top_end = 0
    max_depth = 0
    for off, cid, length, is_container, pstart, parent, depth, parent_off in iter_chunks(mm, problems=problems):
        s = stats.get(cid)
        if s is None:
            s = stats[cid] = IdStats(cid, is_container)
        idx = s.count
        s.count += 1
        s.total += length
        if s.min_len is None or length < s.min_len:
            s.min_len, s.min_idx = length, idx
        if s.max_len is None or length > s.max_len:
            s.max_len, s.max_idx = length, idx
        s.offsets.append(off)
        s.lengths.append(length)
        s.len_hist[length] += 1
        s.parents[parent] += 1
        s.depths.add(depth)
        max_depth = max(max_depth, depth)
        if depth == 0:
            top_end = max(top_end, pstart + length)
        if parent_off >= 0 and len(children[parent_off]) < CHILD_PATTERN_CAP + 1:
            children[parent_off].append(cid)
        if is_container:
            container_ids[off] = cid
            continue
        data_start, pad, kind = split_payload(mm, pstart, length)
        s.pads[kind] += 1
        s.data_hist[length - pad] += 1
        if cid == FILLER_ID and length:
            body = bytes(mm[pstart:pstart + length])
            if body.count(b"\x11") == length:
                s.filler["all 0x11"] += 1
            elif body.count(b"\x00") == length:
                s.filler["all zero"] += 1
            else:
                s.filler["mixed"] += 1
                if len(filler_mixed) < 5:
                    filler_mixed.append((off, length))
        elif cid == FILLER_ID:
            s.filler["empty"] += 1
    for off, cid in container_ids.items():
        kids = children.get(off, [])
        pat = tuple(kids[:CHILD_PATTERN_CAP]) + (("...",) if len(kids) > CHILD_PATTERN_CAP else ())
        stats[cid].patterns[pat] += 1
    return stats, problems, top_end, max_depth, filler_mixed


def parse_ids(values):
    out = []
    for v in values or []:
        for part in v.split(","):
            part = part.strip()
            if part:
                out.append(int(part, 0))
    return out


def example_indices(s, n):
    """Chunk indices to show: first, smallest, largest, then evenly spaced, last."""
    picks = [0, s.min_idx, s.max_idx]
    if s.count > 1:
        picks.append(s.count // 2)
        picks.append(s.count - 1)
    seen, out = set(), []
    for i in picks:
        if i not in seen:
            seen.add(i)
            out.append(i)
    for k in range(1, s.count):                      # fill with evenly spaced ones if n is large
        if len(out) >= n:
            break
        i = k * s.count // (n + 1)
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out[:n]


def record_lines(mm, data_start, data_len, stride, nrec):
    lines = []
    n = min(nrec, data_len // stride) if stride else 0
    for r in range(n):
        base = data_start + r * stride
        words = [struct.unpack_from("<I", mm, base + k)[0] for k in range(0, stride, 4)]
        lines.append("  record %d (stride 0x%X) at 0x%X" % (r, stride, base))
        for row in range(0, len(words), 8):
            chunk = words[row:row + 8]
            lines.append("    +0x%02X  " % (row * 4) + " ".join("%08X" % w for w in chunk))
            if any(float_text(w) != "-" and w for w in chunk):
                lines.append("          " + " ".join("%8s" % float_text(w) for w in chunk))
    return lines


def column_rows(mm, s, stride, max_chunks, max_records):
    """Per-column table over a sample of this ID's leaf chunks."""
    if s.count == 0:
        return [], 0
    step = max(1, s.count // max_chunks)
    cols = [[] for _ in range(stride // 4)]
    records = 0
    for i in range(0, s.count, step):
        off, length = s.offsets[i], s.lengths[i]
        data_start, pad, _ = split_payload(mm, off + 8, length)
        n = (length - pad) // stride
        for r in range(n):
            if records >= max_records:
                break
            base = data_start + r * stride
            for k in range(stride // 4):
                cols[k].append(struct.unpack_from("<I", mm, base + 4 * k)[0])
            records += 1
        if records >= max_records:
            break
    rows = []
    for k, vals in enumerate(cols):
        if not vals:
            continue
        kind = classify(vals)
        rows.append({
            "offset": "0x%02X" % (4 * k), "distinct": len(set(vals)), "kind": kind,
            "min": min(vals), "max": max(vals),
            "sample": " ".join("%08X" % v for v in vals[:4]),
            "sample_f": " ".join(float_text(v) for v in vals[:4]) if kind == "float?" else "",
        })
    return rows, records


def ledger_text(row):
    if not row:
        return "(not in ledger)"
    return "%s | %s | %s | tool: %s" % (row.get("name", ""), row.get("file", ""), row.get("status", ""),
                                        row.get("tool", "") or "-")


def describe_id(mm, s, row, args, strides):
    """Return (report lines, index dict, column rows)."""
    L = []
    p = L.append
    kind = "container" if s.container else "leaf"
    p("=" * 78)
    p("0x%08X  %s  x%d" % (s.cid, kind, s.count))
    p("ledger: " + ledger_text(row))
    parents = ", ".join("%s x%d" % ("(root)" if k == -1 else "0x%08X" % k, v)
                        for k, v in s.parents.most_common(6))
    p("parents: %s   depth: %s" % (parents, ",".join(str(d) for d in sorted(s.depths))))
    p("payload sizes: min %d, max %d, total %d   first at 0x%X" % (
        s.min_len, s.max_len, s.total, s.offsets[0]))
    hist = ", ".join("%d x%d" % kv for kv in sorted(s.len_hist.items(), key=lambda kv: (-kv[1], kv[0]))[:8])
    p("size histogram: %s%s" % (hist, " ..." if len(s.len_hist) > 8 else ""))

    index = {"stride": "", "stride_how": "", "pads": ""}
    cols = []
    if s.container:
        for pat, n in s.patterns.most_common(3):
            p("children (x%d): %s" % (n, " ".join("0x%08X" % c if c != "..." else c for c in pat) or "(none)"))
        if len(s.patterns) > 3:
            p("(%d distinct child sequences)" % len(s.patterns))
        p("")
        return L, index, cols

    p("padding: " + ", ".join("%s %d" % kv for kv in sorted(s.pads.items())))
    index["pads"] = ",".join("%s=%d" % kv for kv in sorted(s.pads.items()))
    an = stride_analysis(s.data_hist)
    p("data size (payload minus pad): gcd %d, gcd of differences %d%s" % (
        an["gcd"], an["diff_gcd"], ", header %d" % an["header"] if an["header"] is not None else ""))
    stride, how = choose_stride(an)
    if s.cid in strides:
        stride, how = strides[s.cid], "given with --stride"
    if stride:
        p("stride used: 0x%X (%s)" % (stride, how))
        index["stride"] = "0x%X" % stride
        index["stride_how"] = how
        if stride % 4:
            p("  note: stride is not a multiple of 4, record words below are unaligned views")
        hdr = an["header"] or 0
        bad = sum(c for d, c in s.data_hist.items() if (d - hdr) % stride)
        if bad:
            p("  WARNING: %d chunk(s) have a data size that does not fit header %d + n x 0x%X" % (bad, hdr, stride))
    else:
        p("stride: none (%s). Give one with --stride 0x%X=N" % (how, s.cid))

    if s.cid == FILLER_ID:
        p("filler content: " + ", ".join("%s %d" % kv for kv in sorted(s.filler.items())))
        p("")
        return L, index, cols

    for n, i in enumerate(example_indices(s, args.examples)):
        off, length = s.offsets[i], s.lengths[i]
        data_start, pad, pkind = split_payload(mm, off + 8, length)
        data_len = length - pad
        tag = "first" if i == 0 else "smallest" if i == s.min_idx else "largest" if i == s.max_idx else "chunk"
        p("--- example %d: %s (#%d of %d) at 0x%X, payload %d, data at 0x%X (%s, pad %d), data %d bytes" % (
            n, tag, i, s.count, off, length, data_start, pkind, pad, data_len))
        head = bytes(mm[data_start:data_start + args.hex_bytes])
        p(hexdump(head, data_start))
        strs = ascii_strings(bytes(mm[data_start:data_start + 256]))
        if strs:
            p("  strings: " + " | ".join(strs))
        if stride and data_len >= stride:
            L.extend(record_lines(mm, data_start, data_len, stride, args.records))
    if stride:
        cols, nrec = column_rows(mm, s, stride, args.column_chunks, args.column_records)
        p("columns (stride 0x%X, %d records sampled):" % (stride, nrec))
        for r in cols[:48]:
            extra = "  f: " + r["sample_f"] if r["sample_f"] else ""
            p("  +%s  distinct=%-6d %-9s min=0x%08X max=0x%08X%s" % (
                r["offset"], r["distinct"], r["kind"], r["min"], r["max"], extra))
        if len(cols) > 48:
            p("  ... %d more columns in the columns TSV" % (len(cols) - 48))
    p("")
    return L, index, cols


def coverage(stats, registry, file_size, top_end):
    by_status = defaultdict(lambda: [0, 0, 0])      # ids, chunks, bytes
    for s in stats.values():
        row = registry.get(s.cid)
        if s.cid == FILLER_ID:
            status = "filler (id 0)"
        elif row:
            status = row.get("status", "?") or "?"
        else:
            status = "UNREGISTERED"
        nbytes = 8 * s.count + (0 if s.container else s.total)
        c = by_status[status]
        c[0] += 1
        c[1] += s.count
        c[2] += nbytes
    lines = ["Coverage by ledger status (chunk headers + leaf payloads; container payloads are counted through their children):"]
    for status, (ids, chunks, nbytes) in sorted(by_status.items(), key=lambda kv: -kv[1][2]):
        lines.append("  %-14s %4d ids %9d chunks %14d bytes %6.2f%%" % (
            status, ids, chunks, nbytes, 100.0 * nbytes / max(1, file_size)))
    lines.append("  bytes after the last top-level chunk: %d" % max(0, file_size - top_end))
    return lines


def run_file(path, args, registry, strides, only, exclude):
    stem = pathlib.Path(path).stem
    with open(path, "rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        size = len(mm)
        stats, problems, top_end, max_depth, filler_mixed = scan(mm)
        ids = sorted(stats)
        sections = []
        index_rows = []
        column_all = []
        split = {}
        for cid in ids:
            s = stats[cid]
            row = registry.get(cid)
            if (only and cid not in only) or cid in exclude:
                lines, index, cols = ["=" * 78, "0x%08X  (skipped)  x%d" % (cid, s.count), ""], {"stride": "", "stride_how": "", "pads": ""}, []
            else:
                lines, index, cols = describe_id(mm, s, row, args, strides)
            sections.extend(lines)
            split[cid] = lines
            index_rows.append((s, row, index))
            for r in cols:
                column_all.append((cid, index["stride"], r))

        head = ["=== %s (%d bytes) ===" % (path, size),
                "%d chunks, %d distinct IDs, max nesting depth %d" % (
                    sum(s.count for s in stats.values()), len(stats), max_depth)]
        for off, why, depth in problems[:10]:
            head.append("WALK PROBLEM at 0x%X (depth %d): %s" % (off, depth, why))
        if len(problems) > 10:
            head.append("... %d more walk problems" % (len(problems) - 10))
        head.append("")
        head.extend(coverage(stats, registry, size, top_end))
        if filler_mixed:
            head.append("")
            head.append("Filler chunks (id 0) with content that is not pad: first at " +
                        ", ".join("0x%X (%d bytes)" % m for m in filler_mixed))
        head.append("")
        head.append("Index (c = container, l = leaf):")
        head.append("  %-10s %s %8s  %-12s %-21s %-9s  %s" % ("id", "k", "count", "parent", "size min-max", "stride", "ledger"))
        for s, row, index in index_rows:
            top_parent = s.parents.most_common(1)[0][0]
            head.append("  0x%08X %s %8d  %-12s %-21s %-9s  %s" % (
                s.cid, "c" if s.container else "l", s.count,
                "(root)" if top_parent == -1 else "0x%08X" % top_parent,
                "%d-%d" % (s.min_len, s.max_len), index["stride"],
                (row.get("name", "") + " [" + row.get("status", "") + "]") if row else "-"))
        head.append("")
        report = head + sections

    report_path = out_path(TOOL_NAME, stem + ".txt")
    report_path.write_text("\n".join(report) + "\n", encoding="utf-8")

    index_path = out_path(TOOL_NAME, stem + "_index.tsv")
    with open(index_path, "w", encoding="utf-8") as f:
        f.write("chunk_id\tkind\tparents\tdepth_min\tdepth_max\tcount\tsize_min\tsize_max\tsize_total\t"
                "first_offset\tpadding\tstride\tstride_how\tledger_name\tledger_file\tledger_status\tledger_tool\n")
        for s, row, index in index_rows:
            parents = ";".join("%s:%d" % ("root" if k == -1 else "0x%08X" % k, v) for k, v in s.parents.most_common())
            row = row or {}
            f.write("\t".join(str(x) for x in (
                "0x%08X" % s.cid, "container" if s.container else "leaf", parents,
                min(s.depths), max(s.depths), s.count, s.min_len, s.max_len, s.total, "0x%X" % s.offsets[0],
                index["pads"], index["stride"], index["stride_how"],
                row.get("name", ""), row.get("file", ""), row.get("status", ""), row.get("tool", ""))) + "\n")

    columns_path = out_path(TOOL_NAME, stem + "_columns.tsv")
    with open(columns_path, "w", encoding="utf-8") as f:
        f.write("chunk_id\tstride\toffset\tdistinct\tkind\tmin_u32\tmax_u32\tsample\tsample_float\n")
        for cid, stride, r in column_all:
            f.write("\t".join(str(x) for x in (
                "0x%08X" % cid, stride, r["offset"], r["distinct"], r["kind"],
                "0x%08X" % r["min"], "0x%08X" % r["max"], r["sample"], r["sample_f"])) + "\n")

    written = [report_path, index_path, columns_path]
    if args.split:
        d = out_dir(TOOL_NAME) / stem
        d.mkdir(parents=True, exist_ok=True)
        for cid, lines in split.items():
            (d / ("%08X.txt" % cid)).write_text("\n".join(lines) + "\n", encoding="utf-8")
        written.append(d)
    return head, written


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+")
    ap.add_argument("--examples", type=int, default=3)
    ap.add_argument("--records", type=int, default=3)
    ap.add_argument("--hex-bytes", type=int, default=96)
    ap.add_argument("--ids", action="append")
    ap.add_argument("--exclude", action="append")
    ap.add_argument("--stride", action="append", default=[])
    ap.add_argument("--column-chunks", type=int, default=300)
    ap.add_argument("--column-records", type=int, default=4000)
    ap.add_argument("--split", action="store_true")
    ap.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    ap.add_argument("--no-registry", action="store_true")
    args = ap.parse_args()

    registry = {} if args.no_registry else load_registry(args.registry)
    strides = {}
    for item in args.stride:
        key, _, val = item.partition("=")
        strides[int(key, 0)] = int(val, 0)
        if strides[int(key, 0)] <= 0 or strides[int(key, 0)] > MAX_STRIDE * 8:
            ap.error("--stride %s: size out of range" % item)
    only = set(parse_ids(args.ids))
    exclude = set(parse_ids(args.exclude))

    for path in args.files:
        head, written = run_file(path, args, registry, strides, only, exclude)
        print("\n".join(head))
        print("")
        for w in written:
            print("Wrote %s" % w)
        print("")


if __name__ == "__main__":
    main()
