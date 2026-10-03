#!/usr/bin/env python3
"""chunk_registry.py - compare the chunk IDs in a real file with chunk_registry.tsv.

Reports, for each chunk ID found in the file: count, size range, registry file label
(stream / region / unlabeled), status, and owning tool. Flags:
  UNREGISTERED   chunk ID is in the file but not in the registry
  LABEL-MISMATCH the registry label differs from --kind (for example a region chunk
                 found in a stream file)
  NOT-FOUND      registry row has no chunk of that ID in this file

Usage:
    python common/chunk_registry.py STREAML5RA.BUN --kind stream
    python common/chunk_registry.py L5RA.BUN --kind region
    python common/chunk_registry.py FILE.BUN --registry my_registry.tsv

Output: outputs/chunk_registry/<file stem>_coverage.tsv (also a printed summary).
"""
# stream bootstrap: make the shared library folders importable
import pathlib as _pl, sys as _sys
_sys.path[:0] = [str(_pl.Path(__file__).resolve().parents[1] / _d) for _d in ['common', 'region', 'scenery', 'solids_materials']]

import argparse
import mmap
import pathlib
from collections import defaultdict

from nfs_outputs import out_path
from nfs_region_common import walk_chunks

TOOL_NAME = "chunk_registry"
DEFAULT_REGISTRY = pathlib.Path(__file__).resolve().parent / "chunk_registry.tsv"


def load_registry(path):
    """Return {chunk_id: row dict}. Lines starting with # are comments."""
    rows = {}
    header = None
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            cols = line.split("\t")
            if header is None:
                header = cols
                continue
            row = dict(zip(header, cols + [""] * (len(header) - len(cols))))
            rows[int(row["chunk_id"], 16)] = row
    return rows


def scan_file(path):
    """Return {chunk_id: [count, total, min, max, first_offset]}."""
    stats = {}
    with open(path, "rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        for offset, raw_id, _hex, length, _is_container, _start in walk_chunks(mm):
            cid = int.from_bytes(raw_id, "little")
            s = stats.get(cid)
            if s is None:
                stats[cid] = [1, length, length, length, offset]
            else:
                s[0] += 1
                s[1] += length
                s[2] = min(s[2], length)
                s[3] = max(s[3], length)
    return stats


def build_rows(stats, registry, kind):
    rows = []
    for cid in sorted(stats):
        count, total, lo, hi, first = stats[cid]
        reg = registry.get(cid)
        flags = []
        if reg is None:
            flags.append("UNREGISTERED")
        elif kind and reg["file"] not in (kind, "unlabeled"):
            flags.append("LABEL-MISMATCH")
        rows.append({
            "chunk_id": "0x%08X" % cid, "count": count, "total_bytes": total,
            "min_size": lo, "max_size": hi, "first_offset": "0x%X" % first,
            "file": reg["file"] if reg else "", "status": reg["status"] if reg else "",
            "tool": reg["tool"] if reg else "", "name": reg["name"] if reg else "",
            "flags": ",".join(flags),
        })
    for cid, reg in sorted(registry.items()):
        if kind and reg["file"] not in (kind, "unlabeled"):
            continue  # a region chunk is not expected in a stream file
        if cid not in stats:
            rows.append({
                "chunk_id": "0x%08X" % cid, "count": 0, "total_bytes": 0, "min_size": "",
                "max_size": "", "first_offset": "", "file": reg["file"], "status": reg["status"],
                "tool": reg["tool"], "name": reg["name"], "flags": "NOT-FOUND",
            })
    return rows


COLS = ["chunk_id", "count", "total_bytes", "min_size", "max_size", "first_offset",
        "file", "status", "tool", "name", "flags"]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", help="Stream or region .BUN file")
    ap.add_argument("--kind", choices=["stream", "region"], help="Which file type this is")
    ap.add_argument("--registry", default=str(DEFAULT_REGISTRY), help="Path to chunk_registry.tsv")
    ap.add_argument("--out", help="Output TSV (default outputs/chunk_registry/<stem>_coverage.tsv)")
    args = ap.parse_args()

    registry = load_registry(args.registry)
    stats = scan_file(args.file)
    rows = build_rows(stats, registry, args.kind)

    out = pathlib.Path(args.out) if args.out else out_path(
        TOOL_NAME, pathlib.Path(args.file).stem + "_coverage.tsv")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\t".join(COLS) + "\n")
        for r in rows:
            f.write("\t".join(str(r[c]) for c in COLS) + "\n")

    by_flag = defaultdict(list)
    for r in rows:
        for fl in (r["flags"].split(",") if r["flags"] else ["ok"]):
            by_flag[fl].append(r)
    print(f"{args.file}: {len(stats)} distinct chunk IDs, registry has {len(registry)} rows")
    for fl in ("UNREGISTERED", "LABEL-MISMATCH", "NOT-FOUND"):
        print(f"\n{fl}: {len(by_flag.get(fl, []))}")
        for r in by_flag.get(fl, [])[:40]:
            print(f"  {r['chunk_id']}  x{r['count']}  sizes {r['min_size']}-{r['max_size']}  {r['name']}")
    status = defaultdict(int)
    for r in rows:
        if r["count"]:
            status[r["status"] or "(none)"] += 1
    print("\nFound chunk IDs by status: " + ", ".join(f"{k}={v}" for k, v in sorted(status.items())))
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
