"""
Combines three already-working, independently-verified pieces into one CSV
report, so scenery identity (DAE <-> Blender), group/override membership,
and the real SceneryGuid all show up on one row per scenery node:

  - nfs_scenery_dae_scan.parse_section_folder()  -> per-section DAE nodes
    (identity/position as AssetDumper wrote them)
  - nfs_scn_ref_match.match_by_section()          -> DAE <-> Blender object
    matching (unchanged - same bbox/origin dual-distance, mesh-name
    pre-filter, and ambiguity detection already fixed and verified there)
  - nfs_stream_scenery.load_stream_scenery()      -> SceneryGuid, group/
    override membership (groups_for()), reading the stream file and,
    optionally, a region file merged on top (a region file can carry the
    override/group system with zero instance data - see that module's own
    docstring - so merging lets a level's overrides be found regardless of
    which file they actually live in for a given game/build)

This script does NOT reimplement or alter any of those three - it only
calls them and joins the results. If a match/group/instance behavior looks
wrong, the bug almost certainly belongs in one of those modules, not here.

IMPORTANT PRECONDITION: the group/instance join below is
(section, dae_node_index) -> stream_scenery's (section_number,
instance_number). This is only correct once AssetDumper's per-section
instance counter is fixed to increment before its RFL_/SHD_/failed-lookup
skip checks (a real, confirmed bug at the time this script was written -
today dae_node_index is the position in the FILTERED node list, not the
true raw instance number). Re-export and re-run nfs_scenery_dae_scan
against the fixed AssetDumper build before trusting the group/override
columns this script adds - the identity/match columns from the other two
modules are unaffected by that bug and are trustworthy regardless.

Usage:
    python3 nfs_scn_ref_report.py DAE_FOLDER BLENDER_JSON STREAM_BUN OUT_CSV [REGION_BUN] [tolerance]
    python3 nfs_scn_ref_report.py
        (no args - pops up pickers instead; region file pick can be
        cancelled/skipped, everything else is required)
"""
# stream bootstrap: make the shared library folders importable
import pathlib as _pl, sys as _sys
_sys.path[:0] = [str(_pl.Path(__file__).resolve().parents[1] / _d) for _d in ['common', 'region', 'scenery', 'solids_materials']]

import csv
import json
import sys

from nfs_scenery_dae_scan import parse_section_folder
from nfs_scn_ref_match import match_by_section, write_report as _base_write_report
from nfs_stream_scenery import load_stream_scenery


EXTRA_FIELDNAMES = ['scenery_guid', 'stream_name', 'override_groups']


def enrich_with_stream_scenery(results, stream_scenery):
    """Adds scenery_guid/stream_name/override_groups to each result row IN
    PLACE, looked up via (section, dae_node_index) against stream_scenery.
    Rows with no section/dae_node_index (the EXTRA_IN_BLENDER rows written
    by nfs_scn_ref_match) or whose index falls outside the stream data's
    real instance list are left with blank enrichment columns rather than
    guessed at or skipped - a blank is a visible, honest "no data for
    this", never silently wrong."""
    missing_section = 0
    missing_instance = 0

    for r in results:
        r['scenery_guid'] = ''
        r['stream_name'] = ''
        r['override_groups'] = ''

        section = r.get('section')
        node_index = r.get('dae_node_index')
        if section == '' or node_index == '':
            continue  # EXTRA_IN_BLENDER row - nothing to join against

        section = int(section)
        node_index = int(node_index)

        stream_section = stream_scenery.sections.get(section)
        if stream_section is None:
            missing_section += 1
        elif not (0 <= node_index < len(stream_section.instances)):
            missing_instance += 1
        else:
            instance = stream_section.instances[node_index]
            r['scenery_guid'] = f"0x{instance.scenery_guid:08X}"
            r['stream_name'] = stream_section.name_for(instance) or ''

        groups = stream_scenery.groups_for(section, node_index)
        if groups:
            r['override_groups'] = '; '.join(g.display_key() for g in groups)

    if missing_section or missing_instance:
        print(f"[nfs_scn_ref_report] {missing_section} row(s) had no stream data for "
              f"their section at all, {missing_instance} row(s) had a dae_node_index "
              f"outside that section's real instance count - expected until the "
              f"AssetDumper instance-index fix lands and the DAE folder is re-exported "
              f"with it; if this is already a post-fix export, these numbers are worth "
              f"investigating rather than ignoring.")


def write_full_report(results, unmatched_blender, out_path):
    """Same shape as nfs_scn_ref_match.write_report, with the three
    enrichment columns appended - written directly here (rather than
    patched into that module) so that module stays untouched and its own
    tests/behavior can't be affected by this script."""
    from nfs_scn_ref_match import write_report
    import io

    # Reuse write_report's exact row-shape logic for the base columns by
    # writing to an in-memory CSV first, then re-reading it back out and
    # adding the three extra columns per row - avoids duplicating
    # write_report's EXTRA_IN_BLENDER row-construction logic here.
    buf = io.StringIO()
    base_fieldnames = ['section', 'dae_node_index', 'dae_node_id', 'dae_name', 'dae_geometry_name',
                        'position_is_exact', 'dae_position', 'matched_blender_name',
                        'matched_blender_mesh', 'matched_via', 'distance', 'status']

    # results already carry the enrichment columns (added in place above) -
    # write everything in one pass instead of round-tripping through
    # write_report(), so the enrichment columns aren't lost.
    fieldnames = base_fieldnames + EXTRA_FIELDNAMES
    with open(out_path, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in results:
            w.writerow({k: r.get(k, '') for k in fieldnames})
        for name in unmatched_blender:
            row = {k: '' for k in fieldnames}
            row['matched_blender_name'] = name
            row['status'] = 'EXTRA_IN_BLENDER'
            w.writerow(row)


def _pick_paths():
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()

    dae_folder = filedialog.askdirectory(
        title="Select the folder of section .dae exports (one per SectionNumber)")
    if not dae_folder:
        sys.exit("No DAE folder selected, aborting.")

    blender_json_path = filedialog.askopenfilename(
        title="Select the Blender position dump (.json)",
        filetypes=[("JSON files", "*.json"), ("All files", "*.*")])
    if not blender_json_path:
        sys.exit("No Blender dump selected, aborting.")

    stream_path = filedialog.askopenfilename(
        title="Select the stream .BUN file (scenery instances/groups)",
        filetypes=[("Bundle files", "*.bun *.BUN"), ("All files", "*.*")])
    if not stream_path:
        sys.exit("No stream file selected, aborting.")

    region_path = filedialog.askopenfilename(
        title="Optionally select a region .BUN file to merge (Cancel to skip)",
        filetypes=[("Bundle files", "*.bun *.BUN"), ("All files", "*.*")])
    # Cancel is fine here - region_path just comes back as '' and is skipped.

    out_path = filedialog.asksaveasfilename(
        title="Save the combined report as...",
        defaultextension=".csv",
        filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
    if not out_path:
        sys.exit("No output path chosen, aborting.")

    root.destroy()
    return dae_folder, blender_json_path, stream_path, region_path, out_path


if __name__ == '__main__':
    if len(sys.argv) >= 5:
        dae_folder = sys.argv[1]
        blender_json_path = sys.argv[2]
        stream_path = sys.argv[3]
        out_path = sys.argv[4]
        region_path = sys.argv[5] if len(sys.argv) > 5 else ''
        tolerance = float(sys.argv[6]) if len(sys.argv) > 6 else 2.0
    else:
        dae_folder, blender_json_path, stream_path, region_path, out_path = _pick_paths()
        tolerance = 2.0

    sections = parse_section_folder(dae_folder)
    total_nodes = sum(len(nodes) for nodes in sections.values())
    print(f"Loaded {total_nodes} scenery nodes across {len(sections)} sections "
          f"from {dae_folder!r}")

    blender_records = json.load(open(blender_json_path))
    print(f"Loaded {len(blender_records)} Blender object positions from {blender_json_path!r}")

    stream_scenery = load_stream_scenery(stream_path)
    if region_path:
        region_scenery = load_stream_scenery(region_path)
        stream_scenery.merge(region_scenery)
        print(f"Merged region file data from {region_path!r} - combined: {stream_scenery.summary()}")

    results, unmatched_blender = match_by_section(sections, blender_records, tolerance)
    enrich_with_stream_scenery(results, stream_scenery)
    write_full_report(results, unmatched_blender, out_path)

    from nfs_scn_ref_match import summarize
    summarize(results, unmatched_blender)
    groups_found = sum(1 for r in results if r['override_groups'])
    print(f"{groups_found} row(s) matched at least one override group")
    print(f"Report written to {out_path}")
