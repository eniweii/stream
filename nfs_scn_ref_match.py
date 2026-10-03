"""
Matches a whole folder of section DAE exports (nfs_scenery_dae_scan.py's
parse_section_folder - AssetDumper's ExportScenerySections naming
convention, one {SectionNumber}.dae per section) against a single
whole-scene Blender position dump (blender_scenery_position_dump.py) by
nearest 3D distance, and writes one combined CSV report across every
section found in the folder.

This is a first empirical check, not a finished matching pipeline: plain
nearest-neighbor, O(n*m) PER SECTION (nodes are only ever matched against
the whole Blender dump, not against each other across sections), no
attempt at resolving ties beyond flagging them. Fine at per-section object
counts (tens to low hundreds); a whole-level Blender dump can be large, so
matching runs section-by-section rather than testing every DAE node against
every Blender object in the entire scene at once.

After the distance passes, a last whole-scene pass (match_unique_names)
matches leftover rows by name alone when the name is unique on both sides.
matched_via shows the method: bbox, origin, name_fallback+..., name_unique.

Usage (either works):

    python3 nfs_scn_ref_match.py SECTION_FOLDER blender_dump.json report.csv [tolerance]

    python3 nfs_scn_ref_match.py
        (no args - pops up folder/file pickers instead)

`tolerance` (world units, default 2.0) is the max distance for a match to
be accepted at all - matches farther than this are reported as NO_MATCH
rather than a wrong guess. Worth widening for approximate (bbox-fallback)
DAE positions, which are centers of a whole chop's geometry rather than an
exact point, so real matches can land a few units off depending on how
regular the fragment's shape is.
"""

import csv
import json
import math
import re
import sys

from nfs_scenery_dae_scan import parse_section_folder


# Blender appends .001, .002, ... when object names collide. Past .999 the
# suffix grows to 4+ digits (real example: XO_TrackBarrierPlayer_1.1149),
# so match 3 or more digits, not exactly 3.
_BLENDER_DEDUP_SUFFIX_RE = re.compile(r"\.\d{3,}$")

# Asset/mesh names in the two sources can use spaces or other separators
# differently. Canonicalize all non-alphanumeric runs to one underscore.
_NAME_SEPARATOR_RE = re.compile(r"[^A-Z0-9]+")
_MULTIPLE_UNDERSCORE_RE = re.compile(r"_+")


def normalize_mesh_name(name):
    """
    Canonicalize scenery/mesh names for matching.

    Rules:
      - case-insensitive
      - remove Blender's auto-dedup suffix (.001, .002, .003, ...)
      - treat spaces/punctuation/separators as equivalent underscores
      - collapse repeated underscores
      - strip leading/trailing underscores

    Examples:
      PAN Exlights Island       -> PAN_EXLIGHTS_ISLAND
      PAN_EXLIGHTS_ISLAND       -> PAN_EXLIGHTS_ISLAND
      SKYDOME_Xenon             -> SKYDOME_XENON
      XOA_TrainSkyC_1b_00.001   -> XOA_TRAINSKYC_1B_00

    Note: strings such as `u0020` are left intact; the `u0020` characters are
    part of the actual asset name rather than being decoded into a literal
    space.
    """
    if not name:
        return ''

    name = str(name).strip().upper()

    # Only strip Blender's numeric dedup suffix when it occurs at the end.
    name = _BLENDER_DEDUP_SUFFIX_RE.sub('', name)

    # Normalize separators (spaces, hyphens, dots, etc.) to underscores.
    name = _NAME_SEPARATOR_RE.sub('_', name)

    # Collapse accidental/repeated separators and clean the edges.
    name = _MULTIPLE_UNDERSCORE_RE.sub('_', name).strip('_')

    return name


def distance(a, b):
    return math.sqrt(sum((a[i] - b[i]) ** 2 for i in range(3)))


def best_distance_to_record(node_pos, rec):
    """Tries both position candidates a Blender record can carry - the
    bbox center ('position', correct for chop/deinstanced content with an
    identity transform) and the object's own matrix_world translation
    ('origin', correct for genuine per-instance content) - and returns
    whichever is closer, plus which one won. A real match run showed this
    matters: building-type objects failed to match by a fixed,
    model-specific 2-10 unit gap that was purely this measurement
    mismatch, not missing data. Falls back to 'position' alone (as
    'bbox') for older dumps that don't have an 'origin' field yet."""
    d_bbox = distance(node_pos, tuple(rec['position']))
    if 'origin' not in rec:
        return d_bbox, 'bbox'
    d_origin = distance(node_pos, tuple(rec['origin']))
    return (d_bbox, 'bbox') if d_bbox <= d_origin else (d_origin, 'origin')


def match(dae_nodes, blender_records, tolerance=2.0, tie_epsilon=1e-4, progress_every=200):
    """Returns a list of dicts, one per DAE node, plus the set of blender
    record indices that were used by at least one match (for reporting
    unmatched Blender objects separately).

    Flags AMBIGUOUS in both directions:
      - one DAE node has multiple Blender objects within tie_epsilon of
        the best distance (can't tell which is the real match)
      - multiple DAE nodes end up choosing the same Blender object (only
        checked after the fact, so this doesn't catch a node that could
        have gone elsewhere - it just flags that overlap happened)

    A candidate's mesh_name must match the DAE node's geometry_name after
    normalize_mesh_name() canonicalization before it's even considered for
    distance comparison - position alone is never allowed to decide a
    match between two genuinely different models.

    Prints progress every `progress_every` nodes - this is an O(n*m) scan
    (every DAE node against every Blender object), so with a full-level
    dump on both sides this can run long enough that silence would be
    misleading about whether it's actually working.
    """
    results = []
    used_by = {}  # blender_index -> list of dae node_index that chose it
    total = len(dae_nodes)
    mesh_index = {}

    for j, rec in enumerate(blender_records):
        mesh_index.setdefault(normalize_mesh_name(rec.get('mesh_name')), []).append(j)

    print(f"Matching {total} DAE nodes against {len(blender_records)} Blender objects...")

    for i, node in enumerate(dae_nodes, 1):
        target_mesh = normalize_mesh_name(node.geometry_name)
        candidate_indices = mesh_index.get(target_mesh, [])

        dists = [
            (j,) + best_distance_to_record(node.position, blender_records[j])
            for j in candidate_indices
        ]
        dists.sort(key=lambda x: x[1])

        if not dists or dists[0][1] > tolerance:
            status = 'NO_MATCH'
            matched_name = matched_mesh = matched_ref = ''
            best_idx, best_dist_out = None, (dists[0][1] if dists else '')
        else:
            best_idx, best_dist, best_ref = dists[0]
            tied = [j for j, d, _ in dists if d <= best_dist + tie_epsilon]

            if len(tied) > 1:
                status = 'AMBIGUOUS'
                matched_name = ', '.join(blender_records[j]['name'] for j in tied)
                matched_mesh = blender_records[best_idx]['mesh_name']
                matched_ref = best_ref
            else:
                status = 'MATCHED'
                matched_name = blender_records[best_idx]['name']
                matched_mesh = blender_records[best_idx]['mesh_name']
                matched_ref = best_ref

            best_dist_out = best_dist
            used_by.setdefault(best_idx, []).append(node.node_index)

        results.append({
            'section': node.section,
            'dae_node_index': node.node_index,
            'dae_node_id': node.node_id,
            'dae_name': node.name,
            'dae_geometry_name': node.geometry_name,
            'position_is_exact': node.position_is_exact,
            'dae_position': node.position,
            'matched_blender_name': matched_name,
            'matched_blender_mesh': matched_mesh,
            'matched_via': matched_ref,
            'distance': best_dist_out,
            'status': status,
        })

        if i % progress_every == 0 or i == total:
            print(f"  ...{i}/{total} matched ({100 * i // total}%)")

    # Also flag the many-to-one direction: a Blender object claimed as the
    # single best match by more than one (already-MATCHED, non-tied) node.
    # Use the candidate record directly rather than searching by name so
    # duplicate names (if present) cannot select the wrong index.
    for r in results:
        if r['status'] == 'MATCHED':
            matched_name = r['matched_blender_name']
            idx = next(
                (i for i, rec in enumerate(blender_records)
                 if rec['name'] == matched_name),
                None,
            )
            if idx is not None and len(used_by.get(idx, [])) > 1:
                r['status'] = 'AMBIGUOUS'

    unmatched_indices = {i for i in range(len(blender_records)) if i not in used_by}

    # SECOND PASS: geometry/mesh-name filtering is right for the vast
    # majority of content (genuinely-shared models SHOULD share a mesh
    # datablock), but breaks specifically for content like panorama/
    # backdrop geometry, where Blender's importer can silently dedupe or
    # share simple mesh datablocks (flat planes, domes) across objects
    # that are otherwise completely distinct placements. Confirmed on real
    # data: multiple NO_MATCH rows had a dae_name IDENTICAL to an
    # EXTRA_IN_BLENDER object's name, at a real (non-zero) position - the
    # object was right there, just filtered out by mesh-name.
    #
    # Fallback: for still-unmatched DAE nodes, try the node's own NAME
    # (not geometry_name) against still-unused Blender objects' NAMES (not
    # mesh_name). Position + tolerance is still required exactly as in the
    # first pass - this is never a blind string match, a name match with
    # no nearby candidate is still NO_MATCH. Tags matched_via with
    # "name_fallback+..." so it's visible in the report which pass
    # actually resolved a given row, rather than looking identical to a
    # normal geometry-based match.
    name_index = {}
    for j in unmatched_indices:
        name_index.setdefault(normalize_mesh_name(blender_records[j].get('name')), []).append(j)

    fallback_used_by = {}
    for r in results:
        if r['status'] != 'NO_MATCH':
            continue
        target_name = normalize_mesh_name(r['dae_name'])
        candidate_indices = [j for j in name_index.get(target_name, []) if j in unmatched_indices]
        if not candidate_indices:
            continue

        dists = [
            (j,) + best_distance_to_record(r['dae_position'], blender_records[j])
            for j in candidate_indices
        ]
        dists.sort(key=lambda x: x[1])
        if dists[0][1] > tolerance:
            continue

        best_idx, best_dist, best_ref = dists[0]
        tied = [j for j, d, _ in dists if d <= best_dist + tie_epsilon]

        if len(tied) > 1:
            r['status'] = 'AMBIGUOUS'
            r['matched_blender_name'] = ', '.join(blender_records[j]['name'] for j in tied)
        else:
            r['status'] = 'MATCHED'
            r['matched_blender_name'] = blender_records[best_idx]['name']

        r['matched_blender_mesh'] = blender_records[best_idx]['mesh_name']
        r['matched_via'] = f"name_fallback+{best_ref}"
        r['distance'] = best_dist
        unmatched_indices.discard(best_idx)
        fallback_used_by.setdefault(best_idx, []).append(r['dae_node_index'])

    # Same many-to-one check as the primary pass, scoped to fallback matches.
    for r in results:
        if r.get('matched_via', '').startswith('name_fallback') and r['status'] == 'MATCHED':
            idx = next(
                (i for i, rec in enumerate(blender_records)
                 if rec['name'] == r['matched_blender_name']),
                None,
            )
            if idx is not None and len(fallback_used_by.get(idx, [])) > 1:
                r['status'] = 'AMBIGUOUS'

    unmatched_blender = [blender_records[i]['name'] for i in sorted(unmatched_indices)]
    return results, unmatched_blender


def write_report(results, unmatched_blender, out_path):
    fieldnames = [
        'section', 'dae_node_index', 'dae_node_id', 'dae_name',
        'dae_geometry_name', 'position_is_exact', 'dae_position',
        'matched_blender_name', 'matched_blender_mesh', 'matched_via',
        'distance', 'status'
    ]
    with open(out_path, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in results:
            w.writerow(r)
        for name in unmatched_blender:
            w.writerow({
                'section': '',
                'dae_node_index': '',
                'dae_node_id': '',
                'dae_name': '',
                'dae_geometry_name': '',
                'position_is_exact': '',
                'dae_position': '',
                'matched_blender_name': name,
                'matched_blender_mesh': '',
                'matched_via': '',
                'distance': '',
                'status': 'EXTRA_IN_BLENDER',
            })


def bbox_for_positions(positions, padding=100.0):
    """Bounding box (with padding) around a list of (x,y,z) positions -
    used to cut a section's Blender candidate pool down from the whole
    scene to just what's actually nearby, before the expensive per-node
    distance matching runs."""
    xs = [p[0] for p in positions]
    ys = [p[1] for p in positions]
    zs = [p[2] for p in positions]
    return (
        min(xs) - padding,
        min(ys) - padding,
        min(zs) - padding,
        max(xs) + padding,
        max(ys) + padding,
        max(zs) + padding,
    )


def in_bbox(pos, bbox):
    x0, y0, z0, x1, y1, z1 = bbox
    return x0 <= pos[0] <= x1 and y0 <= pos[1] <= y1 and z0 <= pos[2] <= z1


def match_by_section(sections, blender_records, tolerance=2.0, padding=100.0):
    """Runs match() once PER SECTION against only the Blender objects that
    fall within that section's own (padded) bounding box, instead of one
    global pass comparing every DAE node against the entire scene.

    This is the actual fix for the O(n*m) blowup with a full-level dump on
    both sides: a section's handful of nodes only ever needs comparing
    against the handful of Blender objects actually near it, not
    thousands of objects from unrelated parts of the map. Total unmatched
    Blender objects is computed across ALL sections at the end (by name,
    since Blender object names are unique within a scene) rather than
    per-section in isolation, so an object correctly doesn't get flagged
    as "extra" just because it happened to fall in a different section's
    candidate pool than expected.
    """
    all_results = []
    matched_names = set()
    section_nums = sorted(sections.keys())

    for i, section_num in enumerate(section_nums, 1):
        nodes = sections[section_num]
        if not nodes:
            continue

        bbox = bbox_for_positions([n.position for n in nodes], padding)
        # Keep an object if its bbox center OR its origin is in range. Huge
        # objects (panoramas) can have a bbox center far from the pivot the
        # DAE position refers to, so bbox center alone drops them.
        candidates = [
            rec for rec in blender_records
            if in_bbox(tuple(rec['position']), bbox)
            or ('origin' in rec and in_bbox(tuple(rec['origin']), bbox))
        ]

        print(
            f"[{i}/{len(section_nums)}] section {section_num}: "
            f"{len(nodes)} DAE nodes, "
            f"{len(candidates)}/{len(blender_records)} Blender objects in range"
        )

        if not candidates:
            for node in nodes:
                all_results.append({
                    'section': node.section,
                    'dae_node_index': node.node_index,
                    'dae_node_id': node.node_id,
                    'dae_name': node.name,
                    'dae_geometry_name': node.geometry_name,
                    'position_is_exact': node.position_is_exact,
                    'dae_position': node.position,
                    'matched_blender_name': '',
                    'matched_blender_mesh': '',
                    'matched_via': '',
                    'distance': '',
                    'status': 'NO_MATCH',
                })
            continue

        results, _ = match(
            nodes,
            candidates,
            tolerance,
            progress_every=10**9,
        )

        for r in results:
            if r['status'] in ('MATCHED', 'AMBIGUOUS'):
                for name in r['matched_blender_name'].split(', '):
                    if name:
                        matched_names.add(name)

        all_results.extend(results)

    match_unique_names(all_results, blender_records, matched_names)

    unmatched_blender = [
        rec['name'] for rec in blender_records
        if rec['name'] not in matched_names
    ]
    return all_results, unmatched_blender


def match_unique_names(results, blender_records, matched_names):
    """Last pass, whole scene, no distance limit. A NO_MATCH DAE row and an
    unclaimed Blender object match when their normalized names are equal
    AND that name is unique on both sides (exactly one unresolved DAE row,
    exactly one unclaimed Blender object). The DAE node name and the
    Blender object name come from the same source data, so a unique name
    is a reliable identity even when position or mesh name failed (huge
    panorama objects, for example).

    Names with 2+ candidates on either side stay NO_MATCH - no guessing.
    Rows are tagged matched_via='name_unique'. 'distance' still holds the
    nearest distance, so sort by it to find suspect pairs. Updates
    `results` and `matched_names` in place."""
    dae_by_name = {}
    for r in results:
        if r['status'] == 'NO_MATCH':
            key = normalize_mesh_name(r['dae_name'])
            if key:
                dae_by_name.setdefault(key, []).append(r)

    blender_by_name = {}
    for rec in blender_records:
        if rec['name'] in matched_names:
            continue
        key = normalize_mesh_name(rec['name'])
        if key:
            blender_by_name.setdefault(key, []).append(rec)

    count = 0
    for key, rows in dae_by_name.items():
        recs = blender_by_name.get(key, [])
        if len(rows) != 1 or len(recs) != 1:
            continue
        r, rec = rows[0], recs[0]
        dist, _ = best_distance_to_record(r['dae_position'], rec)
        r['status'] = 'MATCHED'
        r['matched_blender_name'] = rec['name']
        r['matched_blender_mesh'] = rec['mesh_name']
        r['matched_via'] = 'name_unique'
        r['distance'] = dist
        matched_names.add(rec['name'])
        count += 1

    print(f"Unique-name pass: {count} extra matches")


def category_prefix(name):
    """First underscore-delimited segment of a name, uppercased - e.g.
    'XBU_TorytallCenterNeonA_1a_00' -> 'XBU', 'PAN_Casino_Landmarks' -> 'PAN'.
    Used to auto-group match results by naming convention instead of
    maintaining a hand-written, always-incomplete keyword list (a real
    example: a manual PAN_/NEON/LIGHT keyword list still missed TRN_PANO_*
    and SKY_* panorama/skybox content in a real report). Names with no
    underscore return the whole (uppercased) name rather than crashing."""
    name = str(name).upper()
    return name.split('_', 1)[0] if name else '(blank)'


def summarize_by_category(results, unmatched_blender):
    """Per-category (auto-derived prefix) status breakdown - run this on
    any report to spot which naming conventions are driving NO_MATCH/
    AMBIGUOUS counts, without needing to guess keywords ad hoc."""
    from collections import Counter

    counts = {}
    for r in results:
        cat = category_prefix(r['dae_geometry_name'])
        counts.setdefault(cat, Counter())[r['status']] += 1

    for name in unmatched_blender:
        cat = category_prefix(name)
        counts.setdefault(cat, Counter())['EXTRA_IN_BLENDER'] += 1

    print()
    print(
        f"{'category':<12} {'total':>7} {'MATCHED':>9} {'AMBIG':>7} "
        f"{'NO_MATCH':>9} {'EXTRA':>7}"
    )
    for cat, c in sorted(counts.items(), key=lambda kv: -sum(kv[1].values())):
        total = sum(c.values())
        print(
            f"{cat:<12} {total:>7} {c.get('MATCHED', 0):>9} "
            f"{c.get('AMBIGUOUS', 0):>7} {c.get('NO_MATCH', 0):>9} "
            f"{c.get('EXTRA_IN_BLENDER', 0):>7}"
        )


def summarize(results, unmatched_blender):
    counts = {}
    for r in results:
        counts[r['status']] = counts.get(r['status'], 0) + 1

    print(f"DAE nodes: {len(results)}")
    for status in ('MATCHED', 'AMBIGUOUS', 'NO_MATCH'):
        print(f"  {status}: {counts.get(status, 0)}")
    print(f"Blender objects with no matching DAE node: {len(unmatched_blender)}")
    summarize_by_category(results, unmatched_blender)


def _pick_paths():
    """No CLI args given - fall back to simple native pickers instead of
    requiring the user to know/type paths. tkinter ships with standard
    Python on Windows/Mac; on Linux it may need a separate OS package
    installed, in which case this raises and the CLI-args path is the
    fallback."""
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()

    dae_folder = filedialog.askdirectory(
        title="Select the folder of section .dae exports (one per SectionNumber)"
    )
    if not dae_folder:
        sys.exit("No DAE folder selected, aborting.")

    blender_json_path = filedialog.askopenfilename(
        title="Select the Blender position dump (.json)",
        filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
    )
    if not blender_json_path:
        sys.exit("No Blender dump selected, aborting.")

    out_path = filedialog.asksaveasfilename(
        title="Save the match report as...",
        defaultextension=".csv",
        filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
    )
    if not out_path:
        sys.exit("No output path chosen, aborting.")

    root.destroy()
    return dae_folder, blender_json_path, out_path


if __name__ == '__main__':
    if len(sys.argv) >= 4:
        dae_folder = sys.argv[1]
        blender_json_path = sys.argv[2]
        out_path = sys.argv[3]
        tolerance = float(sys.argv[4]) if len(sys.argv) > 4 else 2.0
    else:
        dae_folder, blender_json_path, out_path = _pick_paths()
        tolerance = 2.0

    sections = parse_section_folder(dae_folder)
    total_nodes = sum(len(nodes) for nodes in sections.values())
    print(
        f"Loaded {total_nodes} scenery nodes across {len(sections)} sections "
        f"from {dae_folder!r}"
    )

    with open(blender_json_path, encoding='utf-8') as f:
        blender_records = json.load(f)
    print(
        f"Loaded {len(blender_records)} Blender object positions "
        f"from {blender_json_path!r}"
    )

    results, unmatched_blender = match_by_section(
        sections,
        blender_records,
        tolerance,
    )
    write_report(results, unmatched_blender, out_path)
    summarize(results, unmatched_blender)
    print(f"Report written to {out_path}")
