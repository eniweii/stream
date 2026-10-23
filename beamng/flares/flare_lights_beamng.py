#!/usr/bin/env python3
"""Build BeamNG flare datablocks and PointLights from an enriched Carbon flare TSV.

Input: output of flares/flare_report.py --enriched (flares_full.tsv)
Outputs next to the TSV by default:
  - nfsc.flares.datablocks.json : LightFlareData + LightAnimData definitions
  - items.level.json             : JSONL PointLight objects
  - nfsc_flares_build_report.txt : counts, scale approximations, texture checks

The five flare sprites already belong under levels/nfsc/Assets/Flares/. The generator
finds them there relative to this script's repository root (and the current working
folder), reuses the actual filename/extension in flareTexture, and reads dimensions for
elementRect. --texture-source-dir is optional for dimensions when source assets live elsewhere.
"""

import argparse
import csv
import json
import re
import struct
import sys
import zlib
from collections import defaultdict
from pathlib import Path

CONFIG = {
    "PREFIX": "nfsc_",
    "TEXTURE_DIR": "levels/nfsc/Assets/Flares/",
    "TEXTURE_EXT": ".dds",
    "TEXTURE_SIZE": (256, 256),
    "OVERALL_SCALE_MULTIPLIER": 1.0,
    "LIGHT_FLARE_SCALE": 1.0,
    "HEIGHT_OFFSET": 150.0,
    "OCCLUSION_RADIUS": 0.0,
    "RENDER_REFLECT_PASS": False,
}

TEXTURE_NAMES = {
    "HEADLIGHTFLAREINNER",
    "HEADLIGHTFLAREOUTER",
    "HEADLIGHTGLOW",
    "HEADLIGHTCATSEYE",
    "LAMPPOSTFLARE",
}
IMAGE_EXTENSIONS = (".png", ".dds", ".tga", ".bmp", ".jpg", ".jpeg", ".tif", ".tiff")
# Carbon's timer is a half-period: amber 0.4s, red 0.5s, green 0.45s on and off.
BLINK_CYCLES = {"amber": 0.8, "red": 1.0, "green": 0.9}
BLINK_LABELS = {"amber": "amber", "red": "red", "green": "green"}


def parse_float(value, default=0.0):
    try:
        text = str(value).strip()
        return float(text) if text else default
    except (ValueError, TypeError):
        return default


def optional_float(value):
    if value is None or not str(value).strip():
        return None
    try:
        return float(str(value).strip())
    except (ValueError, TypeError):
        return None


def safe_name(raw):
    """Convert external vault names into stable datablock identifiers."""
    slug = re.sub(r"[^A-Za-z0-9_]+", "_", str(raw).strip()).strip("_").lower()
    return slug or "unnamed"


def flare_datablock_name(params_name, used_names):
    base = CONFIG["PREFIX"] + "flare_" + safe_name(params_name)
    name = base
    if name in used_names and used_names[name] != params_name:
        name = f"{base}_{zlib.crc32(str(params_name).encode('utf-8')):08x}"
    used_names[name] = params_name
    return name


def texture_stem(value):
    raw = Path(str(value).strip().replace("\\", "/")).name
    return Path(raw).stem.upper()


def texture_game_path(texture, texture_dir, texture_ext):
    stem = texture_stem(texture)
    folder = str(texture_dir).replace("\\", "/").strip("/")
    ext = texture_ext if texture_ext.startswith(".") else "." + texture_ext
    return f"{folder}/{stem}{ext}" if folder else f"{stem}{ext}"


def repo_root():
    """Root of the checked-out stream repository (beamng/flares/this_file.py)."""
    return Path(__file__).resolve().parents[2]


def find_level_texture_file(stem, texture_dir, preferred_ext):
    """Find an existing texture under the configured game asset folder.

    Keep the actual asset's case and extension in the emitted BeamNG path. Prefer the
    configured extension only when more than one version of the same sprite exists.
    """
    ext = preferred_ext if preferred_ext.startswith(".") else "." + preferred_ext
    extensions = [ext] + [e for e in IMAGE_EXTENSIONS if e.lower() != ext.lower()]
    folders = []
    configured = Path(texture_dir)
    folders.append(configured if configured.is_absolute() else repo_root() / configured)
    cwd_folder = configured if configured.is_absolute() else Path.cwd() / configured
    if cwd_folder not in folders:
        folders.append(cwd_folder)
    seen = set()
    for root in folders:
        marker = str(root.resolve())
        if marker in seen:
            continue
        seen.add(marker)
        if not root.is_dir():
            continue
        for suffix in extensions:
            for filename in (stem + suffix, stem.lower() + suffix):
                candidate = root / filename
                if candidate.is_file():
                    return candidate
        # Last-resort case-insensitive stem match for existing assets whose filename
        # uses mixed case not covered by all-uppercase/all-lowercase variants.
        for candidate in root.iterdir():
            if candidate.is_file() and candidate.stem.casefold() == stem.casefold() and candidate.suffix.lower() in {e.lower() for e in extensions}:
                return candidate
    return None


def resolved_texture_game_path(texture, texture_dir, texture_ext):
    """Use an existing level asset's actual filename and extension if available."""
    stem = texture_stem(texture)
    found = find_level_texture_file(stem, texture_dir, texture_ext)
    folder = str(texture_dir).replace("\\", "/").strip("/")
    if found:
        filename = found.name
    else:
        ext = texture_ext if texture_ext.startswith(".") else "." + texture_ext
        filename = stem + ext
    return f"{folder}/{filename}" if folder else filename


def image_dimensions(path):
    """Read common image dimensions without needing Pillow or a DDS decoder."""
    try:
        data = Path(path).read_bytes()[:32]
    except OSError:
        return None
    # PNG: width/height are big-endian in IHDR at byte 16.
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        width, height = struct.unpack(">II", data[16:24])
        return (width, height) if width and height else None
    # DDS header: height and width at offsets 12 and 16.
    if data.startswith(b"DDS ") and len(data) >= 20:
        height, width = struct.unpack_from("<II", data, 12)
        return (width, height) if width and height else None
    # TGA header: width / height are little-endian 16-bit values.
    if len(data) >= 18 and data[2] in (1, 2, 3, 9, 10, 11):
        width, height = struct.unpack_from("<HH", data, 12)
        return (width, height) if width and height else None
    # BMP: width and height are signed 32-bit values from offset 18.
    if data.startswith(b"BM") and len(data) >= 26:
        width, height = struct.unpack_from("<ii", data, 18)
        return (abs(width), abs(height)) if width and height else None
    return None


def find_texture_file(stem, source_dirs):
    for root in source_dirs:
        if not root:
            continue
        root = Path(root)
        if not root.is_dir():
            continue
        # Exact-case match first, then common image extensions.
        candidates = [root / f"{stem}{ext}" for ext in IMAGE_EXTENSIONS]
        candidates.extend(root / f"{stem.lower()}{ext}" for ext in IMAGE_EXTENSIONS)
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        # Allow mixed-case filenames and extension case in existing level assets.
        allowed_extensions = {ext.lower() for ext in IMAGE_EXTENSIONS}
        for candidate in root.iterdir():
            if (candidate.is_file() and candidate.stem.casefold() == stem.casefold()
                    and candidate.suffix.lower() in allowed_extensions):
                return candidate
    return None


def detect_texture_dimensions(stem, source_dirs, fallback_size):
    source = find_texture_file(stem, source_dirs)
    if source:
        dims = image_dimensions(source)
        if dims:
            return dims, source, None
        return fallback_size, source, f"could not read dimensions of {source}"
    return fallback_size, None, f"{stem}: no source image found; elementRect uses fallback {fallback_size[0]}x{fallback_size[1]}"


def entry_scale(row, layer, multiplier):
    """Choose a fixed BeamNG scale from Carbon's distance-dependent scale range."""
    lo = optional_float(row.get(f"{layer}_MinScale"))
    hi = optional_float(row.get(f"{layer}_Maxscale"))
    if lo is None and hi is None:
        value = 1.0
    elif lo is None:
        value = hi
    elif hi is None:
        value = lo
    else:
        value = (lo + hi) / 2.0
    if value <= 0.0:
        value = 1.0
    return round(value * multiplier, 6)


def light_color(row, layer):
    """Use marker tint when non-zero; otherwise Carbon's vault flare colour."""
    tint = str(row.get("tint") or "").strip()
    if re.fullmatch(r"[0-9a-fA-F]{8}", tint) and tint.lower() != "00000000":
        return [round(int(tint[i:i + 2], 16) / 255.0, 6) for i in range(0, 8, 2)]
    return [
        round(parse_float(row.get(f"{layer}_r"), 1.0), 6),
        round(parse_float(row.get(f"{layer}_g"), 1.0), 6),
        round(parse_float(row.get(f"{layer}_b"), 1.0), 6),
        round(parse_float(row.get(f"{layer}_a"), 1.0), 6),
    ]


def layer_is_valid(row, layer):
    # The enriched report column is L0_params / L1_params (plural).
    return bool((row.get(f"{layer}_params") or "").strip() and (row.get(f"{layer}_texture") or "").strip())


def read_enriched_tsv(path):
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        required = {"section", "instance", "type_name", "x", "y", "z", "tint", "L0_params", "L0_texture"}
        missing = sorted(required - set(reader.fieldnames or []))
        if missing:
            raise ValueError(
                "Input is not the enriched flare TSV. Missing columns: " + ", ".join(missing) +
                ". Run flares/flare_report.py ... --enriched flares_full.tsv first."
            )
        return list(reader)


def build_outputs(rows, output_dir, *, texture_dir=None, texture_ext=None, texture_source_dir=None,
                  texture_size=None, overall_scale_multiplier=None, light_flare_scale=None,
                  height_offset=None, occlusion_radius=None, render_reflect_pass=None):
    """Build output JSON and return stats; split out for deterministic tests."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    texture_dir = CONFIG["TEXTURE_DIR"] if texture_dir is None else texture_dir
    texture_ext = CONFIG["TEXTURE_EXT"] if texture_ext is None else texture_ext
    texture_size = tuple(texture_size or CONFIG["TEXTURE_SIZE"])
    overall_scale_multiplier = CONFIG["OVERALL_SCALE_MULTIPLIER"] if overall_scale_multiplier is None else overall_scale_multiplier
    light_flare_scale = CONFIG["LIGHT_FLARE_SCALE"] if light_flare_scale is None else light_flare_scale
    height_offset = CONFIG["HEIGHT_OFFSET"] if height_offset is None else height_offset
    occlusion_radius = CONFIG["OCCLUSION_RADIUS"] if occlusion_radius is None else occlusion_radius
    render_reflect_pass = CONFIG["RENDER_REFLECT_PASS"] if render_reflect_pass is None else render_reflect_pass

    source_dirs = []
    if texture_source_dir:
        source_dirs.append(Path(texture_source_dir))
    # Convenient automatic checks: in-level assets, near TSV/output, and CWD Assets/Flares.
    target_asset_dir = Path(texture_dir)
    if not target_asset_dir.is_absolute():
        source_dirs.append(repo_root() / target_asset_dir)
    source_dirs.extend([Path.cwd() / texture_dir, output_dir / "Assets" / "Flares", output_dir])
    flare_blocks = {}
    flare_signature = {}
    used_names = {}
    animation_blocks = {}
    level_lines = []
    warnings = []
    layers_written = 0
    skipped_rows = 0
    layers_skipped = 0
    texture_dims_used = {}
    instance_occurrence = defaultdict(int)
    blink_lights = 0
    static_lights = 0
    layer_counts = defaultdict(int)

    for row_index, row in enumerate(rows, start=2):
        section = (row.get("section") or "").strip()
        instance = (row.get("instance") or "").strip()
        type_name = (row.get("type_name") or "unknown").strip()
        override_name = (row.get("override_names") or "").strip()
        x = parse_float(row.get("x"))
        y = parse_float(row.get("y"))
        z = parse_float(row.get("z")) + height_offset
        made_light = False

        # Keep the existing naming scheme: occurrence count is local to (section, instance),
        # while L0/L1 are sibling layers for one marker row.
        instance_occurrence[(section, instance)] += 1
        entry_num = instance_occurrence[(section, instance)]
        for layer in ("L0", "L1"):
            if not layer_is_valid(row, layer):
                if (row.get(f"{layer}_params") or "").strip() or (row.get(f"{layer}_texture") or "").strip():
                    layers_skipped += 1
                    warnings.append(f"row {row_index} {layer}: incomplete params/texture metadata; layer skipped")
                continue
            params_name = (row.get(f"{layer}_params") or "").strip()
            texture_name = texture_stem(row.get(f"{layer}_texture"))
            if not texture_name:
                layers_skipped += 1
                warnings.append(f"row {row_index} {layer}: missing texture name; layer skipped")
                continue
            if texture_name not in TEXTURE_NAMES:
                warnings.append(f"row {row_index} {layer}: unrecognized Carbon flare texture {texture_name!r}")
            scale = entry_scale(row, layer, overall_scale_multiplier)
            dimensions, source_image, dimension_warning = detect_texture_dimensions(texture_name, source_dirs, texture_size)
            if dimension_warning:
                warnings.append(dimension_warning)
            texture_dims_used[texture_name] = dimensions
            game_texture = resolved_texture_game_path(texture_name, texture_dir, texture_ext)
            name = flare_datablock_name(params_name, used_names)
            signature = (texture_name, game_texture, dimensions, scale)
            if name not in flare_blocks:
                width, height = dimensions
                # Match BeamNG's own serialized LightFlareData schema: an uppercase
                # Elements array containing per-element objects, rather than Torque-style
                # parallel arrays. BeamNG's saved examples expose 20 element slots.
                # Use numeric component arrays for elementRect/elementTint (not strings).
                elements = [{
                    "elementDist": 0.0,
                    "elementRect": [0, 0, int(width), int(height)],
                    "elementRotate": False,
                    "elementScale": 1.0,
                    "elementTint": [1.0, 1.0, 1.0, 1.0],
                    "elementUseLightColor": True,
                }]
                # Unused slots mirror the null placeholders in BeamNG's stock flare
                # datablocks. This keeps the serialized layout compatible with its editor.
                for _ in range(19):
                    elements.append({
                        "elementDist": None,
                        "elementRect": None,
                        "elementRotate": None,
                        "elementScale": None,
                        "elementTint": None,
                        "elementUseLightColor": None,
                    })
                flare_blocks[name] = {
                    "name": name,
                    "class": "LightFlareData",
                    "flareTexture": game_texture,
                    "overallScale": scale,
                    "occlusionRadius": round(float(occlusion_radius), 6),
                    "renderReflectPass": bool(render_reflect_pass),
                    "Elements": elements,
                }
                flare_signature[name] = signature
            elif flare_signature[name] != signature:
                raise ValueError(
                    f"Vault entry {params_name!r} produces conflicting flare data in row {row_index}: "
                    f"existing={flare_signature[name]!r}, new={signature!r}. Check the enriched TSV."
                )

            light = {
                "name": f"generatedLight_{section}_{instance}_{entry_num}_{layer}",
                "class": "PointLight",
                "__parent": type_name,
                "position": [round(x, 4), round(y, 4), round(z, 4)],
                "animate": False,
                "brightness": 0.397887349,
                "castShadows": type_name.lower() == "lamppost",
                "color": light_color(row, layer),
                "intensity": 25000,
                "radius": 50.0 if type_name.lower() == "lamppost" else 1.0,
                "shadowDarkenColor": [0, 0, 0, 0],
                "texSize": 256,
                "flareType": name,
                "flareScale": round(float(light_flare_scale), 6),
                "sectionID": section,
            }
            if override_name:
                light["sectionOverrideName"] = override_name

            blink_timer = (row.get("blink_timer") or "").strip().lower()
            if blink_timer in BLINK_LABELS:
                cycle = optional_float(row.get("blink_cycle_s"))
                if cycle is None or cycle <= 0:
                    cycle = BLINK_CYCLES[blink_timer]
                anim_name = CONFIG["PREFIX"] + "lightAnim_blink_" + blink_timer
                if anim_name not in animation_blocks:
                    animation_blocks[anim_name] = {
                        "name": anim_name,
                        "class": "LightAnimData",
                        "brightnessA": 0.0,
                        "brightnessZ": 1.0,
                        "brightnessPeriod": round(cycle, 6),
                        "brightnessKeys": "aza",
                        "brightnessSmooth": False,
                    }
                else:
                    # If a malformed TSV assigns different cycles to a shared timer, don't hide it.
                    existing_cycle = animation_blocks[anim_name]["brightnessPeriod"]
                    if abs(existing_cycle - cycle) > 1e-5:
                        warnings.append(f"{blink_timer} blink cycle mismatch {existing_cycle} vs {cycle}; using first")
                light["animate"] = True
                light["animationType"] = anim_name
                light["animationPeriod"] = round(cycle, 6)
                # Carbon's timer is shared globally; keep each light on the same phase.
                light["animationPhase"] = 0.0
                blink_lights += 1
            else:
                static_lights += 1

            level_lines.append(json.dumps(light, separators=(",", ":")))
            made_light = True
            layers_written += 1
            layer_counts[layer] += 1

        if not made_light:
            skipped_rows += 1

    datablocks = {}
    datablocks.update(flare_blocks)
    datablocks.update(animation_blocks)
    datablocks_path = output_dir / "nfsc.flares.datablocks.json"
    items_path = output_dir / "items.level.json"
    report_path = output_dir / "nfsc_flares_build_report.txt"
    with datablocks_path.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(datablocks, fh, indent=2)
        fh.write("\n")
    with items_path.open("w", encoding="utf-8", newline="\n") as fh:
        for line in level_lines:
            fh.write(line + "\n")

    lines = [
        "Carbon flare -> BeamNG build report",
        f"Input flare marker rows:        {len(rows)}",
        f"PointLights written:             {layers_written}",
        f"  L0 layers:                     {layer_counts['L0']}",
        f"  L1 layers:                     {layer_counts['L1']}",
        f"Rows skipped (no mapped layer):  {skipped_rows}",
        f"Layers skipped:                  {layers_skipped}",
        f"LightFlareData datablocks:        {len(flare_blocks)}",
        f"LightAnimData datablocks:         {len(animation_blocks)}",
        f"Blinking PointLights:             {blink_lights}",
        f"Static PointLights:               {static_lights}",
        "",
        "Configuration:",
        f"  Texture game path:              {texture_dir}",
        f"  Texture extension:              {texture_ext}",
        f"  Fallback texture size:          {texture_size[0]}x{texture_size[1]}",
        f"  Overall-scale multiplier:        {overall_scale_multiplier}",
        f"  PointLight flareScale:           {light_flare_scale}",
        f"  Height offset:                  +{height_offset}",
        "",
        "Approximation notes:",
        "  overallScale = midpoint(MinScale, Maxscale) * multiplier; non-positive/missing ranges use 1.0.",
        "  Carbon MinSize, MaxSize and Power are distance-dependent and have no direct LightFlareData equivalent.",
        "  Each vault entry is emitted once; every exported flare uses one sprite at elementDist=0.",
        "  Blink animation uses a non-smoothed A/Z brightness step; validate period/duty cycle in-game.",
        "  A non-zero marker tint overrides the vault colour; an all-zero tint falls back to L*_rgba.",
        "",
        "Texture dimensions used:",
    ]
    for stem, (width, height) in sorted(texture_dims_used.items()):
        lines.append(f"  {stem}: {width}x{height}")
    if warnings:
        lines += ["", f"Warnings ({len(warnings)}):"]
        lines.extend(f"  - {warning}" for warning in dict.fromkeys(warnings))
    lines += ["", "Generated files:", f"  {datablocks_path}", f"  {items_path}"]
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return {
        "rows": len(rows), "lights": layers_written, "flare_datablocks": len(flare_blocks),
        "animation_datablocks": len(animation_blocks), "skipped_rows": skipped_rows,
        "warnings": list(dict.fromkeys(warnings)), "datablocks_path": str(datablocks_path),
        "items_path": str(items_path), "report_path": str(report_path),
    }


def choose_tsv():
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as exc:  # pragma: no cover - only needed for GUI launches
        raise RuntimeError("Pass the enriched TSV path as an argument when Tk is unavailable.") from exc
    root = tk.Tk()
    root.withdraw()
    path = filedialog.askopenfilename(
        title="Select enriched Carbon flare TSV (flares_full.tsv)",
        filetypes=[("TSV files", "*.tsv"), ("Text files", "*.txt"), ("All files", "*.*")],
    )
    root.destroy()
    return Path(path) if path else None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Generate BeamNG LightFlareData, LightAnimData and PointLights.")
    parser.add_argument("tsv", nargs="?", help="Enriched TSV from flare_report.py --enriched (uses file picker if omitted)")
    parser.add_argument("--output-dir", help="Output folder (default: same folder as input TSV)")
    parser.add_argument("--texture-dir", default=CONFIG["TEXTURE_DIR"], help="Game-relative folder containing converted sprite textures")
    parser.add_argument("--texture-ext", default=CONFIG["TEXTURE_EXT"], help="Fallback texture extension if no existing level asset is found (default: .dds)")
    parser.add_argument("--texture-source-dir", help="Optional extra folder with images, used only if dimensions can't be read from levels/nfsc/Assets/Flares")
    parser.add_argument("--texture-size", nargs=2, type=int, default=CONFIG["TEXTURE_SIZE"], metavar=("WIDTH", "HEIGHT"),
                        help="Fallback full-image pixel dimensions if texture files are not found (default: 256 256)")
    parser.add_argument("--overall-scale", type=float, default=CONFIG["OVERALL_SCALE_MULTIPLIER"], help="Multiplier applied to midpoint of Carbon MinScale/Maxscale")
    parser.add_argument("--flare-scale", type=float, default=CONFIG["LIGHT_FLARE_SCALE"], help="PointLight flareScale multiplier")
    parser.add_argument("--height-offset", type=float, default=CONFIG["HEIGHT_OFFSET"], help="World Z offset in metres (default +150)")
    parser.add_argument("--occlusion-radius", type=float, default=CONFIG["OCCLUSION_RADIUS"], help="LightFlareData occlusionRadius; 0 uses raycasts")
    parser.add_argument("--render-reflect-pass", action="store_true", help="Enable flare reflection pass")
    args = parser.parse_args(argv)

    gui_mode = not args.tsv
    tsv_path = Path(args.tsv) if args.tsv else choose_tsv()
    if not tsv_path:
        print("No file selected. Exiting.")
        return 0
    if not tsv_path.is_file():
        parser.error(f"input TSV does not exist: {tsv_path}")
    rows = read_enriched_tsv(tsv_path)
    output_dir = Path(args.output_dir) if args.output_dir else tsv_path.resolve().parent
    result = build_outputs(
        rows, output_dir, texture_dir=args.texture_dir, texture_ext=args.texture_ext,
        texture_source_dir=args.texture_source_dir, texture_size=tuple(args.texture_size),
        overall_scale_multiplier=args.overall_scale, light_flare_scale=args.flare_scale,
        height_offset=args.height_offset, occlusion_radius=args.occlusion_radius,
        render_reflect_pass=args.render_reflect_pass,
    )
    print(f"Wrote {result['lights']} PointLights, {result['flare_datablocks']} LightFlareData and "
          f"{result['animation_datablocks']} LightAnimData datablocks.")
    print(f"Datablocks -> {result['datablocks_path']}")
    print(f"Level objects -> {result['items_path']}")
    print(f"Report -> {result['report_path']}")
    if result["warnings"]:
        print(f"Warnings: {len(result['warnings'])}; see {result['report_path']}")
    if gui_mode:
        try:  # pragma: no cover - GUI only
            from tkinter import messagebox
            messagebox.showinfo("Flare Export Complete", f"Generated {result['lights']} PointLights.\n\n"
                                f"Datablocks:\n{result['datablocks_path']}\n\nObjects:\n{result['items_path']}")
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
