"""Synthetic tests for the standalone traffic corridor converter (no game files needed)."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from traffic_corridors import (build_corridors, merged_type_rules, get_oriented_zone,
                                merge_connected_corridors, harmonize_zone_endpoints,
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
