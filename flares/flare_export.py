#!/usr/bin/env python3
"""
flare_export.py - One pass over the Carbon scenery flares: discover and extract, then enrich.

  1. flare_scenery_scan.run : STREAML5RA.BUN -> outputs/flares/flares.tsv
  2. flare_report.run       : flares.tsv + light_flares_cg.yml -> outputs/flares/flares_full.tsv

Run without arguments to get two file pickers: the stream file (STREAML5RA.BUN) and the dumped
folder holding the vault yml (light_flares_cg.yml is searched for in it). common/hashes_main.txt
names the override groups (bin hashes) and the hashed vault collections (VLT hashes).
Next step: beamng/flares/flare_lights_beamng.py on flares_full.tsv.
"""
# stream bootstrap: make the shared library folders importable
import pathlib as _pl, sys as _sys
_sys.path[:0] = [str(_pl.Path(__file__).resolve().parents[1] / _d) for _d in ['common', 'flares']]

import argparse
from pathlib import Path

import flare_report
import flare_scenery_scan
from nfs_outputs import out_dir

TOOL = "flares"
YML_GLOB = "*light_flares_cg*.y*ml"
DEFAULT_HASHES = Path(__file__).resolve().parents[1] / "common" / "hashes_main.txt"


def _tk():
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as exc:
        raise RuntimeError("Tk is unavailable: pass the stream file and the dump folder as arguments.") from exc
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    return root, filedialog


def pick_stream():
    root, fd = _tk()
    path = fd.askopenfilename(title="Select the Carbon stream file (STREAML5RA.BUN)",
                              filetypes=[("BUN files", "*.BUN *.bun"), ("All files", "*.*")])
    root.destroy()
    return Path(path) if path else None


def pick_dump():
    root, fd = _tk()
    path = fd.askdirectory(title="Select the dumped folder with the vault yml (light_flares_cg.yml)")
    root.destroy()
    return Path(path) if path else None


def pick_yml(start):
    root, fd = _tk()
    path = fd.askopenfilename(title="Select light_flares_cg.yml", initialdir=str(start),
                              filetypes=[("YAML files", "*.yml *.yaml"), ("All files", "*.*")])
    root.destroy()
    return Path(path) if path else None


def find_yml(dump):
    found = sorted(p for p in dump.rglob(YML_GLOB) if p.is_file())
    if len(found) == 1:
        return found[0]
    print(f"{len(found)} vault ymls matching {YML_GLOB} under {dump}; pick one.")
    return pick_yml(found[0].parent if found else dump)


def main():
    ap = argparse.ArgumentParser(description="Scan, extract and enrich the Carbon scenery flares in one pass.")
    ap.add_argument("stream", nargs="?", help="STREAML5RA.BUN (file picker if omitted)")
    ap.add_argument("dump", nargs="?", help="Dumped folder with light_flares_cg.yml (folder picker if omitted)")
    ap.add_argument("--yml", help="Use this vault yml instead of searching the dump folder")
    ap.add_argument("--hashes", default=str(DEFAULT_HASHES), help="Hash list (default common/hashes_main.txt)")
    ap.add_argument("--lod", type=int, default=2, help="solid_keys index (2 = LOD c, as the game does)")
    ap.add_argument("--sections", nargs="+", type=int, help="Only these section numbers")
    ap.add_argument("--include-2600", action="store_true", help="Keep section 2600 (moving objects)")
    ap.add_argument("--lamppost-outer", action="store_true", help="See flare_report.py --lamppost-outer")
    args = ap.parse_args()

    stream = Path(args.stream) if args.stream else pick_stream()
    if not stream:
        return print("No stream file selected.")
    yml = Path(args.yml) if args.yml else None
    if not yml:
        dump = Path(args.dump) if args.dump else pick_dump()
        if not dump:
            return print("No dumped folder selected.")
        yml = find_yml(dump)
    if not yml:
        return print("No vault yml selected.")
    hashes = args.hashes if args.hashes and Path(args.hashes).is_file() else None
    if not hashes:
        print("hashes_main not found: override groups and vault collections stay hashed.")

    out = out_dir(TOOL)
    tsv, enriched = out / "flares.tsv", out / "flares_full.tsv"

    print(f"\n=== Scan: {stream} ===")
    flare_scenery_scan.run(argparse.Namespace(
        file=str(stream), lod=args.lod, sections=args.sections, json=None, tsv=str(tsv), hashes=hashes,
        include_2600=args.include_2600, max_print=0, near=None, radius=30.0))

    print(f"\n=== Enrich with {yml} ===")
    flare_report.run(argparse.Namespace(
        tsv=str(tsv), yml=str(yml), enriched=str(enriched), lamppost_outer=args.lamppost_outer, hashes=hashes))

    print(f"\nDone. Output folder: {out}\n  {tsv.name}\n  {enriched.name}")
    print("Next: python beamng/flares/flare_lights_beamng.py (pick flares_full.tsv)")


if __name__ == "__main__":
    main()
