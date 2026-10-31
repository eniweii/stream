"""Topology diagnostics, continuous-road stitching and adjacent-lane pairing.

This module intentionally operates on the corridor dictionaries already produced by
region/traffic_corridors.py. It does not parse CARP itself and has no Tkinter
requirements, so the topology logic can be exercised by synthetic tests.
"""
from __future__ import annotations

import math
from typing import Any

_LAST_DIAGNOSTICS: dict[str, Any] = {"joins": [], "by_source_key": {}, "summary": {}}


def _distance(a, b) -> float:
    return math.sqrt(sum((float(a[k]) - float(b[k])) ** 2 for k in range(3)))


def _oriented_endpoints(c):
    a, b = int(c["node_start"]), int(c["node_end"])
    za = int(c.get("zone_raw_start", c.get("zone_index", -1)))
    zb = int(c.get("zone_raw_end", c.get("zone_index", -1)))
    if c.get("direction") == "reverse":
        return b, a, zb, za
    return a, b, za, zb


def _signature(c):
    """Properties that cannot safely change inside one DecalRoad."""
    return (
        round(float(c.get("drivability", -1.0)), 4),
        bool(c.get("one_way", True)),
        bool(c.get("auto_junction", True)),
        bool(c.get("auto_lanes", False)),
        int(c.get("lanes_left", 0)),
        int(c.get("lanes_right", 1)),
        bool(c.get("over_objects", False)),
    )


def _unit_tangent(nodes, at_end: bool):
    if len(nodes) < 2:
        return (0.0, 0.0, 0.0)
    a, b = (nodes[-2], nodes[-1]) if at_end else (nodes[0], nodes[1])
    d = [float(b[k]) - float(a[k]) for k in range(3)]
    mag = math.sqrt(sum(v * v for v in d))
    return tuple(v / mag for v in d) if mag > 1e-9 else (0.0, 0.0, 0.0)


def _connection_cost(a, b):
    """Prefer close endpoints and aligned forward tangents; topology is primary."""
    gap = _distance(a["nodes"][-1], b["nodes"][0])
    ta, tb = _unit_tangent(a["nodes"], True), _unit_tangent(b["nodes"], False)
    dot = sum(ta[k] * tb[k] for k in range(3))
    # Keep the geometric gap as the main term. A direction reversal adds a large
    # penalty without overriding a confirmed shared source node/profile-zone key.
    return gap + max(0.0, 1.0 - dot) * 2.0, gap, dot


def _can_chain(c):
    road_id = int(c.get("road_id", -1))
    return (road_id >= 0 and not c.get("source_decision", False)
            and (not c.get("source_no_traffic", False) or c.get("force_export", False)))


def get_last_merge_diagnostics():
    """Return diagnostics from the most recent merge_connected_corridors call."""
    return _LAST_DIAGNOSTICS


def merge_connected_corridors(corridors, *, endpoint_tolerance=0.75, return_diagnostics=False):
    global _LAST_DIAGNOSTICS
    """Merge continuous compatible lane pieces with deterministic one-to-one links.

    Continuations are matched by source node + raw profile-zone identity and the
    road-level signature. At a branch, an unambiguous best candidate can be used;
    ties are kept split and explicitly diagnosed. If several pieces target the same
    successor, the best incoming candidate is selected rather than discarding every
    link, which was a source of unexplained disconnects in the former algorithm.

    ``endpoint_tolerance`` is retained for caller compatibility and appears in
    diagnostics only; source topology is authoritative for a unique continuation.
    """
    del endpoint_tolerance
    eligible = [c for c in corridors if c.get("export", False) and not c.get("excluded", False)
                and len(c.get("nodes") or []) >= 2]
    oriented = {}
    groupable = set()
    starts = {}
    by_source_key = {}
    joins = []

    for i, c in enumerate(eligible):
        start_node, end_node, start_zone, end_zone = _oriented_endpoints(c)
        sig = _signature(c)
        oriented[i] = (start_node, end_node, start_zone, end_zone, sig)
        source_key = str(c.get("source_key", c.get("id", i)))
        by_source_key[source_key] = {
            "source_key": source_key,
            "incoming": {"status": "unresolved", "target": None, "reason": "not evaluated"},
            "outgoing": {"status": "unresolved", "target": None, "reason": "not evaluated"},
        }
        if _can_chain(c):
            groupable.add(i)
            starts.setdefault((start_node, start_zone, sig), []).append(i)
        else:
            by_source_key[source_key]["incoming"] = {"status": "standalone", "target": None,
                                                     "reason": "decision connector or non-chainable source segment"}
            by_source_key[source_key]["outgoing"] = {"status": "standalone", "target": None,
                                                     "reason": "decision connector or non-chainable source segment"}

    proposals = {}  # source index -> (target index, score, gap, tangent dot)
    for i in sorted(groupable):
        c = eligible[i]
        _start_node, end_node, _start_zone, end_zone, sig = oriented[i]
        candidates = [j for j in starts.get((end_node, end_zone, sig), ())
                      if j != i and j in groupable]
        source_key = str(c.get("source_key", c.get("id", i)))
        diag = by_source_key[source_key]["outgoing"]
        if not candidates:
            # Tell identity/setting failures apart when a source-node neighbor exists.
            any_node = [j for j, other in enumerate(eligible) if j != i
                        and oriented[j][0] == end_node and oriented[j][2] == end_zone]
            any_zone = [j for j, other in enumerate(eligible) if j != i and oriented[j][0] == end_node]
            if any_node:
                reason = "neighbor exists but road-level settings are incompatible"
                status = "settings_mismatch"
            elif any_zone:
                reason = "neighbor shares the source node but not the raw profile-zone identity"
                status = "profile_zone_mismatch"
            else:
                reason = "no eligible successor uses this oriented source endpoint"
                status = "no_continuation"
            diag.update(status=status, reason=reason)
            continue
        ranked = []
        for j in candidates:
            score, gap, dot = _connection_cost(c, eligible[j])
            ranked.append((score, j, gap, dot))
        ranked.sort(key=lambda x: (x[0], x[1]))
        if len(ranked) > 1 and ranked[1][0] - ranked[0][0] < 0.15:
            diag.update(status="ambiguous_branch", reason="multiple compatible successors have indistinguishable scores",
                        candidates=[str(eligible[x[1]].get("source_key", eligible[x[1]].get("id", x[1]))) for x in ranked[:4]])
            continue
        score, j, gap, dot = ranked[0]
        proposals[i] = (j, score, gap, dot)
        diag.update(status="candidate", target=str(eligible[j].get("source_key", eligible[j].get("id", j))),
                    gap=round(gap, 4), tangent_dot=round(dot, 4), reason="unique best topology-compatible successor")

    # Resolve multiple incoming claims one target at a time. Keep the strongest
    # parent if clearly better; leave a genuine tie split and report it.
    incoming_claims = {}
    for i, (j, score, gap, dot) in proposals.items():
        incoming_claims.setdefault(j, []).append((score, i, gap, dot))
    selected_links = {}
    rejected_by_conflict = set()
    for target, claims in incoming_claims.items():
        claims.sort(key=lambda x: (x[0], x[1]))
        if len(claims) == 1:
            selected_links[claims[0][1]] = target
            continue
        if claims[1][0] - claims[0][0] >= 0.15:
            selected_links[claims[0][1]] = target
            rejected_by_conflict.update(x[1] for x in claims[1:])
        else:
            rejected_by_conflict.update(x[1] for x in claims)
            for _, src, _, _ in claims:
                key = str(eligible[src].get("source_key", eligible[src].get("id", src)))
                by_source_key[key]["outgoing"].update(status="ambiguous_predecessor", target=None,
                    reason="multiple predecessors claim this successor with indistinguishable scores")
            target_key = str(eligible[target].get("source_key", eligible[target].get("id", target)))
            by_source_key[target_key]["incoming"] = {"status": "ambiguous_predecessor", "target": None,
                "reason": "multiple equally plausible predecessors; no unsafe merge performed"}

    # Enforce one incoming and one outgoing link. Any conflict losers retain a
    # precise diagnostic instead of silently falling out of the chain graph.
    next_link = dict(selected_links)
    prev_link = {j: i for i, j in next_link.items()}
    for i, (j, _score, gap, dot) in proposals.items():
        skey = str(eligible[i].get("source_key", eligible[i].get("id", i)))
        tkey = str(eligible[j].get("source_key", eligible[j].get("id", j)))
        if next_link.get(i) == j:
            by_source_key[skey]["outgoing"].update(status="joined", target=tkey,
                reason="selected one-to-one continuation", gap=round(gap, 4), tangent_dot=round(dot, 4))
            by_source_key[tkey]["incoming"] = {"status": "joined", "target": skey,
                "reason": "selected one-to-one continuation", "gap": round(gap, 4), "tangent_dot": round(dot, 4)}
        elif i not in rejected_by_conflict and by_source_key[skey]["outgoing"]["status"] == "candidate":
            by_source_key[skey]["outgoing"].update(status="not_selected", target=tkey,
                reason="candidate was not selected by one-to-one predecessor matching", gap=round(gap, 4))
        elif i in rejected_by_conflict and by_source_key[skey]["outgoing"]["status"] == "candidate":
            by_source_key[skey]["outgoing"].update(status="incoming_conflict", target=tkey,
                reason="a better-supported or equally plausible predecessor also claims this successor", gap=round(gap, 4))

    # Fill every incoming endpoint with a concrete outcome. This is important for
    # the viewer: a missing predecessor must not be shown as an unexplained blank.
    for i, c in enumerate(eligible):
        if i not in groupable:
            continue
        key = str(c.get("source_key", c.get("id", i)))
        incoming_info = by_source_key[key]["incoming"]
        if incoming_info.get("status") in ("joined", "ambiguous_predecessor"):
            continue
        start_node, _end_node, start_zone, _end_zone, sig = oriented[i]
        exact_predecessors = [j for j in groupable if j != i
                              and oriented[j][1] == start_node
                              and oriented[j][3] == start_zone
                              and oriented[j][4] == sig]
        node_predecessors = [j for j in range(len(eligible)) if j != i
                             and oriented[j][1] == start_node]
        zone_predecessors = [j for j in node_predecessors if oriented[j][3] == start_zone]
        if exact_predecessors:
            incoming_info.update(status="not_selected", reason="a topology-compatible predecessor did not win one-to-one matching")
        elif zone_predecessors:
            incoming_info.update(status="settings_mismatch", reason="predecessor shares the source endpoint but road-level settings differ")
        elif node_predecessors:
            incoming_info.update(status="profile_zone_mismatch", reason="predecessor shares the source node but not its raw profile-zone identity")
        else:
            incoming_info.update(status="no_predecessor", reason="no eligible predecessor uses this oriented source endpoint")

    # Open chains first, then any residual cycles. Connector roads remain standalone.
    chains, visited = [], set()
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
        merged_nodes = [list(map(float, n)) for n in first["nodes"]]
        source_keys = [str(eligible[i].get("source_key", eligible[i].get("id", i))) for i in chain]
        source_segments = [int(eligible[i].get("segment_index", -1)) for i in chain]
        source_segment_ids = [int(eligible[i].get("source_segment_id", eligible[i].get("segment_index", -1))) for i in chain]
        source_zones = [int(eligible[i].get("zone_index", -1)) for i in chain]
        source_zone_raw = [[int(eligible[i].get("zone_raw_start", eligible[i].get("zone_index", -1))),
                            int(eligible[i].get("zone_raw_end", eligible[i].get("zone_index", -1)))] for i in chain]
        source_road_ids = [int(eligible[i].get("road_id", -1)) for i in chain]
        for idx in chain[1:]:
            next_nodes = [list(map(float, n)) for n in eligible[idx]["nodes"]]
            if merged_nodes and next_nodes:
                a, b = merged_nodes[-1], next_nodes[0]
                shared = [round((a[k] + b[k]) * 0.5, 4) for k in range(3)] + [round((a[3] + b[3]) * 0.5, 4)]
                merged_nodes[-1] = shared
                next_nodes[0] = shared.copy()
                next_nodes = next_nodes[1:]
            merged_nodes.extend(next_nodes)
        road_id = int(first.get("road_id", -1))
        if road_id >= 0 and len(chain) > 1:
            merged_id = f"road_{road_id}_zone_{first.get('zone_index', -1)}_from_{first.get('segment_index', chain[0])}"
        else:
            merged_id = str(first.get("id", f"zone_{chain[0]}"))
        merged.update({
            "id": merged_id, "source_key": source_keys[0], "source_keys": source_keys,
            "source_segment_indices": source_segments, "source_segment_ids": source_segment_ids,
            "source_road_ids": source_road_ids, "source_zone_indices": source_zones,
            "source_zone_raw_endpoints": source_zone_raw, "merged_corridor_count": len(chain),
            "nodes": merged_nodes, "width_start": round(merged_nodes[0][3], 4) if merged_nodes else first.get("width_start"),
            "width_end": round(merged_nodes[-1][3], 4) if merged_nodes else first.get("width_end"),
            "export": bool(first.get("export", False)), "height_offset_applied": False,
            "is_lane_pair": False,
        })
        out.append(merged)
    out.sort(key=lambda c: (int(c.get("road_id", -1)), int(c.get("segment_index", -1)),
                            int(c.get("zone_index", -1)), str(c.get("id", ""))))
    status_counts = {}
    for row in by_source_key.values():
        for side in ("incoming", "outgoing"):
            status = row[side]["status"]
            status_counts[status] = status_counts.get(status, 0) + 1
    joins = [{"source_key": key, "incoming": value["incoming"], "outgoing": value["outgoing"]}
             for key, value in sorted(by_source_key.items())]
    _LAST_DIAGNOSTICS = {"joins": joins, "by_source_key": by_source_key,
                         "summary": {"eligible_corridors": len(eligible), "merged_roads": len(out),
                                     "link_count": len(next_link), "status_counts": status_counts}}
    if return_diagnostics:
        return out, _LAST_DIAGNOSTICS
    return out


def _resample_nodes(nodes, count):
    """Resample [x,y,z,width] nodes by cumulative 3D arc length."""
    pts = [list(map(float, n[:4])) for n in nodes]
    if len(pts) < 2:
        return pts * count
    distances = [0.0]
    for i in range(1, len(pts)):
        distances.append(distances[-1] + _distance(pts[i - 1], pts[i]))
    total = distances[-1]
    if total < 1e-8:
        return [pts[0][:] for _ in range(count)]
    out, cursor = [], 0
    for k in range(count):
        target = total * k / max(1, count - 1)
        while cursor < len(distances) - 2 and distances[cursor + 1] < target:
            cursor += 1
        span = distances[cursor + 1] - distances[cursor]
        t = 0.0 if span < 1e-9 else (target - distances[cursor]) / span
        out.append([pts[cursor][j] + (pts[cursor + 1][j] - pts[cursor][j]) * t for j in range(4)])
    return out


def _segment_zone_map(road):
    segs = road.get("source_segment_indices") or [road.get("segment_index", -1)]
    zones = road.get("source_zone_indices") or [road.get("zone_index", -1)]
    return {int(s): int(zones[i]) for i, s in enumerate(segs) if i < len(zones)}


def find_adjacent_lane_pairs(roads, *, min_spacing=2.0, max_spacing=12.0, max_height_gap=1.75):
    """Return same-direction adjacent lane-pair candidates, including rejections.

    Candidate discovery is conservative: source chains must share segment coverage
    and be adjacent in their profile-zone indices. Geometry then confirms side-by-
    side alignment, reasonable spacing, height agreement and consistent direction.
    """
    roads = [r for r in roads if r.get("export", False) and len(r.get("nodes") or []) >= 2
             and not r.get("source_decision", False) and int(r.get("road_id", -1)) >= 0]
    candidates = []
    for ai in range(len(roads)):
        a = roads[ai]
        aset = set(map(int, a.get("source_segment_indices") or [a.get("segment_index", -1)]))
        amap = _segment_zone_map(a)
        for bi in range(ai + 1, len(roads)):
            b = roads[bi]
            bset = set(map(int, b.get("source_segment_indices") or [b.get("segment_index", -1)]))
            common = sorted(aset & bset)
            if not common:
                continue
            # Do not pair unrelated roads just because they run close together.
            overlap = len(common) / max(1, max(len(aset), len(bset)))
            if overlap < 0.6:
                continue
            candidate = {"road_a": a, "road_b": b, "road_a_id": str(a.get("id", ai)),
                         "road_b_id": str(b.get("id", bi)), "accepted": False,
                         "common_segments": common, "overlap_ratio": round(overlap, 4)}
            if aset != bset:
                candidate["reason"] = "source segment coverage differs; this initial mode only merges full-length lane pairs"
                candidates.append(candidate)
                continue
            bmap = _segment_zone_map(b)
            shared_zone_pairs = [(amap[s], bmap[s]) for s in common if s in amap and s in bmap]
            adjacent_votes = sum(1 for za, zb in shared_zone_pairs if abs(za - zb) == 1)
            same_direction = (a.get("direction", "forward") == b.get("direction", "forward")
                              and bool(a.get("one_way", True)) == bool(b.get("one_way", True)))
            if not same_direction:
                candidate["reason"] = "travel direction or one-way setting differs"
                candidates.append(candidate); continue
            if not shared_zone_pairs or adjacent_votes / len(shared_zone_pairs) < 0.6:
                candidate["reason"] = "source profile zones are not consistently adjacent"
                candidates.append(candidate); continue
            count = max(8, min(160, max(len(a["nodes"]), len(b["nodes"]))))
            na, nb = _resample_nodes(a["nodes"], count), _resample_nodes(b["nodes"], count)
            separations = [_distance(pa, pb) for pa, pb in zip(na, nb)]
            mean_gap = sum(separations) / len(separations)
            max_gap = max(separations)
            mean_height_gap = sum(abs(pa[2] - pb[2]) for pa, pb in zip(na, nb)) / len(na)
            ta, tb = _unit_tangent(na, False), _unit_tangent(nb, False)
            tangent_dot = sum(ta[k] * tb[k] for k in range(3))
            if not (min_spacing <= mean_gap <= max_spacing) or max_gap > max_spacing * 1.8:
                candidate["reason"] = "lane centerline spacing is outside the safe range"
            elif mean_height_gap > max_height_gap:
                candidate["reason"] = "lane paths have incompatible elevation"
            elif tangent_dot < 0.85:
                candidate["reason"] = "lane paths do not follow a compatible direction"
            else:
                candidate["accepted"] = True
                candidate["reason"] = "same-direction adjacent zones with compatible geometry"
            candidate.update({"mean_spacing": round(mean_gap, 3), "max_spacing": round(max_gap, 3),
                              "mean_height_gap": round(mean_height_gap, 3), "tangent_dot": round(tangent_dot, 4),
                              "sampled_a": na, "sampled_b": nb})
            candidates.append(candidate)
    # Deterministic non-overlapping matching: prefer strongest topology overlap and
    # stable road identifiers. A chain can participate in only one pair per export.
    accepted = sorted((c for c in candidates if c["accepted"]),
                      key=lambda c: (-len(c["common_segments"]), c["road_a_id"], c["road_b_id"]))
    used = set()
    for c in accepted:
        if c["road_a_id"] in used or c["road_b_id"] in used:
            c["accepted"] = False
            c["reason"] = "one of these lanes is already assigned to a stronger adjacent pair"
        else:
            used.update((c["road_a_id"], c["road_b_id"]))
    return candidates


def merge_adjacent_lane_pairs(roads, candidates):
    """Replace accepted pairs with centered two-lane DecalRoad candidates."""
    accepted = [c for c in candidates if c.get("accepted", False)]
    used_ids = {c["road_a_id"] for c in accepted} | {c["road_b_id"] for c in accepted}
    by_id = {str(r.get("id", i)): r for i, r in enumerate(roads)}
    result = [dict(r) for r in roads if str(r.get("id", "")) not in used_ids]
    for idx, pair in enumerate(accepted):
        a = by_id[pair["road_a_id"]]
        b = by_id[pair["road_b_id"]]
        na, nb = pair["sampled_a"], pair["sampled_b"]
        nodes = []
        for pa, pb in zip(na, nb):
            separation = _distance(pa, pb)
            width = max(0.1, float(pa[3]) * 0.5 + separation + float(pb[3]) * 0.5)
            nodes.append([round((pa[0] + pb[0]) * 0.5, 4), round((pa[1] + pb[1]) * 0.5, 4),
                          round((pa[2] + pb[2]) * 0.5, 4), round(width, 4)])
        all_keys = list(dict.fromkeys(list(a.get("source_keys", [a.get("source_key", pair["road_a_id"])]))
                                       + list(b.get("source_keys", [b.get("source_key", pair["road_b_id"])]))))
        segments = list(dict.fromkeys(list(a.get("source_segment_indices", [a.get("segment_index", -1)]))
                                      + list(b.get("source_segment_indices", [b.get("segment_index", -1)]))))
        segment_ids = list(dict.fromkeys(list(a.get("source_segment_ids", [a.get("source_segment_id", -1)]))
                                         + list(b.get("source_segment_ids", [b.get("source_segment_id", -1)]))))
        source_roads = list(dict.fromkeys(list(a.get("source_road_ids", [a.get("road_id", -1)]))
                                          + list(b.get("source_road_ids", [b.get("road_id", -1)]))))
        zones = list(dict.fromkeys(list(a.get("source_zone_indices", [a.get("zone_index", -1)]))
                                   + list(b.get("source_zone_indices", [b.get("zone_index", -1)]))))
        road = dict(a)
        road.update({
            "id": f"autojoin_{idx}_{a.get('id', 'a')}_{b.get('id', 'b')}",
            "source_key": all_keys[0] if all_keys else f"autojoin_{idx}",
            "source_keys": all_keys, "source_segment_indices": segments,
            "source_segment_ids": segment_ids, "source_road_ids": source_roads,
            "source_zone_indices": zones, "nodes": nodes,
            "width_start": nodes[0][3], "width_end": nodes[-1][3],
            "merged_corridor_count": int(a.get("merged_corridor_count", 1)) + int(b.get("merged_corridor_count", 1)),
            "is_lane_pair": True, "lane_pair_sources": [pair["road_a_id"], pair["road_b_id"]],
            "auto_lanes": False, "lanes_left": 1, "lanes_right": 1,
            "one_way": True, "direction": a.get("direction", "forward"),
            "export": True,
        })
        result.append(road)
    result.sort(key=lambda r: (int(r.get("road_id", -1)), int(r.get("segment_index", -1)), str(r.get("id", ""))))
    return result
