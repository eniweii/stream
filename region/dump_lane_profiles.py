"""Read-only dump of CARP lane profiles, to settle what lane offsets mean.

Run from the repository root:
    python region/dump_lane_profiles.py L5RA.BUN --segment 2129
    python region/dump_lane_profiles.py L5RA.BUN --profile 317 --profile 441
    python region/dump_lane_profiles.py L5RA.BUN --examples 8   (summary + 8 odd profiles)

Nothing is written and no source record is changed.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nfs_carp_parser import find_carp_offset, load_road_network_from_carp  # noqa: E402
from traffic_corridors import get_oriented_zone  # noqa: E402

FLAG_START_INVERTED = 1 << 10
FLAG_END_INVERTED = 1 << 9
TOL = 0.15  # metres allowed between neighbouring lane edges

# kRoadSegment* flag bits from the MW decomp (WRoadElem.h)
SEGMENT_FLAG_NAMES = ["Decision", "NoTraffic", "RaceRouteForward", "Intersection", "Entrance",
                      "CopsXorTraffic", "OneWay", "Shortcut", "Curved", "EndInverted",
                      "StartInverted", "ChopperStayLow", "CrossesBarrier",
                      "CrossesDriveThroughBarrier", "LaneMap", "InRace"]


def profile_rows(profile):
    rows = []
    for i in range(int(profile.numZones)):
        width = float(profile.lane_width(i))
        offset = float(profile.lane_offset(i))
        rows.append({"zone": i, "type": int(profile.lane_type(i)), "width": width, "offset": offset,
                     "lo": offset - width / 2.0, "hi": offset + width / 2.0})
    return rows


def center_tiles(rows):
    """True if lanes, ordered by offset, touch edge to edge when offset is the lane centre."""
    ordered = sorted(rows, key=lambda r: r["offset"])
    return all(abs(b["lo"] - a["hi"]) <= TOL for a, b in zip(ordered, ordered[1:]))


def print_profile(index, profile):
    rows = profile_rows(profile)
    print(f"profile {index}: numZones={profile.numZones} middleZone={profile.middleZone} "
          f"centre-tiles={center_tiles(rows)}")
    print("  zone type   width  offset      lo      hi")
    for r in rows:
        mark = "  <- middleZone" if r["zone"] == int(profile.middleZone) else ""
        print(f"  {r['zone']:4d} {r['type']:4d} {r['width']:7.2f} {r['offset']:7.2f} "
              f"{r['lo']:7.2f} {r['hi']:7.2f}{mark}")


def classify(profile, rows):
    """One short tag per profile so the odd ones can be listed."""
    offsets = [r["offset"] for r in rows]
    if all(abs(o) < 0.05 for o in offsets):
        return "all-zero"
    has_neg = any(o < -0.05 for o in offsets)
    has_pos = any(o > 0.05 for o in offsets)
    sign = "mixed" if has_neg and has_pos else ("positive" if has_pos else "negative")
    # offset = left edge of the lane, lanes laid out in zone order?
    edge_chain = all(abs((a["offset"] + a["width"]) - b["offset"]) <= TOL for a, b in zip(rows, rows[1:]))
    # offset equal to the cumulative width of the zones before it?
    run, cumulative = 0.0, True
    for r in rows:
        if abs(run - r["offset"]) > TOL:
            cumulative = False
            break
        run += r["width"]
    tags = [sign]
    if center_tiles(rows):
        tags.append("centre-tiles")
    if edge_chain:
        tags.append("left-edge-chain")
    if cumulative:
        tags.append("cumulative-width")
    if int(profile.middleZone) > 0 and sign != "mixed":
        tags.append("middle>0-single-sign")
    return " ".join(tags)


def flag_names(flags):
    return ",".join(n for i, n in enumerate(SEGMENT_FLAG_NAMES) if flags & (1 << i)) or "-"


def _fmt_zone(z):
    return f"t{z['type']} {z['side'][0].upper()} {z['offset']:+6.2f} w{z['width']:.2f}"


def print_pairings(pa, pb, start_inv, end_inv):
    """Show how zones pair across a segment: current (same logical index) and the
    game's node rule (nth traffic lane per direction group, clamped to the last)."""
    na, nb = int(pa.numZones), int(pb.numZones)
    print("  pairing A (current: same logical zone index at both ends)")
    print("    idx  start-end zone                      end zone                            note")
    for zi in range(min(na, nb)):
        a = get_oriented_zone(pa, zi, start_inv)
        b = get_oriented_zone(pb, zi, end_inv)
        notes = []
        if a["side"] != b["side"]:
            notes.append("SIDE CHANGES")
        if (a["offset"] < 0) != (b["offset"] < 0) and max(abs(a["offset"]), abs(b["offset"])) > 0.5:
            notes.append("crosses centre")
        if a["type"] != b["type"]:
            notes.append("type change")
        print(f"    {zi:3d}  {_fmt_zone(a):36s}{_fmt_zone(b):36s}{' '.join(notes)}")
    if na != nb:
        print(f"    (zone counts differ: {na} vs {nb}; the extra zones have no partner)")

    def groups(profile, inverted):
        zones = [get_oriented_zone(profile, i, inverted) for i in range(int(profile.numZones))]
        middle = zones[0]["middle_zone"] if zones else 0
        right = [z for z in zones if z["logical_index"] >= middle and z["type"] == 1]
        left = [z for z in reversed([z for z in zones if z["logical_index"] < middle]) if z["type"] == 1]
        return {"right": right, "left": left}

    ga, gb = groups(pa, start_inv), groups(pb, end_inv)
    print("  pairing B (game node rule: nth traffic lane from the middle outward per direction group)")
    for side in ("right", "left"):
        la, lb = ga[side], gb[side]
        print(f"    {side} group: {len(la)} traffic lane(s) at start, {len(lb)} at end")
        for n in range(max(len(la), len(lb))):
            a = la[min(n, len(la) - 1)] if la else None
            b = lb[min(n, len(lb) - 1)] if lb else None
            print(f"      n={n}  {_fmt_zone(a) if a else '-':36s}{_fmt_zone(b) if b else '-'}")


def summary(network, examples=0):
    total = tiles = 0
    counts, tags = {}, {}
    shown = 0
    for index, profile in enumerate(network.profiles):
        rows = profile_rows(profile)
        if not rows:
            continue
        total += 1
        counts[len(rows)] = counts.get(len(rows), 0) + 1
        tiles += center_tiles(rows)
        tag = classify(profile, rows)
        tags[tag] = tags.get(tag, 0) + 1
        if examples and shown < examples and "centre-tiles" not in tag:
            shown += 1
            print(f"[example, tag: {tag}]")
            print_profile(index, profile)
    print(f"profiles: {total}")
    print(f"  offset = lane centre, lanes tile edge to edge: {tiles} ({100.0 * tiles / max(1, total):.0f}%)")
    print("  zone count histogram: " + ", ".join(f"{k}:{v}" for k, v in sorted(counts.items())))
    print("  profile kinds:")
    for tag, n in sorted(tags.items(), key=lambda kv: -kv[1]):
        print(f"    {n:5d}  {tag}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bun")
    ap.add_argument("--profile", type=int, action="append", default=[])
    ap.add_argument("--segment", type=int, action="append", default=[])
    ap.add_argument("--examples", type=int, default=0, help="also list N profiles that do not tile as lane centres")
    args = ap.parse_args()

    data = open(args.bun, "rb").read()
    network = load_road_network_from_carp(data, find_carp_offset(data))
    if network is None:
        sys.exit("no road network found in this file")
    summary(network, args.examples)

    for seg_i in args.segment:
        seg = network.segments[seg_i]
        flags = int(seg.flags) & 0xffff
        print(f"\nsegment {seg_i}: nodes {seg.nodeStart} -> {seg.nodeEnd}, flags=0x{flags:04x} [{flag_names(flags)}] "
              f"roadID={getattr(seg, 'roadID', '?')}")
        for node_i in (int(seg.nodeStart), int(seg.nodeEnd)):
            node = network.nodes[node_i]
            print(f"node {node_i}: profileIndex={node.profileIndex}")
            print_profile(int(node.profileIndex), network.profiles[int(node.profileIndex)])
        pa = network.profiles[int(network.nodes[int(seg.nodeStart)].profileIndex)]
        pb = network.profiles[int(network.nodes[int(seg.nodeEnd)].profileIndex)]
        print_pairings(pa, pb, bool(flags & FLAG_START_INVERTED), bool(flags & FLAG_END_INVERTED))
    for p in args.profile:
        print()
        print_profile(p, network.profiles[p])


if __name__ == "__main__":
    main()
