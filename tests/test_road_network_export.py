"""Synthetic tests for road_network.json and the lane helpers (no game files needed)."""
import json
import pathlib
import struct
import sys
import unittest

_root = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(_root / d) for d in ("common", "region")]

from nfs_carp_parser import (RoadNetwork, RoadNode, RoadProfile, Road, RoadSegment,   # noqa: E402
                             segment_flag_names)
import export_road_network   # noqa: E402
import road_lanes            # noqa: E402


def lane(lane_type, width_raw, offset_raw):
    return (lane_type & 0xf) | ((width_raw & 0x3fff) << 4) | ((offset_raw & 0x3fff) << 18)


def make_profile(middle, lanes):
    data = bytearray(64)
    data[0], data[1] = len(lanes), middle
    data[2:4] = b"\xaa\xaa"
    for i, value in enumerate(lanes):
        struct.pack_into("<I", data, 4 + 4 * i, value)
    return RoadProfile(bytes(data), 0)


def make_node(x, y, z, profile, segments):
    data = bytearray(32)
    struct.pack_into("<fff", data, 0, x, y, z)
    struct.pack_into("<hh", data, 12, 7, profile)
    data[16], data[17] = len(segments), 0xAA
    for i in range(7):
        struct.pack_into("<h", data, 18 + 2 * i, segments[i] if i < len(segments) else -21846)
    return RoadNode(bytes(data), 0)


def make_segment(a, b, flags=0, road=0, end_len=0, start_len=0, end_handle=(1, 2, 3), start_handle=(250, 251, 252)):
    data = bytearray(22)
    struct.pack_into("<8h", data, 0, a, b, 100, road, 3, flags, end_len, start_len)
    data[16:22] = bytes(end_handle) + bytes(start_handle)
    return RoadSegment(bytes(data), 0)


def make_road():
    return Road(struct.pack("<hhBBh", 256, 50, 255, 67, 0), 0)


def make_network():
    # profile 0: one carriageway, middleZone 0, signed offsets; profile 1: 4 lanes, middleZone 2
    p0 = make_profile(0, [lane(1, 400, 0x4000 - 400), lane(1, 400, 400)])
    p1 = make_profile(2, [lane(1, 400, 800), lane(1, 400, 400), lane(1, 400, 400), lane(1, 400, 800)])
    nodes = [make_node(0, 5, 0, 0, [0]), make_node(100, 6, 0, 1, [0, 1]), make_node(200, 7, 0, 1, [1])]
    segments = [make_segment(0, 1, flags=0x0100 | 0x0040), make_segment(1, 2, flags=0x0400)]
    return RoadNetwork(None, nodes, [p0, p1], [make_road()], segments)


class RoadNetworkExportTests(unittest.TestCase):
    def test_flag_names(self):
        self.assertEqual(segment_flag_names(0x0140), ["OneWay", "Curved"])
        self.assertEqual(segment_flag_names(-1 & 0xffff)[0], "Decision")

    def test_export_is_plain_and_complete(self):
        data = export_road_network.build_road_network(make_network())
        json.dumps(data)
        self.assertEqual(data["counts"], {"nodes": 3, "segments": 2, "roads": 1, "profiles": 2})
        self.assertEqual(data["nodes"][0]["segments"], [0])          # numSegments cuts the padding
        self.assertEqual(data["nodes"][1]["segments"], [0, 1])
        self.assertEqual(data["nodes"][1]["position"], [100.0, 6.0, 0.0])
        seg = data["segments"][0]
        self.assertEqual((seg["nodeStart"], seg["nodeEnd"], seg["flags"]), (0, 1, 0x0140))
        self.assertEqual(seg["flagNames"], ["OneWay", "Curved"])
        self.assertEqual(seg["endHandle"], [1, 2, 3])        # parser: bytes 16-18 are the end handle
        self.assertEqual(seg["startHandle"], [250, 251, 252])
        self.assertEqual(data["profiles"][0]["numZones"], 2)
        self.assertEqual(len(data["profiles"][1]["lanes"]), 4)
        self.assertAlmostEqual(data["profiles"][0]["lanes"][0]["offset"], -400 * 100 / 8191, places=3)
        self.assertEqual(data["roads"][0]["minWidth"], 67)

    def test_no_engine_keys(self):
        text = json.dumps(export_road_network.build_road_network(make_network())).lower()
        self.assertNotIn("beamng", text)

    def test_oriented_zone_sign_rule(self):
        net = make_network()
        p1 = net.profiles[1]   # middleZone 2: zones 0,1 are left (negated), 2,3 right
        self.assertEqual([road_lanes.oriented_zone(p1, i, False)["side"] for i in range(4)],
                         ["left", "left", "right", "right"])
        self.assertLess(road_lanes.oriented_zone(p1, 0, False)["offset"], 0)
        self.assertGreater(road_lanes.oriented_zone(p1, 3, False)["offset"], 0)
        mirrored = road_lanes.oriented_zone(p1, 0, True)   # mirrored: raw zone 3, now left
        self.assertEqual(mirrored["raw_index"], 3)
        self.assertEqual(mirrored["side"], "left")

    def test_strips_and_pairing(self):
        net = make_network()
        strips = road_lanes.segment_strips(net, net.segments[0])   # profile 0 (2 zones) -> profile 1 (4 zones)
        self.assertTrue(strips)
        self.assertTrue(all(s["a_width"] > 0 for s in strips))
        pairs = road_lanes.pair_zones(net.profiles[1], net.profiles[1], False, False)
        self.assertEqual(pairs, [(0, 0, 0), (1, 1, 1), (2, 2, 2), (3, 3, 3)])

    def test_curve_sampling(self):
        net = make_network()
        straight = road_lanes.segment_curve(net, net.segments[1])      # not flagged Curved
        self.assertEqual(straight, [(100.0, 6.0, 0.0), (200.0, 7.0, 0.0)])
        # Curved, both handles point along +z (127 = full length), 30000/65535 * 500 = about 229 m
        curved = make_segment(0, 1, flags=0x0100, end_len=30000, start_len=30000,
                              end_handle=(0, 0, 127), start_handle=(0, 0, 127))
        points = road_lanes.segment_curve(net, curved, steps=10)
        self.assertEqual(len(points), 11)
        self.assertEqual(points[0], (0.0, 5.0, 0.0))
        self.assertEqual(points[-1], (100.0, 6.0, 0.0))
        self.assertGreater(max(p[2] for p in points), 50.0)            # bulges along +z

    def test_lane_direction_rule(self):
        self.assertEqual(road_lanes.lane_direction("right", False), "forward")
        self.assertEqual(road_lanes.lane_direction("left", False), "reverse")
        self.assertEqual(road_lanes.lane_direction("left", True), "forward")
        net = make_network()
        by_side = {s["side"]: s["direction"] for s in road_lanes.segment_strips(net, net.segments[1])}
        self.assertEqual(by_side["right"], "forward")
        self.assertEqual(by_side["left"], "reverse")
        one_way = make_segment(1, 2, flags=0x0040)
        self.assertTrue(all(s["direction"] == "forward" for s in road_lanes.segment_strips(net, one_way)))

    def test_palette_and_lane_types(self):
        colors = [road_lanes.lane_type_color(t) for t in range(16)]
        self.assertEqual(len(set(colors)), 16)
        self.assertEqual(road_lanes.lane_types_used(make_network()), {1: 6})

    def test_text_helpers(self):
        net = make_network()
        text = "\n".join(road_lanes.describe_segment(net, 0))
        self.assertIn("OneWay", text)
        self.assertIn("road #0", text)
        census = road_lanes.profile_census_lines(net)
        self.assertTrue(census[0].startswith("profiles: 2"))
        self.assertTrue(road_lanes.profile_lines(0, net.profiles[0])[0].startswith("profile #0"))


if __name__ == "__main__":
    unittest.main()
