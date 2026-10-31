#!/usr/bin/env python3
"""export_zones.py - write zones.json from a region file.

Same output as the "Export zones.json..." button in viewers/nfs_region_viewer.py
(the button calls build_zones() from this file).

zones.json is a plain table of every TrackPathZone in the region file (L5RA.BUN),
all 15 types, nothing filtered. The runtime streamer reads it and decides per type
what a zone does (STREAMER_PREDICTION look-ahead, TUNNEL, HIDDEN, TRAFFIC_PATTERN,
NO_COP_SPAWN, ...). Barriers are not exported.

Coordinates are the raw region plane, the same as sections.json, so zones and
sections line up with no extra conversion. "data" is the raw per-type payload
(4 x i32), untouched. For STREAMER_PREDICTION, data[0] and data[1] look like
section IDs. user_data (a runtime pointer), cached_index and visit_info (runtime
state) are not written.

Usage:
    python region/export_zones.py L5RA.BUN
    python region/export_zones.py L5RA.BUN --game carbon -o zones.json

Output (default): outputs/export_zones/zones.json
"""
# stream bootstrap: make the shared library folders importable
import pathlib as _pl, sys as _sys
_sys.path[:0] = [str(_pl.Path(__file__).resolve().parents[1] / _d) for _d in ['common', 'region', 'scenery', 'solids_materials']]

import argparse
import json
from collections import Counter

from nfs_outputs import out_path

TOOL_NAME = "export_zones"


def build_zones(track_paths):
    """Return the list of zone dicts, in file order. "id" is the index in that order.

    track_paths   result of nfs_trackpath.load_track_paths(), world.track_paths
    """
    zones = []
    for i, z in enumerate(track_paths.zones):
        zones.append({
            "id": i,
            "type": z.type,
            "typeName": z.type_name,
            "position": [round(z.position[0], 3), round(z.position[1], 3)],
            "direction": [round(z.direction[0], 5), round(z.direction[1], 5)],
            "elevation": round(z.elevation, 3),
            "boundsMin": [round(z.bbox_min[0], 3), round(z.bbox_min[1], 3)],
            "boundsMax": [round(z.bbox_max[0], 3), round(z.bbox_max[1], 3)],
            "points": [[round(x, 3), round(y, 3)] for x, y in z.points],
            "data": list(z.data),
            "zoneSource": z.zone_source,
        })
    return zones


def count_by_type(zones):
    """Return [(typeName, count), ...] sorted by type number."""
    counts = Counter((z["type"], z["typeName"]) for z in zones)
    return [(name, n) for (_t, name), n in sorted(counts.items())]


def write_zones_json(zones, path):
    with open(path, 'w') as f:
        json.dump({"zones": zones}, f, indent=2)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("region_file", help="Region .BUN (for example L5RA.BUN)")
    ap.add_argument("--game", default="carbon", help="carbon, mw, prostreet, undercover (default carbon)")
    ap.add_argument("-o", "--out", help="Output path (default outputs/export_zones/zones.json)")
    args = ap.parse_args()

    from nfs_region_parser import load_region_file

    world = load_region_file(args.region_file, game=args.game)
    if not world.track_paths or not world.track_paths.zones:
        raise SystemExit("No track path zones found in this region file - nothing to export.")
    zones = build_zones(world.track_paths)
    path = args.out or str(out_path(TOOL_NAME, "zones.json"))
    write_zones_json(zones, path)
    print(f"Wrote {len(zones)} zones to {path}")
    for name, n in count_by_type(zones):
        print(f"  {name}: {n}")


if __name__ == "__main__":
    main()
