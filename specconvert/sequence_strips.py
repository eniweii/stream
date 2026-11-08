#!/usr/bin/env python3
r"""
sequence_strips.py

Turns the image sequences of material_animations.json into native BeamNG image sequences.

For every sequence it joins the frame textures, left to right, into one strip (one for the diffuse, one for
the emissive when the sequence has emissive frames), then writes the sequence settings on every stage that
shows the sequence in materials.json. The GPU then steps through the strip and Lua has nothing to rewrite.
material_animations.json is updated as well: each converted sequence gets a "strip" block and each of its
stage entries a "sequence" block, so the level host can switch between the native sequence and the Lua frame
swap (see lua/ge/extensions/nfsc/components/textureAnimation.lua).

The materials populator runs this itself: its "Native sequence strips (texconv)" option makes Populate
animations build the strips in the same pass, and its "Scale bit" option is the --no-scale switch. This file also
runs on its own, on the output of Populate animations (it reads the frame_swap entries that step writes):

    python sequence_strips.py "C:\path\main.materials.json" --textures "C:\path\textures"

Both ways are safe to repeat: a stage that was converted earlier is put back on its single frame texture first
(revert_strips, using the strip and sequence blocks of the old material_animations.json), then converted again.

Needs texconv.exe next to this script (or --texconv) and Pillow. Frames are decoded with texconv, joined with
Pillow and encoded again with texconv in the format of the first frame, with a full mip chain. No colour
conversion is asked of texconv, so the pixels are only recompressed.

Stage fields written (BeamNG v1.5 stage):
    baseColorMap, emissiveMap (when the stage has one and the sequence has emissive frames)
    animFlags            Sequence (16) + Scale (8), added to a scroll flag that is already there
    sequenceFramePerSec  the fps of the sequence
    sequenceSegmentSize  1 / number of frames

Use --no-scale to write only the Sequence bit (a mesh whose UV covers just the first frame needs that).
A sequence listed in --exclude (key or name) stays on the Lua frame swap. The mesh UVs must lie inside 0..1:
sequence animations do not work with tiled UVs.

Originals are never overwritten without a timestamped .bak next to them.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from PIL import Image

# texconv.exe is expected to sit next to this script, not on PATH
TEXCONV_DEFAULT = Path(__file__).resolve().parent / "texconv.exe"

SEQUENCE_ANIM_FLAG = 16       # Torque Material animFlags: Scroll 1, Rotate 2, Wave 4, Scale 8, Sequence 16
SCALE_ANIM_FLAG = 8           # with Scale the mesh UV may cover the whole strip
STRIP_FOLDER = "animstrips"   # created under the texture folder
ANIMATIONS_JSON_NAME = "material_animations.json"
MAX_STRIP_WIDTH = 8192        # wider strips are skipped, the sequence stays on the frame swap
HASH_RE = re.compile(r"0x([0-9A-Fa-f]{8})_")

DXGI_FORMATS = {
    28: "R8G8B8A8_UNORM", 71: "BC1_UNORM", 72: "BC1_UNORM_SRGB", 74: "BC2_UNORM", 75: "BC2_UNORM_SRGB",
    77: "BC3_UNORM", 78: "BC3_UNORM_SRGB", 98: "BC7_UNORM", 99: "BC7_UNORM_SRGB",
}
FOURCC_FORMATS = {b"DXT1": "BC1_UNORM", b"DXT3": "BC2_UNORM", b"DXT5": "BC3_UNORM"}
DEFAULT_FORMAT = "BC3_UNORM"


def run_texconv(texconv, args):
    cmd = [str(texconv), "-nologo", "-y"] + args
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"texconv failed: {' '.join(cmd)}\n{result.stdout}\n{result.stderr}")


def dds_format(path):
    """texconv format name of a DDS file, DEFAULT_FORMAT when the header is not one of the known ones."""
    try:
        with open(path, "rb") as f:
            head = f.read(148)
    except OSError:
        return DEFAULT_FORMAT
    if len(head) < 88 or head[0:4] != b"DDS ":
        return DEFAULT_FORMAT
    four_cc = head[84:88]
    if four_cc == b"DX10" and len(head) >= 132:
        return DXGI_FORMATS.get(int.from_bytes(head[128:132], "little"), DEFAULT_FORMAT)
    return FOURCC_FORMATS.get(four_cc, DEFAULT_FORMAT)


def disk_path_for(game_path, base_prefix, texture_root):
    """Disk file of a game path such as /levels/nfsc/assets/textures/x.dds, or None."""
    path = (game_path or "").replace("\\", "/")
    prefix = (base_prefix or "").replace("\\", "/")
    if not path or not prefix or not path.startswith(prefix):
        return None
    return os.path.join(texture_root, *path[len(prefix):].split("/"))


def detect_base_prefix(animations, texture_root):
    """The game path that texture_root stands for, found by locating the first frame file under it."""
    for anim in animations.values():
        for frame in anim.get("frames") or []:
            path = (frame.get("path") or "").replace("\\", "/")
            if not path:
                continue
            name = path.rsplit("/", 1)[-1]
            for current_root, _dirs, filenames in os.walk(texture_root):
                if name in filenames:
                    rel = os.path.relpath(os.path.join(current_root, name), texture_root).replace(os.sep, "/")
                    if path.endswith(rel):
                        return path[:-len(rel)]
    return ""


def safe_name(text):
    return re.sub(r"[^A-Za-z0-9_]+", "_", text or "").strip("_") or "SEQ"


def join_strip(images, cell_size, fill=(0, 0, 0, 255)):
    """One RGBA image: the frames left to right, each resized to cell_size. A None frame is a filled cell."""
    width, height = cell_size
    strip = Image.new("RGBA", (width * len(images), height), fill)
    for i, image in enumerate(images):
        if image is None:
            continue
        if image.size != cell_size:
            image = image.resize(cell_size, Image.LANCZOS)
        strip.paste(image, (i * width, 0))
    return strip


def build_strip_file(texconv, runner, sources, out_dir, stem, log, fill=(0, 0, 0, 255)):
    """sources: disk paths (None = missing frame). Writes out_dir/<stem>.dds and returns (cells, cell_w, cell_h)."""
    fmt = dds_format(next(s for s in sources if s))
    images = []
    with tempfile.TemporaryDirectory() as tmp:
        for i, source in enumerate(sources):
            if not source:
                images.append(None)
                continue
            frame_dir = os.path.join(tmp, f"f{i}")
            os.makedirs(frame_dir)
            runner(texconv, ["-f", "R8G8B8A8_UNORM", "-ft", "png", "-m", "1", "-o", frame_dir, source])
            png = os.path.join(frame_dir, Path(source).stem + ".png")
            with Image.open(png) as img:
                images.append(img.convert("RGBA"))
        sizes = [img.size for img in images if img is not None]
        cell = (max(s[0] for s in sizes), max(s[1] for s in sizes))
        if len(set(sizes)) > 1:
            log(f"    frames differ in size {sorted(set(sizes))}; every cell is resized to {cell[0]}x{cell[1]}")
        strip = join_strip(images, cell, fill)
        if strip.size[0] > MAX_STRIP_WIDTH:
            raise ValueError(f"strip would be {strip.size[0]} px wide (limit {MAX_STRIP_WIDTH})")
        strip_png = os.path.join(tmp, stem + ".png")
        strip.save(strip_png)
        os.makedirs(out_dir, exist_ok=True)
        runner(texconv, ["-f", fmt, "-ft", "dds", "-m", "0", "-o", str(out_dir), strip_png])
    log(f"    {stem}.dds: {len(sources)} cell(s) of {cell[0]}x{cell[1]}, {fmt}")
    return len(sources), cell[0], cell[1]


def wanted_animations(anim_data, exclude):
    """Keys of the sequences used by frame_swap stages and not excluded."""
    animations = anim_data.get("animations") or {}
    skip = {x.lower() for x in exclude or []}
    used = {}
    for entries in (anim_data.get("materials") or {}).values():
        for entry in entries:
            if isinstance(entry, dict) and entry.get("type") == "frame_swap" and entry.get("animation") in animations:
                used.setdefault(entry["animation"], []).append(entry)
    keys = []
    for key in sorted(used):
        if key.lower() in skip or str(animations[key].get("name") or "").lower() in skip:
            continue
        keys.append(key)
    return keys, used


def build_strips(anim_data, texture_root, base_prefix, texconv, exclude=None, log=print, runner=run_texconv, dry_run=False):
    """Writes the strip files. Returns key -> strip block (path, emissive, cells, cellWidth, cellHeight, segment)."""
    animations = anim_data.get("animations") or {}
    keys, used = wanted_animations(anim_data, exclude)
    strips = {}
    out_dir = os.path.join(texture_root, STRIP_FOLDER)
    for key in keys:
        anim = animations[key]
        name = safe_name(anim.get("name") or key)
        frames = anim.get("frames") or []
        log(f"  {anim.get('name') or key}: {len(frames)} frame(s), {anim.get('fps')} fps")
        try:
            if len(frames) < 2:
                raise ValueError("fewer than two frames")
            diffuse = [disk_path_for(f.get("path"), base_prefix, texture_root) for f in frames]
            missing = [i + 1 for i, d in enumerate(diffuse) if not d or not os.path.isfile(d)]
            if missing:
                raise ValueError(f"no texture file for frame(s) {missing}")
            stem = f"ANIMSTRIP_{name}_{key.replace('0x', '').replace('0X', '')}"
            block = {"path": f"{base_prefix}{STRIP_FOLDER}/{stem}.dds", "cells": len(frames), "segment": round(1.0 / len(frames), 8)}
            has_emissive_stage = any(e.get("emissive") for e in used[key])
            emissive = [disk_path_for(f.get("emissive"), base_prefix, texture_root) for f in frames]
            emissive = [e if e and os.path.isfile(e) else None for e in emissive]
            if dry_run:
                cells, cw, ch = len(frames), 0, 0
            else:
                cells, cw, ch = build_strip_file(texconv, runner, diffuse, out_dir, stem, log)
            block.update({"cellWidth": cw, "cellHeight": ch})
            if has_emissive_stage and any(emissive):
                absent = [i + 1 for i, e in enumerate(emissive) if not e]
                if absent:
                    log(f"    no emissive file for frame(s) {absent}; those cells are black in the emissive strip")
                if not dry_run:
                    build_strip_file(texconv, runner, emissive, out_dir, stem + "_emissive", log)
                block["emissive"] = f"{base_prefix}{STRIP_FOLDER}/{stem}_emissive.dds"
            elif has_emissive_stage:
                log("    the stages have an emissive map but no frame has an emissive file; the emissive map stays as it is")
            strips[key] = block
        except Exception as exc:
            log(f"    skipped, stays on the Lua frame swap: {exc}")
    return strips


def hash_of(path):
    match = HASH_RE.match((path or "").replace("\\", "/").rsplit("/", 1)[-1])
    return "0x" + match.group(1).upper() if match else None


def apply_strips(materials_data, anim_data, strips, use_scale=True, log=print):
    """Writes the sequence fields on the stages and the strip/sequence blocks in anim_data. Returns stage changes."""
    animations = anim_data.get("animations") or {}
    added = SEQUENCE_ANIM_FLAG | (SCALE_ANIM_FLAG if use_scale else 0)
    changed = 0
    for key, block in strips.items():
        animations[key]["strip"] = block
    for name, entries in (anim_data.get("materials") or {}).items():
        mat = materials_data.get(name)
        stages = (mat or {}).get("Stages") or []
        for entry in entries:
            block = strips.get(entry.get("animation")) if isinstance(entry, dict) and entry.get("type") == "frame_swap" else None
            if not block:
                continue
            index = int(entry.get("stage", 0))
            stage = stages[index] if index < len(stages) else None
            if not stage:
                log(f"  {name}: stage {index} not found in materials.json; left alone")
                continue
            frames = animations[entry["animation"]].get("frames") or []
            frame_hashes = {hash_of(f.get("path")) for f in frames}
            current = stage.get("baseColorMap")
            if current != block["path"] and hash_of(current) not in frame_hashes:
                log(f"  {name}: stage {index} shows {current}, which is not a frame of the sequence; left alone")
                continue
            old_flags = stage.get("animFlags") if isinstance(stage.get("animFlags"), int) else 0
            flags_off = old_flags & ~added
            wanted = {"baseColorMap": block["path"], "animFlags": flags_off | added,
                      "sequenceFramePerSec": float(animations[entry["animation"]].get("fps") or 0),
                      "sequenceSegmentSize": block["segment"]}
            if stage.get("emissiveMap") and block.get("emissive"):
                wanted["emissiveMap"] = block["emissive"]
            if any(stage.get(k) != v for k, v in wanted.items()):
                stage.update(wanted)
                changed += 1
            entry["sequence"] = {"flagsOn": flags_off | added, "flagsOff": flags_off}
    anim_data["version"] = max(3, int(anim_data.get("version") or 0))
    anim_data["notes"] = (str(anim_data.get("notes") or "").split(" Strips:")[0] +
                          " Strips: a sequence with a strip block can run natively (the strip is written on its stages, "
                          "animFlags sequence/scale plus sequenceFramePerSec and sequenceSegmentSize); a stage entry's "
                          "sequence block holds the animFlags to write for native (flagsOn) and for the frame swap (flagsOff).")
    return changed


def revert_strips(materials_data, anim_data, log=print):
    """Puts every stage that convert wrote back on its single frame texture and drops the strip and sequence
    blocks from anim_data. Returns the number of stages restored. Needs the old material_animations.json (the
    blocks say which frame a stage showed and which flags it had)."""
    animations = anim_data.get("animations") or {}
    restored = 0
    for name, entries in (anim_data.get("materials") or {}).items():
        stages = (materials_data.get(name) or {}).get("Stages") or []
        for entry in entries:
            seq = entry.pop("sequence", None) if isinstance(entry, dict) else None
            anim = animations.get(entry.get("animation")) if seq else None
            strip = (anim or {}).get("strip")
            index = int(entry.get("stage", 0)) if seq else 0
            stage = stages[index] if seq and index < len(stages) else None
            if not stage or not strip or stage.get("baseColorMap") != strip.get("path"):
                continue
            frames = anim.get("frames") or []
            frame = frames[int(entry.get("frame", 0))] if int(entry.get("frame", 0)) < len(frames) else {}
            if not frame.get("path"):
                log(f"  {name}: frame {entry.get('frame')} has no texture path, the stage keeps its strip")
                continue
            stage["baseColorMap"] = frame["path"]
            if strip.get("emissive") and stage.get("emissiveMap") == strip["emissive"]:
                if frame.get("emissive"):
                    stage["emissiveMap"] = frame["emissive"]
                else:
                    log(f"  {name}: frame {entry.get('frame')} has no emissive file, the stage keeps its emissive strip")
            flags_off = int(seq.get("flagsOff") or 0)
            if flags_off:
                stage["animFlags"] = flags_off
            else:
                stage.pop("animFlags", None)
            stage.pop("sequenceFramePerSec", None)
            stage.pop("sequenceSegmentSize", None)
            restored += 1
    for anim in animations.values():
        if isinstance(anim, dict):
            anim.pop("strip", None)
    return restored


def build_and_apply(materials_data, anim_data, texture_root, base_prefix, texconv, exclude=None, use_scale=True,
                    log=print, runner=run_texconv, dry_run=False):
    """The whole step on data in memory: puts earlier strips back, builds the strips, writes the stage fields.
    Returns (strips, number of stages restored or written, 0 means materials.json needs no write)."""
    restored = revert_strips(materials_data, anim_data, log)
    if restored:
        log(f"{restored} stage(s) of an earlier run put back on their frame texture")
    strips = build_strips(anim_data, texture_root, base_prefix, texconv, exclude, log, runner, dry_run)
    changed = apply_strips(materials_data, anim_data, strips, use_scale, log)
    return strips, changed + restored


def backup(path, log):
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    target = f"{path}.{stamp}.bak"
    shutil.copy2(path, target)
    log(f"Backup: {target}")


def convert(materials_path, texture_root, animations_path=None, texconv=TEXCONV_DEFAULT, exclude=None,
            use_scale=True, base_prefix=None, dry_run=False, log=print, runner=run_texconv):
    animations_path = animations_path or os.path.join(os.path.dirname(materials_path), ANIMATIONS_JSON_NAME)
    with open(materials_path, "r", encoding="utf-8") as f:
        materials_data = json.load(f)
    with open(animations_path, "r", encoding="utf-8") as f:
        anim_data = json.load(f)
    prefix = base_prefix if base_prefix is not None else detect_base_prefix(anim_data.get("animations") or {}, texture_root)
    if not prefix:
        raise SystemExit("Could not tell which game path the texture folder stands for; pass --base-prefix (e.g. /levels/nfsc/assets/textures/).")
    log(f"Texture folder {texture_root} = {prefix}")
    strips, changed = build_and_apply(materials_data, anim_data, texture_root, prefix, texconv, exclude, use_scale,
                                      log, runner, dry_run)
    log(f"{len(strips)} sequence(s) converted, {changed} stage(s) changed")
    if dry_run:
        log("Dry run: nothing written.")
        return strips
    if changed:
        backup(materials_path, log)
        with open(materials_path, "w", encoding="utf-8") as f:
            json.dump(materials_data, f, indent=4)
        log(f"Wrote {materials_path}")
    backup(animations_path, log)
    with open(animations_path, "w", encoding="utf-8") as f:
        json.dump(anim_data, f, indent=2)
    log(f"Wrote {animations_path}")
    return strips


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("materials", type=Path, help="materials.json the populator wrote")
    ap.add_argument("--textures", type=Path, required=True, help="texture folder (the one given to the populator)")
    ap.add_argument("--animations", type=Path, default=None, help="material_animations.json (default: next to materials.json)")
    ap.add_argument("--texconv", type=Path, default=TEXCONV_DEFAULT, help="Path to texconv.exe (default: next to this script)")
    ap.add_argument("--base-prefix", default=None, help="game path of the texture folder (default: detected)")
    ap.add_argument("--exclude", nargs="*", default=[], help="sequence keys or names that stay on the frame swap")
    ap.add_argument("--no-scale", action="store_true", help="write only the Sequence bit, not Scale")
    ap.add_argument("--dry-run", action="store_true", help="report what would change, write nothing")
    args = ap.parse_args()
    if not args.dry_run and not args.texconv.exists():
        sys.exit(f"texconv not found at {args.texconv}")
    convert(str(args.materials), str(args.textures), str(args.animations) if args.animations else None, args.texconv,
            args.exclude, not args.no_scale, args.base_prefix, args.dry_run)


if __name__ == "__main__":
    main()
