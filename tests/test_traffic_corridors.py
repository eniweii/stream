"""Synthetic tests for the standalone traffic corridor converter (no game files needed)."""
import json
import math
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from traffic_corridors import (
    build_corridors, merged_type_rules, get_oriented_zone,
                                merge_connected_corridors, harmonize_zone_endpoints, combine_carriageways,
                                thin_nodes, thin_roads, resolve_junctions, scale_road_widths, keep_shoulders_near_shortcuts, preserve_winding,
                                drop_shoulders, extend_general_roads, shoulder_guides, _max_turn,
                                _corridor_merge_signature,
                                FLAG_START_INVERTED, FLAG_DECISION, FLAG_NO_TRAFFIC)
from traffic_node_exporter import write_items_level, write_source_manifest


class FakeProfile:
    def __init__(self, types, widths, offsets, middle):
        self.numZones = len(types)
        self.middleZone = middle
        self.types, self.widths, self.offsets = types, widths, offsets

    def lane_type(self, i): return self.types[i]
    def lane_width(self, i): return self.widths[i]
    def lane_offset(self, i): return self.offsets[i]


class FakeNode:
    def __init__(self, position, index, profile=0):
        self.position, self.index = position, index
        self.profileIndex, self.numSegments = profile, 1


class FakeSegment:
    def __init__(self, flags=0, start=0, end=1, index=0, road_id=0):
        self.nodeStart, self.nodeEnd, self.flags, self.index = start, end, flags, index
        self.roadID = road_id
        self.startHandle = bytes((0, 0, 0))
        self.endHandle = bytes((0, 0, 0))
        self.startHandleLength = self.endHandleLength = 0


def make_network(flags=0):
    profile = FakeProfile([3, 1, 1], [4, 4, 4], [6, 2, 6], 1)
    return SimpleNamespace(
        nodes=[FakeNode((0.0, 0.0, 0.0), 0), FakeNode((0.0, 0.0, 30.0), 1)],
        profiles=[profile], segments=[FakeSegment(flags)], roads=[])


def make_network_with(types, widths, offsets, middle, flags=0, segments=1):
    profile = FakeProfile(types, widths, offsets, middle)
    nodes = [FakeNode((0.0, 0.0, 30.0 * i), i) for i in range(segments + 1)]
    segs = [FakeSegment(flags, i, i + 1, index=i, road_id=0) for i in range(segments)]
    return SimpleNamespace(nodes=nodes, profiles=[profile], segments=segs, roads=[])


def export_input(network, **kw):
    result = build_corridors(network, merged_type_rules(), {}, vertical_offset=150, **kw)
    harmonize_zone_endpoints(result["corridors"])
    return result["corridors"]


class CombineCarriagewayTests(unittest.TestCase):
    # sidewalk, lane, lane, median, lane, lane, sidewalk (lane width 4, median one lane wide)
    FIVE = ([3, 1, 1, 4, 1, 1, 3], [4] * 7, [12, 8, 4, 0, 4, 8, 12], 3)

    def test_median_carriageway_is_one_road_with_dead_gap(self):
        corridors = export_input(make_network_with(*self.FIVE))
        out, report = combine_carriageways(corridors)
        roads = [c for c in out if c.get("combined")]
        self.assertEqual(len(roads), 1)
        road = roads[0]
        self.assertEqual((road["lanes_left"], road["lanes_right"], road["one_way"], road["auto_lanes"]),
                         (2, 2, False, False))
        # outer edge to outer edge: 2 lanes + 4 m median + 2 lanes = 20 m, centred on the axis
        self.assertAlmostEqual(road["nodes"][0][3], 20.0, places=3)
        self.assertAlmostEqual(road["nodes"][0][1], 0.0, places=3)       # BeamNG y = -nfs x
        self.assertAlmostEqual(road["nodes"][0][0], 0.0, places=3)       # BeamNG x = nfs z (start)
        self.assertAlmostEqual(road["nodes"][-1][0], 30.0, places=3)
        self.assertEqual(report["median_corridors_dropped"], 1)
        self.assertEqual(report["lane_corridors_replaced"], 4)
        # sidewalks (type 3) stay separate roads, the median is not a road of its own
        types = sorted(c["source_type"] for c in out if not c.get("combined"))
        self.assertEqual(types, [3, 3])

    def test_no_median_two_plus_two_and_one_plus_one(self):
        out, _ = combine_carriageways(export_input(make_network_with([3, 1, 1, 1, 1, 3], [4] * 6, [10, 6, 2, 2, 6, 10], 3)))
        road = [c for c in out if c.get("combined")][0]
        self.assertEqual((road["lanes_left"], road["lanes_right"]), (2, 2))
        self.assertAlmostEqual(road["nodes"][0][3], 16.0, places=3)
        out, _ = combine_carriageways(export_input(make_network_with([3, 1, 1, 3], [4] * 4, [6, 2, 2, 6], 2)))
        road = [c for c in out if c.get("combined")][0]
        self.assertEqual((road["lanes_left"], road["lanes_right"]), (1, 1))

    def test_uneven_sides_are_one_road_with_a_lane_count_per_side(self):
        net = make_network_with([3, 1, 1, 1, 1, 1, 3], [4] * 7, [10, 6, 2, 2, 6, 10, 14], 3)
        out, report = combine_carriageways(export_input(net))
        road = [c for c in out if c.get("combined")][0]
        self.assertEqual((road["lanes_left"], road["lanes_right"], road["one_way"]), (2, 3, False))
        self.assertAlmostEqual(road["nodes"][0][3], 20.0, places=3)      # outer lane edge to outer lane edge
        self.assertEqual(report["kinds"], {"two-way uneven": 1})

    def test_one_way_segment_is_one_road_with_lanes_right(self):
        from traffic_corridors import FLAG_ONE_WAY
        out, report = combine_carriageways(export_input(make_network_with(*self.FIVE, flags=FLAG_ONE_WAY)))
        road = [c for c in out if c.get("combined")][0]
        self.assertEqual((road["lanes_left"], road["lanes_right"], road["one_way"]), (0, 4, True))
        self.assertAlmostEqual(road["nodes"][0][3], 20.0, places=3)      # the median stays a dead gap
        self.assertEqual(report["median_corridors_dropped"], 1)
        self.assertEqual(sorted(c["source_type"] for c in out if not c.get("combined")), [3, 3])

    def test_lanes_on_the_left_only_use_lanes_left_and_keep_node_order(self):
        out, _ = combine_carriageways(export_input(make_network_with([3, 1, 1, 3], [4] * 4, [14, 10, 6, 2], 4)))
        road = [c for c in out if c.get("combined")][0]
        self.assertEqual((road["lanes_left"], road["lanes_right"], road["one_way"]), (2, 0, True))
        self.assertAlmostEqual(road["nodes"][0][0], 0.0, places=3)       # still starts at the segment start
        self.assertAlmostEqual(road["nodes"][-1][0], 30.0, places=3)

    def test_other_zone_between_lanes_stays_its_own_road(self):
        out, _ = combine_carriageways(export_input(make_network_with([3, 1, 1, 3, 1, 1, 3], [4] * 7, [12, 8, 4, 0, 4, 8, 12], 3)))
        road = [c for c in out if c.get("combined")][0]
        self.assertEqual((road["lanes_left"], road["lanes_right"]), (2, 2))
        self.assertEqual(sorted(c["source_type"] for c in out if not c.get("combined")), [3, 3, 3])

    def test_decision_connector_lanes_become_one_road_only_when_asked(self):
        net = make_network_with([3, 1, 1, 4, 1, 1, 3], [4] * 7, [12, 8, 4, 0, 4, 8, 12], 3, flags=FLAG_DECISION)
        out, report = combine_carriageways(export_input(net, include_decision_connectors=True))
        self.assertFalse(any(c.get("combined") for c in out))                    # default: one road per lane
        self.assertEqual(report["skipped_segments"], {"decision or NoTraffic segment": 1})
        out, report = combine_carriageways(export_input(net, include_decision_connectors=True), combine_connectors=True)
        road = [c for c in out if c.get("combined")]
        self.assertEqual(len(road), 1)
        self.assertEqual((road[0]["lanes_left"], road[0]["lanes_right"]), (2, 2))
        self.assertTrue(road[0]["source_decision"])                              # still a connector for the junction logic
        self.assertEqual(report["median_corridors_dropped"], 1)
        out, report = combine_carriageways(
            export_input(make_network_with(*self.FIVE, flags=FLAG_NO_TRAFFIC), include_decision_connectors=True), combine_connectors=True)
        self.assertEqual(report["combined_segments"], 0)                          # NoTraffic is never combined

    def test_no_traffic_segment_is_not_combined(self):
        from traffic_corridors import FLAG_NO_TRAFFIC
        out, report = combine_carriageways(export_input(make_network_with(*self.FIVE, flags=FLAG_NO_TRAFFIC)))
        self.assertFalse(any(c.get("combined") for c in out))
        self.assertEqual(report["combined_segments"], 0)

    def test_combined_pieces_chain_into_one_decalroad_and_serialize(self):
        corridors = export_input(make_network_with(*self.FIVE, segments=3))
        out, _ = combine_carriageways(corridors)
        roads = merge_connected_corridors(out)
        combined = [r for r in roads if r.get("combined")]
        self.assertEqual(len(combined), 1)
        self.assertEqual(combined[0]["merged_corridor_count"], 3)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "roads.items.level.json"
            write_items_level(roads, path)
            objs = [json.loads(line) for line in path.read_text().splitlines()]
        decal = [o for o in objs if o["lanesLeft"] == 2][0]
        self.assertEqual((decal["lanesLeft"], decal["lanesRight"], decal["oneWay"], decal["autoLanes"]),
                         (2, 2, False, False))
        self.assertAlmostEqual(decal["nodes"][0][2], 150.0)


class ThinningAndConnectorMergeTests(unittest.TestCase):
    def test_straight_line_collapses_but_max_spacing_keeps_long_pieces_sampled(self):
        line = [[0.0, 3.0 * i, 0.0, 4.0] for i in range(11)]          # 30 m, node every 3 m
        self.assertEqual(len(thin_nodes(line, 0.15, 0.0)), 2)
        thinned = thin_nodes(line, 0.15, 25.0)
        self.assertEqual(len(thinned), 3)
        self.assertEqual((thinned[0], thinned[-1]), (line[0], line[-1]))
        self.assertIs(thin_nodes(line, 0.0, 25.0), line)               # 0 = off

    def test_curve_and_width_change_keep_their_nodes(self):
        arc = [[20 * math.sin(i / 10 * 1.2), 20 * (1 - math.cos(i / 10 * 1.2)), 0.0, 4.0] for i in range(11)]
        kept = thin_nodes(arc, 0.15, 0.0)
        self.assertGreater(len(kept), 2)
        self.assertLess(len(kept), len(arc))
        for p in arc:   # every dropped node stays within tolerance of the thinned polyline
            best = min(_point_to_segment(p, kept[i], kept[i + 1]) for i in range(len(kept) - 1))
            self.assertLessEqual(best, 0.151)
        widening = [[0.0, 3.0 * i, 0.0, 4.0 + i] for i in range(11)]   # linear width: still collapses
        self.assertEqual(len(thin_nodes(widening, 0.15, 0.0)), 2)
        step = [[0.0, 3.0 * i, 0.0, 4.0 if i < 5 else 9.0] for i in range(11)]
        self.assertGreater(len(thin_nodes(step, 0.15, 0.0)), 2)

    def test_thin_roads_reports_counts_and_leaves_input_alone(self):
        road = {"id": "r", "nodes": [[0.0, 3.0 * i, 0.0, 4.0] for i in range(11)]}
        out, report = thin_roads([road], 0.15, 0.0)
        self.assertEqual(len(road["nodes"]), 11)
        self.assertEqual(report, {"nodes_before": 11, "nodes_after": 2})
        self.assertEqual(len(out[0]["nodes"]), 2)

    @staticmethod
    def _piece(name, road_id, a, b, zone, decision, x0):
        return {"id": name, "source_key": name, "road_id": road_id, "node_start": a, "node_end": b,
                "zone_raw_start": zone, "zone_raw_end": zone, "zone_index": zone, "direction": "forward",
                "segment_index": a, "drivability": 1.0, "one_way": True, "auto_junction": True,
                "auto_lanes": False, "lanes_left": 0, "lanes_right": 1, "over_objects": False,
                "source_decision": decision, "export": True,
                "nodes": [[x0 + 5.0 * i, 0.0, 0.0, 4.0] for i in range(3)]}

    def test_connector_chains_with_its_roads_only_when_unique(self):
        approach = self._piece("a", 0, 0, 1, 1, False, 0.0)
        connector = self._piece("c", -1, 1, 2, 1, True, 10.0)
        exit_lane = self._piece("e", 1, 2, 3, 1, False, 20.0)
        pieces = [approach, connector, exit_lane]
        self.assertEqual(len(merge_connected_corridors(pieces)), 3)             # default: connectors stay separate
        merged = merge_connected_corridors(pieces, merge_decision=True)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["merged_corridor_count"], 3)
        # a second connector leaving the same lane is a real branch: nothing is joined to it
        other = self._piece("c2", -1, 1, 4, 1, True, 10.0)
        roads = merge_connected_corridors([approach, connector, other, exit_lane], merge_decision=True)
        self.assertEqual(sorted(r["merged_corridor_count"] for r in roads), [1, 1, 2])


def _point_to_segment(p, a, b):
    ab = [b[i] - a[i] for i in range(3)]
    ab2 = sum(v * v for v in ab)
    ap = [p[i] - a[i] for i in range(3)]
    t = max(0.0, min(1.0, sum(ap[i] * ab[i] for i in range(3)) / ab2)) if ab2 > 1e-12 else 0.0
    return math.sqrt(sum((ap[i] - t * ab[i]) ** 2 for i in range(3)))


class DecalRoadDetailTests(unittest.TestCase):
    def test_detail_is_written_and_defaults_to_one(self):
        from traffic_node_exporter import corridor_to_decalroad
        road = {"id": "r", "export": True, "nodes": [[0.0, 0.0, 0.0, 4.0], [0.0, 10.0, 0.0, 4.0]]}
        self.assertEqual(corridor_to_decalroad(road)["detail"], 1.0)
        self.assertEqual(corridor_to_decalroad(road, detail=0.5)["detail"], 0.5)
        with self.assertRaises(ValueError):
            corridor_to_decalroad(road, detail=0)


def _arm(name, node_start, node_end, points, raw=5, **extra):
    c = {"id": name, "source_key": name, "road_id": 0, "node_start": node_start, "node_end": node_end,
         "zone_raw_start": raw, "zone_raw_end": raw, "zone_index": raw, "direction": "forward",
         "segment_index": node_start, "drivability": 1.0, "one_way": False, "auto_junction": True,
         "auto_lanes": False, "lanes_left": 1, "lanes_right": 1, "over_objects": False,
         "source_decision": False, "source_type": 1, "combined": True, "export": True,
         "nodes": [[float(x), float(y), 0.0, 8.0] for x, y in points]}
    c.update(extra)
    return c


def _connector(name, a, b, sidewalk=False):
    return _arm(name, a, b, [(0, 0), (1, 1)], road_id=-1, source_decision=True, combined=False,
                source_type=3 if sidewalk else 1, one_way=True, lanes_left=0, lanes_right=1)


class JunctionRoadTests(unittest.TestCase):
    def _t_junction(self):
        left = _arm("left", 1, 10, [(-60, 0), (-30, 0), (-10, 0)])       # runs into the junction
        right = _arm("right", 11, 2, [(10, 0), (30, 0), (60, 0)])        # runs away from it
        south = _arm("south", 3, 12, [(0, -60), (0, -30), (0, -15)], raw=6)
        cons = [_connector("c1", 10, 11), _connector("c2", 12, 10), _connector("c3", 11, 12),
                _connector("walk", 10, 12, sidewalk=True)]
        return [left, right, south] + cons

    def test_t_junction_stem_ends_on_a_node_of_the_through_road(self):
        out, report = resolve_junctions(self._t_junction())
        self.assertEqual((report["junctions_resolved"], report["through_roads"], report["stems_on_road"]), (1, 1, 1))
        self.assertEqual(report["connectors_removed"], 3)                  # the sidewalk connector stays
        self.assertEqual([c["id"] for c in out if c.get("source_decision")], ["walk"])
        through = [c for c in out if c.get("combined_junction") and "through" in c["id"]][0]
        stem = [c for c in out if c.get("combined_junction") and "stem" in c["id"]][0]
        self.assertEqual([n[:2] for n in through["nodes"]], [[-10.0, 0.0], [0.0, 0.0], [10.0, 0.0]])
        self.assertEqual([n[:2] for n in stem["nodes"]], [[0.0, -15.0], [0.0, 0.0]])
        self.assertEqual((through["lanes_left"], through["lanes_right"], through["one_way"]), (1, 1, False))

    def test_stem_runs_past_the_through_road_by_the_overlap(self):
        out, _ = resolve_junctions(self._t_junction(), overlap=2.0)
        stem = [c for c in out if c.get("combined_junction") and "stem" in c["id"]][0]
        self.assertEqual([n[:2] for n in stem["nodes"]], [[0.0, -15.0], [0.0, 0.0], [0.0, 2.0]])
        through = [c for c in out if c.get("combined_junction") and "through" in c["id"]][0]
        self.assertEqual([n[:2] for n in through["nodes"]], [[-10.0, 0.0], [0.0, 0.0], [10.0, 0.0]])   # through road unchanged

    def test_arms_and_junction_roads_chain_into_long_roads(self):
        out, _ = resolve_junctions(self._t_junction())
        roads = [r for r in merge_connected_corridors(out) if not r.get("source_decision")]
        self.assertEqual(sorted(r["merged_corridor_count"] for r in roads), [2, 3])
        main = [r for r in roads if r["merged_corridor_count"] == 3][0]
        xs = [n[0] for n in main["nodes"]]
        self.assertEqual((min(xs), max(xs)), (-60.0, 60.0))
        self.assertIn([0.0, 0.0], [n[:2] for n in main["nodes"]])

    def test_single_lane_arm_is_an_arm_too(self):
        junction = self._t_junction()
        junction[2] = _arm("south", 3, 12, [(0, -60), (0, -30), (0, -15)], raw=6, combined=False,
                           one_way=True, lanes_left=0, lanes_right=1)
        out, report = resolve_junctions(junction)
        self.assertEqual((report["junctions_resolved"], report["stems_on_road"]), (1, 1))
        stem = [c for c in out if c.get("combined_junction") and "stem" in c["id"]][0]
        self.assertEqual((stem["one_way"], stem["lanes_left"], stem["lanes_right"]), (True, 0, 1))

    def test_one_way_arms_pair_entering_with_leaving_only(self):
        kw = dict(one_way=True, lanes_left=0, lanes_right=2)
        west = _arm("west", 1, 10, [(-60, 0), (-10, 0)], **kw)            # flow into the junction
        east = _arm("east", 11, 2, [(10, 0), (60, 0)], **kw)              # flow out of it
        south = _arm("south", 3, 12, [(0, -60), (0, -15)], raw=6, **kw)   # second entering arm
        cons = [_connector("c1", 10, 11), _connector("c2", 12, 11)]
        out, report = resolve_junctions([west, east, south] + cons)
        through = [c for c in out if "through" in c["id"]][0]
        self.assertEqual((through["one_way"], through["lanes_left"], through["lanes_right"]), (True, 0, 2))
        self.assertEqual([n[:2] for n in through["nodes"]][0], [-10.0, 0.0])      # flows west -> east
        self.assertEqual(report["stems_on_road"], 1)
        # two entering arms facing each other must not become one through road
        east_in = _arm("east", 2, 11, [(60, 0), (10, 0)], **kw)
        out, report = resolve_junctions([west, east_in, south, _connector("c1", 10, 11), _connector("c2", 12, 11)])
        self.assertEqual((report["through_roads"], report["arms_meeting_at_middle"]), (0, 3))

    def test_runoff_ends_get_a_pair_and_sidewalk_connectors_go(self):
        junction = self._t_junction()
        run = dict(source_type=3, combined=False, one_way=True, lanes_left=0, lanes_right=1)
        junction += [
            _arm("run_w", 21, 10, [(-60, 12), (-30, 12), (-10, 12)], raw=1, segment_index=101, **run),
            _arm("run_e", 11, 22, [(10, 12), (30, 12), (60, 12)], raw=1, segment_index=102, **run),
            _arm("run_s", 23, 12, [(12, -60), (12, -30), (12, -15)], raw=1, segment_index=103, **run)]
        out, report = resolve_junctions(junction)
        pieces = [c for c in out if c.get("runoff_extension")]
        self.assertEqual(len(pieces), 2)                                   # west-east pair + south end onto the east arm
        pair = [c for c in pieces if c["nodes"][0][1] == 12.0 and c["nodes"][-1][1] == 12.0][0]
        self.assertEqual(sorted([pair["nodes"][0][0], pair["nodes"][-1][0]]), [-10.0, 10.0])
        self.assertTrue(all((c["one_way"], c["lanes_left"], c["lanes_right"]) == (False, 1, 1) for c in pieces))
        self.assertEqual([c["id"] for c in out if c.get("source_decision")], [])    # walk connector dropped
        self.assertEqual(report["runoff_ends_without_pair"], 0)

    def test_connection_radius_keeps_big_junctions_unchanged(self):
        junction = self._t_junction()                      # through chord is 20 m, stem 15 m
        out, report = resolve_junctions(junction, connect_radius=50)
        self.assertEqual(report["junctions_resolved"], 1)
        out, report = resolve_junctions(junction, connect_radius=8)        # arms meet 11 m from their middle
        self.assertEqual(report["junctions_resolved"], 0)
        self.assertEqual(report["skipped_junctions"], {"an arm is farther than the connection radius": 1})
        self.assertEqual(len(out), len(junction))                                  # connectors kept, nothing added
        self.assertEqual(sum(1 for c in out if c.get("source_decision")), 4)

    def test_runoff_connections_stay_within_the_radius(self):
        junction = self._t_junction()
        run = dict(source_type=3, combined=False, one_way=True, lanes_left=0, lanes_right=1)
        junction += [_arm("run_w", 21, 10, [(-60, 12), (-30, 12), (-10, 12)], raw=1, segment_index=101, **run),
                     _arm("run_e", 11, 22, [(10, 12), (30, 12), (60, 12)], raw=1, segment_index=102, **run)]
        out, report = resolve_junctions(junction, connect_radius=50)
        self.assertEqual(sum(1 for c in out if c.get("runoff_extension")), 1)       # 20 m pair
        out, report = resolve_junctions(junction, connect_radius=19)
        self.assertEqual(sum(1 for c in out if c.get("runoff_extension")), 0)       # 20 m is beyond the radius
        self.assertEqual(report["runoff_ends_without_pair"], 2)

    def test_unresolved_junction_runoff_runs_onto_the_arm_next_to_it(self):
        junction = self._t_junction()
        run = dict(source_type=3, combined=False, one_way=True, lanes_left=0, lanes_right=1)
        junction.append(_arm("run_s", 23, 12, [(12, -60), (12, -30), (12, -6)], raw=1, segment_index=103, **run))
        out, report = resolve_junctions(junction, connect_radius=8)           # too big for the radius: connectors stay
        self.assertEqual(report["junctions_resolved"], 0)
        pieces = [c for c in out if c.get("runoff_extension")]
        self.assertEqual(len(pieces), 1)
        self.assertEqual(report["runoff_ends_without_pair"], 0)
        self.assertEqual([round(v, 3) for v in pieces[0]["nodes"][-1][:2]], [12.0, 0.0])   # on the east arm

    def test_median_connector_is_dropped_in_an_unresolved_junction_too(self):
        junction = self._t_junction()
        junction.append(_arm("gap", 10, 12, [(0, 0), (1, 1)], road_id=-1, source_decision=True, combined=False,
                             source_type=4, one_way=True, lanes_left=0, lanes_right=1))
        out, report = resolve_junctions(junction, connect_radius=8)
        self.assertEqual(report["junctions_resolved"], 0)
        self.assertNotIn("gap", [c["id"] for c in out])
        self.assertEqual(sum(1 for c in out if c.get("source_decision")), 4)         # traffic and walk connectors stay

    def test_width_tolerance_keeps_small_width_wobbles_out(self):
        line = [[0.0, 3.0 * i, 0.0, 16.0 + (0.8 if i == 5 else 0.0)] for i in range(11)]
        self.assertGreater(len(thin_nodes(line, 0.3, 0.0)), 2)                      # 0.4 m half-difference > 0.3 m
        self.assertEqual(len(thin_nodes(line, 0.3, 0.0, width_tolerance=0.5)), 2)

    def test_width_scale_changes_every_node_and_keeps_input(self):
        road = {"id": "r", "nodes": [[0.0, 0.0, 0.0, 8.0], [0.0, 10.0, 0.0, 12.0]], "width_start": 8.0, "width_end": 12.0}
        out = scale_road_widths([road], 0.9)
        self.assertEqual([n[3] for n in out[0]["nodes"]], [7.2, 10.8])
        self.assertEqual((out[0]["width_start"], out[0]["width_end"]), (7.2, 10.8))
        self.assertEqual(road["nodes"][0][3], 8.0)
        self.assertIs(scale_road_widths([road], 1.0)[0], road)
        with self.assertRaises(ValueError):
            scale_road_widths([road], 0)

    def test_merged_road_ids_stay_unique(self):
        out, _ = resolve_junctions(self._t_junction())
        ids = [r["id"] for r in merge_connected_corridors(out)]
        self.assertEqual(len(ids), len(set(ids)))

    def test_four_way_roads_share_one_crossing_node(self):
        arms = [_arm("w", 1, 10, [(-60, 0), (-10, 0)]), _arm("e", 11, 2, [(10, 0), (60, 0)]),
                _arm("s", 3, 12, [(0, -60), (0, -10)], raw=6), _arm("n", 13, 4, [(0, 10), (0, 60)], raw=6)]
        cons = [_connector("c1", 10, 11), _connector("c2", 12, 13), _connector("c3", 11, 12)]
        out, report = resolve_junctions(arms + cons)
        self.assertEqual((report["through_roads"], report["crossings"], report["stems_on_road"]), (2, 1, 0))
        centres = [[n[:2] for n in c["nodes"] if abs(n[0]) < 1e-6 and abs(n[1]) < 1e-6]
                   for c in out if c.get("combined_junction")]
        self.assertEqual([len(c) for c in centres], [1, 1])

    def test_uncombined_arm_keeps_its_connectors(self):
        junction = self._t_junction()
        junction[2] = _arm("south", 3, 12, [(0, -60), (0, -30), (0, -15)], raw=6, combined=False)
        junction.append(_arm("south2", 3, 12, [(4, -60), (4, -30), (4, -15)], raw=7, combined=False))
        out, report = resolve_junctions(junction)
        self.assertEqual(len(out), len(junction))
        self.assertEqual(report["skipped_junctions"], {"an arm is not a combined road": 1})


def _road(name, source_type, points, width=3.0):
    return {"id": name, "source_type": source_type, "nodes": [[x, y, 0.0, width] for x, y in points]}


class ShouldersConnectShortcutsTests(unittest.TestCase):
    def test_shoulder_is_kept_where_it_connects_a_shortcut_to_a_main_road(self):
        roads = [_road("cut", 10, [(40, 0), (60, 0)]),
                 _road("main", 1, [(-300, 4), (300, 4)], width=8.0),
                 _road("shoulder", 3, [(-200, 3), (0, 3), (100, 3), (300, 3)], width=2.0)]
        out, report = keep_shoulders_near_shortcuts(roads, extend=10.0)
        by_id = {r["id"]: r for r in out}
        self.assertIn("main", by_id)                                              # other roads are untouched
        piece = by_id["shoulder_near0"]
        xs = [n[0] for n in piece["nodes"]]
        self.assertTrue(26 < min(xs) < 34 and 66 < max(xs) < 74)                  # shortcut contact 40..60 plus 10 m each side
        self.assertEqual((report["trimmed"], report["dropped"], report["kept_whole"]), (1, 0, 0))

    def test_shoulder_that_never_reaches_a_main_road_or_a_shortcut_is_dropped(self):
        roads = [_road("cut", 10, [(0, 0), (100, 0)]),
                 _road("lonely", 3, [(0, 2), (100, 2)], width=2.0),          # touches the shortcut, no main road anywhere
                 _road("far", 3, [(0, 500), (100, 500)], width=2.0),
                 _road("main", 1, [(0, 600), (100, 600)], width=8.0)]
        out, report = keep_shoulders_near_shortcuts(roads)
        self.assertEqual(sorted(r["id"] for r in out), ["cut", "main"])
        self.assertEqual(report["dropped"], 2)

    def test_a_long_contact_stays_whole_and_sidewalks_count_as_shoulders(self):
        roads = [_road("cut", 10, [(0, 0), (100, 0)]), _road("main", 1, [(0, 4), (100, 4)], width=8.0),
                 _road("walk", 2, [(10, 2), (90, 2)], width=2.0)]
        out, report = keep_shoulders_near_shortcuts(roads)
        self.assertIs([r for r in out if r["id"] == "walk"][0], roads[2])
        self.assertEqual(report["kept_whole"], 1)

    def test_contact_is_judged_on_the_scaled_widths(self):
        roads = [_road("cut", 10, [(0, 0), (100, 0)], width=4.0), _road("main", 1, [(0, 3), (100, 3)], width=8.0),
                 _road("walk", 2, [(0, 5), (100, 5)], width=4.0)]
        self.assertEqual(keep_shoulders_near_shortcuts(roads)[1]["dropped"], 0)                    # 2 + 2 + 1.5 reaches the 5 m gap
        self.assertEqual(keep_shoulders_near_shortcuts(roads, width_scale=0.5)[1]["dropped"], 1)   # narrower roads no longer touch

    def test_two_shortcuts_give_two_pieces(self):
        roads = [_road("a", 10, [(0, 0), (10, 0)]), _road("b", 10, [(400, 0), (410, 0)]),
                 _road("main", 1, [(-100, 4), (600, 4)], width=8.0), _road("walk", 2, [(-100, 2), (500, 2)], width=2.0)]
        out, report = keep_shoulders_near_shortcuts(roads, extend=6.0)
        self.assertEqual([r["id"] for r in out if r["id"].startswith("walk")], ["walk_near0", "walk_near1"])


class MergeSignatureRoleTests(unittest.TestCase):
    def test_shoulder_median_and_lane_never_share_a_chain_even_with_the_same_drivability(self):
        def sig(source_type):
            return _corridor_merge_signature({"source_type": source_type, "drivability": 1.0, "one_way": True,
                                              "lanes_left": 0, "lanes_right": 1})
        self.assertNotEqual(sig(3), sig(1))
        self.assertNotEqual(sig(3), sig(10))
        self.assertNotEqual(sig(4), sig(1))
        self.assertEqual(sig(2), sig(3))                 # sidewalk and shoulder are the same role
        self.assertEqual(sig(1), sig(10))                # traffic and general road may still continue into each other


class PreserveWindingTests(unittest.TestCase):
    def test_reverse_lane_goes_back_to_source_winding_with_lanes_left(self):
        road = {"id": "r", "direction": "reverse", "one_way": True, "lanes_left": 0, "lanes_right": 1,
                "nodes": [[30, 0, 0, 4], [15, 0, 0, 5], [0, 0, 0, 6]], "width_start": 4, "width_end": 6}
        out, count = preserve_winding([road])
        self.assertEqual(count, 1)
        fixed = out[0]
        self.assertEqual([n[0] for n in fixed["nodes"]], [0, 15, 30])
        self.assertEqual((fixed["lanes_left"], fixed["lanes_right"], fixed["direction"]), (1, 0, "forward"))
        self.assertEqual((fixed["width_start"], fixed["width_end"]), (6, 4))
        self.assertEqual(road["nodes"][0][0], 30)                       # the input is not modified

    def test_forward_two_way_and_combined_roads_are_left_alone(self):
        roads = [{"id": "a", "direction": "forward", "one_way": True, "lanes_left": 0, "lanes_right": 1, "nodes": [[0, 0, 0, 4], [9, 0, 0, 4]]},
                 {"id": "b", "direction": "reverse", "one_way": False, "lanes_left": 1, "lanes_right": 1, "nodes": [[0, 0, 0, 4], [9, 0, 0, 4]]},
                 {"id": "c", "direction": "forward", "one_way": True, "lanes_left": 2, "lanes_right": 0, "nodes": [[0, 0, 0, 8], [9, 0, 0, 8]]}]
        out, count = preserve_winding(roads)
        self.assertEqual(count, 0)
        self.assertEqual(out, roads)


class TrafficCorridorTests(unittest.TestCase):
    def test_side_and_type_are_derived_from_middle_zone(self):
        p = FakeProfile([3, 1, 1], [4, 4, 4], [6, 2, 6], 1)
        self.assertEqual(get_oriented_zone(p, 0, False)["side"], "left")
        self.assertEqual(get_oriented_zone(p, 1, False)["side"], "right")
        self.assertEqual(get_oriented_zone(p, 1, False)["type"], 1)

    def test_inversion_mirrors_zone_order(self):
        p = FakeProfile([3, 1, 10], [4, 4, 4], [6, 2, 6], 1)
        inverted = get_oriented_zone(p, 0, True)
        self.assertEqual(inverted["raw_index"], 2)
        self.assertEqual(inverted["type"], 10)
        self.assertEqual(inverted["side"], "left")

    def test_directions_and_beamng_transform(self):
        result = build_corridors(make_network(), merged_type_rules(), {}, vertical_offset=150)
        by_zone = {c["zone_index"]: c for c in result["corridors"]}
        self.assertEqual(by_zone[0]["direction"], "reverse")
        self.assertEqual(by_zone[1]["direction"], "forward")
        # Corridor node heights remain unoffset. The serializer applies +150 m
        # once, so the game-facing value is explicit and cannot be omitted by a stale preview.
        self.assertEqual(by_zone[1]["nodes"][0][2], 0.0)
        self.assertEqual(by_zone[1]["nodes"][0][0], 0.0)

    def test_local_override_wins_over_pattern(self):
        network = make_network()
        pattern = {
            "name": "low-priority", "enabled": True,
            "match": {"zone_index": 1, "start_type": 1, "end_type": 1,
                      "start_zone_count": 3, "end_zone_count": 3,
                      "min_start_width": 3.9, "max_start_width": 4.1},
            "overrides": {"drivability": 0.4},
        }
        result = build_corridors(network, merged_type_rules(),
                                 {"segment:0:zone:1": {"drivability": 1.0}}, patterns=[pattern])
        self.assertEqual(result["corridors"][1]["drivability"], 1.0)
        result2 = build_corridors(network, merged_type_rules(), {}, patterns=[pattern])
        self.assertEqual(result2["corridors"][1]["drivability"], 0.4)

    def test_exported_zone_node_widths_vary_per_node(self):
        start_profile = FakeProfile([3, 1, 1], [4, 4, 4], [6, 2, 6], 1)
        end_profile = FakeProfile([3, 1, 1], [5, 5, 5], [7, 2.5, 7], 1)
        network = SimpleNamespace(
            nodes=[FakeNode((0.0, 2.5, 0.0), 0, profile=0),
                   FakeNode((0.0, 5.0, 30.0), 1, profile=1)],
            profiles=[start_profile, end_profile],
            segments=[FakeSegment(flags=1 << 8, start=0, end=1, index=0, road_id=0)],
            roads=[SimpleNamespace()],
        )
        result = build_corridors(network, merged_type_rules(), {}, vertical_offset=150.0, curve_steps=4,
                                 include_decision_connectors=True)
        lane = next(c for c in result["corridors"] if c["zone_index"] == 1)
        road = merge_connected_corridors([lane])[0]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "one_lane.jsonl"
            count = write_items_level([road], path, vertical_offset=150.0)
            entries = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(count, 1)  # not one DecalRoad per node
        self.assertEqual(len(entries[0]["nodes"]), 5)
        self.assertAlmostEqual(entries[0]["nodes"][0][3], 4.0)
        self.assertAlmostEqual(entries[0]["nodes"][-1][3], 5.0)
        self.assertAlmostEqual(entries[0]["nodes"][0][2], 152.5)
        self.assertAlmostEqual(entries[0]["nodes"][-1][2], 155.0)

    def test_decision_connectors_and_no_traffic_flags(self):
        decision = build_corridors(make_network(FLAG_DECISION), merged_type_rules(), {})
        self.assertFalse(any(c["export"] for c in decision["corridors"]))
        included = build_corridors(make_network(FLAG_DECISION), merged_type_rules(), {},
                                   include_decision_connectors=True)
        self.assertTrue(any(c["export"] for c in included["corridors"]))
        no_traffic = build_corridors(make_network(FLAG_NO_TRAFFIC), merged_type_rules(), {})
        self.assertFalse(any(c["export"] for c in no_traffic["corridors"]))
        forced = build_corridors(make_network(FLAG_NO_TRAFFIC), merged_type_rules(),
                                 {"segment:0:zone:1": {"force_export": True}})
        self.assertTrue(forced["corridors"][1]["export"])


    def test_contiguous_lane_corridors_merge_into_one_road_per_zone(self):
        profile = FakeProfile([3, 1, 1], [4, 4, 4], [6, 2, 6], 1)
        network = SimpleNamespace(
            nodes=[FakeNode((0.0, 0.0, 0.0), 0),
                   FakeNode((0.0, 0.0, 30.0), 1),
                   FakeNode((0.0, 0.0, 60.0), 2)],
            profiles=[profile],
            segments=[FakeSegment(start=0, end=1, index=0, road_id=4),
                      FakeSegment(start=1, end=2, index=1, road_id=4)],
            roads=[SimpleNamespace()],
        )
        derived = build_corridors(network, merged_type_rules(), {})
        exportable = [c for c in derived["corridors"] if c["export"]]
        roads = merge_connected_corridors(derived["corridors"])
        self.assertEqual(len(exportable), 6)
        self.assertEqual(len(roads), 3)
        self.assertTrue(all(r["merged_corridor_count"] == 2 for r in roads))
        self.assertTrue(all(len(r["nodes"]) == 3 for r in roads))
        self.assertTrue(all(len(r["source_keys"]) == 2 for r in roads))
        # The same source road/zone chain is stable across its two segment pieces.
        self.assertEqual({r["road_id"] for r in roads}, {4})
        self.assertEqual({r["zone_index"] for r in roads}, {0, 1, 2})
        from traffic_node_exporter import corridor_to_decalroad
        names = [corridor_to_decalroad(r, vertical_offset=150.0)["name"] for r in roads]
        self.assertEqual(len(names), len(set(names)))
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "chains.jsonl"
            written = write_items_level(roads, path, vertical_offset=150.0)
            objects = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(written, 3)
        self.assertTrue(all(len(obj["nodes"]) == 3 for obj in objects))

    def test_decision_connectors_are_not_stitched_into_regular_road_chains(self):
        profile = FakeProfile([3, 1, 1], [4, 4, 4], [6, 2, 6], 1)
        network = SimpleNamespace(
            nodes=[FakeNode((0.0, 0.0, 0.0), 0),
                   FakeNode((0.0, 0.0, 30.0), 1),
                   FakeNode((0.0, 0.0, 60.0), 2)],
            profiles=[profile],
            segments=[FakeSegment(start=0, end=1, index=0, road_id=4),
                      FakeSegment(flags=FLAG_DECISION, start=1, end=2, index=1, road_id=-1)],
            roads=[SimpleNamespace()],
        )
        derived = build_corridors(network, merged_type_rules(), {}, include_decision_connectors=True)
        roads = merge_connected_corridors(derived["corridors"])
        regular = [r for r in roads if r.get("road_id") == 4]
        connectors = [r for r in roads if r.get("road_id") == -1]
        self.assertEqual(len(regular), 3)
        self.assertEqual(len(connectors), 3)
        self.assertTrue(all(r["merged_corridor_count"] == 1 for r in connectors))

    def test_topological_join_does_not_depend_on_offset_endpoint_distance(self):
        defaults = {
            "road_id": 4, "classification": "Traffic", "drivability": 1.0,
            "one_way": True, "auto_junction": True, "auto_lanes": False,
            "lanes_left": 0, "lanes_right": 1, "over_objects": True,
            "export": True, "excluded": False, "source_decision": False,
            "source_no_traffic": False, "direction": "forward",
        }
        first = dict(defaults, id="first", source_key="first", segment_index=10,
                     source_segment_id=10, zone_index=1, zone_raw_start=1, zone_raw_end=1,
                     node_start=0, node_end=1,
                     nodes=[[0, 0, 0, 4], [10, 0, 0, 4]])
        # Same shared source node/raw profile zone; different logical zone label
        # and a >0.75 m lateral endpoint discrepancy due to per-segment tangent.
        second = dict(defaults, id="second", source_key="second", segment_index=11,
                      source_segment_id=11, road_id=5, zone_index=7, zone_raw_start=1, zone_raw_end=1,
                      node_start=1, node_end=2,
                      nodes=[[10.95, 0.4, 0, 4], [20, 3, 0, 4]])
        roads = merge_connected_corridors([first, second])
        self.assertEqual(len(roads), 1)
        self.assertEqual(roads[0]["merged_corridor_count"], 2)
        self.assertEqual(roads[0]["source_segment_indices"], [10, 11])
        self.assertEqual(roads[0]["source_road_ids"], [4, 5])
        self.assertEqual(len(roads[0]["nodes"]), 3)
        self.assertAlmostEqual(roads[0]["nodes"][1][0], 10.475)
        self.assertAlmostEqual(roads[0]["nodes"][1][1], 0.2)

    def test_zone_endpoint_harmonization_aligns_approach_and_turn_connector(self):
        common = {"export": True, "excluded": False, "direction": "forward",
                  "node_start": 0, "node_end": 1,
                  "zone_raw_start": 1, "zone_raw_end": 2,
                  "zone_index": 1,
                  "nodes": [[0, 0, 0, 4], [10.8, 0.3, 2, 4]]}
        approach = dict(common, road_id=4, source_decision=False, source_key="approach")
        turn = dict(common, road_id=-1, source_decision=True, source_key="turn",
                    node_start=1, node_end=2,
                    zone_raw_start=2, zone_raw_end=1,
                    nodes=[[10.0, 0, 2, 4], [14, 5, 2, 4]])
        info = harmonize_zone_endpoints([approach, turn])
        self.assertEqual(info["snapped_endpoint_groups"], 1)
        for k in range(3):
            self.assertAlmostEqual(approach["nodes"][-1][k], turn["nodes"][0][k])
        self.assertEqual(approach["nodes"][-1][3], 4)
        self.assertEqual(turn["nodes"][0][3], 4)

    def test_over_objects_toggle_is_applied_by_exporter(self):
        result = build_corridors(make_network(), merged_type_rules(), {})
        with tempfile.TemporaryDirectory() as temp:
            on_path = Path(temp) / "over_on.jsonl"
            off_path = Path(temp) / "over_off.jsonl"
            write_items_level(result["corridors"], on_path, over_objects=True)
            write_items_level(result["corridors"], off_path, over_objects=False)
            on_object = json.loads(on_path.read_text().splitlines()[0])
            off_object = json.loads(off_path.read_text().splitlines()[0])
            self.assertIs(on_object["overObjects"], True)
            self.assertIs(off_object["overObjects"], False)

    def test_exclusion_and_export_format(self):
        result = build_corridors(make_network(), merged_type_rules(),
                                 {"segment:0:zone:1": {"excluded": True}})
        self.assertFalse(result["corridors"][1]["export"])
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "items.level.json"
            count = write_items_level(result["corridors"], out, over_objects=True)
            lines = out.read_text(encoding="utf-8").splitlines()
            self.assertEqual(count, 2)
            self.assertEqual(len(lines), count)  # line-delimited JSON, not a JSON array
            self.assertTrue(all(json.loads(line)["class"] == "DecalRoad" for line in lines))
            obj = json.loads(lines[0])
            self.assertEqual(obj["nodes"][0][2], 150.0)
            self.assertTrue(obj["autoJunction"])
            self.assertTrue(obj["overObjects"])
            self.assertTrue(obj["improvedSpline"])
            # The offset is applied during serialization, not baked into the
            # preview nodes; opting out is possible for controlled comparisons.
            zero = Path(temp) / "zero_offset.jsonl"
            write_items_level(result["corridors"], zero, vertical_offset=0.0)
            zero_obj = json.loads(zero.read_text().splitlines()[0])
            self.assertEqual(zero_obj["nodes"][0][2], 0.0)
            manifest = Path(temp) / "source_manifest.json"
            write_source_manifest(result["corridors"], manifest, vertical_offset=150.0)
            manifest_data = json.loads(manifest.read_text())
            self.assertEqual(len(manifest_data["corridors"]), 3)
            self.assertEqual(manifest_data["vertical_offset"], 150.0)
            self.assertTrue(manifest_data["vertical_offset_applied_during_serialization"])


class GeneralRoadExtensionTests(unittest.TestCase):
    def test_free_end_runs_along_its_heading_to_the_traffic_road_and_past_its_middle(self):
        roads = [_road("main", 1, [(-200, 50), (200, 50)], width=8.0),
                 _road("cut", 10, [(0, -100), (0, 0)], width=4.0)]
        out, report = extend_general_roads(roads, overlap=2.0)
        ext = [r for r in out if r.get("general_extension") and r["id"] == "cut_ext1"]
        self.assertEqual(len(ext), 1)                                              # the far end (y = -100) is free too
        self.assertEqual([n[:2] for n in (ext[0]["nodes"][0], ext[0]["nodes"][-1])], [[0.0, 0.0], [0.0, 52.0]])
        self.assertIn([0.0, 50.0], [n[:2] for n in ext[0]["nodes"]])             # a node on the traffic road's middle
        self.assertEqual((report["free_ends"], report["extended"], report["extended_along_heading"]), (2, 2, 1))
        self.assertEqual(out[:2], roads)                                           # source roads are untouched

    def test_heading_that_misses_goes_to_the_nearest_point(self):
        roads = [_road("main", 1, [(-200, 50), (200, 50)], width=8.0),
                 _road("cut", 10, [(60, 0), (100, 10)], width=4.0)]                # heads away from the road, 40 m off it
        out, report = extend_general_roads(roads, overlap=1.0)
        ends = [r for r in out if r.get("general_extension")]
        self.assertGreaterEqual(report["extended_to_nearest_point"], 1)
        self.assertTrue(all(abs(r["nodes"][-1][1] - 51.0) < 1e-6 or abs(r["nodes"][0][1] - 51.0) < 1e-6 for r in ends))

    def test_ends_that_already_touch_a_road_are_left_alone(self):
        roads = [_road("main", 1, [(-200, 0), (200, 0)], width=8.0),
                 _road("cut", 10, [(0, 3), (0, 100)], width=4.0)]                  # start overlaps the traffic road's width
        out, report = extend_general_roads(roads)
        self.assertEqual(report["already_touching"], 1)
        self.assertFalse([r for r in out if r["id"] == "cut_ext0"])

    def test_extension_at_the_start_keeps_the_roads_winding_and_lanes(self):
        cut = _road("cut", 10, [(0, 0), (0, 100)], width=4.0)
        cut.update({"lanes_left": 0, "lanes_right": 2, "one_way": True})
        roads = [_road("main", 1, [(-200, -50), (200, -50)], width=8.0), cut]
        out, _ = extend_general_roads(roads, overlap=2.0)
        ext = [r for r in out if r["id"] == "cut_ext0"][0]
        self.assertEqual([n[:2] for n in (ext["nodes"][0], ext["nodes"][-1])], [[0.0, -52.0], [0.0, 0.0]])   # runs along the road's winding
        self.assertEqual((ext["lanes_left"], ext["lanes_right"], ext["one_way"]), (0, 2, True))

    def test_extension_width_tapers_to_the_traffic_road_and_never_grows(self):
        roads = [_road("main", 1, [(-200, 50), (200, 50)], width=8.0), _road("cut", 10, [(0, -100), (0, 0)], width=30.0)]
        ext = [r for r in extend_general_roads(roads)[0] if r["id"] == "cut_ext1"][0]
        self.assertEqual((ext["nodes"][0][3], ext["nodes"][-1][3]), (30.0, 8.0))
        roads = [_road("main", 1, [(-200, 50), (200, 50)], width=8.0), _road("cut", 10, [(0, -100), (0, 0)], width=3.0)]
        ext = [r for r in extend_general_roads(roads)[0] if r["id"] == "cut_ext1"][0]
        self.assertEqual(max(n[3] for n in ext["nodes"]), 3.0)

    def test_nothing_in_reach_means_no_extension(self):
        roads = [_road("main", 1, [(0, 900), (100, 900)], width=8.0), _road("cut", 10, [(0, 0), (0, 50)], width=4.0)]
        out, report = extend_general_roads(roads, max_reach=100.0)
        self.assertEqual((len(out), report["no_traffic_road_in_reach"]), (2, 2))

    def _connector(self, node_start, node_end, points, source_type=3, width=2.0):
        c = _road("link", source_type, points, width=width)
        c.update({"node_start": node_start, "node_end": node_end, "source_decision": True})
        return c

    def test_extension_follows_the_shoulder_connector_of_its_end_node_without_corners(self):
        main = _road("main", 1, [(-200, 0), (200, 0)], width=8.0)
        cut = _road("cut", 10, [(60, 40), (60, 60)], width=3.0)                    # free start end at (60, 40), pointing down
        cut.update({"node_start": 7, "node_end": 8})
        link = self._connector(3, 7, [(46, 4), (52, 12), (58, 25), (63, 37)])      # gentle curve from the main road to node 7,
        plain, _ = extend_general_roads([main, cut])                               # starting 3 m beside the road end
        followed, report = extend_general_roads([main, cut], guides=[link])
        straight = [r for r in plain if r["id"] == "cut_ext0"][0]
        guided = [r for r in followed if r["id"] == "cut_ext0"][0]
        self.assertEqual(report["followed_connector_path"], 1)
        self.assertNotEqual(guided["nodes"], straight["nodes"])
        self.assertEqual(guided["nodes"][-1][:2], [60.0, 40.0])                    # an extension at the road's start runs towards it
        self.assertLessEqual(_max_turn(guided["nodes"]), 30.0)                     # no sharp angles anywhere
        self.assertAlmostEqual(guided["nodes"][1][1], 0.0, delta=0.5)              # ends on the main road's middle ...
        self.assertLess(guided["nodes"][0][1], -1.0)                               # ... and runs on past it (the overlap)

    def test_connector_that_turns_back_against_the_roads_direction_is_not_used(self):
        main = _road("main", 1, [(-200, 0), (200, 0)], width=8.0)
        cut = _road("cut", 10, [(60, 40), (60, 60)], width=3.0)                    # extension heads down (0, -1)
        cut.update({"node_start": 7, "node_end": 8})
        sets_off_backwards = self._connector(3, 7, [(60, 2), (90, 20), (70, 60), (60, 40)])      # leaves the end upwards
        arrives_backwards = self._connector(3, 7, [(55, -6), (60, -12), (70, -10), (62, 20), (60, 40)])   # reaches the road heading up
        for link in (sets_off_backwards, arrives_backwards):
            out, report = extend_general_roads([main, cut], guides=[link])
            self.assertEqual(report["followed_connector_path"], 0)
        good = self._connector(3, 7, [(46, 4), (52, 12), (58, 25), (63, 37)])
        out, report = extend_general_roads([main, cut], guides=[sets_off_backwards, good])
        self.assertEqual(report["followed_connector_path"], 1)                    # the connector that goes the same way is picked

    def test_hooked_connector_is_followed_only_up_to_the_hook_then_goes_straight_into_the_road(self):
        main = _road("main", 1, [(-200, 0), (200, 0)], width=8.0)
        cut = _road("cut", 10, [(60, 40), (60, 60)], width=3.0)                    # extension heads down (0, -1)
        cut.update({"node_start": 7, "node_end": 8})
        hook = self._connector(7, 3, [(60, 40), (63, 28), (68, 15), (64, 5), (52, 8), (46, 14)])   # sets off right, then hooks back
        out, report = extend_general_roads([main, cut], guides=[hook])
        ext = [r for r in out if r["id"] == "cut_ext0"][0]
        self.assertEqual((report["followed_connector_path"], report["connector_unhooked"]), (1, 1))
        self.assertTrue(all(n[0] > 55.0 for n in ext["nodes"]))                    # never swings round the hook (x down to 46)
        self.assertEqual(ext["nodes"][-1][:2], [60.0, 40.0])
        self.assertAlmostEqual(ext["nodes"][1][1], 0.0, delta=0.5)                 # ends on the main road's middle ...
        self.assertLess(ext["nodes"][0][1], -1.0)                                  # ... and runs on past it, for autoJunction
        self.assertLessEqual(_max_turn(ext["nodes"]), 30.0)

    def test_connector_whose_path_would_wrinkle_the_road_is_not_used(self):
        main = _road("main", 1, [(-200, 0), (200, 0)], width=8.0)
        cut = _road("cut", 10, [(60, 40), (60, 60)], width=3.0)
        cut.update({"node_start": 7, "node_end": 8})
        link = self._connector(3, 7, [(40, 2), (40, 40), (60, 40)])                # right-angle corner
        out, report = extend_general_roads([main, cut], guides=[link])
        self.assertEqual((report["followed_connector_path"], report["connector_too_sharp"]), (0, 1))
        self.assertEqual(report["extended_along_heading"] + report["extended_to_nearest_point"], 2)   # both ends stay straight

    def test_connector_is_ignored_when_it_is_elsewhere_leads_nowhere_or_is_too_long(self):
        main = _road("main", 1, [(-200, 0), (200, 0)], width=8.0)
        cut = _road("cut", 10, [(60, 40), (60, 60)], width=3.0)
        cut.update({"node_start": 7, "node_end": 8})
        other_node = self._connector(3, 9, [(40, 2), (40, 40), (60, 40)])          # belongs to another node
        elsewhere = self._connector(3, 7, [(300, 300), (300, 340)])                # starts far from this end
        nowhere = self._connector(3, 7, [(60, 40), (60, 90)])                      # leads away from every traffic road
        long = self._connector(3, 7, [(60, 40), (60, 100), (-100, 100), (-100, 2)])  # reaches the road, but a long way round
        for link in (other_node, elsewhere, nowhere, long):
            out, report = extend_general_roads([main, cut], guides=[link])
            self.assertEqual(report["followed_connector_path"], 0)

    def test_shoulder_guides_keep_only_sidewalk_and_shoulder_decision_connectors(self):
        zones = [{"id": "lane", "source_type": 1, "source_decision": True, "nodes": [[0, 0, 0, 3], [10, 0, 0, 3]]},
                 {"id": "plain", "source_type": 3, "source_decision": False, "nodes": [[0, 0, 0, 3], [10, 0, 0, 3]]},
                 {"id": "link", "source_type": 3, "source_decision": True, "nodes": [[0, 0, 0, 3], [10, 0, 0, 3]]},
                 {"id": "walk", "source_type": 2, "source_decision": True, "nodes": [[0, 0, 0, 3], [10, 0, 0, 3]]}]
        self.assertEqual([g["id"] for g in shoulder_guides(zones)], ["link", "walk"])
        self.assertEqual(len(zones), 4)

class DropShouldersTests(unittest.TestCase):
    def test_sidewalk_and_shoulder_zones_are_left_out(self):
        zones = [{"id": str(t), "source_type": t} for t in (1, 2, 3, 4, 10)]
        kept, dropped = drop_shoulders(zones)
        self.assertEqual(([z["id"] for z in kept], dropped), (["1", "4", "10"], 2))
        self.assertEqual(len(zones), 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
