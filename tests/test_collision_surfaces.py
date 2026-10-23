"""Synthetic tests for the collision surface census and the VLT hash (no game files needed)."""
import pathlib as _pl, sys as _sys
_sys.path[:0] = [str(_pl.Path(__file__).resolve().parents[1] / _d) for _d in ['common', 'region', 'scenery', 'solids_materials']]

import tempfile
import unittest
from pathlib import Path

from nfs_collision_pack import (CollisionPack, CollisionInstance, CollisionArticle,
                                CollisionStrip, CollisionEdge)
from nfs_hashing import vlt_hash, bin_hash
import nfs_collision_surfaces as surf


def instance(group, instance_id):
    return CollisionInstance(instance_id, 0, 0, group, instance_id, 0.0, 0.0, 0.0,
                             (0.0, 0.0, 0.0), 1.0, (1.0, 0.0, 0.0), (0.0, 0.0, 1.0))


def article(number, table, strip_surfaces=(), edge_surfaces=()):
    a = CollisionArticle(number, 0)
    a.surface_hashes = list(table)
    if strip_surfaces:   # one strip: len(surfaces) triangles = len + 2 vertices
        verts = [(i, 0, 0) for i in range(len(strip_surfaces) + 2)]
        a.strips.append(CollisionStrip((0, 0, 0), 1.0, 0, verts, list(strip_surfaces)))
    for s in edge_surfaces:
        a.edges.append(CollisionEdge((0, 0, 0), (1, 1, 0), s, 0, 0.0))
    return a


def packs(asphalt, grass):
    p1 = CollisionPack(0, 101, 'p1', 0, 0, 0, [instance(0, 0), instance(5, 1)], [],
                       {0: article(0, [asphalt, grass], [0, 0, 1], [0]),
                        1: article(1, [grass], [0], [0, 3])}, 0, {}, 0x70)
    p2 = CollisionPack(0, 102, 'p2', 0, 0, 0, [instance(0, 0)], [],
                       {0: article(0, [], [0], [0])}, 0, {}, 0x70)
    return [p1, p2]


class SurfaceTests(unittest.TestCase):
    def test_vlt_hash_known_values(self):
        self.assertEqual(vlt_hash('headlight_inner'), 0x356F692A)      # carbon flare key 0
        self.assertEqual(vlt_hash('brakelight_inner'), 0xB60DAC69)     # carbon flare key 2
        self.assertNotEqual(vlt_hash('asphalt'), bin_hash('asphalt'))

    def test_census_uses_each_articles_own_table(self):
        asphalt, grass = vlt_hash('asphalt'), vlt_hash('grass')
        rows, totals = surf.surface_census(packs(asphalt, grass))
        self.assertEqual((totals['articles'], totals['triangles'], totals['edges']), (3, 5, 4))
        a, g = rows[asphalt], rows[grass]
        self.assertEqual((a.triangles, a.edges), (2, 1))                 # article 0: index 0 twice
        self.assertEqual((g.triangles, g.edges), (2, 1))                 # article 0 index 1 + article 1 index 0
        self.assertEqual(g.group_articles, {(101, 1)})                   # instance with group 5
        self.assertEqual(rows[surf.NO_TABLE].triangles, 1)
        self.assertEqual(rows[surf.NO_TABLE].edges, 1)
        self.assertEqual(rows[surf.BAD_INDEX].edges, 1)                  # edge index 3, table of 1

    def test_section_filter(self):
        rows, totals = surf.surface_census(packs(1, 2), sections={102})
        self.assertEqual(totals['articles'], 1)
        self.assertEqual(set(rows), {surf.NO_TABLE})

    def test_tsv_resolves_names(self):
        asphalt, grass = vlt_hash('asphalt'), vlt_hash('grass')
        with tempfile.TemporaryDirectory() as tmp:
            words = Path(tmp) / 'words.txt'
            words.write_text('# comment\nasphalt\nconcrete\n', encoding='utf-8')
            rows, _ = surf.surface_census(packs(asphalt, grass))
            path, resolved, named = surf.write_surface_mapping(rows, str(Path(tmp) / 'out' / 'm.tsv'), str(words))
            lines = Path(path).read_text(encoding='utf-8').splitlines()
            self.assertEqual(lines[0].split('\t')[:3], ['raw_hash', 'vlt_name', 'bin_name'])
            by_hash = {l.split('\t')[0]: l.split('\t') for l in lines[1:]}
            self.assertEqual(by_hash[f"0x{asphalt:08X}"][1], 'asphalt')
            self.assertEqual(by_hash[f"0x{grass:08X}"][1], '')           # not in the wordlist: stays unresolved
            self.assertEqual((resolved, named), (3, 1))
            self.assertIn(surf.NO_TABLE, by_hash)


if __name__ == '__main__':
    unittest.main()
