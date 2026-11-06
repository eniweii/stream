#!/usr/bin/env python3
"""export_sections.py - write sections.json from a region file and a stream file.

Same output as the "Export sections.json..." button in viewers/nfs_region_viewer.py
(the button now calls build_sections() from this file).

sections.json holds the drivable-section boundary polygons plus each drivable section's
related-section list. Its "overrideNames" table maps an override group's hex key ("0x454C22D2") to its
name from hashes_main, for the groups that can be named; the runtime folds hex and name into one override. Only drivable boundaries are point-in-polygon tested at runtime
(NFSMW VisibleSectionManager::FindBoundary / FindClosestBoundary). Non-drivable sections
are pulled in only through a drivable section's related list.
A related ID is kept if it has a boundary OR stream data. Only IDs with neither are dropped.

Depth (stacked) sections: a boundary can name up to two depth sections (the fields the
region viewer shows as ID_over and unk2) with a depth name each (elevationHash and unk3).
A depth section has no boundary and no stream data of its own: it shares its base section's
polygon and is a drivable section with its own related list. Each section with depth
sections gets "depth": [{"id", "name", "related"}, ...]. The top-level "elevationPolys" list
holds every elevation triangle ({"name", "points": [[x, y, z] x 3]}, names unsigned).
Runtime rule (Hyperlinked get_drivable_section / get_depth_name): inside a boundary that
has depth sections, take the elevation triangle that contains the position in x/y and whose
surface height is the highest one still below the position's z; its name picks the depth
entry with the same name, otherwise the base section is used.

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


def _u32(value):
    return value & 0xFFFFFFFF


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

    def related_of(section_id):
        rel = world.relations_by_id.get(section_id)
        visible_ids = getattr(rel, 'visible_related_chunk_ids', rel.relatedChunkIDs if rel else [])
        # Drop only IDs missing BOTH a boundary and stream data (non-existent).
        return [i for i in visible_ids if len(missing_reasons(i)) < 2]

    sections = []
    boundaryless_kept = set()
    for b in world.boundaries:
        if not world.is_drivable(b.ID):
            continue
        related = related_of(b.ID)
        boundaryless_kept.update(i for i in related if i not in world.by_id)
        entry = {
            "id": b.ID,
            "points": [[round(x, 3), round(y, 3)] for x, y in b.points],
            "boundsMin": [round(b.boundsMin[0], 3), round(b.boundsMin[1], 3)],
            "boundsMax": [round(b.boundsMax[0], 3), round(b.boundsMax[1], 3)],
            "related": related,
        }
        depth = []
        for depth_id, depth_name in ((b.ID_over, b.elevationHash), (b.unk2, b.unk3)):
            if not depth_id:
                continue
            if depth_id not in world.relations_by_id:
                print(f"[build_sections] WARNING: depth section {depth_id} of section {b.ID} "
                      f"has no relations record, its related list is empty")
            depth_related = related_of(depth_id)
            boundaryless_kept.update(i for i in depth_related if i not in world.by_id)
            depth.append({"id": depth_id, "name": _u32(depth_name), "related": depth_related})
        if depth:
            entry["depth"] = depth
        sections.append(entry)
    return sections, boundaryless_kept


def build_elevation_polys(world):
    """Return every elevation triangle, in file order. The runtime checks all of them
    (the highest surface below the position wins), so none are filtered by section."""
    return [{
        "name": _u32(t.hash),
        "points": [[round(x, 3), round(y, 3), round(z, 3)] for x, y, z in t.points],
    } for t in world.elevation]


def build_override_names(stream_scenery):
    """Return {"0xKEY": name} for every override group whose key the hash dictionary can name.

    The runtime keeps objects that carry the hex key and objects that carry the name as one override, so
    nothing is hashed in game. Groups without a known name are left out (they stay hex in the list).
    """
    names = {}
    for g in stream_scenery.groups:
        name = g.name
        if name:
            names[f"0x{g.key & 0xFFFFFFFF:08X}"] = name
    return dict(sorted(names.items()))


def write_sections_json(sections, path, elevation_polys=None, override_names=None):
    data = {"sections": sections}
    if elevation_polys is not None:
        data["elevationPolys"] = elevation_polys
    if override_names:
        data["overrideNames"] = override_names
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)


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
    polys = build_elevation_polys(world)
    override_names = build_override_names(stream)
    write_sections_json(sections, path, polys, override_names)
    print(f"Wrote {len(sections)} drivable sections ({sum(1 for s in sections if 'depth' in s)} with depth "
          f"sections), {len(polys)} elevation triangles and {len(override_names)} override names to {path}")
    print(f"{len(boundaryless_kept)} related sections have stream data but no boundary (kept).")


if __name__ == "__main__":
    main()
