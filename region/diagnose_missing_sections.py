#!/usr/bin/env python3
"""diagnose_missing_sections.py - which sections never reach sections.json, and why.

Read-only. Writes nothing into the exports. It rebuilds what export_sections.py
exports (every drivable boundary plus every ID in a drivable section's related
list) and compares it with every section the files know about: boundaries,
stream-file data, and IDs named in a relations record.

For every section that is known but not exported, it prints:
    boundary (yes/no), stream data (yes/no), drivable (yes/no/?),
    ID_over and elevationHash from the boundary, and which exported section
    (if any) names it as ID_over.

It also lists every boundary with a non-zero ID_over or elevationHash (the
stacked / overpass candidates) with the export status of both partners, so the
"depth sections" gap can be matched against the real data before anything is
changed in the export.

Usage:
    python region/diagnose_missing_sections.py L5RA.BUN STREAML5RA.BUN
    python region/diagnose_missing_sections.py L5RA.BUN STREAML5RA.BUN --game carbon -o missing.txt
"""
# stream bootstrap: make the shared library folders importable
import pathlib as _pl, sys as _sys
_sys.path[:0] = [str(_pl.Path(__file__).resolve().parents[1] / _d) for _d in ['common', 'region', 'scenery', 'solids_materials']]

import argparse

from export_sections import build_sections


def _stream_ids(stream_scenery):
    ids = set(stream_scenery.sections)
    ids.update(getattr(stream_scenery, '_override_section_numbers', ()))
    return ids


def analyze(world, stream_scenery):
    """Return a dict with the exported ID set, the missing rows and the stacked rows."""
    sections, _ = build_sections(world, stream_scenery)
    exported = {s["id"] for s in sections}
    for s in sections:
        exported.update(s["related"])

    stream_ids = _stream_ids(stream_scenery)
    known = set(world.by_id) | stream_ids
    for r in world.relations:
        known.add(r.ID)
        known.update(getattr(r, 'visible_related_chunk_ids', r.relatedChunkIDs))
    known.discard(0)

    over_of = {}
    for b in world.boundaries:
        if b.ID_over:
            over_of.setdefault(b.ID_over, []).append(b.ID)

    def row(i):
        b = world.by_id.get(i)
        return {
            "id": i,
            "boundary": b is not None,
            "stream": i in stream_ids,
            "drivable": world.is_drivable(i),
            "ID_over": b.ID_over if b else 0,
            "elevationHash": b.elevationHash if b else 0,
            "named_as_over_by": over_of.get(i, []),
            "exported": i in exported,
        }

    missing = [row(i) for i in sorted(known - exported)]
    stacked = [row(b.ID) for b in world.boundaries if b.ID_over or b.elevationHash]
    partners = sorted({b.ID_over for b in world.boundaries if b.ID_over})
    partner_rows = [row(i) for i in partners]
    return {"exported": exported, "known": known, "missing": missing,
            "stacked": stacked, "partners": partner_rows}


def _fmt(r):
    drv = {True: "yes", False: "no", None: "?"}[r["drivable"]]
    return (f"{r['id']:>6}  boundary={'yes' if r['boundary'] else 'no ':3}  "
            f"stream={'yes' if r['stream'] else 'no ':3}  drivable={drv:3}  "
            f"ID_over={r['ID_over']:<6} hash=0x{r['elevationHash'] & 0xFFFFFFFF:08X}  "
            f"exported={'yes' if r['exported'] else 'no '}  "
            f"named_as_over_by={r['named_as_over_by'] or '-'}")


def report_lines(result):
    missing = result["missing"]
    lines = [f"known sections: {len(result['known'])}, exported (drivable + related): "
             f"{len(result['exported'])}, known but not exported: {len(missing)}", ""]
    groups = {}
    for r in missing:
        key = ("boundary" if r["boundary"] else "no boundary",
               "stream data" if r["stream"] else "no stream data")
        groups.setdefault(key, []).append(r)
    lines.append("Missing, by what they have:")
    for key in sorted(groups):
        lines.append(f"  {key[0]} + {key[1]}: {len(groups[key])}")
    lines.append("")
    lines.append("Missing sections:")
    lines.extend("  " + _fmt(r) for r in missing)
    lines.append("")
    lines.append("Boundaries with a non-zero ID_over or elevationHash (stacked candidates):")
    lines.extend("  " + _fmt(r) for r in result["stacked"])
    lines.append("")
    lines.append("ID_over partners (the section a stacked boundary points at):")
    lines.extend("  " + _fmt(r) for r in result["partners"])
    return lines


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("region_file", help="Region .BUN (for example L5RA.BUN)")
    ap.add_argument("stream_file", help="Stream .BUN (for example STREAML5RA.BUN)")
    ap.add_argument("--game", default="carbon", help="carbon, mw, prostreet, undercover (default carbon)")
    ap.add_argument("-o", "--out", help="Also write the report to this text file")
    args = ap.parse_args()

    from nfs_region_parser import load_region_file
    from nfs_stream_scenery import load_stream_scenery

    world = load_region_file(args.region_file, game=args.game)
    stream = load_stream_scenery(args.stream_file)
    lines = report_lines(analyze(world, stream))
    text = "\n".join(lines)
    print(text)
    if args.out:
        with open(args.out, 'w') as f:
            f.write(text + "\n")


if __name__ == "__main__":
    main()
