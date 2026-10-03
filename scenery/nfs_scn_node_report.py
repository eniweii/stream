"""
One CSV row per DAE scenery node, keyed by dae_node_id. No Blender dump and no
DAE<->Blender matching: the Blender importer now tags every object with the id
of the node it came from (custom property "dae_node"), so the Blender side
joins on that id directly (see apply_section_props_by_node.py).

Row sources (all unchanged modules, only called here):
  - nfs_scenery_dae_scan.parse_section_folder()  -> DAE nodes per section
  - nfs_stream_scenery.load_stream_scenery()     -> scenery_guid, stream name,
    override groups, joined on (section, dae_node_index) = stream instance
    number (needs an AssetDumper build where the instance counter increments
    before any skip, and a DAE folder exported with it)
  - nfs_scn_ref_report.enrich_with_stream_scenery() -> the join itself

Node numbers may jump (RFL_/SHD_/SHADOW instances are counted, then skipped).
That is expected. Nothing here assumes contiguous numbers.

Checks printed at the end:
  - rows with no stream data (section or instance index missing)
  - rows whose stream_name and dae_name disagree (prefix match, so a
    truncated name still counts as equal). A high number means the
    node-to-instance link is off.

Usage:
    python3 nfs_scn_node_report.py DAE_FOLDER STREAM_BUN OUT_CSV [REGION_BUN]
    python3 nfs_scn_node_report.py
        (no args - pops up pickers; the region file pick can be cancelled)
"""
# stream bootstrap: make the shared library folders importable
import pathlib as _pl, sys as _sys
_sys.path[:0] = [str(_pl.Path(__file__).resolve().parents[1] / _d) for _d in ['common', 'region', 'scenery', 'solids_materials']]

import csv
import sys

from nfs_scenery_dae_scan import parse_section_folder
from nfs_scn_ref_report import enrich_with_stream_scenery, EXTRA_FIELDNAMES
from nfs_stream_scenery import load_stream_scenery


BASE_FIELDNAMES = [
    'section', 'dae_node_index', 'dae_node_id', 'dae_name',
    'dae_geometry_name', 'position_is_exact', 'dae_position',
]
FIELDNAMES = BASE_FIELDNAMES + EXTRA_FIELDNAMES


def build_rows(sections):
    """One row per DAE node, ordered by section then node index."""
    rows = []
    for section in sorted(sections):
        for node in sorted(sections[section], key=lambda n: n.node_index):
            rows.append({
                'section': node.section,
                'dae_node_index': node.node_index,
                'dae_node_id': node.node_id,
                'dae_name': node.name,
                'dae_geometry_name': node.geometry_name,
                'position_is_exact': node.position_is_exact,
                'dae_position': node.position,
            })
    return rows


def names_agree(dae_name, stream_name):
    """Case-insensitive; equal, or one is a truncated prefix of the other."""
    a, b = dae_name.lower(), stream_name.lower()
    return a.startswith(b) or b.startswith(a)


def find_name_disagreements(rows):
    """Rows that have a stream_name but whose dae_name does not agree."""
    return [r for r in rows
            if r['stream_name'] and not names_agree(r['dae_name'], r['stream_name'])]


def write_rows(rows, out_path):
    with open(out_path, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, '') for k in FIELDNAMES})


def _pick_paths():
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()

    dae_folder = filedialog.askdirectory(
        title="Select the folder of section .dae exports (one per SectionNumber)")
    if not dae_folder:
        sys.exit("No DAE folder selected, aborting.")

    stream_path = filedialog.askopenfilename(
        title="Select the stream .BUN file (scenery instances/groups)",
        filetypes=[("Bundle files", "*.bun *.BUN"), ("All files", "*.*")])
    if not stream_path:
        sys.exit("No stream file selected, aborting.")

    region_path = filedialog.askopenfilename(
        title="Optionally select a region .BUN file to merge (Cancel to skip)",
        filetypes=[("Bundle files", "*.bun *.BUN"), ("All files", "*.*")])

    out_path = filedialog.asksaveasfilename(
        title="Save the node report as...",
        defaultextension=".csv",
        filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
    if not out_path:
        sys.exit("No output path chosen, aborting.")

    root.destroy()
    return dae_folder, stream_path, region_path, out_path


if __name__ == '__main__':
    if len(sys.argv) >= 4:
        dae_folder, stream_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
        region_path = sys.argv[4] if len(sys.argv) > 4 else ''
    else:
        dae_folder, stream_path, region_path, out_path = _pick_paths()

    sections = parse_section_folder(dae_folder)
    rows = build_rows(sections)
    print(f"Loaded {len(rows)} scenery nodes across {len(sections)} sections from {dae_folder!r}")

    stream_scenery = load_stream_scenery(stream_path)
    if region_path:
        stream_scenery.merge(load_stream_scenery(region_path))
        print(f"Merged region file data from {region_path!r} - combined: {stream_scenery.summary()}")

    enrich_with_stream_scenery(rows, stream_scenery)

    bad = find_name_disagreements(rows)
    print(f"{sum(1 for r in rows if r['override_groups'])} row(s) have at least one override group")
    print(f"{len(bad)} row(s) where stream_name and dae_name disagree"
          + (" - the node-to-instance link is off, first few:" if bad else " - link looks correct"))
    for r in bad[:10]:
        print(f"    {r['dae_node_id']}: dae_name={r['dae_name']!r} stream_name={r['stream_name']!r}")

    write_rows(rows, out_path)
    print(f"Report written to {out_path}")
