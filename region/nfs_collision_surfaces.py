"""
Collision surface census: which surface hashes the collision packs of the stream file use, what
name each hash resolves to, and how many triangles and barrier edges carry it.

Writes surface_mapping.tsv (one row per distinct raw surface hash). No vault data is read yet;
the properties of a surface (the vault collection of that name) are wired in later.

How a face gets its surface (MW decomp, WWorldPos.cpp / WCollisionMgr.cpp / WCollision.h, the
same lookup for faces and barriers):
    triangle  the surface byte of the strip vertex that completes it (its third vertex), read by
              nfs_collision_pack as strip.surfaces[t]
    barrier   edge.surface
    Both are an INDEX into the surface table of the article that holds the strip or edge
    (article.surface_hashes[index], one u32 per entry, stored after the strips and edges).
    The index is local to its article: the same index means different surfaces in different
    articles, so a surface is only known after the lookup. The hash is the key of a vault
    collection (the VLT hash family, see nfs_hashing.vlt_hash) of the simsurface class.
    The flags byte next to the surface byte (WSurface flags, e.g. bit 4 on faces, bit 0x10 on
    barriers) is not read by nfs_collision_pack, so it is not part of this census.

Names come from hashes_main.txt (common/, optional): every line hashed with vlt_hash gives
vlt_name; every line hashed with bin_hash gives bin_name, kept only as a cross-check. A hash
that resolves in neither stays unresolved and keeps its raw hash; nothing is guessed.

Columns of surface_mapping.tsv:
    raw_hash        0xXXXXXXXX, or <no surface table> (article has faces but an empty table)
                    or <index out of range> (index >= number of table entries)
    vlt_name        name whose VLT hash is raw_hash (blank if unresolved)
    bin_name        name whose bin hash is raw_hash (blank if none; compare with vlt_name)
    triangles       strip triangles using this surface
    edges           barrier edges using this surface
    articles        articles whose table is used by at least one face or edge of this row
    sections        distinct sections
    group_articles  of those articles, how many belong to a barrier group (group number != 0)

Usage:
    python region/nfs_collision_surfaces.py STREAML5RA.BUN [--out DIR] [--sections 1021,1100-1110]
Default folder: outputs/nfs_collision_surfaces/. Checked on synthetic data
(tests/test_collision_surfaces.py); run it on the real file first.
"""
# stream bootstrap: make the shared library folders importable
import pathlib as _pl, sys as _sys
_sys.path[:0] = [str(_pl.Path(__file__).resolve().parents[1] / _d) for _d in ['common', 'region', 'scenery', 'solids_materials']]

import argparse
import os

from nfs_collision_pack import load_collision_packs
from nfs_hash_dictionary import load_dictionary, load_vlt_dictionary
from nfs_outputs import out_dir

TOOL_NAME = 'nfs_collision_surfaces'
NO_TABLE = '<no surface table>'
BAD_INDEX = '<index out of range>'


def surface_key(article, index):
    """The surface of a face or edge: the raw hash (int) from the article's own surface table,
    or NO_TABLE (the article has no table) or BAD_INDEX (index past the end of the table)."""
    table = article.surface_hashes
    if not table:
        return NO_TABLE
    if index >= len(table):
        return BAD_INDEX
    return table[index]


def material_name(key, names):
    """DAE material name of a surface key; names is {vlt hash: name}. A hash with no name keeps
    its raw hash in the name, so an unresolved surface is never merged into another one."""
    if key == NO_TABLE:
        return 'nfs_surface_notable'
    if key == BAD_INDEX:
        return 'nfs_surface_badindex'
    if key == 0:
        return 'nfs_surface_none'          # the null surface reference
    name = names.get(key)
    if name:
        return 'nfs_surface_' + ''.join(c if c.isalnum() or c == '_' else '_' for c in name)
    return f"nfs_surface_0x{key:08X}"


class SurfaceRow:
    __slots__ = ('triangles', 'edges', 'articles', 'sections', 'group_articles')

    def __init__(self):
        self.triangles = 0
        self.edges = 0
        self.articles = set()      # (section, instance id)
        self.sections = set()
        self.group_articles = set()


def surface_census(packs, sections=None):
    """{raw hash (int) or NO_TABLE / BAD_INDEX: SurfaceRow}, plus totals.
    sections is an optional set of section numbers."""
    rows = {}
    totals = {'packs': 0, 'articles': 0, 'triangles': 0, 'edges': 0, 'instances_without_article': 0}
    for pack in packs:
        if sections is not None and pack.section_number not in sections:
            continue
        totals['packs'] += 1
        for ci in pack.instances:
            article = pack.articles.get(ci.instance_id)
            if article is None:
                totals['instances_without_article'] += 1
                continue
            totals['articles'] += 1

            def row_for(index):
                key = surface_key(article, index)
                row = rows.get(key)
                if row is None:
                    row = rows[key] = SurfaceRow()
                row.articles.add((pack.section_number, ci.instance_id))
                row.sections.add(pack.section_number)
                if ci.group_number:
                    row.group_articles.add((pack.section_number, ci.instance_id))
                return row

            for strip in article.strips:
                for index in strip.surfaces:
                    row_for(index).triangles += 1
                    totals['triangles'] += 1
            for edge in article.edges:
                row_for(edge.surface).edges += 1
                totals['edges'] += 1
    return rows, totals


def write_surface_mapping(rows, path, names_path=None):
    """Writes surface_mapping.tsv, most used first. Returns (path, faces and edges with a VLT
    name, distinct hashes with a VLT name)."""
    kwargs = {'path': names_path} if names_path else {}
    vlt_names = load_vlt_dictionary(**kwargs)
    bin_names = load_dictionary(**kwargs)

    def sort_key(item):
        key, row = item
        return (-(row.triangles + row.edges), str(key))

    resolved = named = 0
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write('raw_hash\tvlt_name\tbin_name\ttriangles\tedges\tarticles\tsections\tgroup_articles\n')
        for key, row in sorted(rows.items(), key=sort_key):
            if isinstance(key, int):
                raw = f"0x{key:08X}"
                vlt_name = vlt_names.get(key, '')
                bin_name = bin_names.get(key, '')
            else:
                raw, vlt_name, bin_name = key, '', ''
            if vlt_name:
                resolved += row.triangles + row.edges
                named += 1
            f.write(f"{raw}\t{vlt_name}\t{bin_name}\t{row.triangles}\t{row.edges}\t"
                    f"{len(row.articles)}\t{len(row.sections)}\t{len(row.group_articles)}\n")
    return path, resolved, named


def main(argv=None):
    from nfs_collision_dae_export import parse_section_filter
    ap = argparse.ArgumentParser(
        description="Census of the collision surface hashes of the stream file: "
                    "surface_mapping.tsv with the resolved names and the triangle/edge counts.")
    ap.add_argument('stream_bun', help="stream file (STREAML5RA.BUN)")
    ap.add_argument('--out', help="output folder (default outputs/nfs_collision_surfaces/)")
    ap.add_argument('--sections', default='', help="section numbers, e.g. 1021,1100-1110 (default all)")
    ap.add_argument('--hashes', help="wordlist (default common/hashes_main.txt)")
    args = ap.parse_args(argv)

    packs = load_collision_packs(args.stream_bun)
    rows, totals = surface_census(packs, parse_section_filter(args.sections))
    folder = args.out or str(out_dir(TOOL_NAME))
    path, resolved, named = write_surface_mapping(rows, os.path.join(folder, 'surface_mapping.tsv'), args.hashes)

    faces = totals['triangles'] + totals['edges']
    hashes = sum(1 for k in rows if isinstance(k, int))
    print(f"[{TOOL_NAME}] {totals['packs']} pack(s), {totals['articles']} article(s), "
          f"{totals['triangles']} triangle(s), {totals['edges']} edge(s)")
    print(f"[{TOOL_NAME}] {hashes} distinct surface hash(es), {named} resolved by VLT name; "
          f"{resolved} of {faces} face(s) and edge(s) carry a resolved name")
    for special in (NO_TABLE, BAD_INDEX):
        if special in rows:
            print(f"[{TOOL_NAME}] {special}: {rows[special].triangles} triangle(s), "
                  f"{rows[special].edges} edge(s)")
    print(f"[{TOOL_NAME}] wrote {path}")


if __name__ == '__main__':
    main()
