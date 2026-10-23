"""
Collision DAE export: the collision packs of the stream file (chunk 0x3B801, read by
nfs_collision_pack.py) written as one COLLADA file per collision shape, in BeamNG
coordinates, ready to open in Blender and later to stream.

A window opens when the script runs without arguments. With a stream file argument it
runs on the command line (--help lists the options).

Shapes (one DAE each, named <section>_base.dae and <section>_g<group number>.dae):
    base   every instance of the section whose group number is 0 (always on)
    group  every instance of the section with that group number (barrier group, only
           active while the scenery group of that number is enabled, see
           nfs_collision_pack.match_by_group). The group key hash, the same hash the
           visual objects carry as sectionOverrideName, is written to collision_index.json
           when the region file is given.
The section number of a pack is the visual section id (every instance of the real file lies
inside the polygon of its own section, checked against sections.json).

Geometry of an article (CollisionArticle) is read the way nfs_collision_pack.write_collision_obj
reads it:
    strips  triangle strips: N vertices give N - 2 triangles. Odd triangles swap the first two
            vertices to normalize strip parity. Face orientation then mirrors the game's
            CalcCollisionFaceNormal: after NFS-to-BeamNG conversion, reverse a face only when
            its NFS-Y / BeamNG-Z normal component is negative. Do not read kUpFacing as a
            blanket instruction to force each triangle up or down.
    edges   barrier walls: each source edge becomes a quad (2 triangles) between YBot and
            YTop. Winding is aligned with WCollisionBarrier::GetNormal after the BeamNG axis
            mapping. Optional double-sided mode adds exactly overlapping reverse-wound faces;
            this is off by default because it causes conflicting viewport normals.
World position of a vertex: nfs_collision_pack.local_to_world (instance matrix rows and true
position). Then NFS (x, y, z) -> BeamNG (z, -x, y + vertical offset), the same mapping as
region/export_sections.py and the traffic node exporter; the vertical offset (default 150) is
applied here once. Triangle-strip parity is normalized after conversion. Face orientation then
follows WCollisionMgr.cpp::CalcCollisionFaceNormal: reverse a face only when its vertical
normal component is negative (NFS +Y maps to BeamNG +Z). This avoids treating kUpFacing as a
blanket instruction to force every face up or down.

Streaming note: collision is not streamed as ordinary section geometry. Group-0/base collision
is static; only nonzero-group collision follows group/override triggers.

Per shape, equal vertices (rounded to 0.1 mm) are welded and each triangle gets one flat
normal.

Materials: one material per collision surface, like the game: a mesh holds several materials
and each triangle (strip triangle or wall quad) belongs to the surface of its face/edge. The
surface is looked up in the surface table of its own article (nfs_collision_surfaces.py explains
the lookup) and named nfs_surface_<VLT name from hashes_main.txt>. Special names: nfs_surface_none
(null reference 0x00000000), nfs_surface_0xHASH (hash without a name, never merged),
nfs_surface_notable and nfs_surface_badindex (article without a table / index past its end).
Each material has a flat viewport color from its name, to tell surfaces apart in Blender.
collision_surface_materials.json lists every material with its raw hash and an empty
"groundType" to fill in; an existing file keeps the groundType values already entered.

Output: <folder>/<section>_base.dae, <section>_g<group>.dae and collision_index.json
(shape list, counts, materials per shape, options). Default folder: outputs/nfs_collision_dae/collision/.
Layout of the pack data: see the docstring of nfs_collision_pack.py. Checked on synthetic data
(tests/test_collision_dae_export.py); run it on the real file first.
"""
# stream bootstrap: make the shared library folders importable
import pathlib as _pl, sys as _sys
_sys.path[:0] = [str(_pl.Path(__file__).resolve().parents[1] / _d) for _d in ['common', 'region', 'scenery', 'solids_materials']]

import argparse
import colorsys
import datetime
import json
import math
import os
import queue
import threading
import zlib

from nfs_collision_pack import load_collision_packs, true_position, local_to_world
from nfs_collision_surfaces import surface_key, material_name
from nfs_hash_dictionary import load_vlt_dictionary
from nfs_outputs import out_dir

TOOL_NAME = 'nfs_collision_dae_export'
DEFAULT_VERTICAL_OFFSET = 150.0
WELD_DIGITS = 4            # vertices closer than 0.1 mm are one vertex
MIN_WALL_HEIGHT = 1e-4     # an edge with a smaller bottom to top distance is not a wall
MIN_TRIANGLE_AREA2 = 1e-12 # squared cross product length below which a triangle is dropped

_pack_cache = {}           # (path, mtime, size) -> packs, so a second export does not reload


# ---- Geometry ----

def to_beamng(point, vertical_offset):
    """NFS (x, y, z) -> BeamNG (z, -x, y + vertical offset)."""
    return (point[2], -point[0], point[1] + vertical_offset)


class Shape:
    """One DAE: welded vertices and triangles (three vertex indexes and a material name).
    materials: {material name: [raw hash text or None, triangle count]}."""
    __slots__ = ('section', 'group', 'vertices', 'lookup', 'triangles', 'instances',
                 'strip_triangles', 'wall_quads', 'dropped', 'materials')

    def __init__(self, section, group):
        self.section = section
        self.group = group
        self.vertices = []
        self.lookup = {}
        self.triangles = []
        self.instances = 0
        self.strip_triangles = 0
        self.wall_quads = 0
        self.dropped = 0        # degenerate triangles and walls
        self.materials = {}

    @property
    def name(self):
        return f"{self.section}_base" if self.group == 0 else f"{self.section}_g{self.group}"

    def _vertex(self, point):
        key = tuple(round(c, WELD_DIGITS) for c in point)
        index = self.lookup.get(key)
        if index is None:
            index = len(self.vertices)
            self.vertices.append(key)
            self.lookup[key] = index
        return index

    def add_triangle(self, a, b, c, surface):
        """Adds a triangle from three BeamNG points; surface is (material name, raw hash text
        or None). Returns False when it is degenerate (nothing is added then, so no loose
        vertices are left)."""
        a, b, c = (tuple(round(v, WELD_DIGITS) for v in point) for point in (a, b, c))
        if _normal(a, b, c)[3] < MIN_TRIANGLE_AREA2:
            return False
        material, hash_text = surface
        self.triangles.append((self._vertex(a), self._vertex(b), self._vertex(c), material))
        self.materials.setdefault(material, [hash_text, 0])[1] += 1
        return True


def _normal(a, b, c):
    u = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
    v = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
    n = (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])
    return n[0], n[1], n[2], n[0] * n[0] + n[1] * n[1] + n[2] * n[2]


def parse_section_filter(text):
    """'1021, 1100-1110' -> set of section numbers. Blank gives None (all sections)."""
    text = (text or '').strip()
    if not text:
        return None
    numbers = set()
    for part in text.replace(';', ',').split(','):
        part = part.strip()
        if not part:
            continue
        if '-' in part:
            low, high = part.split('-', 1)
            numbers.update(range(int(low), int(high) + 1))
        else:
            numbers.add(int(part))
    return numbers


def build_shapes(packs, sections=None, strips=True, walls=True, double_sided_walls=False,
                 vertical_offset=DEFAULT_VERTICAL_OFFSET, progress=None, names=None):
    """One Shape per (section, group number). Returns (shapes, stats). shapes is sorted by
    section, then group (base first). names is {vlt hash: surface name} (default: the VLT
    dictionary of hashes_main.txt)."""
    if names is None:
        names = load_vlt_dictionary()

    def surface_of(article, index):
        key = surface_key(article, index)
        return material_name(key, names), (f"0x{key:08X}" if isinstance(key, int) else None)

    shapes = {}
    stats = {'packs': 0, 'instances': 0, 'no_article': 0}
    chosen = [p for p in packs if sections is None or p.section_number in sections]
    for done, pack in enumerate(chosen):
        stats['packs'] += 1
        for ci in pack.instances:
            article = pack.articles.get(ci.instance_id)
            if article is None:
                stats['no_article'] += 1
                continue
            stats['instances'] += 1
            shape = shapes.get((pack.section_number, ci.group_number))
            if shape is None:
                shape = shapes[(pack.section_number, ci.group_number)] = Shape(
                    pack.section_number, ci.group_number)
            shape.instances += 1
            position = true_position(ci)
            if strips:
                for strip in article.strips:
                    points = [to_beamng(local_to_world(ci, v, position), vertical_offset)
                              for v in strip.vertices]
                    for t in range(len(points) - 2):
                        a, b, c = points[t], points[t + 1], points[t + 2]
                        # WCollisionStrip::MakeNextFace slides the vertex window. For an
                        # independent triangle mesh, swap the first two vertices on odd
                        # triangles to normalize triangle-strip parity.
                        if t & 1:
                            a, b = b, a
                        # Mirror WCollisionMgr.cpp::CalcCollisionFaceNormal: the game's
                        # computed normal is flipped only when its NFS-Y component is negative.
                        # NFS +Y maps to BeamNG +Z. kUpFacing participates in collision-test
                        # side selection; it is not a per-face +/-Z normal instruction.
                        normal_z = _normal(a, b, c)[2]
                        if normal_z < 0.0:
                            b, c = c, b
                        surface = surface_of(article, strip.surfaces[t] if t < len(strip.surfaces)
                                             else len(article.surface_hashes))
                        ok = shape.add_triangle(a, b, c, surface)
                        if ok:
                            shape.strip_triangles += 1
                        else:
                            shape.dropped += 1
            if walls:
                for edge in article.edges:
                    a = local_to_world(ci, edge.minimum, position)
                    b = local_to_world(ci, edge.maximum, position)
                    y_bottom, y_top = min(a[1], b[1]), max(a[1], b[1])
                    if y_top - y_bottom < MIN_WALL_HEIGHT or (a[0] == b[0] and a[2] == b[2]):
                        shape.dropped += 1
                        continue
                    p0 = to_beamng((a[0], y_bottom, a[2]), vertical_offset)
                    p1 = to_beamng((b[0], y_bottom, b[2]), vertical_offset)
                    p2 = to_beamng((b[0], y_top, b[2]), vertical_offset)
                    p3 = to_beamng((a[0], y_top, a[2]), vertical_offset)
                    surface = surface_of(article, edge.surface)

                    # Match WCollisionBarrier::GetNormal in WCollision.h. In NFS space
                    # the barrier normal is (delta_z, 0, -delta_x); after the NFS -> BeamNG
                    # mapping (z, -x, y), its horizontal direction is (-delta_x, -delta_z, 0).
                    # Edge endpoint order can vary, so pick the triangle winding from that target
                    # normal instead of assuming the source endpoints always have one ordering.
                    expected_normal = (-(b[0] - a[0]), -(b[2] - a[2]), 0.0)
                    raw_normal = _normal(p0, p1, p2)[:3]
                    facing_dot = sum(raw_normal[i] * expected_normal[i] for i in range(3))
                    if facing_dot < 0.0:
                        wall_triangles = ((p0, p2, p1), (p0, p3, p2))
                    else:
                        wall_triangles = ((p0, p1, p2), (p0, p2, p3))

                    added = sum(shape.add_triangle(ta, tb, tc, surface)
                                for ta, tb, tc in wall_triangles)
                    if double_sided_walls:
                        # These reverse-wound copies are exactly coplanar with the front faces;
                        # keep the option for special cases but don't enable it by default, since
                        # Blender's viewport can show overlapping/opposite normal artifacts.
                        added += sum(shape.add_triangle(ta, tc, tb, surface)
                                     for ta, tb, tc in wall_triangles)
                    if added:
                        shape.wall_quads += 1
                    else:
                        shape.dropped += 1
        if progress:
            progress('Reading packs', done + 1, len(chosen))
    ordered = sorted(shapes.values(), key=lambda s: (s.section, s.group))
    return ordered, stats


# ---- DAE ----

def _floats(values):
    return ' '.join(f"{v:.6g}" if abs(v) < 1e5 else f"{v:.4f}" for v in values)


def material_color(name):
    """A flat color from the material name, so surfaces can be told apart in the viewport."""
    hue = (zlib.crc32(name.encode('ascii', errors='ignore')) % 360) / 360.0
    r, g, bl = colorsys.hsv_to_rgb(hue, 0.55, 0.85)
    return f"{r:.3f} {g:.3f} {bl:.3f} 1"


def dae_text(shape):
    """The COLLADA document of one shape (1.4.1, Z up, meters): one <triangles> block and one
    material per surface."""
    name = shape.name
    positions = [c for v in shape.vertices for c in v]
    normals = []
    by_material = {}
    for n, (ia, ib, ic, material) in enumerate(shape.triangles):
        nx, ny, nz, length2 = _normal(shape.vertices[ia], shape.vertices[ib], shape.vertices[ic])
        length = math.sqrt(length2)
        normals.extend((nx / length, ny / length, nz / length))
        by_material.setdefault(material, []).extend((ia, n, ib, n, ic, n))
    materials = sorted(by_material)
    now = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    effects = ''.join(
        f'    <effect id="{m}-fx"><profile_COMMON><technique sid="common"><lambert><diffuse>'
        f'<color>{material_color(m)}</color></diffuse></lambert></technique></profile_COMMON></effect>\n'
        for m in materials)
    material_defs = ''.join(
        f'    <material id="{m}" name="{m}"><instance_effect url="#{m}-fx"/></material>\n'
        for m in materials)
    blocks = ''.join(
        f'        <triangles material="{m}" count="{len(by_material[m]) // 6}">\n'
        f'          <input semantic="VERTEX" source="#{name}-vertices" offset="0"/>\n'
        f'          <input semantic="NORMAL" source="#{name}-normals" offset="1"/>\n'
        f'          <p>{" ".join(map(str, by_material[m]))}</p>\n'
        f'        </triangles>\n'
        for m in materials)
    bindings = ''.join(f'<instance_material symbol="{m}" target="#{m}"/>' for m in materials)
    return f"""<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
  <asset>
    <contributor><authoring_tool>{TOOL_NAME}</authoring_tool></contributor>
    <created>{now}</created>
    <modified>{now}</modified>
    <unit name="meter" meter="1"/>
    <up_axis>Z_UP</up_axis>
  </asset>
  <library_effects>
{effects}  </library_effects>
  <library_materials>
{material_defs}  </library_materials>
  <library_geometries>
    <geometry id="{name}-mesh" name="{name}">
      <mesh>
        <source id="{name}-positions">
          <float_array id="{name}-positions-array" count="{len(positions)}">{_floats(positions)}</float_array>
          <technique_common><accessor source="#{name}-positions-array" count="{len(shape.vertices)}" stride="3"><param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/></accessor></technique_common>
        </source>
        <source id="{name}-normals">
          <float_array id="{name}-normals-array" count="{len(normals)}">{_floats(normals)}</float_array>
          <technique_common><accessor source="#{name}-normals-array" count="{len(shape.triangles)}" stride="3"><param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/></accessor></technique_common>
        </source>
        <vertices id="{name}-vertices"><input semantic="POSITION" source="#{name}-positions"/></vertices>
{blocks}      </mesh>
    </geometry>
  </library_geometries>
  <library_visual_scenes>
    <visual_scene id="Scene" name="Scene">
      <node id="{name}" name="{name}" type="NODE"><instance_geometry url="#{name}-mesh"><bind_material><technique_common>{bindings}</technique_common></bind_material></instance_geometry></node>
    </visual_scene>
  </library_visual_scenes>
  <scene><instance_visual_scene url="#Scene"/></scene>
</COLLADA>
"""


# ---- Export ----

def _group_keys(region_path):
    """group number -> (key hex, resolved name or None), from the region file's scenery groups."""
    from nfs_stream_scenery import load_stream_scenery
    scenery = load_stream_scenery(region_path)
    return {g.group_number: (f"0x{g.key:08X}", g.name) for g in scenery.groups}


def _packs_for(stream_path, log):
    stat = os.stat(stream_path)
    key = (os.path.abspath(stream_path), stat.st_mtime, stat.st_size)
    if key not in _pack_cache:
        log(f"Reading {stream_path} ...")
        _pack_cache.clear()
        _pack_cache[key] = load_collision_packs(stream_path)
    return _pack_cache[key]


def _write_material_list(out_folder, index):
    """collision_surface_materials.json: every material with its raw hash, triangle total and
    a "groundType" to fill in. Values already entered in an existing file are kept (also for
    materials this export did not produce), so re-exporting never loses them. Returns the number
    of materials."""
    path = os.path.join(out_folder, 'collision_surface_materials.json')
    old = {}
    try:
        with open(path, encoding='utf-8') as f:
            old = {m['material']: m for m in json.load(f).get('materials', []) if 'material' in m}
    except (OSError, ValueError, AttributeError):
        pass
    found = {}
    for row in index:
        for m in row['materials']:
            entry = found.setdefault(m['material'], {'hash': m['hash'], 'triangles': 0, 'shapes': 0})
            entry['triangles'] += m['triangles']
            entry['shapes'] += 1
    merged = {}
    for name, entry in old.items():      # keep what is there, refresh the counts of what was exported
        merged[name] = dict(entry)
    for name, entry in found.items():
        merged[name] = {'material': name, 'hash': entry['hash'],
                        'groundType': old.get(name, {}).get('groundType', ''),
                        'triangles': entry['triangles'], 'shapes': entry['shapes']}
    with open(path, 'w', encoding='utf-8') as f:
        json.dump({'materials': [merged[k] for k in sorted(merged)]}, f, indent=1)
    return len(found)


def export_collision(stream_path, out_folder, region_path=None, sections=None, strips=True,
                     walls=True, double_sided_walls=False,
                     vertical_offset=DEFAULT_VERTICAL_OFFSET, log=print, progress=None, names=None):
    """Writes every shape as a DAE plus collision_index.json and
    collision_surface_materials.json to out_folder. Returns a summary dict. log(text) and
    progress(phase, done, total) are optional callbacks; names is {vlt hash: name}."""
    packs = _packs_for(stream_path, log)
    log(f"{len(packs)} collision pack(s) in the file.")
    shapes, stats = build_shapes(packs, sections, strips, walls, double_sided_walls,
                                 vertical_offset, progress, names)
    keys = {}
    if region_path:
        log(f"Reading groups from {region_path} ...")
        keys = _group_keys(region_path)

    os.makedirs(out_folder, exist_ok=True)
    index = []
    written = empty = 0
    for done, shape in enumerate(shapes):
        if not shape.triangles:
            empty += 1
        else:
            with open(os.path.join(out_folder, shape.name + '.dae'), 'w', encoding='utf-8',
                      newline='\n') as f:
                f.write(dae_text(shape))
            written += 1
            row = {'file': shape.name + '.dae', 'section': shape.section, 'group': shape.group,
                   'instances': shape.instances, 'vertices': len(shape.vertices),
                   'triangles': len(shape.triangles), 'stripTriangles': shape.strip_triangles,
                   'wallQuads': shape.wall_quads, 'dropped': shape.dropped,
                   'materials': [{'material': m, 'hash': h, 'triangles': n}
                                 for m, (h, n) in sorted(shape.materials.items())]}
            if shape.group and shape.group in keys:
                row['groupKey'], resolved = keys[shape.group]
                if resolved:
                    row['groupName'] = resolved
            index.append(row)
        if progress:
            progress('Writing DAE files', done + 1, len(shapes))

    triangles = sum(r['triangles'] for r in index)
    unknown = sum(1 for r in index if r['group'] and 'groupKey' not in r)
    summary = {'files': written, 'empty_shapes': empty, 'triangles': triangles,
               'packs': stats['packs'], 'instances': stats['instances'],
               'instances_without_article': stats['no_article'],
               'group_shapes': sum(1 for r in index if r['group']),
               'group_shapes_without_key': unknown if region_path else None}
    material_summary = _write_material_list(out_folder, index)
    summary['materials'] = material_summary
    manifest = {'tool': TOOL_NAME, 'stream': os.path.abspath(stream_path),
                'region': os.path.abspath(region_path) if region_path else None,
                'coordinateMapping': 'NFS (x,y,z) -> BeamNG (z,-x,y+verticalOffset)',
                'windingPolicy': 'strip parity + game CalcCollisionFaceNormal rule + barrier GetNormal',
                'streamingPolicy': {'baseCollision': 'static_not_streamed',
                                    'groupCollision': 'group_triggered_only'},
                'verticalOffset': vertical_offset,
                'options': {'strips': strips, 'walls': walls, 'doubleSidedWalls': double_sided_walls,
                            'sections': sorted(sections) if sections else 'all'},
                'summary': summary, 'shapes': index}
    with open(os.path.join(out_folder, 'collision_index.json'), 'w', encoding='utf-8') as f:
        json.dump(manifest, f, indent=1)
    log(f"Wrote {written} DAE file(s), {triangles} triangle(s), to {out_folder}")
    log(f"{material_summary} surface material(s) in collision_surface_materials.json")
    log(f"{summary['group_shapes']} group shape(s); {empty} empty shape(s) skipped; "
        f"{stats['no_article']} instance(s) without an article.")
    if region_path and unknown:
        log(f"{unknown} group shape(s) have a group number that is not a scenery group in the "
            f"region file (no key written).")
    return summary


# ---- Window ----

def _settings_path():
    return out_dir(TOOL_NAME) / 'settings.json'


def _load_settings():
    try:
        return json.loads(_settings_path().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def run_window():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    class App(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title("Collision DAE export")
            self.minsize(680, 480)
            s = _load_settings()
            self.stream_var = tk.StringVar(value=s.get('stream', ''))
            self.region_var = tk.StringVar(value=s.get('region', ''))
            self.out_var = tk.StringVar(
                value=s.get('out', str(out_dir(TOOL_NAME) / 'collision')))
            self.sections_var = tk.StringVar(value=s.get('sections', ''))
            self.offset_var = tk.StringVar(value=str(s.get('offset', DEFAULT_VERTICAL_OFFSET)))
            self.strips_var = tk.BooleanVar(value=s.get('strips', True))
            self.walls_var = tk.BooleanVar(value=s.get('walls', True))
            # Ignore the legacy 'double' setting once: older versions saved True by default,
            # which creates coincident reverse-wound wall faces. Persist the new choice separately.
            self.double_var = tk.BooleanVar(value=s.get('double_sided_walls_v2', False))
            self.status_var = tk.StringVar(value='Ready')
            self.events = queue.Queue()
            self.busy = False
            self._build()
            self.after(100, self._poll)

        def _build(self):
            body = ttk.Frame(self, padding=10)
            body.pack(fill=tk.BOTH, expand=True)
            body.columnconfigure(1, weight=1)
            body.rowconfigure(5, weight=1)

            def path_row(row, label, var, command):
                ttk.Label(body, text=label).grid(row=row, column=0, sticky='w', pady=2)
                ttk.Entry(body, textvariable=var).grid(row=row, column=1, sticky='ew', padx=6)
                ttk.Button(body, text='Browse...', command=command).grid(row=row, column=2)

            path_row(0, 'Stream file', self.stream_var,
                     lambda: self._pick(self.stream_var, 'Stream file (STREAML5RA.BUN)'))
            path_row(1, 'Region file (group keys, optional)', self.region_var,
                     lambda: self._pick(self.region_var, 'Region file (L5RA.BUN)'))
            path_row(2, 'Output folder', self.out_var, self._pick_folder)

            options = ttk.LabelFrame(body, text='Options', padding=8)
            options.grid(row=3, column=0, columnspan=3, sticky='ew', pady=8)
            ttk.Label(options, text='Sections (blank = all, e.g. 1021, 1100-1110)').grid(
                row=0, column=0, sticky='w')
            ttk.Entry(options, textvariable=self.sections_var, width=24).grid(
                row=0, column=1, sticky='w', padx=6)
            ttk.Label(options, text='Vertical offset').grid(row=1, column=0, sticky='w', pady=2)
            ttk.Entry(options, textvariable=self.offset_var, width=8).grid(
                row=1, column=1, sticky='w', padx=6)
            ttk.Checkbutton(options, text='Triangle strips (surfaces)',
                            variable=self.strips_var).grid(row=2, column=0, sticky='w')
            ttk.Checkbutton(options, text='Barrier walls (edges stood up as quads)',
                            variable=self.walls_var).grid(row=3, column=0, sticky='w')
            ttk.Checkbutton(options,
                            text='Double-sided walls (adds overlapping reverse faces; optional)',
                            variable=self.double_var).grid(row=4, column=0, sticky='w')

            actions = ttk.Frame(body)
            actions.grid(row=4, column=0, columnspan=3, sticky='ew')
            actions.columnconfigure(1, weight=1)
            self.export_button = ttk.Button(actions, text='Export', command=self._on_export)
            self.export_button.grid(row=0, column=0)
            self.progress = ttk.Progressbar(actions, mode='determinate')
            self.progress.grid(row=0, column=1, sticky='ew', padx=8)
            ttk.Label(actions, textvariable=self.status_var, width=24).grid(row=0, column=2)

            log_frame = ttk.Frame(body)
            log_frame.grid(row=5, column=0, columnspan=3, sticky='nsew', pady=(8, 0))
            log_frame.columnconfigure(0, weight=1)
            log_frame.rowconfigure(0, weight=1)
            self.log_text = tk.Text(log_frame, height=10, state='disabled', wrap='word')
            scroll = ttk.Scrollbar(log_frame, command=self.log_text.yview)
            self.log_text.configure(yscrollcommand=scroll.set)
            self.log_text.grid(row=0, column=0, sticky='nsew')
            scroll.grid(row=0, column=1, sticky='ns')

        def _pick(self, var, title):
            path = filedialog.askopenfilename(title=title, initialdir=os.path.dirname(var.get()) or None)
            if path:
                var.set(path)

        def _pick_folder(self):
            path = filedialog.askdirectory(title='Output folder', initialdir=self.out_var.get() or None)
            if path:
                self.out_var.set(path)

        def _log(self, text):
            self.log_text.configure(state='normal')
            self.log_text.insert(tk.END, text + '\n')
            self.log_text.see(tk.END)
            self.log_text.configure(state='disabled')

        def _on_export(self):
            if self.busy:
                return
            try:
                params = {'stream_path': self.stream_var.get().strip(),
                          'out_folder': self.out_var.get().strip(),
                          'region_path': self.region_var.get().strip() or None,
                          'sections': parse_section_filter(self.sections_var.get()),
                          'strips': self.strips_var.get(), 'walls': self.walls_var.get(),
                          'double_sided_walls': self.double_var.get(),
                          'vertical_offset': float(self.offset_var.get())}
            except ValueError:
                messagebox.showerror('Collision DAE export',
                                     'Sections or vertical offset is not a number.')
                return
            if not os.path.isfile(params['stream_path']):
                messagebox.showerror('Collision DAE export', 'Choose the stream file first.')
                return
            if params['region_path'] and not os.path.isfile(params['region_path']):
                messagebox.showerror('Collision DAE export', 'The region file does not exist.')
                return
            if not params['out_folder']:
                messagebox.showerror('Collision DAE export', 'Choose an output folder.')
                return
            if not (params['strips'] or params['walls']):
                messagebox.showerror('Collision DAE export', 'Enable strips or walls.')
                return
            self._save_settings()
            self.busy = True
            self.export_button.state(['disabled'])
            self.progress.configure(value=0)
            self.status_var.set('Loading stream file...')
            threading.Thread(target=self._work, args=(params,), daemon=True).start()

        def _save_settings(self):
            data = {'stream': self.stream_var.get(), 'region': self.region_var.get(),
                    'out': self.out_var.get(), 'sections': self.sections_var.get(),
                    'offset': self.offset_var.get(), 'strips': self.strips_var.get(),
                    'walls': self.walls_var.get(),
                    'double_sided_walls_v2': self.double_var.get()}
            try:
                _settings_path().write_text(json.dumps(data, indent=1), encoding='utf-8')
            except OSError:
                pass

        def _work(self, params):
            try:
                export_collision(
                    log=lambda text: self.events.put(('log', text)),
                    progress=lambda phase, done, total: self.events.put(('progress', (phase, done, total))),
                    **params)
                self.events.put(('done', None))
            except Exception as exc:    # shown in the window, the log keeps the details
                self.events.put(('error', f"{type(exc).__name__}: {exc}"))

        def _poll(self):
            try:
                while True:
                    kind, value = self.events.get_nowait()
                    if kind == 'log':
                        self._log(value)
                    elif kind == 'progress':
                        phase, done, total = value
                        self.progress.configure(maximum=max(total, 1), value=done)
                        self.status_var.set(f"{phase} {done}/{total}")
                    elif kind in ('done', 'error'):
                        if kind == 'error':
                            self._log('Error: ' + value)
                        self.status_var.set('Done' if kind == 'done' else 'Failed')
                        self.busy = False
                        self.export_button.state(['!disabled'])
            except queue.Empty:
                pass
            self.after(100, self._poll)

    App().mainloop()


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Export the collision packs of the stream file as DAE files (no argument "
                    "opens the window).")
    ap.add_argument('stream_bun', nargs='?', help="stream file (STREAML5RA.BUN)")
    ap.add_argument('--out', help="output folder (default outputs/nfs_collision_dae/collision/)")
    ap.add_argument('--region-bun', metavar='REGION',
                    help="region file (L5RA.BUN): writes the group key of each group shape to "
                         "collision_index.json")
    ap.add_argument('--sections', default='', help="section numbers, e.g. 1021,1100-1110 (default all)")
    ap.add_argument('--vertical-offset', type=float, default=DEFAULT_VERTICAL_OFFSET,
                    help=f"added to the height once (default {DEFAULT_VERTICAL_OFFSET:g})")
    ap.add_argument('--hashes', help="wordlist for the surface names (default common/hashes_main.txt)")
    ap.add_argument('--no-strips', action='store_true', help="skip the triangle strips")
    ap.add_argument('--no-walls', action='store_true', help="skip the barrier walls")
    wall_sides = ap.add_mutually_exclusive_group()
    wall_sides.add_argument('--double-sided-walls', dest='double_sided_walls', action='store_true',
                            help='add overlapping reverse-wound copies of each barrier quad')
    wall_sides.add_argument('--single-sided-walls', dest='double_sided_walls', action='store_false',
                            help=argparse.SUPPRESS)  # backward-compatible; single-sided is default
    ap.set_defaults(double_sided_walls=False)
    args = ap.parse_args(argv)
    if not args.stream_bun:
        run_window()
        return
    if args.no_strips and args.no_walls:
        ap.error("--no-strips and --no-walls together leave nothing to export")
    out = args.out or str(out_dir(TOOL_NAME) / 'collision')
    export_collision(args.stream_bun, out, args.region_bun, parse_section_filter(args.sections),
                     not args.no_strips, not args.no_walls, args.double_sided_walls,
                     args.vertical_offset, log=lambda text: print(f"[{TOOL_NAME}] {text}"),
                     names=load_vlt_dictionary(args.hashes) if args.hashes else None)


if __name__ == '__main__':
    main()
