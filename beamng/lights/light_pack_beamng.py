#!/usr/bin/env python3
"""Build BeamNG PointLights from the Carbon light pack TSV (lights/light_pack_scan.py).

Input:  lights.tsv (outputs/light_pack_scan/lights.tsv)
Output (next to the TSV by default):
  - items.level.json              : JSONL PointLight objects
  - nfsc_light_pack_build_report.txt

The light pack lights are not real lights (spherical harmonics data), so every light
is a PointLight and nothing of the pack is converted beyond what is listed here:
  name        {name}_{pack_section}_{index}  (the light's own name, then pack section and index)
  position    world x, y, z + height offset (same offset as the flares)
  color       r, g, b of the packed color
  brightness  fixed 0.397887349 (BeamNG's value); intensity, far_start, far_end and falloff are not mapped
  sectionID   pack_section, so the section streamer can switch the lights with their section
"""
import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

CONFIG = {
    "HEIGHT_OFFSET": 150.0,
    "BRIGHTNESS": 0.397887349,
    "RADIUS_FROM_SIZE": True,   # AssetDumper used size as the radius; False uses FIXED_RADIUS
    "FIXED_RADIUS": 1.0,
}


def parse_float(value, default=0.0):
    try:
        text = str(value).strip()
        return float(text) if text else default
    except (ValueError, TypeError):
        return default


def object_name(row):
    """Light name from the TSV, then pack section and index (unique per light)."""
    name = (row.get("name") or "").strip() or "light"
    return f"{name}_{(row.get('pack_section') or '').strip()}_{(row.get('index') or '').strip()}"


def read_lights_tsv(path):
    with Path(path).open("r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


def build_outputs(rows, output_dir, *, height_offset=None, brightness=None, radius_from_size=None, fixed_radius=None):
    """Build items.level.json and the report; returns stats (split out for tests)."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    height_offset = CONFIG["HEIGHT_OFFSET"] if height_offset is None else height_offset
    brightness = CONFIG["BRIGHTNESS"] if brightness is None else brightness
    radius_from_size = CONFIG["RADIUS_FROM_SIZE"] if radius_from_size is None else radius_from_size
    fixed_radius = CONFIG["FIXED_RADIUS"] if fixed_radius is None else fixed_radius

    lines = []
    names = Counter()
    sections = Counter()
    for row in rows:
        section = (row.get("pack_section") or "").strip()
        name = object_name(row)
        names[name] += 1
        sections[section] += 1
        light = {
            "name": name,
            "class": "PointLight",
            "position": [round(parse_float(row.get("world_x")), 4),
                         round(parse_float(row.get("world_y")), 4),
                         round(parse_float(row.get("world_z")) + height_offset, 4)],
            "animate": False,
            "brightness": brightness,
            "castShadows": False,
            "color": [round(parse_float(row.get("r")), 6), round(parse_float(row.get("g")), 6),
                      round(parse_float(row.get("b")), 6), 1],
            "radius": round(parse_float(row.get("size")), 6) if radius_from_size else fixed_radius,
            "shadowDarkenColor": [0, 0, 0, 0],
            "texSize": 256,
            "sectionID": section,
        }
        lines.append(json.dumps(light, separators=(",", ":")))

    items_path = output_dir / "items.level.json"
    with items_path.open("w", encoding="utf-8", newline="\n") as fh:
        for line in lines:
            fh.write(line + "\n")

    duplicates = sorted(n for n, c in names.items() if c > 1)
    report_path = output_dir / "nfsc_light_pack_build_report.txt"
    report = [
        "Carbon light pack -> BeamNG build report",
        "",
        f"Rows read:            {len(rows)}",
        f"PointLights written:  {len(lines)}",
        f"Sections:             {len(sections)}",
        f"Height offset:        {height_offset}",
        f"Brightness:           {brightness}",
        f"Radius:               {'size column' if radius_from_size else fixed_radius}",
        f"Duplicate names:      {len(duplicates)}",
    ]
    report.extend(f"  {n}" for n in duplicates[:20])
    report_path.write_text("\n".join(report) + "\n", encoding="utf-8")

    return {"rows": len(rows), "lights": len(lines), "sections": len(sections), "duplicates": duplicates,
            "items_path": str(items_path), "report_path": str(report_path)}


def choose_tsv():
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as exc:  # pragma: no cover - only needed for GUI launches
        raise RuntimeError("Pass the lights.tsv path as an argument when Tk is unavailable.") from exc
    root = tk.Tk()
    root.withdraw()
    start = Path(__file__).resolve().parents[2] / "outputs" / "light_pack_scan"
    path = filedialog.askopenfilename(
        initialdir=str(start) if start.is_dir() else None,
        title="Select Carbon light pack TSV (lights.tsv)",
        filetypes=[("TSV files", "*.tsv"), ("Text files", "*.txt"), ("All files", "*.*")],
    )
    root.destroy()
    return Path(path) if path else None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Generate BeamNG PointLights from lights.tsv.")
    parser.add_argument("tsv", nargs="?", help="lights.tsv from lights/light_pack_scan.py (file picker if omitted)")
    parser.add_argument("--output-dir", help="Output folder (default: same folder as the input TSV)")
    parser.add_argument("--height-offset", type=float, default=CONFIG["HEIGHT_OFFSET"], help="World Z offset in metres (default +150)")
    parser.add_argument("--fixed-radius", type=float, help="Use this radius for every light instead of the size column")
    args = parser.parse_args(argv)

    gui_mode = not args.tsv
    tsv_path = Path(args.tsv) if args.tsv else choose_tsv()
    if not tsv_path:
        print("No file selected. Exiting.")
        return 0
    if not tsv_path.is_file():
        parser.error(f"input TSV does not exist: {tsv_path}")
    rows = read_lights_tsv(tsv_path)
    output_dir = Path(args.output_dir) if args.output_dir else tsv_path.resolve().parent
    result = build_outputs(rows, output_dir, height_offset=args.height_offset,
                           radius_from_size=args.fixed_radius is None,
                           fixed_radius=args.fixed_radius if args.fixed_radius is not None else CONFIG["FIXED_RADIUS"])
    print(f"Wrote {result['lights']} PointLights from {result['sections']} sections.")
    print(f"Level objects -> {result['items_path']}")
    print(f"Report -> {result['report_path']}")
    if result["duplicates"]:
        print(f"Warning: {len(result['duplicates'])} duplicate object names; see {result['report_path']}")
    if gui_mode:
        try:  # pragma: no cover - GUI only
            from tkinter import messagebox
            messagebox.showinfo("Light Pack Export Complete", f"Generated {result['lights']} PointLights.\n\n{result['items_path']}")
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
