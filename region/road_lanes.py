"""road_lanes.py - lane geometry and profile text for a parsed CARP road network.

Plain stream-side helper for the region viewer's lane view. No GUI, no file I/O and no game
engine transform: offsets are lateral metres, positive = right of the segment direction.

Rules used (same as the ones settled for the road network, see tool_gaps.md / the viewer):
  - The lane profile belongs to the road NODE; a segment uses the profiles of both end nodes.
  - A lane stores a signed offset. Zones left of middleZone are negated (the game's
    WRoadProfile::GetRelativeLaneOffset), zones from middleZone on keep their sign.
  - StartInverted / EndInverted mirror the zone order of that end's profile.
  - When both ends have the same structure zones pair by index. Otherwise they pair per
    direction group, nth traffic lane from the middle outward, extra traffic lanes clamp onto
    the last lane of the other end (lanes merge).
"""
from nfs_carp_parser import segment_flag_names

FLAG_START_INVERTED = 1 << 10
FLAG_END_INVERTED = 1 << 9
TRAFFIC_LANE_TYPE = 1
TOL = 0.15   # metres allowed between neighbouring lane edges when testing "tiles as lane centres"

FLAG_ONE_WAY = 1 << 6
FLAG_CURVED = 1 << 8

# Display colours only, one per lane type (the type is a 4 bit field). The meaning of most
# lane type numbers is not settled; only type 1 (traffic) is confirmed.
LANE_TYPE_COLORS = [
    "#9e9e9e", "#31b77a", "#6d8aa0", "#d99a32", "#a879c9", "#e0607e", "#8cbf3f", "#b8b8b8",
    "#c9b037", "#d9703a", "#55a9e7", "#3fc1c9", "#9a6b4a", "#7a6fd0", "#e8e8e8", "#c44bb5",
]


def lane_type_color(lane_type):
    return LANE_TYPE_COLORS[int(lane_type) & 0xf]


def lane_types_used(network):
    """{lane type: number of profile zones with that type}, over all profiles."""
    counts = {}
    for profile in network.profiles:
        for i in range(min(int(profile.numZones), len(profile.lanes))):
            t = int(profile.lane_type(i))
            counts[t] = counts.get(t, 0) + 1
    return counts


def lane_direction(side, one_way):
    """"forward" = start node to end node. Zones right of middleZone run forward, zones on the
    left run backward, a OneWay segment runs everything forward (same rule as the corridor
    code; not checked against the game)."""
    return "forward" if (one_way or side == "right") else "reverse"


def _signed_byte(value):
    value = int(value) & 0xff
    return value - 256 if value >= 128 else value


def segment_curve(network, segment, steps=10):
    """Centre line of a segment as raw (x, y, z) points: [start, end] when the segment is not
    flagged Curved, else `steps` + 1 points on the cubic curve given by its handles (MW decomp:
    handle length = u16 * 500 / 65535, handle direction = signed bytes / 127)."""
    start = tuple(float(c) for c in network.nodes[int(segment.nodeStart)].position)
    end = tuple(float(c) for c in network.nodes[int(segment.nodeEnd)].position)
    if not (int(segment.flags) & FLAG_CURVED):
        return [start, end]
    sh = [_signed_byte(b) for b in segment.startHandle]
    eh = [_signed_byte(b) for b in segment.endHandle]
    sh_len = (int(segment.startHandleLength) & 0xffff) * 500.0 / 65535.0
    eh_len = (int(segment.endHandleLength) & 0xffff) * 500.0 / 65535.0
    c1 = tuple(start[i] + sh[i] * sh_len / 127.0 for i in range(3))
    c2 = tuple(end[i] + eh[i] * eh_len / 127.0 for i in range(3))
    steps = max(2, int(steps))
    points = []
    for k in range(steps + 1):
        t = k / steps
        u = 1.0 - t
        points.append(tuple(u ** 3 * start[i] + 3 * u * u * t * c1[i] + 3 * u * t * t * c2[i] + t ** 3 * end[i]
                            for i in range(3)))
    return points


def oriented_zone(profile, logical_index, inverted):
    """Zone `logical_index` of a profile in segment order (mirrored when `inverted`), or None."""
    if profile is None or not (0 <= logical_index < int(profile.numZones)):
        return None
    n = int(profile.numZones)
    raw_index = n - 1 - logical_index if inverted else logical_index
    middle = n - int(profile.middleZone) if inverted else int(profile.middleZone)
    middle = max(0, min(n, middle))
    raw_offset = float(profile.lane_offset(raw_index))
    is_left = logical_index < middle
    return {
        "logical_index": logical_index,
        "raw_index": raw_index,
        "type": int(profile.lane_type(raw_index)),
        "width": abs(float(profile.lane_width(raw_index))),
        "offset": -raw_offset if is_left else raw_offset,
        "side": "left" if is_left else "right",
        "middle_zone": middle,
    }


def pair_zones(pa, pb, start_inverted, end_inverted):
    """Pair the zones of a segment's two end profiles: [(key, index_at_start, index_at_end)]."""
    na, nb = int(pa.numZones), int(pb.numZones)
    zones_a = [z for z in (oriented_zone(pa, i, start_inverted) for i in range(na)) if z]
    zones_b = [z for z in (oriented_zone(pb, i, end_inverted) for i in range(nb)) if z]

    def is_traffic(zone):
        return zone["type"] == TRAFFIC_LANE_TYPE

    if na == nb and all(a["side"] == b["side"] and is_traffic(a) == is_traffic(b)
                        for a, b in zip(zones_a, zones_b)):
        return [(i, i, i) for i in range(na)]

    def group(zones, side, traffic):
        picked = [z["logical_index"] for z in zones if z["side"] == side and is_traffic(z) == traffic]
        return list(reversed(picked)) if side == "left" else picked   # middle outward

    raw_pairs = []
    for side in ("right", "left"):
        for traffic in (True, False):
            la, lb = group(zones_a, side, traffic), group(zones_b, side, traffic)
            if not la or not lb:
                continue
            count = max(len(la), len(lb)) if traffic else min(len(la), len(lb))
            for k in range(count):
                raw_pairs.append((la[min(k, len(la) - 1)], lb[min(k, len(lb) - 1)]))
    raw_pairs.sort()
    used, next_key, pairs = set(), max(na, nb), []
    for ia, ib in raw_pairs:
        key = ia
        if key in used:
            key, next_key = next_key, next_key + 1
        used.add(key)
        pairs.append((key, ia, ib))
    return pairs


def segment_profiles(network, segment):
    """(profile at start, profile at end, start inverted, end inverted), or None when a node
    or profile reference is out of range."""
    a, b = int(segment.nodeStart), int(segment.nodeEnd)
    if not (0 <= a < len(network.nodes) and 0 <= b < len(network.nodes)):
        return None
    ia, ib = int(network.nodes[a].profileIndex), int(network.nodes[b].profileIndex)
    if not (0 <= ia < len(network.profiles) and 0 <= ib < len(network.profiles)):
        return None
    flags = int(segment.flags) & 0xffff
    return (network.profiles[ia], network.profiles[ib],
            bool(flags & FLAG_START_INVERTED), bool(flags & FLAG_END_INVERTED))


def segment_strips(network, segment):
    """One dict per paired zone: type, side, and offset/width at the start and at the end."""
    found = segment_profiles(network, segment)
    if found is None:
        return []
    pa, pb, inv_a, inv_b = found
    one_way = bool(int(segment.flags) & FLAG_ONE_WAY)
    strips = []
    for key, ia, ib in pair_zones(pa, pb, inv_a, inv_b):
        za, zb = oriented_zone(pa, ia, inv_a), oriented_zone(pb, ib, inv_b)
        if za is None or zb is None:
            continue
        strips.append({"key": key, "type": za["type"], "side": za["side"],
                       "direction": lane_direction(za["side"], one_way),
                       "a_offset": za["offset"], "a_width": za["width"],
                       "b_offset": zb["offset"], "b_width": zb["width"]})
    return strips


# ---------- text for the info panel ----------

def _zone_line(zone, one_way=False):
    line = (f"    [{zone['logical_index']}] {zone['side'][0].upper()} type={zone['type']}  "
            f"width={zone['width']:.2f}  offset={zone['offset']:+.2f}")
    if zone["type"] == TRAFFIC_LANE_TYPE:
        line += "  runs start->end" if lane_direction(zone["side"], one_way) == "forward" else "  runs end->start"
    return line


def describe_segment(network, segment_index):
    seg = network.segments[segment_index]
    flags = int(seg.flags) & 0xffff
    names = segment_flag_names(flags)
    lines = [f"Segment #{segment_index}",
             f"nodes: #{seg.nodeStart} -> #{seg.nodeEnd}",
             f"length={seg.length}  index={seg.index}",
             f"flags: 0x{flags:04x}  [{', '.join(names) if names else '-'}]"]
    if 0 <= seg.roadID < len(network.roads):
        road = network.roads[seg.roadID]
        lines.append(f"road #{seg.roadID}: scale={road.scale}  length={road.length}  "
                     f"shortcut={road.shortcut}  minWidth={road.minWidth}  speechID={road.speechID}")
    else:
        lines.append(f"road #{seg.roadID}: not in this file's road table")
    found = segment_profiles(network, seg)
    if found is None:
        lines.append("")
        lines.append("A node or profile reference of this segment is out of range.")
        return lines
    pa, pb, inv_a, inv_b = found
    one_way = bool(flags & FLAG_ONE_WAY)
    for label, node, profile, inverted in (("start", seg.nodeStart, pa, inv_a), ("end", seg.nodeEnd, pb, inv_b)):
        lines.append("")
        lines.append(f"{label}: node #{node}, profile #{network.nodes[node].profileIndex}"
                     f"{' (mirrored)' if inverted else ''}")
        lines.append(f"  numZones={profile.numZones}  middleZone={profile.middleZone}")
        for i in range(int(profile.numZones)):
            lines.append(_zone_line(oriented_zone(profile, i, inverted), one_way))
    pairs = pair_zones(pa, pb, inv_a, inv_b)
    if int(pa.numZones) != int(pb.numZones) or any(k != ia or ia != ib for k, ia, ib in pairs):
        lines.append("")
        lines.append("zone pairing (start -> end):")
        for key, ia, ib in pairs:
            lines.append(f"    {ia} -> {ib}")
    return lines


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


def classify_profile(profile, rows):
    """One short tag per profile (sign of the offsets and how the lanes tile)."""
    offsets = [r["offset"] for r in rows]
    if all(abs(o) < 0.05 for o in offsets):
        return "all-zero"
    has_neg = any(o < -0.05 for o in offsets)
    has_pos = any(o > 0.05 for o in offsets)
    sign = "mixed" if has_neg and has_pos else ("positive" if has_pos else "negative")
    edge_chain = all(abs((a["offset"] + a["width"]) - b["offset"]) <= TOL for a, b in zip(rows, rows[1:]))
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


def profile_lines(index, profile):
    rows = profile_rows(profile)
    lines = [f"profile #{index}: numZones={profile.numZones} middleZone={profile.middleZone} "
             f"centre-tiles={center_tiles(rows)}",
             "  zone type   width  offset      lo      hi"]
    for r in rows:
        mark = "  <- middleZone" if r["zone"] == int(profile.middleZone) else ""
        lines.append(f"  {r['zone']:4d} {r['type']:4d} {r['width']:7.2f} {r['offset']:7.2f} "
                     f"{r['lo']:7.2f} {r['hi']:7.2f}{mark}")
    return lines


def profile_census_lines(network, examples=8):
    """Summary of all profiles plus up to `examples` that do not tile as lane centres."""
    total = tiles = shown = 0
    counts, tags, example_lines = {}, {}, []
    for index, profile in enumerate(network.profiles):
        rows = profile_rows(profile)
        if not rows:
            continue
        total += 1
        counts[len(rows)] = counts.get(len(rows), 0) + 1
        tiles += center_tiles(rows)
        tag = classify_profile(profile, rows)
        tags[tag] = tags.get(tag, 0) + 1
        if shown < examples and "centre-tiles" not in tag:
            shown += 1
            example_lines.append("")
            example_lines.append(f"[example, tag: {tag}]")
            example_lines.extend(profile_lines(index, profile))
    lines = [f"profiles: {total}",
             f"  offset = lane centre, lanes tile edge to edge: {tiles} ({100.0 * tiles / max(1, total):.0f}%)",
             "  zone count histogram: " + ", ".join(f"{k}:{v}" for k, v in sorted(counts.items())),
             "  profile kinds:"]
    for tag, n in sorted(tags.items(), key=lambda kv: -kv[1]):
        lines.append(f"    {n:5d}  {tag}")
    return lines + example_lines
