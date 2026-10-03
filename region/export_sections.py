#!/usr/bin/env python3
"""export_sections.py - write sections.json from a region file and a stream file.

Same output as the "Export sections.json..." button in viewers/nfs_region_viewer.py
(the button now calls build_sections() from this file).

sections.json holds the drivable-section boundary polygons plus each drivable section's
related-section list. Only drivable boundaries are point-in-polygon tested at runtime
(NFSMW VisibleSectionManager::FindBoundary / FindClosestBoundary). Non-drivable sections
are pulled in only through a drivable section's related list.
A related ID is kept if it has a boundary OR stream data. Only IDs with neither are dropped.

Usage:
    python region/export_sections.py L5RA.BUN STREAML5RA.BUN
    python region/export_sections.py L5RA.BUN STREAML5RA.BUN --game carbon -o sections.json

Output (default): outputs/export_sections/sections.json
"""
# stream bootstrap: make the shared library folders importable
import pathlib as _pl, sys as _sys
_sys.path[:0] = [str(_pl.Path(__file__).resolve().parents[1] / _d) for _d in ['common', 'region', 'scenery', 'solids_materials']]

import argparse
import json

from nfs_outputs import out_path

TOOL_NAME = "export_sections"


def build_sections(world, stream_scenery):
    """Return (sections, boundaryless_kept).

    world            result of nfs_region_parser.load_region_file()
    stream_scenery   result of nfs_stream_scenery.load_stream_scenery()
    """
    def missing_reasons(i):
        reasons = []
        if i not in world.by_id:
            reasons.append("no boundary")
        if not stream_scenery.has_data_for(i):
            reasons.append("no stream data")
        return reasons

    sections = []
    boundaryless_kept = set()
    for b in world.boundaries:
        if not world.is_drivable(b.ID):
            continue
        rel = world.relations_by_id.get(b.ID)
        visible_ids = getattr(rel, 'visible_related_chunk_ids', rel.relatedChunkIDs if rel else [])
        # Drop only IDs missing BOTH a boundary and stream data (non-existent).
        related = [i for i in visible_ids if len(missing_reasons(i)) < 2]
        boundaryless_kept.update(i for i in related if i not in world.by_id)
        sections.append({
            "id": b.ID,
            "points": [[round(x, 3), round(y, 3)] for x, y in b.points],
            "boundsMin": [round(b.boundsMin[0], 3), round(b.boundsMin[1], 3)],
            "boundsMax": [round(b.boundsMax[0], 3), round(b.boundsMax[1], 3)],
            "related": related,
        })
    return sections, boundaryless_kept


def write_sections_json(sections, path):
    with open(path, 'w') as f:
        json.dump({"sections": sections}, f, indent=2)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("region_file", help="Region .BUN (for example L5RA.BUN)")
    ap.add_argument("stream_file", help="Stream .BUN (for example STREAML5RA.BUN)")
    ap.add_argument("--game", default="carbon", help="carbon, mw, prostreet, undercover (default carbon)")
    ap.add_argument("-o", "--out", help="Output path (default outputs/export_sections/sections.json)")
    args = ap.parse_args()

    from nfs_region_parser import load_region_file
    from nfs_stream_scenery import load_stream_scenery

    world = load_region_file(args.region_file, game=args.game)
    stream = load_stream_scenery(args.stream_file)
    sections, boundaryless_kept = build_sections(world, stream)
    if not sections:
        raise SystemExit("No drivable sections found - nothing to export.")
    path = args.out or str(out_path(TOOL_NAME, "sections.json"))
    write_sections_json(sections, path)
    print(f"Wrote {len(sections)} drivable sections to {path}")
    print(f"{len(boundaryless_kept)} related sections have stream data but no boundary (kept).")


if __name__ == "__main__":
    main()
