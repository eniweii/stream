#!/usr/bin/env python3
"""export_road_network.py - write road_network.json from a region file.

Same output as the "Export road_network.json..." button in viewers/nfs_region_viewer.py
(the button calls build_road_network() from this file).

Plain stream data, no engine schema: the road network of the CARP block (RoadNetworkGroup)
exactly as parsed. Nothing is rotated, offset, transformed or derived.

  nodes     position [x, y, z] as read (y is the height), index, profile (profile index),
            segments (indices of the segments that use the node)
  segments  nodeStart, nodeEnd, length, road, index, flags (unsigned 16 bit) and flagNames,
            startHandleLength, endHandleLength, startHandle, endHandle (raw bytes; the game
            reads them as signed 8 bit)
  roads     scale, length, shortcut, minWidth, speechID
  profiles  numZones, middleZone, lanes: one entry per zone with raw (the packed 32 bit
            value), type, width and offset (decoded, metres)

Lane offsets are signed, as stored. Which side of the road an offset means (zones left of
middleZone are negated) is a rule of the consumer; see region/road_lanes.py.

Usage:
    python region/export_road_network.py L5RA.BUN
    python region/export_road_network.py L5RA.BUN --game carbon -o road_network.json

Output (default): outputs/export_road_network/road_network.json
"""
# stream bootstrap: make the shared library folders importable
import pathlib as _pl, sys as _sys
_sys.path[:0] = [str(_pl.Path(__file__).resolve().parents[1] / _d) for _d in ['common', 'region', 'scenery', 'solids_materials']]

import argparse
import json

from nfs_outputs import out_path
from nfs_carp_parser import segment_flag_names

TOOL_NAME = "export_road_network"
FORMAT_NAME = "nfs-road-network"
FORMAT_VERSION = 1


def build_road_network(network):
    """Return the road network as a plain dict. `network` is world.road_network."""
    nodes = []
    for i, n in enumerate(network.nodes):
        nodes.append({
            "id": i,
            "position": [round(float(c), 4) for c in n.position],
            "index": int(n.index),
            "profile": int(n.profileIndex),
            "segments": [int(s) for s in n.segmentIndex[:int(n.numSegments)]],
        })
    segments = []
    for i, s in enumerate(network.segments):
        flags = int(s.flags) & 0xffff
        segments.append({
            "id": i,
            "nodeStart": int(s.nodeStart),
            "nodeEnd": int(s.nodeEnd),
            "length": int(s.length),
            "road": int(s.roadID),
            "index": int(s.index),
            "flags": flags,
            "flagNames": segment_flag_names(flags),
            "startHandleLength": int(s.startHandleLength) & 0xffff,
            "endHandleLength": int(s.endHandleLength) & 0xffff,
            "startHandle": [int(b) for b in s.startHandle],
            "endHandle": [int(b) for b in s.endHandle],
        })
    roads = []
    for i, r in enumerate(network.roads):
        roads.append({"id": i, "scale": int(r.scale), "length": int(r.length), "shortcut": int(r.shortcut),
                      "minWidth": int(r.minWidth), "speechID": int(r.speechID)})
    profiles = []
    for i, p in enumerate(network.profiles):
        zones = min(int(p.numZones), len(p.lanes))
        profiles.append({
            "id": i,
            "numZones": int(p.numZones),
            "middleZone": int(p.middleZone),
            "lanes": [{"raw": int(p.lanes[z]), "type": int(p.lane_type(z)),
                       "width": round(float(p.lane_width(z)), 4),
                       "offset": round(float(p.lane_offset(z)), 4)} for z in range(zones)],
        })
    return {
        "format": FORMAT_NAME,
        "version": FORMAT_VERSION,
        "counts": {"nodes": len(nodes), "segments": len(segments), "roads": len(roads), "profiles": len(profiles)},
        "nodes": nodes,
        "segments": segments,
        "roads": roads,
        "profiles": profiles,
    }


def write_road_network_json(data, path):
    with open(path, 'w') as f:
        json.dump(data, f, indent=1)


def summary_lines(data):
    c = data["counts"]
    return [f"{c['nodes']} nodes, {c['segments']} segments, {c['roads']} roads, {c['profiles']} profiles"]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("region_file", help="Region .BUN (for example L5RA.BUN)")
    ap.add_argument("--game", default="carbon", help="carbon, mw, prostreet, undercover (default carbon)")
    ap.add_argument("-o", "--out", help="Output path (default outputs/export_road_network/road_network.json)")
    args = ap.parse_args()

    from nfs_region_parser import load_region_file

    world = load_region_file(args.region_file, game=args.game)
    if not world.road_network or not world.road_network.nodes:
        raise SystemExit("No road network found in this region file - nothing to export.")
    data = build_road_network(world.road_network)
    path = args.out or str(out_path(TOOL_NAME, "road_network.json"))
    write_road_network_json(data, path)
    print(f"Wrote {path}")
    for line in summary_lines(data):
        print(line)


if __name__ == "__main__":
    main()
