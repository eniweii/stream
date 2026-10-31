"""Unit tests for the enhanced traffic topology and lane-pair helpers."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "region"))

from traffic_topology import merge_connected_corridors, find_adjacent_lane_pairs, merge_adjacent_lane_pairs


def corridor(key, start, end, z0, z1, *, segment, zone, road=4, width=4.0,
             direction="forward", source_decision=False, nodes=None):
    return {
        "id": key, "source_key": key, "segment_index": segment, "source_segment_id": segment,
        "road_id": road, "node_start": start, "node_end": end,
        "zone_raw_start": zone, "zone_raw_end": zone, "zone_index": zone,
        "source_segment_indices": [segment], "source_segment_ids": [segment],
        "source_zone_indices": [zone], "source_road_ids": [road],
        "direction": direction, "one_way": True, "auto_junction": True, "auto_lanes": False,
        "lanes_left": 0, "lanes_right": 1, "drivability": 1.0, "over_objects": True,
        "source_decision": source_decision, "source_no_traffic": False, "force_export": False,
        "excluded": False, "export": True, "merged_corridor_count": 1,
        "nodes": nodes or [[z0, 0.0, 0.0, width], [z1, 0.0, 0.0, width]],
    }


class TrafficTopologyTests(unittest.TestCase):
    def test_unique_topological_continuation_merges_across_road_id(self):
        a = corridor("a", 0, 1, 0, 10, segment=10, zone=1, road=4)
        b = corridor("b", 1, 2, 10.8, 20, segment=11, zone=1, road=5)
        roads, diagnostics = merge_connected_corridors([a, b], return_diagnostics=True)
        self.assertEqual(len(roads), 1)
        self.assertEqual(roads[0]["source_segment_indices"], [10, 11])
        self.assertEqual(len(roads[0]["nodes"]), 3)
        self.assertEqual(diagnostics["summary"]["link_count"], 1)
        self.assertEqual(diagnostics["by_source_key"]["a"]["outgoing"]["status"], "joined")

    def test_two_equal_successors_are_reported_not_guessed(self):
        a = corridor("a", 0, 1, 0, 10, segment=10, zone=1)
        b = corridor("b", 1, 2, 10, 20, segment=11, zone=1)
        c = corridor("c", 1, 3, 10, 20, segment=12, zone=1)
        roads, diagnostics = merge_connected_corridors([a, b, c], return_diagnostics=True)
        self.assertEqual(diagnostics["by_source_key"]["a"]["outgoing"]["status"], "ambiguous_branch")
        self.assertGreaterEqual(len(roads), 2)

    def test_multiple_predecessors_do_not_drop_every_link(self):
        a = corridor("a", 0, 2, 0, 10, segment=10, zone=1, nodes=[[0, 0, 0, 4], [10, 0, 0, 4]])
        b = corridor("b", 1, 2, 0, 10, segment=11, zone=1, nodes=[[0, 0.5, 0, 4], [10, 3, 0, 4]])
        c = corridor("c", 2, 3, 10, 20, segment=12, zone=1, nodes=[[10.1, 0, 0, 4], [20, 0, 0, 4]])
        roads, diagnostics = merge_connected_corridors([a, b, c], return_diagnostics=True)
        self.assertEqual(diagnostics["summary"]["link_count"], 1)
        self.assertTrue(any("c" in r.get("source_keys", []) for r in roads))

    def test_adjacent_same_direction_lanes_combine_to_centerline(self):
        a = corridor("left", 0, 1, 0, 10, segment=7, zone=0,
                     nodes=[[0, 2, 0, 4], [5, 2, 0, 4], [10, 2, 0, 4]])
        b = corridor("right", 0, 1, 0, 10, segment=7, zone=1,
                     nodes=[[0, -2, 0, 4], [5, -2, 0, 4], [10, -2, 0, 4]])
        # Roads as consumed by lane-pairing are completed chains with source arrays.
        a.update(id="lane_a", source_segment_indices=[7], source_zone_indices=[0])
        b.update(id="lane_b", source_segment_indices=[7], source_zone_indices=[1])
        pairs = find_adjacent_lane_pairs([a, b])
        self.assertEqual(len(pairs), 1)
        self.assertTrue(pairs[0]["accepted"], pairs[0]["reason"])
        merged = merge_adjacent_lane_pairs([a, b], pairs)
        self.assertEqual(len(merged), 1)
        self.assertTrue(merged[0]["is_lane_pair"])
        self.assertEqual(merged[0]["lanes_left"], 1)
        self.assertEqual(merged[0]["lanes_right"], 1)
        self.assertAlmostEqual(merged[0]["nodes"][1][1], 0.0)
        self.assertAlmostEqual(merged[0]["nodes"][1][3], 8.0)

    def test_opposite_directions_are_not_paired(self):
        a = corridor("a", 0, 1, 0, 10, segment=7, zone=0)
        b = corridor("b", 1, 0, 10, 0, segment=7, zone=1, direction="reverse",
                     nodes=[[10, 3, 0, 4], [0, 3, 0, 4]])
        a.update(id="lane_a", source_segment_indices=[7], source_zone_indices=[0])
        b.update(id="lane_b", source_segment_indices=[7], source_zone_indices=[1])
        pairs = find_adjacent_lane_pairs([a, b])
        self.assertEqual(len(pairs), 1)
        self.assertFalse(pairs[0]["accepted"])


if __name__ == "__main__":
    unittest.main()
