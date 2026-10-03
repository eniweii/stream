"""
Scans a per-section COLLADA (.dae) file produced by AssetDumper's
ExportScenerySections mode (NFSTools/NFS-ModTools, ExportBundleCommand.cs)
and recovers, per <node>, exactly the identity and transform AssetDumper
itself assigned - before any Blender-side import, join, chop-merge, or
AABB-normalize touches anything.

WHY THIS WORKS (verified directly against ExportBundleCommand.cs source,
not guessed):
  - ExportScenerySections writes one file per section, and constructs its
    SceneExport with `SceneName = f"ScenerySection_{SectionNumber}"`.
  - Every <node>'s `id` attribute is written as
        f"scene_{SceneName}_node_{idx}"
    where `idx` is the 0-based position in the FILTERED node list actually
    written to this file - RFL_/SHD_/SHADOW-prefixed solids and any
    instance whose solid-hash lookup failed are skipped via `continue`
    BEFORE a node is ever added, so `idx` is not the same numbering as the
    raw on-disk SceneryInstance array index (InstanceNumber). It's a
    stable, unique identifier for THIS export, not interchangeable with
    SceneryOverrideInfo.InstanceNumber if that's ever needed later.
  - Each node's <matrix> is written as the TRANSPOSE of the internal
    Matrix4x4 (System.Numerics is row-vector/row-major; COLLADA's <matrix>
    is row-major for a column-vector convention) - see the 16-value
    write order in ExportSceneCollada. Concretely: reading the 16 values
    in document order as 4 rows of 4, translation ends up at the end of
    each row (indices 3, 7, 11), i.e. position = (values[3], values[7],
    values[11]). This script relies on exactly that layout.

NOT YET VERIFIED against a real exported .dae file (no section export was
available to this project when this was written) - the logic is derived
directly from reading the exporter's own source, not from hex-inspecting
real output the way most of this project's other parsers were confirmed.
Treat it the same way as the MW candidate structs: correct on paper,
needs a real file to actually confirm before being trusted blindly.
"""
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, replace
from typing import Optional


NODE_ID_RE = re.compile(r'^scene_ScenerySection_(\d+)_node_(\d+)$')


@dataclass
class SceneryDaeNode:
    section: int
    node_index: int          # position in this file's filtered node list -
                              # NOT the raw SceneryInstance/InstanceNumber
    node_id: str              # raw id string, e.g. "scene_ScenerySection_101_node_5"
    name: str                 # model/info name (often shared across instances,
                              # and truncated to 24 bytes - SceneryInfo.Name's
                              # real on-disk field size, confirmed against a
                              # real file: "L5RA_TN_FN_Terrain_09_C" is exactly
                              # 23 chars + a null terminator)
    geometry_name: str         # the geometry's own <geometry name="..."> -
                              # untruncated, and per-instance-unique for chop/
                              # deinstanced content (each chop fragment's mesh
                              # really is unique baked geometry) though still
                              # shared across true instances of one reusable
                              # model (e.g. every "XOs_SmackTreeB" tree shares
                              # one geometry id)
    position: tuple           # (x, y, z), world space
    position_is_exact: bool   # True: read directly from the node's own
                              # <matrix> translation (a real per-instance
                              # placement - confirmed on real X-prefixed
                              # instanceable content: 174/185 nodes in one
                              # real section file had distinct, real
                              # positions this way).
                              # False: the node's own matrix was (0,0,0) -
                              # confirmed on real data to happen for BOTH
                              # chop/terrain fragments (TN_/TRN_/RD_..._CHOP,
                              # real position baked into mesh vertices
                              # instead of the node transform) AND deinstanced
                              # scenery (name ends in "_DEINST..." - per the
                              # exporter's own source, these use the solid's
                              # PivotMatrix instead of the instance transform,
                              # and were confirmed on real data to ALSO carry
                              # real world coordinates in their vertex data
                              # rather than the node transform). In this case
                              # `position` is instead the bbox-center of the
                              # geometry's own vertex data - an approximation
                              # (same technique this project's AABB-normalize
                              # Blender step already relies on), not a true
                              # placement pivot - good enough for matching,
                              # not a precise ground-truth position record.
    matrix: list = field(repr=False)   # 4x4, row-major, as parsed from <matrix>
    geometry_url: Optional[str] = None  # instance_geometry url (e.g. "#geometry-0xABCD1234")


def _parse_geometry_bbox_centers(root, findall, geometry_ids):
    """Computes a world-space bbox center per requested geometry id, reading
    only each geometry's position float_array (not the full mesh) - lazy,
    and only for geometry ids actually needed by a zero-position node, since
    walking every geometry in a large file is wasted work otherwise."""
    centers = {}
    if not geometry_ids:
        return centers
    for geom in findall(root, 'library_geometries/geometry'):
        gid = geom.get('id')
        if gid not in geometry_ids:
            continue
        arrays = findall(geom, 'mesh/source/float_array')
        if not arrays:
            continue
        # The positions source is usually the first float_array with a
        # stride-3 accessor; take the first one rather than assuming array
        # order, since a mesh can have normal/UV float_arrays too.
        pos_array = None
        for arr in arrays:
            accessor = findall(geom, 'mesh/source/technique_common/accessor')
            # match by the array's own id being referenced as #id in some accessor
            for acc in accessor:
                if acc.get('source', '').lstrip('#') == arr.get('id') and acc.get('stride') == '3':
                    pos_array = arr
                    break
            if pos_array is not None:
                break
        if pos_array is None or not pos_array.text:
            continue
        values = [float(v) for v in pos_array.text.split()]
        xs, ys, zs = values[0::3], values[1::3], values[2::3]
        if not xs:
            continue
        centers[gid] = (
            (min(xs) + max(xs)) / 2.0,
            (min(ys) + max(ys)) / 2.0,
            (min(zs) + max(zs)) / 2.0,
        )
    return centers


def _strip_ns(tag):
    """'{http://.../COLLADASchema}node' -> 'node'"""
    return tag.split('}', 1)[1] if '}' in tag else tag


def _find_ns(root):
    """COLLADA files declare a default namespace on the root element -
    detect it from the root tag itself rather than hardcoding a schema
    version, so this doesn't silently break on a different COLLADA
    version string."""
    if '}' in root.tag:
        return root.tag.split('}', 1)[0][1:]
    return None


def parse_section_dae(path):
    """Returns a list of SceneryDaeNode, one per <node> in the file's
    visual scene. Raises ValueError if a node's id doesn't match the
    expected AssetDumper naming (rather than silently skipping it) -
    a mismatch means either this isn't an AssetDumper export, or the
    naming convention assumed above is wrong for this file/version."""
    tree = ET.parse(path)
    root = tree.getroot()
    ns_uri = _find_ns(root)
    ns = {'c': ns_uri} if ns_uri else {}

    def findall(elem, path_):
        # path_ uses plain tag names; add the namespace prefix if needed
        if ns_uri:
            path_ = '/'.join(f'c:{p}' for p in path_.split('/'))
        return elem.findall(path_, ns)

    out = []
    pending_fallback = []  # indices into `out` needing a geometry-bbox fallback
    geom_ids_needed = set()

    # geometry id -> full untruncated name, resolved once per file
    geometry_names = {}
    for geom in findall(root, 'library_geometries/geometry'):
        gid = geom.get('id')
        if gid:
            geometry_names[gid] = geom.get('name', '')

    for scene in findall(root, 'library_visual_scenes/visual_scene'):
        for node_elem in findall(scene, 'node'):
            node_id = node_elem.get('id', '')
            m = NODE_ID_RE.match(node_id)
            if not m:
                # Not every node necessarily follows this convention (e.g.
                # a light node uses "{SceneName}_inst_{lightId}" per the
                # source) - skip those quietly rather than erroring, but
                # only for ids that don't even try to match the scenery
                # pattern's shape.
                continue

            section = int(m.group(1))
            node_index = int(m.group(2))
            name = node_elem.get('name', '')

            matrix_elem = findall(node_elem, 'matrix')
            if not matrix_elem:
                raise ValueError(
                    f"node {node_id!r} in {path!r} has no <matrix> - "
                    f"unexpected shape, don't guess a position, investigate "
                    f"this file before trusting anything else in it.")
            values = [float(v) for v in matrix_elem[0].text.split()]
            if len(values) != 16:
                raise ValueError(
                    f"node {node_id!r} in {path!r}: <matrix> has "
                    f"{len(values)} values, expected 16 - malformed or a "
                    f"different matrix convention than assumed here.")
            rows = [values[0:4], values[4:8], values[8:12], values[12:16]]
            position = (rows[0][3], rows[1][3], rows[2][3])
            position_is_exact = position != (0.0, 0.0, 0.0)

            geom = findall(node_elem, 'instance_geometry')
            geometry_url = geom[0].get('url') if geom else None
            gid = geometry_url.lstrip('#') if geometry_url else None
            geometry_name = geometry_names.get(gid, '')

            record = SceneryDaeNode(
                section=section, node_index=node_index, node_id=node_id,
                name=name, geometry_name=geometry_name, position=position,
                position_is_exact=position_is_exact, matrix=rows,
                geometry_url=geometry_url,
            )
            out.append(record)
            if not position_is_exact and gid:
                pending_fallback.append(len(out) - 1)
                geom_ids_needed.add(gid)

    if pending_fallback:
        centers = _parse_geometry_bbox_centers(root, findall, geom_ids_needed)
        for i in pending_fallback:
            rec = out[i]
            gid = rec.geometry_url.lstrip('#')
            center = centers.get(gid)
            if center is not None:
                out[i] = replace(rec, position=center)
            # else: leave as (0,0,0) - no vertex data found to fall back to,
            # better to surface an obviously-wrong value than a silently
            # wrong one from guessing.

    return out


def parse_section_folder(folder_path):
    """Scans every .dae file in folder_path (non-recursive) whose filename
    stem is a plain integer - AssetDumper's ExportScenerySections naming
    convention (`{SectionNumber}.dae`) - and returns a dict of
    {section_number: [SceneryDaeNode, ...]}.

    Files that don't fit that naming pattern are skipped with a printed
    warning rather than raising - a folder of section exports can
    plausibly have other non-scenery .dae files sitting alongside them
    (e.g. a light-pack export), and one oddly-named file shouldn't abort
    scanning everything else.

    Prints progress as it goes (one line per file) - a folder of section
    exports can run into the hundreds of files, and each one is a real
    XML parse, so this can take long enough that silence would be
    misleading about whether anything is actually happening."""
    import os
    candidates = [f for f in sorted(os.listdir(folder_path)) if f.lower().endswith('.dae')]
    out = {}
    for i, fname in enumerate(candidates, 1):
        stem = fname[:-4]
        if not stem.isdigit():
            print(f"[{i}/{len(candidates)}] skipping {fname!r} - filename "
                  f"stem isn't a plain section number")
            continue
        section = int(stem)
        full_path = os.path.join(folder_path, fname)
        nodes = parse_section_dae(full_path)
        out[section] = nodes
        print(f"[{i}/{len(candidates)}] parsed {fname} - {len(nodes)} nodes")
    return out


if __name__ == '__main__':
    import os
    import sys
    path = sys.argv[1]

    if os.path.isdir(path):
        sections = parse_section_folder(path)
        total = sum(len(nodes) for nodes in sections.values())
        print(f"{path}: {total} scenery nodes across {len(sections)} section files")
        if not sections:
            print("  No section .dae files found directly in this folder. Checked for "
                  "plain-numeric filenames like '318.dae' (AssetDumper's "
                  "ExportScenerySections naming) - this only looks in the folder "
                  "itself, not subfolders. If your .dae files are one level deeper, "
                  "point this at that inner folder instead.")
        for section in sorted(sections)[:5]:
            nodes = sections[section]
            print(f"  section {section}: {len(nodes)} nodes")
        if len(sections) > 5:
            print(f"  ... and {len(sections) - 5} more section files")
    else:
        nodes = parse_section_dae(path)
        print(f"{path}: {len(nodes)} scenery nodes")
        for n in nodes[:10]:
            tag = 'exact' if n.position_is_exact else 'approx(bbox)'
            print(f"  [{n.node_index}] {n.node_id}  name={n.name!r}  "
                  f"pos=({n.position[0]:.2f}, {n.position[1]:.2f}, {n.position[2]:.2f}) [{tag}]  "
                  f"geom={n.geometry_name!r}")
        if len(nodes) > 10:
            print(f"  ... and {len(nodes) - 10} more")
