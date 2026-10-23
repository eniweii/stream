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
    offset_magnitude = abs(float(profile.lane_offset(raw_index)))
    is_left = logical_index < middle
    # Profile offsets are stored as magnitudes. Side is determined by zone order.
    return {
        "logical_index": logical_index,
        "raw_index": raw_index,
        "type": int(profile.lane_type(raw_index)),
        "width": max(0.05, width),
        "offset": -offset_magnitude if is_left else offset_magnitude,
        "side": "left" if is_left else "right",
        "middle_zone": middle,
    }


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
        for zi in range(min(int(pa.numZones), int(pb.numZones))):
            za, zb = get_oriented_zone(pa, zi, inv_a), get_oriented_zone(pb, zi, inv_b)
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
        count = min(int(pa.numZones), int(pb.numZones))
        if pa.numZones != pb.numZones:
            zone_mismatches += 1
            warnings.append({"kind": "profile_zone_count_mismatch", "segment": seg_i,
                             "start_count": pa.numZones, "end_count": pb.numZones,
                             "message": "Only corresponding zone indices at both endpoints are generated."})
        samples = sample_segment(network, seg, curve_steps)
        one_way_source = bool(flags & FLAG_ONE_WAY)

        for zi in range(count):
            za, zb = get_oriented_zone(pa, zi, inv_a), get_oriented_zone(pb, zi, inv_b)
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


def merge_connected_corridors(corridors, *, endpoint_tolerance=0.75):
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
        can_join = (road_id >= 0 and not c.get("source_decision", False)
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
        else:
            merged_id = str(first.get("id", f"zone_{chain[0]}"))
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
