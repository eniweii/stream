"""Synthetic regression tests comparing exported strip normals with the MW/Carbon rule."""
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "region"))

from nfs_collision_dae_export import build_shapes, _normal
from nfs_collision_pack import CollisionArticle, CollisionEdge, CollisionInstance, CollisionPack, CollisionStrip

SOURCE_VERTICES = [
    (0.0, 0.0, 0.0),
    (0.0, 0.0, 1.0),
    (1.0, 0.0, 0.0),
    (1.0, 0.0, 1.0),
]


def make_pack(strip_flags):
    article = CollisionArticle(0, 0)
    article.surface_hashes = [0]
    article.strips.append(CollisionStrip((0.0, 0.0, 0.0), 1.0,
                                         strip_flags, list(SOURCE_VERTICES), [0, 0]))
    instance = CollisionInstance(
        0, 0, 0, 0, 0, 0.0, 0.0, 0.0,
        (0.0, 0.0, 0.0), 1.0, (1.0, 0.0, 0.0), (0.0, 0.0, 1.0)
    )
    return CollisionPack(0, 1021, "synthetic", 0, 0, 0,
                         [instance], [], {0: article}, 0, {}, 0x70)


def make_edge_pack(point0, point1):
    article = CollisionArticle(0, 0)
    article.surface_hashes = [0]
    article.edges.append(CollisionEdge(point0, point1, 0, 0, 1.0))
    instance = CollisionInstance(
        0, 0, 0, 0, 0, 0.0, 0.0, 0.0,
        (0.0, 0.0, 0.0), 1.0, (1.0, 0.0, 0.0), (0.0, 0.0, 1.0)
    )
    return CollisionPack(0, 1021, "synthetic-edge", 0, 0, 0,
                         [instance], [], {0: article}, 0, {}, 0x70)


def cross(a, b):
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def game_face_normal_nfs(a, b, c):
    """Mirror WCollisionMgr.cpp::CalcCollisionFaceNormal's orientation rule.

    The decomp calculates (p1 - p0) cross (p0 - p2), then flips the normal if NFS Y < 0.
    This returns an unnormalized direction, which is sufficient for comparing orientation.
    """
    vec_z = tuple(b[i] - a[i] for i in range(3))
    vec_x = tuple(a[i] - c[i] for i in range(3))
    normal = cross(vec_z, vec_x)
    if normal == (0.0, 0.0, 0.0):
        return (0.0, 1.0, 0.0)
    if normal[1] < 0.0:
        normal = tuple(-component for component in normal)
    return normal


def game_normal_beamng(nfs_normal):
    """Map the game normal as a vector, with no position offset."""
    return (nfs_normal[2], -nfs_normal[0], nfs_normal[1])


def exported_shape(strip_flags):
    shapes, _stats = build_shapes([make_pack(strip_flags)],
                                  vertical_offset=150.0, names={})
    assert len(shapes) == 1
    return shapes[0]


class CollisionDaeWindingTests(unittest.TestCase):
    def test_exported_triangles_match_game_face_normal_for_all_flags(self):
        # Strip flags affect collision-test behavior; they must not force opposite mesh normals.
        for flags in (0x00, 0x01, 0x02, 0x03):
            with self.subTest(strip_flags=flags):
                shape = exported_shape(flags)
                self.assertEqual(len(shape.triangles), 2)
                for t, (ia, ib, ic, _material) in enumerate(shape.triangles):
                    actual = _normal(shape.vertices[ia], shape.vertices[ib], shape.vertices[ic])[:3]
                    expected = game_normal_beamng(
                        game_face_normal_nfs(*SOURCE_VERTICES[t:t + 3]))
                    dot = sum(actual[i] * expected[i] for i in range(3))
                    self.assertGreater(dot, 0.0, (flags, t, actual, expected))
                    self.assertGreater(actual[2], 1e-8, (flags, t, actual))

    def test_adjacent_strip_triangles_have_consistent_orientation(self):
        for flags in (0x00, 0x01, 0x02, 0x03):
            with self.subTest(strip_flags=flags):
                shape = exported_shape(flags)
                normals = [
                    _normal(shape.vertices[ia], shape.vertices[ib], shape.vertices[ic])[:3]
                    for ia, ib, ic, _material in shape.triangles
                ]
                dot = sum(normals[0][i] * normals[1][i] for i in range(3))
                self.assertGreater(dot, 0.0, (flags, normals))

    def test_wall_quads_match_game_barrier_normal_for_both_endpoint_orders(self):
        # WCollisionBarrier::GetNormal uses (delta_z, 0, -delta_x) in NFS coordinates.
        # Convert that direction to BeamNG's (z, -x, y) axes and verify both wall triangles.
        for point0, point1 in (
            ((0.0, 0.0, 0.0), (2.0, 5.0, 1.0)),
            ((2.0, 5.0, 1.0), (0.0, 0.0, 0.0)),
        ):
            with self.subTest(point0=point0, point1=point1):
                pack = make_edge_pack(point0, point1)
                shapes, _stats = build_shapes([pack], strips=False, walls=True, names={})
                shape = shapes[0]
                self.assertEqual(len(shape.triangles), 2)
                expected = (-(point1[0] - point0[0]), -(point1[2] - point0[2]), 0.0)
                for ia, ib, ic, _material in shape.triangles:
                    actual = _normal(shape.vertices[ia], shape.vertices[ib], shape.vertices[ic])[:3]
                    dot = sum(actual[i] * expected[i] for i in range(3))
                    self.assertGreater(dot, 0.0, (point0, point1, actual, expected))

    def test_wall_quads_are_single_sided_by_default(self):
        pack = make_edge_pack((0.0, 0.0, 0.0), (2.0, 5.0, 1.0))
        shapes, _stats = build_shapes([pack], strips=False, walls=True, names={})
        self.assertEqual(len(shapes[0].triangles), 2)

    def test_double_sided_wall_option_adds_reversed_overlapping_faces(self):
        pack = make_edge_pack((0.0, 0.0, 0.0), (2.0, 5.0, 1.0))
        shapes, _stats = build_shapes([pack], strips=False, walls=True,
                                      double_sided_walls=True, names={})
        shape = shapes[0]
        self.assertEqual(len(shape.triangles), 4)
        normals = [
            _normal(shape.vertices[ia], shape.vertices[ib], shape.vertices[ic])[:3]
            for ia, ib, ic, _material in shape.triangles
        ]
        for front, reverse in ((normals[0], normals[2]), (normals[1], normals[3])):
            dot = sum(front[i] * reverse[i] for i in range(3))
            self.assertLess(dot, 0.0, (front, reverse))


if __name__ == "__main__":
    unittest.main()
