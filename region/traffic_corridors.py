"""Derive editable lane/zone corridors from a parsed CARP RoadNetwork.

This module has no GUI or file I/O side effects. Source parser records remain
read-only; overrides are applied only to derived corridor dictionaries.

NFS physics-space coordinates are (x, y-up, z-forward). The project's default
BeamNG transform is (x=z, y=-x, z=y + vertical_offset), consistent with the
region viewer's plane mapping. Verify placement against the current Carbon
level before a full export; vertical_offset is configurable in the UI.
"""
from __future__ import annotations

import math

FLAG_DECISION = 1 << 0
FLAG_NO_TRAFFIC = 1 << 1
FLAG_ONE_WAY = 1 << 6
FLAG_CURVED = 1 << 8
FLAG_END_INVERTED = 1 << 9
FLAG_START_INVERTED = 1 << 10

# Defaults are deliberately editable: some CARP type meanings still need
# project-specific validation. Unknown types are visible but excluded by default.
DEFAULT_TYPE_RULES = {
    "0":  {"label": "Unknown type 0", "drivability": -1.0, "color": "#777777", "export": False},
    "1":  {"label": "Traffic", "drivability": 1.0, "color": "#31b77a", "export": True},
    "2":  {"label": "Sidewalk", "drivability": -1.0, "color": "#6d747c", "export": False},
    "3":  {"label": "Shoulder", "drivability": 0.25, "color": "#d99a32", "export": True},
    "4":  {"label": "Median", "drivability": 0.10, "color": "#a879c9", "export": True},
    "5":  {"label": "Unknown type 5", "drivability": -1.0, "color": "#777777", "export": False},
    "6":  {"label": "Unknown type 6", "drivability": -1.0, "color": "#777777", "export": False},
    "7":  {"label": "Barrier", "drivability": -1.0, "color": "#6d747c", "export": False},
    "8":  {"label": "Unknown type 8", "drivability": -1.0, "color": "#777777", "export": False},
    "9":  {"label": "Unknown type 9", "drivability": -1.0, "color": "#777777", "export": False},
    "10": {"label": "General road", "drivability": 0.75, "color": "#55a9e7", "export": True},
    "11": {"label": "Unknown type 11", "drivability": -1.0, "color": "#777777", "export": False},
    "12": {"label": "Unknown type 12", "drivability": -1.0, "color": "#777777", "export": False},
    "13": {"label": "Unknown type 13", "drivability": -1.0, "color": "#777777", "export": False},
    "14": {"label": "Unknown type 14", "drivability": -1.0, "color": "#777777", "export": False},
    "15": {"label": "Unknown type 15", "drivability": -1.0, "color": "#777777", "export": False},
}


def merged_type_rules(custom=None):
    rules = {key: dict(value) for key, value in DEFAULT_TYPE_RULES.items()}
    for key, value in (custom or {}).items():
        k = str(key)
        rules.setdefault(k, {"label": f"Type {k}", "drivability": -1.0,
                             "color": "#777777", "export": False})
        rules[k].update(value)
        rules[k]["drivability"] = float(rules[k].get("drivability", -1.0))
        rules[k]["export"] = bool(rules[k].get("export", False))
    return rules


def signed_byte(value):
    value = int(value) & 0xff
    return value - 256 if value >= 128 else value


def segment_flags(segment):
    return int(segment.flags) & 0xffff


def get_profile(network, node_index):
    if not (0 <= node_index < len(network.nodes)):
        return None
    profile_index = int(network.nodes[node_index].profileIndex)
    if not (0 <= profile_index < len(network.profiles)):
        return None
    return network.profiles[profile_index]


def get_oriented_zone(profile, logical_index, inverted):
    """Get zone properties in segment-local index order, accounting for mirroring."""
    if profile is None or not (0 <= logical_index < int(profile.numZones)):
        return None
    n = int(profile.numZones)
    raw_index = n - 1 - logical_index if inverted else logical_index
    middle = n - int(profile.middleZone) if inverted else int(profile.middleZone)
    middle = max(0, min(n, middle))
    width = abs(float(profile.lane_width(raw_index)))
    raw_offset = float(profile.lane_offset(raw_index))
    is_left = logical_index < middle
    # Same rule as the game's WRoadProfile::GetRelativeLaneOffset: the stored offset
    # is signed and only lanes left of the middle zone are negated. Positive-only
    # profiles come out as before; signed profiles (e.g. a one-carriageway profile
    # with middleZone 0) keep their real lateral positions instead of being
    # collapsed onto each other by abs().
    offset = -raw_offset if is_left else raw_offset
    return {
        "logical_index": logical_index,
        "raw_index": raw_index,
        "type": int(profile.lane_type(raw_index)),
        "width": max(0.05, width),
        "offset": offset,
        "side": "left" if is_left else "right",
        "middle_zone": middle,
    }


TRAFFIC_LANE_TYPE = 1  # kLaneTraffic in the game's WRoadNav::ELaneType


def pair_oriented_zones(pa, pb, start_inverted, end_inverted):
    """Pair zones across a segment: returns [(zone_key, index_at_start, index_at_end)].

    Same-structure profiles keep the plain same-index pairing. When the two ends
    differ (zone count, side of middleZone or traffic/non-traffic zones) the zones
    are paired like the game does at nodes: per direction group (side of middleZone),
    nth traffic lane counted from the middle outward. Extra traffic lanes on the
    larger end clamp onto the last lane of the smaller end (lanes merge); a lane
    never pairs with a lane of the other direction, and a group that is empty at
    one end produces nothing. Non-traffic zones pair in the same order without
    clamping. zone_key is unique per segment and equals the start index for the
    normal one-to-one pairs, so saved overrides keep their keys.
    """
    na, nb = int(pa.numZones), int(pb.numZones)
    zones_a = [get_oriented_zone(pa, i, start_inverted) for i in range(na)]
    zones_b = [get_oriented_zone(pb, i, end_inverted) for i in range(nb)]
    zones_a = [z for z in zones_a if z is not None]
    zones_b = [z for z in zones_b if z is not None]

    def is_traffic(zone):
        return zone["type"] == TRAFFIC_LANE_TYPE

    if na == nb and all(a["side"] == b["side"] and is_traffic(a) == is_traffic(b)
                        for a, b in zip(zones_a, zones_b)):
        return [(i, i, i) for i in range(na)]

    def group(zones, side, traffic):
        picked = [z["logical_index"] for z in zones if z["side"] == side and is_traffic(z) == traffic]
        return list(reversed(picked)) if side == "left" else picked  # middle outward

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


def segment_bezier_points(network, segment):
    """Return cubic controls in NFS source coordinates; straight segment handled separately."""
    start = tuple(float(x) for x in network.nodes[segment.nodeStart].position)
    end = tuple(float(x) for x in network.nodes[segment.nodeEnd].position)
    sh = [signed_byte(x) for x in segment.startHandle]
    eh = [signed_byte(x) for x in segment.endHandle]
    # MW's decomp: handle length stored as u16 * 500/(127*65535), then * byte.
    sh_len = (int(segment.startHandleLength) & 0xffff) * 500.0 / 65535.0
    eh_len = (int(segment.endHandleLength) & 0xffff) * 500.0 / 65535.0
    c1 = tuple(start[i] + sh[i] * sh_len / 127.0 for i in range(3))
    c2 = tuple(end[i] + eh[i] * eh_len / 127.0 for i in range(3))
    return start, c1, c2, end


def sample_segment(network, segment, curve_steps=10):
    """Sample actual source centerline; do not use display-chain smoothing for export."""
    start, c1, c2, end = segment_bezier_points(network, segment)
    curved = bool(segment_flags(segment) & FLAG_CURVED)
    count = max(2, int(curve_steps)) if curved else 1
    result = []
    for i in range(count + 1):
        t = i / count
        if not curved:
            p = tuple(start[k] + (end[k] - start[k]) * t for k in range(3))
        else:
            u = 1.0 - t
            p = tuple(u**3 * start[k] + 3*u*u*t*c1[k] + 3*u*t*t*c2[k] + t**3*end[k]
                       for k in range(3))
        result.append(p)
    return result


def beamng_transform(point, vertical_offset=150.0):
    x, y, z = point
    return (z, -x, y + vertical_offset)


def _right_vector(points, index):
    a = points[max(0, index - 1)]
    b = points[min(len(points) - 1, index + 1)]
    # Same horizontal mapping used by viewer: (x,z) -> (z,-x).
    tx, ty = b[2] - a[2], -(b[0] - a[0])
    length = math.hypot(tx, ty)
    if length < 1e-8:
        return (0.0, -1.0)
    tx, ty = tx / length, ty / length
    return (ty, -tx)


def _corridor_node(source_point, right, offset, width, vertical_offset):
    bx, by, bz = beamng_transform(source_point, vertical_offset)
    return [round(bx + right[0] * offset, 4),
            round(by + right[1] * offset, 4), round(bz, 4), round(max(0.05, width), 4)]


def override_key(segment_index, zone_index):
    return f"segment:{segment_index}:zone:{zone_index}"


def expand_pattern_overrides(network, patterns):
    """Apply enabled reusable patterns to source corridor keys in list order.

    A pattern looks like {name, enabled, match:{zone_index,start_type,end_type,
    min_start_width,max_start_width, start_zone_count,end_zone_count}, overrides:{...}}.
    Later matching patterns win; explicit local overrides win over all patterns.
    """
    result = {}
    for seg_i, seg in enumerate(network.segments):
        a, b = int(seg.nodeStart), int(seg.nodeEnd)
        if not (0 <= a < len(network.nodes) and 0 <= b < len(network.nodes)):
            continue
        pa, pb = get_profile(network, a), get_profile(network, b)
        if pa is None or pb is None:
            continue
        flags = segment_flags(seg)
        inv_a, inv_b = bool(flags & FLAG_START_INVERTED), bool(flags & FLAG_END_INVERTED)
        for zi, ia, ib in pair_oriented_zones(pa, pb, inv_a, inv_b):
            za, zb = get_oriented_zone(pa, ia, inv_a), get_oriented_zone(pb, ib, inv_b)
            if za is None or zb is None:
                continue
            for pattern in patterns:
                if not pattern.get("enabled", True):
                    continue
                match = pattern.get("match", {})
                if "zone_index" in match and int(match["zone_index"]) != zi:
                    continue
                if "start_type" in match and int(match["start_type"]) != za["type"]:
                    continue
                if "end_type" in match and int(match["end_type"]) != zb["type"]:
                    continue
                if "start_zone_count" in match and int(match["start_zone_count"]) != int(pa.numZones):
                    continue
                if "end_zone_count" in match and int(match["end_zone_count"]) != int(pb.numZones):
                    continue
                if "min_start_width" in match and za["width"] < float(match["min_start_width"]):
                    continue
                if "max_start_width" in match and za["width"] > float(match["max_start_width"]):
                    continue
                result.setdefault(override_key(seg_i, zi), {}).update(pattern.get("overrides", {}))
    return result


def build_corridors(network, type_rules=None, overrides=None, *, patterns=None, vertical_offset=150.0,
                    curve_steps=10, include_decision_connectors=False):
    """Produce corridor dictionary including editable metadata and BeamNG-ready nodes.

    Overrides are keyed by override_key(segment array index, oriented zone index).
    Supported fields: classification, drivability, width_scale, offset_delta,
    direction, one_way, auto_junction, auto_lanes, lanes_left, lanes_right,
    over_objects, excluded, force_export. Source Decision connectors and
    NoTraffic segments are not exported by default. Decision connectors may be
    globally included for comparison/testing; force_export is a local escape hatch.
    """
    rules = merged_type_rules(type_rules)
    overrides = overrides or {}
    patterns = patterns or []
    pattern_overrides = expand_pattern_overrides(network, patterns)
    corridors, warnings = [], []
    invalid_refs = 0
    zone_mismatches = 0
    type_transitions = 0

    for seg_i, seg in enumerate(network.segments):
        a, b = int(seg.nodeStart), int(seg.nodeEnd)
        if not (0 <= a < len(network.nodes) and 0 <= b < len(network.nodes)):
            invalid_refs += 1
            warnings.append({"kind": "invalid_node_reference", "segment": seg_i,
                             "message": f"Bad endpoint indices {a}, {b}"})
            continue
        pa, pb = get_profile(network, a), get_profile(network, b)
        if pa is None or pb is None:
            invalid_refs += 1
            warnings.append({"kind": "invalid_profile_reference", "segment": seg_i,
                             "message": "Endpoint references a missing/invalid profile"})
            continue
        flags = segment_flags(seg)
        inv_a, inv_b = bool(flags & FLAG_START_INVERTED), bool(flags & FLAG_END_INVERTED)
        # Source zone topology can change at a node; common logical indices are the
        # safe default. The mismatch is reported so the editor can review it.
        zone_pairs = pair_oriented_zones(pa, pb, inv_a, inv_b)
        if pa.numZones != pb.numZones:
            zone_mismatches += 1
            warnings.append({"kind": "profile_zone_count_mismatch", "segment": seg_i,
                             "start_count": pa.numZones, "end_count": pb.numZones,
                             "message": "Zones are paired per direction group (nth traffic lane from the "
                                        "middle outward); extra traffic lanes merge into the last lane."})
        samples = sample_segment(network, seg, curve_steps)
        one_way_source = bool(flags & FLAG_ONE_WAY)

        for zi, ia, ib in zone_pairs:
            za, zb = get_oriented_zone(pa, ia, inv_a), get_oriented_zone(pb, ib, inv_b)
            if za is None or zb is None:
                continue
            rule_a = rules.get(str(za["type"]), rules["0"])
            rule_b = rules.get(str(zb["type"]), rules["0"])
            key = override_key(seg_i, zi)
            ov = dict(pattern_overrides.get(key, {}))
            # Local overrides have higher precedence than matching reusable patterns.
            ov.update(overrides.get(key, {}))
            has_class_override = "classification" in ov
            classification = str(ov.get("classification", rule_a.get("label", f"Type {za['type']}")))
            selected_class_rule = next((r for r in rules.values() if str(r.get("label", "")) == classification), rule_a)
            drivability_default = min(float(rule_a.get("drivability", -1.0)),
                                      float(rule_b.get("drivability", -1.0)))
            drivability = float(ov.get("drivability", drivability_default))
            width_scale = max(0.05, float(ov.get("width_scale", 1.0)))
            offset_delta = float(ov.get("offset_delta", 0.0))
            direction = ov.get("direction", "forward" if (one_way_source or za["side"] == "right") else "reverse")
            if direction not in ("forward", "reverse"):
                direction = "forward"
            if one_way_source and "direction" not in ov:
                direction = "forward"
            type_transition = za["type"] != zb["type"]
            if type_transition:
                type_transitions += 1
                warnings.append({"kind": "zone_type_transition", "segment": seg_i, "zone": zi,
                                 "start_type": za["type"], "end_type": zb["type"],
                                 "message": "Zone classification changes across this segment; using the lower endpoint drivability."})
            if za["side"] != zb["side"]:
                warnings.append({"kind": "zone_side_transition", "segment": seg_i, "zone": zi,
                                 "start_side": za["side"], "end_side": zb["side"],
                                 "message": "The same logical zone changes sides between endpoint profiles; inspect correspondence before export."})
            nodes = []
            for sample_i, point in enumerate(samples):
                t = sample_i / (len(samples) - 1)
                offset = (za["offset"] + (zb["offset"] - za["offset"]) * t) + offset_delta
                width = (za["width"] + (zb["width"] - za["width"]) * t) * width_scale
                right = _right_vector(samples, sample_i)
                nodes.append(_corridor_node(point, right, offset, width, 0.0))  # export applies the vertical offset exactly once
            if direction == "reverse":
                nodes.reverse()
            if len(nodes) < 2 or any(not math.isfinite(v) for node in nodes for v in node):
                warnings.append({"kind": "invalid_geometry", "segment": seg_i, "zone": zi,
                                 "message": "Invalid/degenerate derived corridor"})
                continue
            excluded = bool(ov.get("excluded", False))
            # Don't silently combine endpoint zone types. Preserve both for diagnostics.
            color_rule = selected_class_rule if has_class_override else rule_a
            export_class_ok = (bool(color_rule.get("export", False)) if has_class_override
                               else bool(rule_a.get("export", False) and rule_b.get("export", False)))
            corridor = {
                "id": key.replace(":", "_"), "source_key": key,
                "segment_index": seg_i, "source_segment_id": int(getattr(seg, "index", seg_i)),
                "road_id": int(getattr(seg, "roadID", -1)),
                "node_start": a, "node_end": b,
                "profile_start": int(network.nodes[a].profileIndex),
                "profile_end": int(network.nodes[b].profileIndex),
                "zone_index": zi, "zone_raw_start": int(za["raw_index"]),
                "zone_raw_end": int(zb["raw_index"]),
                "source_type": za["type"],
                "source_type_end": zb["type"], "source_types": [za["type"], zb["type"]] if type_transition else [za["type"]],
                "classification": classification, "color": ov.get("color", color_rule.get("color", "#777777")),
                "side": za["side"], "source_width_start": round(za["width"], 4),
                "source_width_end": round(zb["width"], 4),
                "source_offset_start": round(za["offset"], 4), "source_offset_end": round(zb["offset"], 4),
                "width_start": round(za["width"] * width_scale, 4),
                "width_end": round(zb["width"] * width_scale, 4),
                "offset_start": round(za["offset"] + offset_delta, 4),
                "offset_end": round(zb["offset"] + offset_delta, 4),
                "drivability": drivability, "direction": direction,
                "one_way": bool(ov.get("one_way", True)),
                "auto_junction": bool(ov.get("auto_junction", True)),
                "auto_lanes": bool(ov.get("auto_lanes", False)),
                "lanes_left": max(0, int(ov.get("lanes_left", 0))),
                "lanes_right": max(0, int(ov.get("lanes_right", 1))),
                "over_objects": bool(ov.get("over_objects", False)),
                "excluded": excluded, "force_export": bool(ov.get("force_export", False)),
                "curved": bool(flags & FLAG_CURVED),
                "source_decision": bool(flags & FLAG_DECISION),
                "source_no_traffic": bool(flags & FLAG_NO_TRAFFIC),
                "segment_flags": flags, "nodes": nodes, "override": ov,
                "export": bool(not excluded and drivability > 0.0 and
                               (bool(ov.get("force_export", False)) or
                                (export_class_ok and not (flags & FLAG_NO_TRAFFIC) and
                                 (include_decision_connectors or not (flags & FLAG_DECISION))))),
            }
            corridors.append(corridor)

    return {
        "schema_version": 1,
        "source_format": "NFS CARP/RNgp road network",
        "coordinate_mapping": "NFS (x,y,z) -> BeamNG (z,-x,y+vertical_offset)",
        "vertical_offset": float(vertical_offset),
        "vertical_offset_applied_to_corridor_nodes": False,
        "corridor_node_height": "unoffset BeamNG Z; apply vertical_offset during export",
        "network_counts": {"nodes": len(network.nodes), "segments": len(network.segments),
                           "profiles": len(network.profiles), "roads": len(network.roads)},
        "derived_counts": {"corridors": len(corridors), "exportable_corridors": sum(c["export"] for c in corridors),
                            "source_decision_segments": sum(1 for s in network.segments if segment_flags(s) & FLAG_DECISION),
                            "source_no_traffic_segments": sum(1 for s in network.segments if segment_flags(s) & FLAG_NO_TRAFFIC),
                            "invalid_references": invalid_refs, "profile_zone_count_mismatches": zone_mismatches,
                            "zone_type_transitions": type_transitions},
        "corridors": corridors, "warnings": warnings,
    }




def _corridor_merge_signature(corridor):
    """BeamNG road-level fields that must remain constant over a road chain.

    Classification names and zone indices are not in this signature: they can
    change at profile transitions. Width is node-local in DecalRoad, so differing
    widths must not force a split. Actual source zone identity is checked at each
    shared endpoint by merge_connected_corridors().
    """
    return (
        round(float(corridor.get("drivability", -1.0)), 4),
        bool(corridor.get("one_way", True)),
        bool(corridor.get("auto_junction", True)),
        bool(corridor.get("auto_lanes", False)),
        int(corridor.get("lanes_left", 0)),
        int(corridor.get("lanes_right", 1)),
        bool(corridor.get("over_objects", False)),
    )


def _oriented_source_endpoints(corridor):
    """Return (start node, end node, start raw zone, end raw zone) in road order.

    The zone raw-index fields identify the actual cross-section entry in the
    shared endpoint profile. They remain stable when a segment is stored in the
    reverse direction; logical zone indexes do not.
    """
    a = int(corridor["node_start"])
    b = int(corridor["node_end"])
    za = int(corridor.get("zone_raw_start", corridor.get("zone_index", -1)))
    zb = int(corridor.get("zone_raw_end", corridor.get("zone_index", -1)))
    if corridor.get("direction") == "reverse":
        return b, a, zb, za
    return a, b, za, zb


def _xyz_distance(a, b):
    return math.sqrt(sum((float(a[i]) - float(b[i])) ** 2 for i in range(3)))


def harmonize_zone_endpoints(corridors):
    """Snap all derived zone endpoints sharing (source node, raw profile zone).

    The zone renderer and exporter must use the same geometry. The source
    profile is attached to each CARP node, so this key is stronger than a
    floating-point proximity check. It also aligns road approaches with the
    endpoints of CARP decision-connector zones, allowing DecalRoad autoJunction
    to see a genuinely shared endpoint. Only x/y/z are snapped; each corridor's
    node-local width remains inherited from its own profile zone.
    """
    endpoint_refs = {}
    for c in corridors:
        nodes = c.get("nodes") or []
        if len(nodes) < 2:
            continue
        start_node, end_node, raw_start, raw_end = _oriented_source_endpoints(c)
        endpoint_refs.setdefault((start_node, raw_start), []).append((nodes, 0))
        endpoint_refs.setdefault((end_node, raw_end), []).append((nodes, -1))

    snapped_groups = 0
    largest_gap = 0.0
    for refs in endpoint_refs.values():
        if len(refs) < 2:
            continue
        endpoints = [nodes[idx] for nodes, idx in refs]
        # Track how much source lateral projection varied before snapping.
        for i in range(len(endpoints)):
            for j in range(i + 1, len(endpoints)):
                largest_gap = max(largest_gap, _xyz_distance(endpoints[i], endpoints[j]))
        shared = [sum(float(point[k]) for point in endpoints) / len(endpoints) for k in range(3)]
        for nodes, idx in refs:
            old = nodes[idx]
            nodes[idx] = [round(shared[0], 4), round(shared[1], 4), round(shared[2], 4), old[3]]
        snapped_groups += 1

    # Keep a plain diagnostic on each derived corridor for the sidecar/export UI.
    for c in corridors:
        c["zone_endpoint_harmonized"] = True
        c["max_source_endpoint_gap"] = round(largest_gap, 4)
    return {"snapped_endpoint_groups": snapped_groups, "max_pre_snap_gap": round(largest_gap, 4)}


def merge_connected_corridors(corridors, *, endpoint_tolerance=0.75, merge_decision=False):
    """Create one DecalRoad for each continuous, compatible lane-zone chain.

    Geometry comes directly from the same per-zone sample arrays rendered in
    Zones mode. Through-road pieces are joined topologically by shared source
    node and raw profile-zone identity, even across a roadID boundary, not by a
    fragile endpoint-distance threshold. Connector (Decision) segments are retained as
    their own full curved DecalRoads; their source endpoints are harmonized with
    matching approach-zone endpoints and BeamNG autoJunction connects them.

    Each DecalRoad contains an ordered list of ALL sampled nodes for the whole
    chain. Position and width live in the node array; objects are not emitted
    per node. The parameter endpoint_tolerance is retained for API compatibility
    and diagnostics but is not used to reject topologically valid joins.

    merge_decision=True also lets Decision connectors take part in chains: a connector
    joins the road it leads into (or the one that leads into it) when the link is
    unique (one successor, one predecessor, same road-level signature). Real branches,
    where several connectors leave or arrive at the same lane, stay split.
    """
    del endpoint_tolerance  # topology is authoritative; geometry gaps are snapped separately
    eligible = [c for c in corridors if c.get("export", False) and not c.get("excluded", False)
                and len(c.get("nodes") or []) >= 2]
    if not eligible:
        return []

    oriented = {}
    groupable = set()
    starts_by_key = {}
    for i, c in enumerate(eligible):
        road_id = int(c.get("road_id", -1))
        start_node, end_node, start_zone, end_zone = _oriented_source_endpoints(c)
        oriented[i] = (start_node, end_node, start_zone, end_zone)
        joinable_source = road_id >= 0 and not c.get("source_decision", False)
        if merge_decision and c.get("source_decision", False):
            joinable_source = True
        can_join = (joinable_source
                    and (not c.get("source_no_traffic", False) or c.get("force_export", False)))
        if not can_join:
            continue
        groupable.add(i)
        key = (start_node, start_zone, _corridor_merge_signature(c))
        starts_by_key.setdefault(key, []).append(i)

    proposed_next = {}
    for i in sorted(groupable):
        c = eligible[i]
        road_id = int(c.get("road_id", -1))
        _start_node, end_node, _start_zone, end_zone = oriented[i]
        key = (end_node, end_zone, _corridor_merge_signature(c))
        candidates = [j for j in starts_by_key.get(key, ())
                      if j != i and j in groupable and eligible[j].get("nodes")]
        if not candidates:
            continue
        if len(candidates) == 1:
            # The shared CARP node/profile-zone identity is definitive, even if
            # segment tangents put their independently offset endpoints apart.
            proposed_next[i] = candidates[0]
            continue
        # Only ambiguous source topology uses geometry as a tiebreaker. A close
        # tie is left split for BeamNG's automatic junction handler.
        end_point = c["nodes"][-1]
        ranked = sorted((_xyz_distance(end_point, eligible[j]["nodes"][0]), j) for j in candidates)
        if len(ranked) == 1 or ranked[1][0] - ranked[0][0] >= 0.15:
            proposed_next[i] = ranked[0][1]

    # A source lane chain must be one-to-one. Do not consume a branch twice.
    incoming = {}
    for i, j in proposed_next.items():
        incoming.setdefault(j, []).append(i)
    next_link = {i: j for i, j in proposed_next.items() if len(incoming.get(j, ())) == 1}
    prev_link = {j: i for i, j in next_link.items()}

    chains = []
    visited = set()

    def walk(first):
        chain, current = [], first
        while current not in visited:
            visited.add(current)
            chain.append(current)
            nxt = next_link.get(current)
            if nxt is None or nxt in visited:
                break
            current = nxt
        return chain

    # Start open chains at pieces without a predecessor. Non-groupable connector
    # roads intentionally become single-piece chains while retaining every node.
    for i in range(len(eligible)):
        if i not in visited and (i not in groupable or i not in prev_link):
            chains.append(walk(i))
    for i in range(len(eligible)):
        if i not in visited:
            chains.append(walk(i))

    out = []
    used_ids = set()
    for chain in chains:
        first = eligible[chain[0]]
        merged = dict(first)
        merged_nodes = [list(map(float, node)) for node in first["nodes"]]
        source_keys = [str(eligible[i].get("source_key", eligible[i].get("id", i))) for i in chain]
        source_segments = [int(eligible[i].get("segment_index", -1)) for i in chain]
        source_segment_ids = [int(eligible[i].get("source_segment_id", eligible[i].get("segment_index", -1))) for i in chain]
        source_zones = [int(eligible[i].get("zone_index", -1)) for i in chain]
        source_zone_raw = [
            [int(eligible[i].get("zone_raw_start", eligible[i].get("zone_index", -1))),
             int(eligible[i].get("zone_raw_end", eligible[i].get("zone_index", -1)))]
            for i in chain
        ]
        source_road_ids = [int(eligible[i].get("road_id", -1)) for i in chain]
        for idx in chain[1:]:
            next_nodes = [list(map(float, node)) for node in eligible[idx]["nodes"]]
            if not next_nodes:
                continue
            if merged_nodes:
                a, b = merged_nodes[-1], next_nodes[0]
                # Topological join already established. Harmonize the shared
                # point instead of leaving two per-segment nodes in one spline.
                shared = [round((a[k] + b[k]) * 0.5, 4) for k in range(4)]
                merged_nodes[-1] = shared
                next_nodes[0] = shared.copy()
                next_nodes = next_nodes[1:]
            merged_nodes.extend(next_nodes)

        road_id = int(first.get("road_id", -1))
        if road_id >= 0 and len(chain) > 1:
            merged_id = (f"road_{road_id}_zone_{first.get('zone_index', -1)}_"
                         f"from_{first.get('segment_index', chain[0])}")
        elif len(chain) > 1:
            merged_id = f"{first.get('id', f'zone_{chain[0]}')}_chain{len(chain)}"
        else:
            merged_id = str(first.get("id", f"zone_{chain[0]}"))
        if merged_id in used_ids:      # junction roads inherit the segment/zone of their arm
            n = 2
            while f"{merged_id}_{n}" in used_ids:
                n += 1
            merged_id = f"{merged_id}_{n}"
        used_ids.add(merged_id)
        merged.update({
            "id": merged_id,
            "source_key": source_keys[0],
            "source_keys": source_keys,
            "source_segment_indices": source_segments,
            "source_segment_ids": source_segment_ids,
            "source_road_ids": source_road_ids,
            "source_zone_indices": source_zones,
            "source_zone_raw_endpoints": source_zone_raw,
            "merged_corridor_count": len(chain),
            "nodes": merged_nodes,
            "width_start": round(merged_nodes[0][3], 4) if merged_nodes else first.get("width_start"),
            "width_end": round(merged_nodes[-1][3], 4) if merged_nodes else first.get("width_end"),
            "export": bool(first.get("export", False)),
            "height_offset_applied": False,
        })
        out.append(merged)

    out.sort(key=lambda c: (int(c.get("road_id", -1)), int(c.get("segment_index", -1)),
                            int(c.get("zone_index", -1)), str(c.get("id", ""))))
    return out


COMBINE_LANE_TYPES = (1, 10)   # traffic and general road lanes can share one DecalRoad
COMBINE_MEDIAN_TYPE = 4        # dead median: becomes part of the road width, not a road


def _lane_at(corridor, t):
    """Lateral offset and width of a zone corridor at fraction t (segment order a -> b)."""
    off = corridor["offset_start"] + (corridor["offset_end"] - corridor["offset_start"]) * t
    width = corridor["width_start"] + (corridor["width_end"] - corridor["width_start"]) * t
    return off, width


def _forward_nodes(corridor):
    """Zone nodes in segment order (a -> b); reverse-direction lanes are stored flipped."""
    nodes = corridor["nodes"]
    return nodes[::-1] if corridor.get("direction") == "reverse" else nodes


def combine_carriageways(corridors):
    """Replace the lanes of a segment that flow the same way by ONE DecalRoad.

    Per segment, the traffic lanes (types 1 and 10) become one road. The nodes sit on the
    middle of the whole carriageway and the node width runs from the outer edge of the
    outermost lane to the outer edge of the outermost lane on the other side, so a median
    (zone type 4) between the lanes is a dead gap inside the width and is not exported as a
    road of its own. Only the number of lanes on each side matters to BeamNG:

    * two-way segment: lanes_left / lanes_right = the lanes on each side, one_way False,
      also when the sides are uneven (4 left, 2 right);
    * one-sided segment (flagged one-way, or all lanes on one side): one_way True and every
      lane on one road, lanes_right = n when the traffic flows along the nodes, lanes_left = n
      when it flows against them; the node order is never reversed.

    Sidewalks, shoulders and every other zone stay separate roads exactly as before. A segment
    keeps the one-lane-per-road export when it is a decision connector or NoTraffic, a lane
    has an override, or the lanes differ in node count. A segment with a single lane is
    already one road. Works on the corridors after harmonize_zone_endpoints() and returns
    (corridors_for_export, report). The input corridors are not modified.
    """
    by_segment = {}
    for c in corridors:
        by_segment.setdefault(int(c.get("segment_index", -1)), []).append(c)

    consumed, combined = set(), []
    skipped, kinds = {}, {}

    def skip(reason):
        skipped[reason] = skipped.get(reason, 0) + 1

    for seg_i, group in by_segment.items():
        lanes = [c for c in group if c.get("export", False) and not c.get("excluded", False)
                 and int(c.get("source_type", -1)) in COMBINE_LANE_TYPES
                 and int(c.get("source_type_end", c.get("source_type", -1))) in COMBINE_LANE_TYPES
                 and len(c.get("nodes") or []) >= 2]
        if len(lanes) < 2:
            continue   # one lane is already one road
        flags = int(group[0].get("segment_flags", 0))
        if flags & (FLAG_DECISION | FLAG_NO_TRAFFIC):
            skip("decision or NoTraffic segment")
            continue
        if any(c.get("override") for c in lanes):
            skip("lane has a pattern or local override")
            continue
        if len({len(c["nodes"]) for c in lanes}) != 1:
            skip("lanes differ in node count")
            continue
        left = [c for c in lanes if c.get("side") == "left"]
        right = [c for c in lanes if c.get("side") == "right"]
        # A median between the lanes becomes the dead gap; any other zone there stays its own road.
        lo = min(min(c["offset_start"], c["offset_end"]) for c in lanes)
        hi = max(max(c["offset_start"], c["offset_end"]) for c in lanes)
        lane_ids = {id(c) for c in lanes}
        medians = []
        for c in group:
            if id(c) in lane_ids:
                continue
            mid = (c["offset_start"] + c["offset_end"]) * 0.5
            if lo < mid < hi and int(c.get("source_type", -1)) == COMBINE_MEDIAN_TYPE:
                medians.append(c)

        one_sided = bool(flags & FLAG_ONE_WAY) or not left or not right
        if one_sided:
            # all lanes flow the same way; lanes_left = traffic against the node order
            against = bool(not (flags & FLAG_ONE_WAY) and left and not right)
            lanes_left, lanes_right, one_way = (len(lanes), 0, True) if against else (0, len(lanes), True)
            anchor = min(lanes, key=lambda c: abs(c["offset_start"]))
            kind = "one-way segment" if flags & FLAG_ONE_WAY else "one-sided profile"
        else:
            lanes_left, lanes_right, one_way = len(left), len(right), False
            anchor = min(right, key=lambda c: c["offset_start"])
            kind = "two-way even" if len(left) == len(right) else "two-way uneven"

        forward = [(c, _forward_nodes(c)) for c in lanes]
        count = len(forward[0][1])
        nodes, off_first, off_last, ok = [], 0.0, 0.0, True
        for i in range(count):
            t = i / (count - 1)
            info = [(_lane_at(c, t), ns[i]) for c, ns in forward]
            edge_min = min(off - w * 0.5 for (off, w), _p in info)
            edge_max = max(off + w * 0.5 for (off, w), _p in info)
            (off_a, _wa), pa = min(info, key=lambda it: it[0][0])
            (off_b, _wb), pb = max(info, key=lambda it: it[0][0])
            if off_b - off_a < 1e-6:
                ok = False
                break
            centre = (edge_min + edge_max) * 0.5
            f = (centre - off_a) / (off_b - off_a)
            nodes.append([round(pa[k] + (pb[k] - pa[k]) * f, 4) for k in range(3)]
                         + [round(max(0.05, edge_max - edge_min), 4)])
            if i == 0:
                off_first = centre
            off_last = centre
        if not ok:
            skip("lanes share one lateral position")
            continue

        road = dict(anchor)
        keys = [str(c.get("source_key")) for c in lanes + medians]
        label = ("%d lanes one-way" % len(lanes)) if one_sided else ("%d+%d lanes" % (lanes_left, lanes_right))
        road.update({
            "id": f"carriageway_{seg_i}", "source_key": str(anchor.get("source_key")),
            "classification": f"Carriageway ({label})", "side": "both",
            "direction": "forward", "one_way": one_way, "auto_lanes": False,
            "lanes_left": lanes_left, "lanes_right": lanes_right,
            "drivability": max(float(c.get("drivability", 0.0)) for c in lanes),
            "nodes": nodes, "width_start": nodes[0][3], "width_end": nodes[-1][3],
            "offset_start": round(off_first, 4), "offset_end": round(off_last, 4),
            "combined": True, "combined_kind": kind, "combined_source_keys": keys,
            "combined_lane_count": len(lanes), "combined_median_count": len(medians),
            "override": {}, "export": True,
        })
        combined.append(road)
        kinds[kind] = kinds.get(kind, 0) + 1
        consumed.update(id(c) for c in lanes + medians)

    out = [c for c in corridors if id(c) not in consumed] + combined
    report = {"combined_segments": len(combined), "kinds": kinds,
              "lane_corridors_replaced": sum(r["combined_lane_count"] for r in combined),
              "median_corridors_dropped": sum(r["combined_median_count"] for r in combined),
              "skipped_segments": skipped}
    return out, report


def _dist3(a, b):
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


def thin_nodes(nodes, tolerance=0.15, max_spacing=25.0, width_tolerance=None):
    """Drop nodes that a straight piece between their neighbours reproduces.

    Douglas-Peucker over x/y/z and node width: a node is kept when it is further than
    `tolerance` metres from the straight line between the kept nodes around it (width:
    half the width difference to the interpolated width). The first and last node are
    always kept, so road ends and shared junction points do not move. `max_spacing`
    (metres, 0 = off) keeps long straight pieces from losing every node in between.
    `width_tolerance` (default = tolerance) is the allowed difference between a node's width
    and the width interpolated from its kept neighbours, as half the difference in metres, so
    small width wobbles do not keep every node of a straight road.
    tolerance <= 0 returns the nodes unchanged.
    """
    count = len(nodes)
    if count <= 2 or tolerance <= 0:
        return nodes
    wtol = tolerance if width_tolerance is None else max(float(width_tolerance), 0.0)
    cum = [0.0]
    for i in range(1, count):
        cum.append(cum[-1] + _dist3(nodes[i - 1], nodes[i]))
    keep, stack = {0, count - 1}, [(0, count - 1)]
    while stack:
        i, j = stack.pop()
        if j - i < 2:
            continue
        a, b = nodes[i], nodes[j]
        ab = [b[m] - a[m] for m in range(3)]
        ab2 = sum(v * v for v in ab)
        span = cum[j] - cum[i]
        worst, worst_dev = -1, -1.0
        for k in range(i + 1, j):
            p = nodes[k]
            ap = [p[m] - a[m] for m in range(3)]
            t = sum(ap[m] * ab[m] for m in range(3)) / ab2 if ab2 > 1e-12 else 0.0
            t = max(0.0, min(1.0, t))
            dev = math.sqrt(sum((ap[m] - t * ab[m]) ** 2 for m in range(3)))
            u = (cum[k] - cum[i]) / span if span > 1e-9 else 0.0
            wdev = abs(p[3] - (a[3] + (b[3] - a[3]) * u)) * 0.5
            d = max(dev / tolerance, wdev / wtol if wtol > 0 else (math.inf if wdev > 0 else 0.0))
            if d > worst_dev:
                worst, worst_dev = k, d
        if worst_dev > 1.0:
            split = worst
        elif max_spacing > 0 and span > max_spacing:
            mid = cum[i] + span * 0.5
            split = min(range(i + 1, j), key=lambda k: abs(cum[k] - mid))
        else:
            continue
        keep.add(split)
        stack.append((i, split))
        stack.append((split, j))
    return [nodes[k] for k in sorted(keep)]


def thin_roads(roads, tolerance=0.15, max_spacing=25.0, width_tolerance=None):
    """thin_nodes() for every merged road. Returns (roads, {"nodes_before", "nodes_after"})."""
    before = after = 0
    out = []
    for road in roads:
        nodes = road.get("nodes") or []
        thinned = thin_nodes(nodes, tolerance, max_spacing, width_tolerance)
        before += len(nodes)
        after += len(thinned)
        if thinned is nodes:
            out.append(road)
        else:
            road = dict(road)
            road["nodes"] = thinned
            out.append(road)
    return out, {"nodes_before": before, "nodes_after": after}


def _seg_param(p, a, b):
    """Closest point parameter of p on segment a-b (2D) and the distance to it."""
    abx, aby = b[0] - a[0], b[1] - a[1]
    ab2 = abx * abx + aby * aby
    t = 0.0 if ab2 < 1e-9 else ((p[0] - a[0]) * abx + (p[1] - a[1]) * aby) / ab2
    t = max(0.0, min(1.0, t))
    return t, math.hypot(p[0] - (a[0] + abx * t), p[1] - (a[1] + aby * t))


def _seg_cross(a, b, c, d):
    """Parameters (t on a-b, u on c-d) when the two 2D segments properly cross, else None."""
    rx, ry, sx, sy = b[0] - a[0], b[1] - a[1], d[0] - c[0], d[1] - c[1]
    den = rx * sy - ry * sx
    if abs(den) < 1e-9:
        return None
    t = ((c[0] - a[0]) * sy - (c[1] - a[1]) * sx) / den
    u = ((c[0] - a[0]) * ry - (c[1] - a[1]) * rx) / den
    return (t, u) if 0.02 < t < 0.98 and 0.02 < u < 0.98 else None


def _lerp_node(a, b, t):
    return [round(a[k] + (b[k] - a[k]) * t, 4) for k in range(4)]


RUNOFF_TYPE = 3   # sidewalk / shoulder lanes: their own roads, extended at junctions


def _arm_flow(c, into):
    """True/False when traffic flows into/out of the junction on a one-way arm, None on two-way."""
    left, right = int(c.get("lanes_left", 0)), int(c.get("lanes_right", 1))
    if not c.get("one_way", False) or (left and right):
        return None
    return into if right else (not into)


def resolve_junctions(corridors, *, min_opposite_dot=-0.3, connect_radius=50.0):
    """Replace junction connectors by roads that end on / cross the through road.

    This is how hand-made BeamNG AI maps do it: no turning connectors, the roads of a
    junction simply meet and BeamNG's autoJunction lets the AI choose. Works on the
    output of combine_carriageways(), per junction (cluster of nodes joined by Decision
    connectors, 3+ arms):

    * every arm must be a combined road, otherwise the junction keeps its traffic connectors;
    * opposite arms (direction dot product below `min_opposite_dot`) with the same lane
      configuration become one through road, passing a node on the chord between the two
      arm ends. Two-way arms pair with two-way arms; one-way arms only pair with a one-way
      arm of the opposite flow (entering with leaving), never two entering arms;
    * two through roads that cross share a node at the crossing (4-way);
    * every remaining arm becomes a short stem that keeps its heading and ends on a node
      placed on the nearest through road (T); an entering one-way stem merges onto it, a
      leaving one starts from it. Without any through road the arms end together at the
      middle of their ends;
    * the traffic and median Decision connectors of the junction are dropped.

    Runoffs (zone type 3, sidewalks and shoulders) stay their own roads. In every junction
    their sidewalk/shoulder connectors are dropped and their ends are extended so each has a
    pair: opposite runoff ends join, other ends run on along their heading to a joined pair or
    to the nearest road, and an end that still has no pair joins the nearest runoff end.
    The extension pieces are two-way (lanes on either side).

    connect_radius (metres, 0 = unlimited) limits every connection to the nearest partner of
    the same kind: a through road, stem or runoff piece is only made when its length is within
    the radius. A junction with an arm that cannot be connected within the radius (a big
    interchange) is left unchanged and keeps its original connectors.

    The short junction roads keep the arms' node/zone identity wherever the direction of
    travel allows it, so merge_connected_corridors() chains them into the arm roads.
    Returns (corridors_for_export, report). The input corridors are not modified.
    """
    radius = float(connect_radius) if connect_radius and connect_radius > 0 else float("inf")
    live = [c for c in corridors if c.get("export", False) and not c.get("excluded", False)]
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    decisions = [c for c in live if c.get("source_decision")]
    for c in decisions:
        parent[find(int(c["node_start"]))] = find(int(c["node_end"]))
    touching, runoff_touching = {}, {}
    lanes_in_segment = {}
    for c in live:
        if (not c.get("source_decision") and not c.get("combined")
                and int(c.get("source_type", -1)) in COMBINE_LANE_TYPES):
            seg = int(c.get("segment_index", -1))
            lanes_in_segment[seg] = lanes_in_segment.get(seg, 0) + 1
    for c in live:
        if c.get("source_decision"):
            continue
        kind = int(c.get("source_type", -1))
        for n in {int(c["node_start"]), int(c["node_end"])}:
            if n not in parent:
                continue
            if c.get("combined") or kind in COMBINE_LANE_TYPES:
                touching.setdefault(find(n), []).append((c, n))
            elif kind == RUNOFF_TYPE:
                runoff_touching.setdefault(find(n), []).append((c, n))
    clusters = {}
    for n in list(parent):
        clusters.setdefault(find(n), set()).add(n)

    skipped, consumed_ids, added = {}, set(), []
    stats = {"junctions_resolved": 0, "through_roads": 0, "stems_on_road": 0, "crossings": 0,
             "arms_meeting_at_middle": 0, "runoff_pieces": 0, "runoff_ends_without_pair": 0}
    sentinel = [0]

    def free_node():
        sentinel[0] += 1
        return -sentinel[0]

    def skip(reason):
        skipped[reason] = skipped.get(reason, 0) + 1

    def inside(c, nodes):
        return int(c["node_start"]) in nodes and int(c["node_end"]) in nodes

    for root in sorted(clusters):
        cluster_nodes = clusters[root]
        arms = touching.get(root, [])
        main_roads = []                          # polylines the runoff ends may run onto
        resolved = False
        if len(arms) >= 3:
            if not all(c.get("combined") or lanes_in_segment.get(int(c.get("segment_index", -1)), 0) == 1
                       for c, _n in arms):
                skip("an arm is not a combined road")
            elif any(inside(c, cluster_nodes) for c, _n in arms):
                skip("an arm connects two junction nodes")
            else:
                resolved = True
        if resolved:
            info = []
            for c, n in arms:
                sa, sb, za, zb = _oriented_source_endpoints(c)   # travel order, also for reverse lanes
                into = sb == n                                    # the traffic of this arm ends at the junction
                nodes = [list(map(float, p)) for p in c["nodes"]]
                toward = nodes if into else nodes[::-1]
                end = toward[-1]
                back = next((p for p in reversed(toward[:-1]) if math.hypot(p[0] - end[0], p[1] - end[1]) >= 3.0), toward[0])
                dx, dy = end[0] - back[0], end[1] - back[1]
                length = math.hypot(dx, dy) or 1.0
                one_way = bool(c.get("one_way", False))
                lanes = (int(c.get("lanes_left", 0)), int(c.get("lanes_right", 1)))
                info.append({"c": c, "node": n, "into": into, "toward": toward, "end": end,
                             "raw_end": int(zb), "raw_start": int(za),
                             "dir": (dx / length, dy / length), "flow_in": _arm_flow(c, into),
                             "lanes": lanes, "one_way": one_way,
                             # the arm piece can be chained into when its flow runs along the node order
                             "chain": (not one_way) or lanes[1] > 0})
                main_roads.append(nodes)

            def compatible(a, b):
                if a["flow_in"] is None and b["flow_in"] is None:
                    return a["lanes"] == b["lanes"]
                if a["flow_in"] is None or b["flow_in"] is None:
                    return False
                return a["flow_in"] != b["flow_in"] and sum(a["lanes"]) == sum(b["lanes"])

            pairs, free = [], set(range(len(info)))
            order = sorted((info[i]["dir"][0] * info[j]["dir"][0] + info[i]["dir"][1] * info[j]["dir"][1], i, j)
                           for i in range(len(info)) for j in range(i + 1, len(info)))
            for dot, i, j in order:
                if dot >= min_opposite_dot:
                    break
                gap = math.hypot(info[i]["end"][0] - info[j]["end"][0], info[i]["end"][1] - info[j]["end"][1])
                if gap > radius:
                    continue
                if i in free and j in free and compatible(info[i], info[j]):
                    pairs.append((i, j))
                    free -= {i, j}

            base_id = f"junction_{root}"
            through = []
            for i, j in pairs:
                a, b = info[i]["end"], info[j]["end"]
                through.append({"i": i, "j": j, "a": a, "b": b, "inserts": [],
                                "degenerate": math.hypot(a[0] - b[0], a[1] - b[1]) < 1.0})
            live_through = [t for t in through if not t["degenerate"]]
            crossings_found = 0
            if len(live_through) >= 2:
                hit = _seg_cross(live_through[0]["a"], live_through[0]["b"], live_through[1]["a"], live_through[1]["b"])
                if hit:
                    t0, t1 = hit
                    n0 = _lerp_node(live_through[0]["a"], live_through[0]["b"], t0)
                    n1 = _lerp_node(live_through[1]["a"], live_through[1]["b"], t1)
                    shared = [round((n0[k] + n1[k]) * 0.5, 4) for k in range(4)]
                    live_through[0]["inserts"].append((t0, shared))
                    live_through[1]["inserts"].append((t1, list(shared)))
                    crossings_found = 1
            stems = []
            if through:
                for s in sorted(free):
                    end = info[s]["end"]
                    best = min(((_seg_param(end, t["a"], t["b"])[1], k) for k, t in enumerate(through)))
                    t = through[best[1]]
                    if t["degenerate"]:
                        point = [round(v, 4) for v in t["a"][:3]] + [end[3]]
                    else:
                        d = info[s]["dir"]
                        ahead = _seg_cross(end, [end[0] + d[0] * 120.0, end[1] + d[1] * 120.0], t["a"], t["b"])
                        if ahead:
                            tt = ahead[1]            # the stem keeps its heading up to the through road
                        else:
                            tt, _d = _seg_param(end, t["a"], t["b"])
                        tt = max(0.05, min(0.95, tt))
                        point = _lerp_node(t["a"], t["b"], tt)
                        point[3] = end[3]
                        t["inserts"].append((tt, _lerp_node(t["a"], t["b"], tt)))
                    stems.append((s, point))
            else:
                mx = sum(info[s]["end"][0] for s in free) / len(free)
                my = sum(info[s]["end"][1] for s in free) / len(free)
                mz = sum(info[s]["end"][2] for s in free) / len(free)
                for s in sorted(free):
                    stems.append((s, [round(mx, 4), round(my, 4), round(mz, 4), info[s]["end"][3]]))
            reach = max([math.hypot(info[s]["end"][0] - pt[0], info[s]["end"][1] - pt[1]) for s, pt in stems]
                        + [math.hypot(t["a"][0] - t["b"][0], t["a"][1] - t["b"][1]) for t in through])
            if reach > radius:
                skip("an arm is farther than the connection radius")
                resolved = False
                main_roads = []
        if resolved:
            stats["crossings"] += crossings_found
            if through:
                stats["stems_on_road"] += len(stems)
            else:
                stats["arms_meeting_at_middle"] += len(free)

            def junction_road(source, nodes, start_id, end_id, suffix, lanes, one_way):
                road = dict(source)
                road.update({
                    "id": f"{base_id}_{suffix}", "source_key": f"junction:{root}:{suffix}",
                    "nodes": [[round(v, 4) for v in p] for p in nodes], "direction": "forward",
                    "node_start": start_id[0], "node_end": end_id[0],
                    "zone_raw_start": start_id[1], "zone_raw_end": end_id[1],
                    "zone_index": start_id[1] if start_id[1] >= 0 else end_id[1],
                    "width_start": nodes[0][3], "width_end": nodes[-1][3],
                    "lanes_left": lanes[0], "lanes_right": lanes[1], "one_way": one_way,
                    "combined_junction": True, "override": {}, "export": True,
                })
                return road

            def flow_lanes(a):
                """Lane configuration of a junction road that follows the arm's traffic."""
                return ((0, sum(a["lanes"])), True) if a["one_way"] else (a["lanes"], False)

            new_roads = []
            for k, t in enumerate(through):
                i, j = t["i"], t["j"]
                if t["degenerate"]:
                    continue    # both arms end at the same point; the road chain already passes through it
                mids = [node for _t, node in sorted(t["inserts"], key=lambda it: it[0])]
                a_info, b_info = info[i], info[j]
                if a_info["one_way"]:
                    flip = a_info["flow_in"] is False          # flow must run from the entering arm to the leaving one
                else:
                    chain_ij = int(a_info["into"]) + int(not b_info["into"])
                    chain_ji = int(b_info["into"]) + int(not a_info["into"])
                    flip = chain_ji > chain_ij
                nodes = [a_info["end"]] + mids + [b_info["end"]]
                if flip:
                    a_info, b_info, nodes = b_info, a_info, nodes[::-1]
                c0, c1 = a_info["c"], b_info["c"]
                start_id = ((a_info["node"], a_info["raw_end"])
                            if a_info["into"] and a_info["chain"] else (free_node(), -1))
                end_id = ((b_info["node"], b_info["raw_start"])
                          if not b_info["into"] and b_info["chain"] else (free_node(), -1))
                lanes, one_way = flow_lanes(a_info)
                new_roads.append(junction_road(c0, nodes, start_id, end_id, f"through{k}", lanes, one_way))
                stats["through_roads"] += 1
            for s, point in stems:
                a_info = info[s]
                c0 = a_info["c"]
                lanes, one_way = flow_lanes(a_info)
                entering = a_info["flow_in"] if a_info["one_way"] else a_info["into"]
                if entering:
                    start = ((a_info["node"], a_info["raw_end"])
                             if a_info["into"] and a_info["chain"] else (free_node(), -1))
                    new_roads.append(junction_road(c0, [a_info["end"], point], start, (free_node(), -1),
                                                   f"stem{s}", lanes, one_way))
                else:
                    end = ((a_info["node"], a_info["raw_start"])
                           if not a_info["into"] and a_info["chain"] else (free_node(), -1))
                    new_roads.append(junction_road(c0, [point, a_info["end"]], (free_node(), -1), end,
                                                   f"stem{s}", lanes, one_way))
            added.extend(new_roads)
            main_roads.extend(r["nodes"] for r in new_roads)
            for c in decisions:
                if inside(c, cluster_nodes) and int(c.get("source_type", -1)) in COMBINE_LANE_TYPES + (COMBINE_MEDIAN_TYPE,):
                    consumed_ids.add(id(c))
            stats["junctions_resolved"] += 1

        # runoffs: every junction, resolved or not
        runoff_arms = runoff_touching.get(root, [])
        if runoff_arms and decisions:
            for c in decisions:
                if inside(c, cluster_nodes) and int(c.get("source_type", -1)) == RUNOFF_TYPE:
                    consumed_ids.add(id(c))
            limit = min(radius, 140.0)
            pieces, unpaired = _extend_runoffs(root, runoff_arms, main_roads, sentinel, max_chord=limit, max_run=limit)
            added.extend(pieces)
            stats["runoff_pieces"] += len(pieces)
            stats["runoff_ends_without_pair"] += unpaired

    out = [c for c in corridors if id(c) not in consumed_ids] + added
    stats["connectors_removed"] = len(consumed_ids)
    stats["junction_roads_added"] = len(added)
    stats["skipped_junctions"] = skipped
    return out, stats


def _extend_runoffs(root, runoff_arms, main_roads, sentinel, *, max_chord=140.0, max_run=60.0):
    """Extend the ends of the runoff roads of one junction so that every end has a pair."""
    arms = []
    for c, n in runoff_arms:
        into = _oriented_source_endpoints(c)[1] == n       # nodes are stored in travel order
        nodes = [list(map(float, p)) for p in c["nodes"]]
        toward = nodes if into else nodes[::-1]
        end = toward[-1]
        back = next((p for p in reversed(toward[:-1]) if math.hypot(p[0] - end[0], p[1] - end[1]) >= 3.0), toward[0])
        dx, dy = end[0] - back[0], end[1] - back[1]
        length = math.hypot(dx, dy) or 1.0
        arms.append({"c": c, "end": end, "dir": (dx / length, dy / length)})
    if not arms:
        return [], 0
    pieces, joined, chords = [], set(), []

    def piece(src, nodes, suffix):
        sentinel[0] += 2
        road = dict(src)
        road.update({
            "id": f"junction_{root}_runoff_{suffix}", "source_key": f"junction:{root}:runoff:{suffix}",
            "nodes": [[round(v, 4) for v in p] for p in nodes], "direction": "forward",
            "node_start": -sentinel[0], "node_end": -sentinel[0] - 1,
            "zone_raw_start": -1, "zone_raw_end": -1, "zone_index": -1,
            "width_start": nodes[0][3], "width_end": nodes[-1][3],
            "one_way": False, "lanes_left": 1, "lanes_right": 1, "auto_lanes": False,
            "combined_junction": True, "runoff_extension": True, "override": {}, "export": True,
        })
        pieces.append(road)

    # 1. opposite runoff ends that look at each other join
    cands = []
    for i in range(len(arms)):
        for j in range(i + 1, len(arms)):
            a, b = arms[i], arms[j]
            ch = (b["end"][0] - a["end"][0], b["end"][1] - a["end"][1])
            dist = math.hypot(*ch)
            if dist < 1.0 or dist > max_chord:
                continue
            h = (ch[0] / dist, ch[1] / dist)
            if a["dir"][0] * b["dir"][0] + a["dir"][1] * b["dir"][1] > -0.3:
                continue
            fa = a["dir"][0] * h[0] + a["dir"][1] * h[1]
            fb = -(b["dir"][0] * h[0] + b["dir"][1] * h[1])
            if fa < 0.2 or fb < 0.2:
                continue
            miss = abs(a["dir"][0] * h[1] - a["dir"][1] * h[0]) + abs(b["dir"][0] * h[1] - b["dir"][1] * h[0])
            if miss < 1.3:
                cands.append((miss, i, j))
    for _miss, i, j in sorted(cands):
        if i in joined or j in joined:
            continue
        joined |= {i, j}
        piece(arms[i]["c"], [arms[i]["end"], arms[j]["end"]], f"pair{i}_{j}")
        chords.append((arms[i]["end"], arms[j]["end"]))

    # 2. remaining ends run on along their heading to a joined pair or the nearest road
    for s in range(len(arms)):
        if s in joined:
            continue
        a = arms[s]
        far = (a["end"][0] + a["dir"][0] * max_run, a["end"][1] + a["dir"][1] * max_run)
        best = None
        polylines = [[p, q] for p, q in chords] + main_roads
        for line in polylines:
            for k in range(len(line) - 1):
                hit = _seg_cross(a["end"], far, line[k], line[k + 1])
                if hit and (best is None or hit[0] < best[0]):
                    best = (hit[0], hit[1], line[k], line[k + 1])
        if best:
            _t, u, p, q = best
            point = _lerp_node(p, q, u)
            point[3] = a["end"][3]
            piece(a["c"], [a["end"], point], f"run{s}")
            joined.add(s)

    # 3. an end without a pair joins the nearest other runoff end
    unpaired = 0
    for s in range(len(arms)):
        if s in joined:
            continue
        a = arms[s]
        others = [(math.hypot(b["end"][0] - a["end"][0], b["end"][1] - a["end"][1]), k)
                  for k, b in enumerate(arms) if k != s and b["c"] is not a["c"]]
        others = [o for o in others if 1.0 <= o[0] <= max_chord]
        if others:
            _d, k = min(others)
            piece(a["c"], [a["end"], arms[k]["end"]], f"near{s}_{k}")
            joined |= {s, k}
        else:
            unpaired += 1
    return pieces, unpaired


def scale_road_widths(roads, factor):
    """Multiply every node width of every road by `factor` (a value near 1.0 makes all roads a bit narrower).

    Used for the export so collision packs next to the roads do not break the DecalRoads.
    Returns the roads unchanged for factor 1.0; otherwise copies with scaled nodes.
    """
    factor = float(factor)
    if not (factor == factor and 0 < factor < float("inf")):
        raise ValueError("width scale must be a positive finite number")
    if factor == 1.0:
        return roads
    out = []
    for road in roads:
        scaled = dict(road)
        scaled["nodes"] = [[*p[:3], round(p[3] * factor, 4)] for p in road.get("nodes") or []]
        if scaled["nodes"]:
            scaled["width_start"], scaled["width_end"] = scaled["nodes"][0][3], scaled["nodes"][-1][3]
        out.append(scaled)
    return out
