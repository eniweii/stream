#!/usr/bin/env python3
"""
carbon_material_viewer.py

Viewer for carbon_material_dictionary.json (written by AssetDumper's
CarbonMaterialDictionary).

Materials tab
  - slots the exporter dropped (no texture found, or a wrong role such as a _D
    texture in the normal slot) are listed in grey; the variant is then replaced
    by the main variant and raises no conflict
  - every variant of a material side by side (slot textures, role, merge target)
  - thumbnails of all slots, plus the slots the variant becomes after the
    replace or merge
  - a live preview of the selected slot (frame swap and UV scroll)
Animations tab
  - frame-swap animations AND UV-scroll textures, with parameters, the
    materials that use them, and a live preview
Tools > Sort textures by alpha usage
  - copies the dumped textures into one folder per alpha usage and writes two
    CSV files that list textures and materials. Three folder modes:
      usage only                       alpha_modulated
      Modulated split by blend type    alpha_modulated/blend_additive
                                       (None and PunchThrough stay flat)
      every usage split by blend type  alpha_none/blend_srccopy

Previews and copying need a folder of dumped textures named like
0xF6E3C2E5_SGN_DRAGONCAFE_ANIM001.dds (hash prefix) or plain
SGN_DRAGONCAFE_ANIM001.png (name only). Previews need Pillow (pip install pillow).

Animation math is copied from hyperlinked (Carbon's real game code):
  frame swap : base = int(fps * time) % count. A material that references
               frame k shows frame (base + k) % count, so each referenced
               frame has its own phase.
  smooth     : offset = frac(time * speed)               speed = raw / 4096
  snap       : offset = frac(int(time / step) * speed)   step  = raw / 256
  offset_scale: offset = raw / -1024, scale = raw / 256 (static)
  A smooth or snap scroll with speed 0 and 0 is no scroll and is ignored.
Not in hyperlinked (the shader is not in that repo), so assumed here:
  smooth/snap  uv' = uv + offset
  offset_scale uv' = uv * scale + offset
Checked by eye against the game: the S and T scroll values drive the swapped
axes (S moves the image vertically, T horizontally). "Swap S/T" is on by
default. Use "Invert scroll" if the direction is still wrong.
Snap scroll: the preview extends one texture toward the scroll direction. A
dashed frame marks the normal one-texture view.

Usage: python carbon_material_viewer.py [dictionary.json] [--textures DIR]
"""

import argparse
import csv
import json
import math
import os
import re
import shutil
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    from PIL import Image, ImageOps, ImageTk
    HAVE_PIL = True
except ImportError:  # the viewer still works without previews
    HAVE_PIL = False

SLOT_ORDER = ["diffuse", "normal", "specular", "height", "opacity"]
TEXTURE_EXTS = (".dds", ".png", ".tga", ".jpg", ".jpeg", ".bmp")
HASH_PREFIX = re.compile(r"^(0x[0-9A-Fa-f]{8})_(.*)$")
ROLE_COLORS = {
    "recommended": "#d6f5d6", "replace": "#e6e6e6",
    "distinct": "#ffe9c7", "animated": "#d6e6ff", "static": "#f2f2f2",
}
ANY_ALPHA = "any alpha"
ANY_BLEND = "any blend"


# --------------------------------------------------------------------------
# data helpers
# --------------------------------------------------------------------------

def to_int(hex_text):
    try:
        return int(hex_text, 16)
    except (TypeError, ValueError):
        return None


def is_zero_scroll(sc):
    """Smooth or snap scroll with speed 0 and 0 does not move."""
    if not sc or sc.get("type") == "OffsetScale":
        return False
    s = sc.get("rawSpeedS", sc.get("speedS"))
    t = sc.get("rawSpeedT", sc.get("speedT"))
    return not s and not t


def effective_role(v):
    return "static" if v.get("_static") else v.get("role", "")


def material_tags(mat):
    tags = []
    variants = mat.get("variants") or []
    roles = {effective_role(v) for v in variants}
    for role in ("replace", "animated", "distinct"):
        if role in roles:
            tags.append(role)
    if any(v.get("mergeInto") for v in variants):
        tags.append("merge")
    if any(v.get("droppedSlots") for v in variants):
        tags.append("dropped")
    if mat.get("conflictingSlots"):
        tags.append("conflict")
    return tags


def solid_total(mat):
    return sum(v.get("solidCount", 0) for v in mat.get("variants") or [])


def scroll_text(rec):
    sc = (rec or {}).get("scroll")
    if not sc:
        return ""
    if sc.get("type") == "OffsetScale":
        return (f"OffsetScale off=({sc.get('offsetS', 0):.3f}, {sc.get('offsetT', 0):.3f}) "
                f"scale=({sc.get('scaleS', 0):.3f}, {sc.get('scaleT', 0):.3f})")
    text = f"{sc.get('type')} S={sc.get('speedS', 0):.4f}/s T={sc.get('speedT', 0):.4f}/s"
    if sc.get("timeStep") is not None:
        text += f" step={sc['timeStep']:.4f}"
    return text


def safe_name(text):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(text or "unknown"))


class Dictionary:
    def __init__(self, data):
        self.summary = data.get("summary", "")
        self.materials = data.get("materials") or {}
        self.textures = data.get("textures") or {}
        self.animations = data.get("animations") or {}

        # A scroll with speed 0 and 0 is no scroll. Drop it from the texture table.
        self.zero_scroll = 0
        for rec in self.textures.values():
            if is_zero_scroll(rec.get("scroll")):
                del rec["scroll"]
                self.zero_scroll += 1

        self.dropped = sum(len(v.get("droppedSlots") or [])
                           for m in self.materials.values() for v in m.get("variants") or [])

        self.anim_users = {}   # animation key -> [(material, variant id, frame)]
        self.tex_users = {}    # texture hash  -> [(material, variant id, slot)]
        for key, mat in self.materials.items():
            for v in mat.get("variants") or []:
                for slot, h in (v.get("slots") or {}).items():
                    self.tex_users.setdefault(h, []).append((key, v.get("id"), slot))
                for a in v.get("animated") or []:
                    if a.get("kind") == "frame_swap":
                        self.anim_users.setdefault(a.get("animation"), []).append(
                            (key, v.get("id"), a.get("frame")))

        self.frame_of = {}     # frame hash -> (animation key, index)
        for akey, a in self.animations.items():
            for i, h in enumerate(a.get("frames") or []):
                self.frame_of.setdefault(h, (akey, i))

        self.scroll_hashes = [h for h, r in self.textures.items() if r.get("scroll")]

        # An older dictionary marks a zero-scroll variant as animated. Show it as static.
        # (Re-export with the fixed exporter to get the real replace and merge roles.)
        for mat in self.materials.values():
            for v in mat.get("variants") or []:
                if v.get("role") == "animated" and not any(
                        self.attrs(h).get("scroll") or self.attrs(h).get("frameSwap")
                        for h in (v.get("slots") or {}).values()):
                    v["_static"] = True

    def attrs(self, h):
        """Texture record. A frame that no material references inherits the
        render attributes of a referenced frame of the same animation."""
        rec = self.textures.get(h)
        if rec is not None:
            return rec
        fo = self.frame_of.get(h)
        if fo:
            for sibling in self.animations[fo[0]].get("frames") or []:
                r = self.textures.get(sibling)
                if r:
                    out = {k: r[k] for k in ("width", "height", "alphaUsage", "alphaBlend", "tiling",
                                             "renderFlags") if r.get(k) is not None}
                    out["frameSwap"] = {"animation": fo[0], "frame": fo[1]}
                    out["inherited"] = True
                    return out
        return {}

    def variant(self, material, vid):
        for v in (self.materials.get(material) or {}).get("variants") or []:
            if v.get("id") == vid:
                return v
        return None

    def main_variant(self, mat):
        rec = mat.get("recommendedVariant")
        variants = mat.get("variants") or []
        for v in variants:
            if v.get("id") == rec:
                return v
        return variants[0] if variants else None

    def material_alpha(self, mat):
        """(alpha usage, blend) of the diffuse slot of the main variant."""
        v = self.main_variant(mat)
        h = ((v or {}).get("slots") or {}).get("diffuse")
        rec = self.attrs(h) if h else {}
        return rec.get("alphaUsage") or "unknown", rec.get("alphaBlend") or "unknown"

    def stats(self):
        variants = [v for m in self.materials.values() for v in m.get("variants") or []]
        removed_variants = sum(1 for v in variants if v.get("replaceWith") is not None or v.get("mergeInto"))
        removed_materials = sum(
            1 for m in self.materials.values()
            if m.get("variants") and all(v.get("mergeInto") for v in m["variants"]))
        return (len(self.materials), len(self.materials) - removed_materials,
                len(variants), len(variants) - removed_variants)


# --------------------------------------------------------------------------
# texture library
# --------------------------------------------------------------------------

class TextureLibrary:
    def __init__(self):
        self.by_hash = {}
        self.by_name = {}
        self.cache = {}
        self.folder = None
        self.count = 0

    def scan(self, folder):
        self.by_hash.clear()
        self.by_name.clear()
        self.cache.clear()
        self.folder = folder
        self.count = 0
        for root, _dirs, files in os.walk(folder):
            for fname in files:
                stem, ext = os.path.splitext(fname)
                if ext.lower() not in TEXTURE_EXTS:
                    continue
                path = os.path.join(root, fname)
                self.count += 1
                m = HASH_PREFIX.match(stem)
                if m:
                    self.by_hash[int(m.group(1), 16)] = path
                    self.by_name.setdefault(m.group(2).lower(), path)
                self.by_name.setdefault(stem.lower(), path)

    def path(self, hash_text, name=None):
        h = to_int(hash_text)
        path = self.by_hash.get(h) if h is not None else None
        if path is None and name:
            path = self.by_name.get(name.lower())
        return path

    def get(self, hash_text, name):
        """Returns an RGBA PIL image, or None."""
        if not HAVE_PIL:
            return None
        path = self.path(hash_text, name)
        if path is None:
            return None
        if path not in self.cache:
            try:
                with Image.open(path) as im:
                    img = im.convert("RGBA")
                img.thumbnail((512, 512))
                self.cache[path] = img
            except Exception:
                self.cache[path] = None
        return self.cache[path]


# --------------------------------------------------------------------------
# UV sampling (wrap modes + scroll window)
# --------------------------------------------------------------------------

def wrap_modes(tiling):
    t = (tiling or "").lower()

    def mode(rep, mir):
        return "mirror" if mir in t else "repeat" if rep in t else "clamp"

    return mode("urepeat", "umirror"), mode("vrepeat", "vmirror")


def axis_window(img, axis, start, length, mode):
    """Cuts [start, start+length) texture units out of the image along one axis."""
    flip = length < 0
    if flip:
        start, length = start + length, -length
    length = max(1e-4, min(length, 8.0))
    size = img.size[axis]

    if mode == "clamp":
        start = max(-8.0, min(8.0, start))
    else:
        period = 2 if mode == "mirror" else 1
        start -= math.floor(start / period) * period
    k0 = math.floor(start)
    k1 = max(k0 + 1, math.ceil(start + length))

    def tile(k):
        if mode == "repeat":
            return img
        if mode == "mirror":
            if k % 2 == 0:
                return img
            return ImageOps.mirror(img) if axis == 0 else ImageOps.flip(img)
        if k == 0:
            return img
        w, h = img.size
        if axis == 0:
            box = (0, 0, 1, h) if k < 0 else (w - 1, 0, w, h)
        else:
            box = (0, 0, w, 1) if k < 0 else (0, h - 1, w, h)
        return img.crop(box).resize((w, h))

    n = k1 - k0
    w, h = img.size
    canvas = Image.new("RGBA", (w * n, h) if axis == 0 else (w, h * n))
    for i, k in enumerate(range(k0, k1)):
        canvas.paste(tile(k), (i * w, 0) if axis == 0 else (0, i * h))

    p0 = int(round((start - k0) * size))
    p1 = min(n * size, p0 + max(1, int(round(length * size))))
    box = (p0, 0, p1, h) if axis == 0 else (0, p0, w, p1)
    out = canvas.crop(box)
    if flip:
        out = ImageOps.mirror(out) if axis == 0 else ImageOps.flip(out)
    return out


def sample_uv(img, wraps, off, scale, span):
    """Renders the texture under uv' = uv * scale + off for uv in [0, span).
    span is (u, v) in textures. The result is span textures wide and high."""
    out = axis_window(img, 0, off[0], scale[0] * span[0], wraps[0])
    out = axis_window(out, 1, off[1], scale[1] * span[1], wraps[1])
    return out.resize((img.width * span[0], img.height * span[1]), Image.BILINEAR)


def scroll_value(t, speed, kind, step):
    """texture::get_scroll from hyperlinked."""
    if kind == "Snap":
        t = float(int(t / step)) if step else 0.0
    x = t * speed
    return x - int(x)


def checker(size, cell=8):
    img = Image.new("RGBA", size, (70, 70, 70, 255))
    light = Image.new("RGBA", (cell, cell), (100, 100, 100, 255))
    for y in range(0, size[1], cell):
        for x in range(0, size[0], cell):
            if ((x // cell) + (y // cell)) % 2:
                img.paste(light, (x, y))
    return img


# --------------------------------------------------------------------------
# alpha sort plan (pure functions, also used by the dialog)
# --------------------------------------------------------------------------

FOLDER_MODES = (
    ("usage", "Alpha usage only"),
    ("modulated", "Alpha usage, and Modulated split by blend type (Blend, Additive, ...)"),
    ("all", "Alpha usage, every usage split by blend type"),
)


def alpha_folder(usage, blend, mode):
    folder = "alpha_" + safe_name(usage).lower()
    if mode == "all" or (mode == "modulated" and usage == "Modulated"):
        folder = os.path.join(folder, "blend_" + safe_name(blend).lower())
    return folder


def alpha_text(usage, blend):
    """Short label for lists. The blend type only matters for Modulated."""
    return f"{usage} / {blend}" if usage == "Modulated" else usage


def plan_alpha_sort(d, lib, mode, diffuse_only, material_keys, include_frames):
    """Returns a list of dicts, one per texture: hash, name, usage, blend, folder,
    source (path or None), materials (list), inherited (bool)."""
    wanted = {}
    for key in material_keys:
        for v in d.materials[key].get("variants") or []:
            for slot, h in (v.get("slots") or {}).items():
                if diffuse_only and slot != "diffuse":
                    continue
                wanted.setdefault(h, set()).add(key)

    entries = {}

    def make(h, mats, inherited, base=None):
        rec = d.attrs(h)
        usage = (base or rec).get("alphaUsage") or "unknown"
        blend = (base or rec).get("alphaBlend") or "unknown"
        name = (d.textures.get(h) or {}).get("name") or ""
        source = lib.path(h, name or None)
        if not name and source:
            stem = os.path.splitext(os.path.basename(source))[0]
            m = HASH_PREFIX.match(stem)
            name = m.group(2) if m else stem
        return {
            "hash": h, "name": name, "usage": usage, "blend": blend,
            "folder": alpha_folder(usage, blend, mode),
            "source": source, "materials": sorted(mats), "inherited": inherited,
        }

    for h, mats in wanted.items():
        entries[h] = make(h, mats, False)

    if include_frames:
        for a in d.animations.values():
            frames = a.get("frames") or []
            owner = next((f for f in frames if f in entries and not entries[f]["inherited"]), None)
            if owner is None:
                continue
            base = {"alphaUsage": entries[owner]["usage"], "alphaBlend": entries[owner]["blend"]}
            for f in frames:
                if f not in entries:
                    entries[f] = make(f, entries[owner]["materials"], True, base)

    return sorted(entries.values(), key=lambda e: (e["folder"], e["name"].lower(), e["hash"]))


# --------------------------------------------------------------------------
# preview panel
# --------------------------------------------------------------------------

class PreviewPanel(ttk.Frame):
    SIZE = 320

    def __init__(self, master, app):
        super().__init__(master, padding=4)
        self.app = app
        self.hash = None
        self.freeze = False
        self.t = 0.0
        self.playing = True
        self._last = time.monotonic()
        self._photo = None

        self.canvas = tk.Canvas(self, width=self.SIZE, height=self.SIZE, bg="#202020", highlightthickness=0)
        self.canvas.pack()

        row = ttk.Frame(self)
        row.pack(fill=tk.X, pady=(4, 0))
        self.play_btn = ttk.Button(row, text="Pause", width=7, command=self.toggle)
        self.play_btn.pack(side=tk.LEFT)
        ttk.Button(row, text="Reset", width=6, command=self.reset).pack(side=tk.LEFT, padx=2)
        ttk.Button(row, text="<", width=3, command=lambda: self.step(-1)).pack(side=tk.LEFT)
        ttk.Button(row, text=">", width=3, command=lambda: self.step(1)).pack(side=tk.LEFT, padx=2)
        self.speed_var = tk.StringVar(value="1x")
        ttk.Combobox(row, textvariable=self.speed_var, width=6, state="readonly",
                     values=["0.1x", "0.25x", "0.5x", "1x", "2x", "4x"]).pack(side=tk.LEFT, padx=4)

        opts = ttk.Frame(self)
        opts.pack(fill=tk.X)
        self.alpha_var = tk.BooleanVar(value=True)
        self.tile_var = tk.BooleanVar(value=False)
        self.invert_var = tk.BooleanVar(value=False)
        self.phase_var = tk.BooleanVar(value=True)
        self.swap_var = tk.BooleanVar(value=True)
        for text, var in (("Alpha", self.alpha_var), ("Tile 2x2", self.tile_var),
                          ("Frame phase", self.phase_var)):
            ttk.Checkbutton(opts, text=text, variable=var, command=self.render).pack(side=tk.LEFT, padx=2)
        opts2 = ttk.Frame(self)
        opts2.pack(fill=tk.X)
        for text, var in (("Swap S/T", self.swap_var), ("Invert scroll", self.invert_var)):
            ttk.Checkbutton(opts2, text=text, variable=var, command=self.render).pack(side=tk.LEFT, padx=2)

        self.info_var = tk.StringVar()
        ttk.Label(self, textvariable=self.info_var, justify=tk.LEFT, wraplength=self.SIZE).pack(
            anchor=tk.W, pady=(4, 0))

        self.after(33, self.tick)

    def speed(self):
        try:
            return float(self.speed_var.get().rstrip("x"))
        except ValueError:
            return 1.0

    def toggle(self):
        self.playing = not self.playing
        self.play_btn.config(text="Pause" if self.playing else "Play")
        self._last = time.monotonic()

    def reset(self):
        self.t = 0.0
        self.render()

    def step(self, direction):
        """Step one frame (or 1/30 s without an animation) and pause."""
        if self.playing:
            self.toggle()
        d = self.app.dict
        fs = (d.attrs(self.hash) if d and self.hash else {}).get("frameSwap")
        anim = d.animations.get(fs["animation"]) if fs else None
        fps = anim.get("fps", 1) if anim else 0
        self.t = max(0.0, self.t + direction * ((1.0 / fps) if fps else 1 / 30))
        self.render()

    def tick(self):
        now = time.monotonic()
        if self.playing and self.hash is not None and not self.freeze:
            self.t += (now - self._last) * self.speed()
            self.render()
        self._last = now
        self.after(33, self.tick)

    def show(self, hash_text, freeze=False):
        """freeze=True shows exactly this texture, with no frame swap and no scroll."""
        self.hash = hash_text
        self.freeze = freeze
        self.t = 0.0
        self.render()

    def clear(self):
        self.hash = None
        self.canvas.delete("all")
        self.info_var.set("")

    def render(self):
        d = self.app.dict
        if self.hash is None or d is None:
            return
        if not HAVE_PIL:
            self.info_var.set("Pillow is not installed. Run: pip install pillow")
            return

        rec = d.attrs(self.hash)
        label = rec.get("name") or (d.textures.get(self.hash) or {}).get("name") or self.hash
        lines = [f"{label}  {rec.get('width', '?')}x{rec.get('height', '?')}"
                 f"  alpha {rec.get('alphaUsage') or '-'} / {rec.get('alphaBlend') or '-'}"]

        shown_hash = self.hash
        fs = None if self.freeze else rec.get("frameSwap")
        anim = d.animations.get(fs["animation"]) if fs else None
        if anim and anim.get("frames"):
            frames = anim["frames"]
            n = len(frames)
            base = int(anim.get("fps", 1) * self.t) % n
            phase = fs.get("frame", 0) if self.phase_var.get() else 0
            idx = (base + phase) % n
            shown_hash = frames[idx]
            lines.append(f"Frame swap: {anim.get('name')}  frame {idx + 1}/{n}  "
                         f"fps {anim.get('fps')}  {anim.get('timeBase')} time  "
                         f"loop {n / max(1, anim.get('fps', 1)):.2f}s  "
                         f"(references frame {fs.get('frame', 0) + 1})")
        elif self.freeze:
            lines.append("Static frame (no animation).")

        shown_name = (d.attrs(shown_hash) or {}).get("name")
        img = self.app.lib.get(shown_hash, shown_name)
        if img is None:
            img = Image.new("RGBA", (64, 64), (255, 0, 255, 255))
            lines.append("Texture file not found. Pick the texture folder.")

        wraps = wrap_modes(rec.get("tiling"))
        off, scale = [0.0, 0.0], (1.0, 1.0)
        sc = None if self.freeze else rec.get("scroll")
        base = 2 if self.tile_var.get() else 1
        spans = [base, base]
        marker = None  # (lo, hi) fractions of the output, per axis, of the normal view
        if sc:
            swap = self.swap_var.get()
            if sc.get("type") == "OffsetScale":
                o = (sc.get("offsetS", 0.0), sc.get("offsetT", 0.0))
                k = (sc.get("scaleS", 0.0) or 1.0, sc.get("scaleT", 0.0) or 1.0)
                off = list(o[::-1] if swap else o)
                scale = k[::-1] if swap else k
                lines.append(scroll_text(rec) + ("  (S/T swapped)" if swap else ""))
            else:
                sign = -1.0 if self.invert_var.get() else 1.0
                step = (sc.get("rawTimeStep") or 0) / 256.0
                speeds = ((sc.get("rawSpeedS") or 0) / 4096.0, (sc.get("rawSpeedT") or 0) / 4096.0)
                vals = tuple(scroll_value(self.t, v, sc["type"], step) for v in speeds)
                if swap:  # S drives the vertical axis, T the horizontal axis
                    vals, speeds = vals[::-1], speeds[::-1]
                off = [sign * vals[0], sign * vals[1]]
                lines.append(f"{scroll_text(rec)}  offset=({off[0]:+.3f}, {off[1]:+.3f})"
                             + ("  (S/T swapped)" if swap else ""))
                if sc["type"] == "Snap":
                    # Extend the view one texture toward the direction the image moves.
                    lo, hi, moved = [0.0, 0.0], [1.0, 1.0], False
                    for ax in (0, 1):
                        if speeds[ax]:
                            moved = True
                            spans[ax] = base + 1
                            if sign * speeds[ax] > 0:    # image moves toward the start of the view
                                off[ax] -= 1.0
                                lo[ax], hi[ax] = 1.0 / spans[ax], 1.0
                            else:
                                lo[ax], hi[ax] = 0.0, base / spans[ax]
                    if moved:
                        marker = (lo, hi)
                        lines.append("Snap: view extended 1 texture toward the scroll. "
                                     "Dashed frame = normal view.")
        lines.append(f"Wrap U/V: {wraps[0]}/{wraps[1]}   t = {self.t:.2f}s")

        out = sample_uv(img, wraps, off, scale, spans) if (sc or base > 1) else img
        if self.alpha_var.get():
            bg = checker(out.size)
            bg.alpha_composite(out)
            out = bg
        else:
            out = out.convert("RGB")

        w, h = out.size
        k = self.SIZE / max(w, h)
        out = out.resize((max(1, int(w * k)), max(1, int(h * k))), Image.NEAREST if k > 1 else Image.BILINEAR)
        self._photo = ImageTk.PhotoImage(out)
        self.canvas.delete("all")
        self.canvas.create_image(self.SIZE // 2, self.SIZE // 2, image=self._photo)
        if marker:
            x0, y0 = (self.SIZE - out.width) / 2, (self.SIZE - out.height) / 2
            self.canvas.create_rectangle(
                x0 + marker[0][0] * out.width, y0 + marker[0][1] * out.height,
                x0 + marker[1][0] * out.width - 1, y0 + marker[1][1] * out.height - 1,
                outline="#ffee00", dash=(4, 3), width=2)
        self.info_var.set("\n".join(lines))


# --------------------------------------------------------------------------
# alpha sort dialog
# --------------------------------------------------------------------------

class AlphaSortDialog(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Sort textures by alpha usage")
        self.geometry("800x640")
        self.transient(app)
        self.plan = []

        self.dest_var = tk.StringVar()
        self.mode_var = tk.StringVar(value="modulated")
        self.diffuse_var = tk.BooleanVar(value=True)
        self.frames_var = tk.BooleanVar(value=True)
        self.scope_var = tk.StringVar(value="all")

        pad = ttk.Frame(self, padding=8)
        pad.pack(fill=tk.BOTH, expand=True)

        row = ttk.Frame(pad)
        row.pack(fill=tk.X)
        ttk.Label(row, text="Destination folder:").pack(side=tk.LEFT)
        ttk.Entry(row, textvariable=self.dest_var).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)
        ttk.Button(row, text="Browse...", command=self.on_browse).pack(side=tk.LEFT)

        opts = ttk.LabelFrame(pad, text="Options", padding=6)
        opts.pack(fill=tk.X, pady=8)
        for value, text in FOLDER_MODES:
            ttk.Radiobutton(opts, text=text, value=value, variable=self.mode_var,
                            command=self.update_plan).pack(anchor=tk.W)
        ttk.Separator(opts).pack(fill=tk.X, pady=4)
        ttk.Checkbutton(opts, text="Diffuse slot textures only (normal and specular maps keep no alpha meaning)",
                        variable=self.diffuse_var, command=self.update_plan).pack(anchor=tk.W)
        ttk.Checkbutton(opts, text="Include every frame of an animation next to the frame that is used",
                        variable=self.frames_var, command=self.update_plan).pack(anchor=tk.W)
        scope = ttk.Frame(opts)
        scope.pack(anchor=tk.W, pady=(4, 0))
        ttk.Label(scope, text="Materials:").pack(side=tk.LEFT)
        ttk.Radiobutton(scope, text="all", value="all", variable=self.scope_var,
                        command=self.update_plan).pack(side=tk.LEFT, padx=6)
        ttk.Radiobutton(scope, text="only the current list (after filters)", value="list",
                        variable=self.scope_var, command=self.update_plan).pack(side=tk.LEFT)

        cols = ("folder", "textures", "found", "missing", "materials")
        self.tree = ttk.Treeview(pad, columns=cols, show="headings", height=10)
        for c, text, width in (("folder", "Folder", 300), ("textures", "Textures", 80), ("found", "Found", 80),
                               ("missing", "Missing", 80), ("materials", "Materials", 80)):
            self.tree.heading(c, text=text)
            self.tree.column(c, width=width, anchor=tk.W if c == "folder" else tk.CENTER)
        self.tree.pack(fill=tk.BOTH, expand=True)

        self.status_var = tk.StringVar()
        ttk.Label(pad, textvariable=self.status_var, wraplength=730, justify=tk.LEFT).pack(anchor=tk.W, pady=6)

        btns = ttk.Frame(pad)
        btns.pack(fill=tk.X)
        self.copy_btn = ttk.Button(btns, text="Copy textures", command=self.on_copy)
        self.copy_btn.pack(side=tk.RIGHT)
        ttk.Button(btns, text="Close", command=self.destroy).pack(side=tk.RIGHT, padx=6)

        self.update_plan()

    def on_browse(self):
        folder = filedialog.askdirectory(title="Destination folder", parent=self)
        if folder:
            self.dest_var.set(folder)

    def scope_keys(self):
        if self.scope_var.get() == "list":
            return list(self.app.visible_keys)
        return list(self.app.dict.materials)

    def update_plan(self):
        d, lib = self.app.dict, self.app.lib
        self.tree.delete(*self.tree.get_children())
        if d is None:
            self.status_var.set("Open a dictionary first.")
            return
        keys = self.scope_keys()
        self.plan = plan_alpha_sort(d, lib, self.mode_var.get(), self.diffuse_var.get(), keys,
                                    self.frames_var.get())
        groups = {}
        for e in self.plan:
            g = groups.setdefault(e["folder"], {"n": 0, "found": 0, "mats": set()})
            g["n"] += 1
            g["found"] += 1 if e["source"] else 0
            g["mats"].update(e["materials"])
        for folder, g in sorted(groups.items()):
            self.tree.insert("", tk.END, values=(folder, g["n"], g["found"], g["n"] - g["found"], len(g["mats"])))
        found = sum(1 for e in self.plan if e["source"])
        note = "" if lib.count else "  No texture folder is loaded. Use File > Texture folder first."
        self.status_var.set(f"{len(self.plan)} texture(s), {found} file(s) found, {len(self.plan) - found} missing. "
                            f"Files are copied, never moved.{note}")
        self.copy_btn.state(["!disabled"] if found else ["disabled"])

    def on_copy(self):
        dest = self.dest_var.get().strip()
        if not dest:
            messagebox.showinfo("Destination", "Choose a destination folder first.", parent=self)
            return
        d = self.app.dict
        copied = 0
        try:
            os.makedirs(dest, exist_ok=True)
            for i, e in enumerate(self.plan):
                e["status"] = "inherited" if e["inherited"] else "ok"
                if not e["source"]:
                    e["status"] = "missing"
                    continue
                folder = os.path.join(dest, e["folder"])
                os.makedirs(folder, exist_ok=True)
                target = os.path.join(folder, os.path.basename(e["source"]))
                if os.path.exists(target) and os.path.getsize(target) != os.path.getsize(e["source"]):
                    target = os.path.join(folder, f"{e['hash']}_{os.path.basename(e['source'])}")
                shutil.copy2(e["source"], target)
                copied += 1
                if i % 25 == 0:
                    self.status_var.set(f"Copying... {i}/{len(self.plan)}")
                    self.update_idletasks()

            with open(os.path.join(dest, "alpha_textures.csv"), "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["folder", "hash", "name", "alpha_usage", "alpha_blend", "status", "source", "materials"])
                for e in self.plan:
                    w.writerow([e["folder"], e["hash"], e["name"], e["usage"], e["blend"], e["status"],
                                e["source"] or "", ";".join(e["materials"])])

            with open(os.path.join(dest, "alpha_materials.csv"), "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["material", "effect", "diffuse_hash", "diffuse_name", "alpha_usage", "alpha_blend",
                            "folder", "variants", "tags"])
                for key in self.scope_keys():
                    mat = d.materials[key]
                    v = d.main_variant(mat)
                    h = ((v or {}).get("slots") or {}).get("diffuse", "")
                    usage, blend = d.material_alpha(mat)
                    w.writerow([key, mat.get("effect", ""), h, (d.attrs(h) or {}).get("name", "") if h else "",
                                usage, blend, alpha_folder(usage, blend, self.mode_var.get()),
                                len(mat.get("variants") or []), " ".join(material_tags(mat))])
        except OSError as ex:
            messagebox.showerror("Copy failed", str(ex), parent=self)
            return

        missing = sum(1 for e in self.plan if not e["source"])
        self.status_var.set(f"Copied {copied} file(s) to {dest}. {missing} missing. "
                            f"Wrote alpha_textures.csv and alpha_materials.csv.")


# --------------------------------------------------------------------------
# application
# --------------------------------------------------------------------------

class App(tk.Tk):
    THUMB = 88

    def __init__(self, path=None, textures=None):
        super().__init__()
        self.title("Carbon Material Viewer")
        self.geometry("1700x900")
        self.dict = None
        self.lib = TextureLibrary()
        self.sort_col, self.sort_rev = "material", False
        self.anim_sort, self.anim_rev = "kind", False
        self.visible_keys = []
        self.variant_rows = []
        self.slot_rows = {}
        self.thumb_refs = {}

        self._build_menu()
        self._build_layout()

        if textures:
            self.load_textures(textures)
        if path:
            self.load_file(path)

    # ---- layout ----
    def _build_menu(self):
        menubar = tk.Menu(self)
        filemenu = tk.Menu(menubar, tearoff=0)
        filemenu.add_command(label="Open dictionary...", command=self.on_open, accelerator="Ctrl+O")
        filemenu.add_command(label="Texture folder...", command=self.on_textures, accelerator="Ctrl+T")
        filemenu.add_separator()
        filemenu.add_command(label="Quit", command=self.destroy)
        menubar.add_cascade(label="File", menu=filemenu)
        tools = tk.Menu(menubar, tearoff=0)
        tools.add_command(label="Sort textures by alpha usage...", command=self.on_alpha_sort)
        menubar.add_cascade(label="Tools", menu=tools)
        self.config(menu=menubar)
        self.bind("<Control-o>", lambda e: self.on_open())
        self.bind("<Control-t>", lambda e: self.on_textures())

    def _build_layout(self):
        top = ttk.Frame(self, padding=(6, 6, 6, 0))
        top.pack(side=tk.TOP, fill=tk.X)
        ttk.Button(top, text="Open dictionary...", command=self.on_open).pack(side=tk.LEFT)
        ttk.Button(top, text="Texture folder...", command=self.on_textures).pack(side=tk.LEFT, padx=4)
        ttk.Button(top, text="Sort by alpha...", command=self.on_alpha_sort).pack(side=tk.LEFT)

        ttk.Label(top, text="Filter:").pack(side=tk.LEFT, padx=(12, 4))
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *a: self.refresh_list())
        ttk.Entry(top, textvariable=self.filter_var, width=24).pack(side=tk.LEFT)

        self.alpha_var = tk.StringVar(value=ANY_ALPHA)
        self.alpha_box = ttk.Combobox(top, textvariable=self.alpha_var, width=16, state="readonly",
                                      values=[ANY_ALPHA])
        self.alpha_box.pack(side=tk.LEFT, padx=8)
        self.alpha_box.bind("<<ComboboxSelected>>", lambda e: self.refresh_list())
        self.blend_var = tk.StringVar(value=ANY_BLEND)
        self.blend_box = ttk.Combobox(top, textvariable=self.blend_var, width=16, state="readonly",
                                      values=[ANY_BLEND])
        self.blend_box.pack(side=tk.LEFT)
        self.blend_box.bind("<<ComboboxSelected>>", lambda e: self.refresh_list())

        self.flag_vars = {}
        for tag in ("replace", "animated", "merge", "conflict", "dropped"):
            var = tk.BooleanVar(value=False)
            self.flag_vars[tag] = var
            ttk.Checkbutton(top, text=tag, variable=var, command=self.refresh_list).pack(side=tk.LEFT, padx=(6, 0))

        info = ttk.Frame(self, padding=(6, 2, 6, 4))
        info.pack(side=tk.TOP, fill=tk.X)
        self.stats_var = tk.StringVar(value="No file loaded")
        ttk.Label(info, textvariable=self.stats_var).pack(side=tk.LEFT)
        self.tex_var = tk.StringVar(value="No texture folder")
        ttk.Label(info, textvariable=self.tex_var, foreground="#666").pack(side=tk.RIGHT)

        paned = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True)

        left = ttk.Frame(paned)
        cols = ("material", "alpha", "variants", "solids", "status")
        self.tree = ttk.Treeview(left, columns=cols, show="headings", selectmode="browse")
        for c, text, width in (("material", "Material", 220), ("alpha", "Alpha", 150),
                               ("variants", "Var", 36), ("solids", "Solids", 50), ("status", "Status", 150)):
            self.tree.heading(c, text=text, command=lambda c=c: self.on_sort(c))
            self.tree.column(c, width=width, anchor=tk.W if c in ("material", "status") else tk.CENTER)
        self.tree.tag_configure("animated", background="#e3eeff")
        self.tree.tag_configure("replace", background="#eaf7ea")
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb = ttk.Scrollbar(left, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.bind("<<TreeviewSelect>>", self.on_material_select)
        paned.add(left, weight=1)
        self.after(200, lambda: paned.sashpos(0, 560))

        self.nb = ttk.Notebook(paned)
        paned.add(self.nb, weight=3)
        self._build_materials_tab()
        self._build_animations_tab()

    def _build_materials_tab(self):
        tab = ttk.Frame(self.nb, padding=4)
        self.nb.add(tab, text="Materials")

        self.header_var = tk.StringVar()
        ttk.Label(tab, textvariable=self.header_var, font=("", 10, "bold")).pack(anchor=tk.W)

        vcols = ("id", "role", "n", "diffuse", "normal", "specular", "height", "opacity", "target", "solids")
        self.var_tree = ttk.Treeview(tab, columns=vcols, show="headings", height=5, selectmode="browse")
        for c, text, width in (("id", "Id", 32), ("role", "Role", 95), ("n", "Slots", 40),
                               ("diffuse", "Diffuse", 130), ("normal", "Normal", 110), ("specular", "Specular", 110),
                               ("height", "Height", 80), ("opacity", "Opacity", 80),
                               ("target", "Replace / merge", 260), ("solids", "Solids", 45)):
            self.var_tree.heading(c, text=text)
            self.var_tree.column(c, width=width, anchor=tk.W, stretch=(c == "target"))
        for role, color in ROLE_COLORS.items():
            self.var_tree.tag_configure(role, background=color)
        self.var_tree.pack(fill=tk.X, pady=(2, 4))
        self.var_tree.bind("<<TreeviewSelect>>", self.on_variant_select)
        self.var_tree.bind("<Double-1>", self.on_variant_double)

        self.variant_info_var = tk.StringVar()
        ttk.Label(tab, textvariable=self.variant_info_var).pack(anchor=tk.W)

        body = ttk.Frame(tab)
        body.pack(fill=tk.BOTH, expand=True, pady=(4, 0))
        self.preview = PreviewPanel(body, self)
        self.preview.pack(side=tk.RIGHT, fill=tk.Y, padx=(8, 0))

        mid = ttk.Frame(body)
        mid.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.thumb_title1 = tk.StringVar()
        ttk.Label(mid, textvariable=self.thumb_title1, font=("", 9, "bold")).pack(anchor=tk.W)
        self.thumb_row1 = ttk.Frame(mid, height=self.THUMB + 36)
        self.thumb_row1.pack(fill=tk.X)
        self.thumb_title2 = tk.StringVar()
        ttk.Label(mid, textvariable=self.thumb_title2, font=("", 9, "bold")).pack(anchor=tk.W, pady=(4, 0))
        self.thumb_row2 = ttk.Frame(mid, height=self.THUMB + 36)
        self.thumb_row2.pack(fill=tk.X)

        ttk.Label(mid, text="Slot attributes (select one to preview)", font=("", 9, "bold")).pack(
            anchor=tk.W, pady=(6, 0))
        lower = ttk.Frame(mid)
        lower.pack(fill=tk.BOTH, expand=True)

        sframe = ttk.Frame(lower)
        sframe.pack(side=tk.RIGHT, fill=tk.Y, padx=(6, 0))
        ttk.Label(sframe, text="Solids").pack(anchor=tk.W)
        self.solid_list = tk.Listbox(sframe, width=18, height=6)
        self.solid_list.pack(side=tk.LEFT, fill=tk.Y)
        svsb = ttk.Scrollbar(sframe, orient=tk.VERTICAL, command=self.solid_list.yview)
        self.solid_list.configure(yscrollcommand=svsb.set)
        svsb.pack(side=tk.RIGHT, fill=tk.Y)

        scols = ("slot", "name", "hash", "alpha", "tiling", "flags", "anim")
        self.slot_tree = ttk.Treeview(lower, columns=scols, show="headings", height=6, selectmode="browse")
        for c, text, width in (("slot", "Slot", 55), ("name", "Name", 120), ("hash", "Hash", 78),
                               ("alpha", "Alpha use / blend", 125), ("tiling", "Tiling", 90),
                               ("flags", "Flags", 55), ("anim", "Animation", 200)):
            self.slot_tree.heading(c, text=text)
            self.slot_tree.column(c, width=width, anchor=tk.W, stretch=(c == "anim"))
        self.slot_tree.tag_configure("dropped", foreground="#a33")
        self.slot_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.slot_tree.bind("<<TreeviewSelect>>", self.on_slot_select)

    def _build_animations_tab(self):
        tab = ttk.Frame(self.nb, padding=4)
        self.nb.add(tab, text="Animations")

        bar = ttk.Frame(tab)
        bar.pack(fill=tk.X)
        ttk.Label(bar, text="Show:").pack(side=tk.LEFT)
        self.kind_var = tk.StringVar(value="all")
        kind = ttk.Combobox(bar, textvariable=self.kind_var, width=12, state="readonly",
                            values=["all", "frame swap", "uv scroll"])
        kind.pack(side=tk.LEFT, padx=4)
        kind.bind("<<ComboboxSelected>>", lambda e: self.refresh_animations())
        ttk.Label(bar, text="Filter:").pack(side=tk.LEFT, padx=(10, 4))
        self.anim_filter_var = tk.StringVar()
        self.anim_filter_var.trace_add("write", lambda *a: self.refresh_animations())
        ttk.Entry(bar, textvariable=self.anim_filter_var, width=24).pack(side=tk.LEFT)
        self.anim_count_var = tk.StringVar()
        ttk.Label(bar, textvariable=self.anim_count_var, foreground="#666").pack(side=tk.RIGHT)

        body = ttk.Frame(tab)
        body.pack(fill=tk.BOTH, expand=True, pady=(4, 0))
        self.anim_preview = PreviewPanel(body, self)
        self.anim_preview.pack(side=tk.RIGHT, fill=tk.Y, padx=(8, 0))

        left = ttk.Frame(body)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        acols = ("kind", "name", "key", "details", "frames", "users")
        self.anim_tree = ttk.Treeview(left, columns=acols, show="headings", height=12, selectmode="browse")
        for c, text, width in (("kind", "Kind", 80), ("name", "Name", 170), ("key", "Key", 85),
                               ("details", "Details", 230), ("frames", "Frames", 50), ("users", "Materials", 65)):
            self.anim_tree.heading(c, text=text, command=lambda c=c: self.on_anim_sort(c))
            self.anim_tree.column(c, width=width, anchor=tk.W, stretch=(c == "details"))
        self.anim_tree.tag_configure("swap", background="#e3eeff")
        self.anim_tree.tag_configure("scroll", background="#fff3d9")
        self.anim_tree.pack(fill=tk.X)
        self.anim_tree.bind("<<TreeviewSelect>>", self.on_anim_select)

        self.detail_title_var = tk.StringVar(value="Frames")
        ttk.Label(left, textvariable=self.detail_title_var, font=("", 10, "bold")).pack(anchor=tk.W, pady=(8, 0))
        self.frame_list = tk.Listbox(left, height=7)
        self.frame_list.pack(fill=tk.X)
        self.frame_list.bind("<<ListboxSelect>>", self.on_frame_select)

        ttk.Label(left, text="Used by", font=("", 10, "bold")).pack(anchor=tk.W, pady=(8, 0))
        uframe = ttk.Frame(left)
        uframe.pack(fill=tk.BOTH, expand=True)
        self.user_list = tk.Listbox(uframe)
        self.user_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        uvsb = ttk.Scrollbar(uframe, orient=tk.VERTICAL, command=self.user_list.yview)
        self.user_list.configure(yscrollcommand=uvsb.set)
        uvsb.pack(side=tk.RIGHT, fill=tk.Y)

    # ---- loading ----
    def on_open(self):
        path = filedialog.askopenfilename(
            title="Open carbon_material_dictionary.json",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")])
        if path:
            self.load_file(path)

    def on_textures(self):
        folder = filedialog.askdirectory(title="Folder with dumped textures (.dds / .png)")
        if folder:
            self.load_textures(folder)

    def load_textures(self, folder):
        self.lib.scan(folder)
        self.tex_var.set(f"{self.lib.count} texture file(s) in {folder}")
        for panel in (self.preview, self.anim_preview):
            panel.render()
        self.on_variant_select(None)

    def on_alpha_sort(self):
        if not self.dict:
            messagebox.showinfo("No dictionary", "Open a dictionary first.")
            return
        AlphaSortDialog(self)

    def load_file(self, path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            messagebox.showerror("Failed to open file", str(e))
            return
        if "materials" not in data:
            messagebox.showerror("Wrong file", "This file has no 'materials'. Open carbon_material_dictionary.json.")
            return

        self.dict = Dictionary(data)
        m_before, m_after, v_before, v_after = self.dict.stats()
        self.stats_var.set(
            f"materials {m_before} -> {m_after} after merge   variants {v_before} -> {v_after}   "
            f"animations {len(self.dict.animations)} frame swap, {len(self.dict.scroll_hashes)} uv scroll"
            + (f"   ({self.dict.zero_scroll} zero-speed scroll ignored)" if self.dict.zero_scroll else "")
            + (f"   dropped slots {self.dict.dropped}" if self.dict.dropped else ""))
        self.title(f"Carbon Material Viewer - {path}")

        usages = sorted({self.dict.material_alpha(m)[0] for m in self.dict.materials.values()})
        self.alpha_box.config(values=[ANY_ALPHA] + usages)
        self.alpha_var.set(ANY_ALPHA)
        blends = sorted({self.dict.material_alpha(m)[1] for m in self.dict.materials.values()
                         if self.dict.material_alpha(m)[0] == "Modulated"})
        self.blend_box.config(values=[ANY_BLEND] + blends)
        self.blend_var.set(ANY_BLEND)
        self.refresh_list()
        self.refresh_animations()
        self.preview.clear()
        self.anim_preview.clear()

    # ---- material list ----
    def on_sort(self, col):
        self.sort_rev = (not self.sort_rev) if self.sort_col == col else False
        self.sort_col = col
        self.refresh_list()

    def refresh_list(self):
        self.tree.delete(*self.tree.get_children())
        self.visible_keys = []
        if not self.dict:
            return
        needle = self.filter_var.get().strip().lower()
        wanted = [t for t, v in self.flag_vars.items() if v.get()]
        alpha = self.alpha_var.get()
        blend_wanted = self.blend_var.get()

        rows = []
        for key, mat in self.dict.materials.items():
            tags = material_tags(mat)
            usage, blend = self.dict.material_alpha(mat)
            if needle and needle not in key.lower():
                continue
            if any(t not in tags for t in wanted):
                continue
            if alpha != ANY_ALPHA and usage != alpha:
                continue
            if blend_wanted != ANY_BLEND and not (usage == "Modulated" and blend == blend_wanted):
                continue
            rows.append((key, mat.get("effect", ""), alpha_text(usage, blend), len(mat.get("variants") or []),
                         solid_total(mat), " ".join(tags)))

        index = {"material": 0, "effect": 1, "alpha": 2, "variants": 3, "solids": 4, "status": 5}[self.sort_col]
        rows.sort(key=lambda r: (r[index] if isinstance(r[index], int) else str(r[index]).lower()),
                  reverse=self.sort_rev)
        for row in rows:
            tags = ()
            if "animated" in row[5]:
                tags = ("animated",)
            elif "replace" in row[5]:
                tags = ("replace",)
            self.tree.insert("", tk.END, iid=row[0], values=(row[0], row[2], row[3], row[4], row[5]), tags=tags)
            self.visible_keys.append(row[0])

    def on_material_select(self, _event):
        sel = self.tree.selection()
        self.var_tree.delete(*self.var_tree.get_children())
        self.slot_tree.delete(*self.slot_tree.get_children())
        self.solid_list.delete(0, tk.END)
        self.variant_info_var.set("")
        self.header_var.set("")
        self._clear_thumbs()
        if not sel:
            return
        key = sel[0]
        mat = self.dict.materials[key]
        usage, blend = self.dict.material_alpha(mat)
        rec = mat.get("recommendedVariant")
        self.header_var.set(
            f"{key}   effect {mat.get('effect', '?')}   alpha {usage} / {blend}   "
            f"recommended variant: {rec if rec is not None else 'none (animated)'}"
            + (f"   conflicting slots: {', '.join(mat['conflictingSlots'])}" if mat.get("conflictingSlots") else ""))

        self.variant_rows = mat.get("variants") or []
        for v in self.variant_rows:
            slots = v.get("slots") or {}
            if v.get("replaceWith") is not None:
                target = f"replace with variant {v['replaceWith']}"
            elif v.get("mergeInto"):
                mi = v["mergeInto"]
                target = f"merge into {mi['material']} (variant {mi['variant']})"
            elif v.get("role") == "recommended":
                target = "recommended"
            else:
                target = ""
            if v.get("droppedSlots"):
                target += ("   " if target else "") + "[dropped: " + ", ".join(
                    f"{x['slot']} {x.get('name') or x['texture']}" for x in v["droppedSlots"]) + "]"

            def label(slot):
                h = slots.get(slot)
                return (self.dict.attrs(h).get("name") or h) if h else "-"

            self.var_tree.insert("", tk.END, iid=str(v["id"]), tags=(effective_role(v),), values=(
                v["id"], effective_role(v), v.get("mappedSlots", ""), label("diffuse"), label("normal"),
                label("specular"), label("height"), label("opacity"), target, v.get("solidCount", "")))
        if self.variant_rows:
            first = mat.get("recommendedVariant")
            self.var_tree.selection_set(str(first if first is not None else self.variant_rows[0]["id"]))

    def _selected_variant(self):
        sel = self.var_tree.selection()
        if not sel:
            return None
        return next((v for v in self.variant_rows if str(v["id"]) == sel[0]), None)

    def on_variant_select(self, _event):
        self.slot_tree.delete(*self.slot_tree.get_children())
        self.solid_list.delete(0, tk.END)
        self.slot_rows = {}
        self._clear_thumbs()
        variant = self._selected_variant()
        if not variant or not self.dict:
            return

        self.variant_info_var.set(
            f"Variant {variant['id']}   role {effective_role(variant)}   EffectId {variant.get('effectId')}   "
            f"Flags {variant.get('flags')}   SortKey {variant.get('sortKey')}   "
            f"slots {variant.get('mappedSlots')}   solids {variant.get('solidCount')}"
            + (f"   differs from target: {', '.join(variant['differsFromTarget'])}"
               if variant.get("differsFromTarget") else ""))
        dropped = variant.get("droppedSlots") or []

        slots = variant.get("slots") or {}
        for slot in SLOT_ORDER:
            h = slots.get(slot)
            if not h:
                continue
            rec = self.dict.attrs(h)
            fs = rec.get("frameSwap")
            parts = []
            if scroll_text(rec):
                parts.append("scroll: " + scroll_text(rec))
            if fs:
                a = self.dict.animations.get(fs["animation"]) or {}
                parts.append(f"frame swap: {a.get('name', fs['animation'])} "
                             f"frame {fs['frame'] + 1}/{a.get('frameCount', '?')}")
            iid = self.slot_tree.insert("", tk.END, values=(
                slot, rec.get("name") or "", h,
                f"{rec.get('alphaUsage') or '-'} / {rec.get('alphaBlend') or '-'}",
                rec.get("tiling") or "", rec.get("renderFlags") or "", "   ".join(parts)))
            self.slot_rows[iid] = h

        for x in dropped:
            self.slot_tree.insert("", tk.END, tags=("dropped",), values=(
                f"{x['slot']} X", x.get("name") or "", x["texture"], "", "", "", "dropped: " + x["reason"]))

        for s in variant.get("solids") or []:
            self.solid_list.insert(tk.END, s)
        extra = variant.get("solidCount", 0) - len(variant.get("solids") or [])
        if extra > 0:
            self.solid_list.insert(tk.END, f"... and {extra} more")

        # thumbnails: this variant, then what it becomes
        self.thumb_title1.set("This variant")
        self._fill_thumbs(self.thumb_row1, slots)
        key = self.tree.selection()[0] if self.tree.selection() else None
        if variant.get("replaceWith") is not None and key:
            target = self.dict.variant(key, variant["replaceWith"])
            self.thumb_title2.set(f"After replace: variant {variant['replaceWith']} of this material")
            self._fill_thumbs(self.thumb_row2, (target or {}).get("slots") or {})
        elif variant.get("mergeInto"):
            mi = variant["mergeInto"]
            target = self.dict.variant(mi["material"], mi["variant"])
            self.thumb_title2.set(f"After merge: {mi['material']} (variant {mi['variant']})")
            self._fill_thumbs(self.thumb_row2, (target or {}).get("slots") or {})
        else:
            self.thumb_title2.set("")

        pick = None
        for iid, h in self.slot_rows.items():
            rec = self.dict.attrs(h)
            if rec.get("scroll") or rec.get("frameSwap"):
                pick = iid
                break
        if pick is None and self.slot_rows:
            pick = next(iter(self.slot_rows))
        if pick:
            self.slot_tree.selection_set(pick)
        else:
            self.preview.clear()

    # ---- thumbnails ----
    def _clear_thumbs(self):
        for row in (self.thumb_row1, self.thumb_row2):
            for child in row.winfo_children():
                child.destroy()
        self.thumb_refs = {}
        self.thumb_title1.set("")
        self.thumb_title2.set("")

    def _fill_thumbs(self, row, slots):
        refs = []
        for slot in SLOT_ORDER:
            h = slots.get(slot)
            if not h:
                continue
            rec = self.dict.attrs(h)
            cell = ttk.Frame(row, padding=2)
            cell.pack(side=tk.LEFT)
            if HAVE_PIL:
                img = self.lib.get(h, rec.get("name"))
                if img is None:
                    thumb = Image.new("RGBA", (self.THUMB, self.THUMB), (90, 40, 40, 255))
                else:
                    k = self.THUMB / max(img.size)
                    scaled = img.resize((max(1, int(img.width * k)), max(1, int(img.height * k))))
                    thumb = checker((self.THUMB, self.THUMB))
                    thumb.alpha_composite(scaled)
                photo = ImageTk.PhotoImage(thumb)
                refs.append(photo)
                lbl = tk.Label(cell, image=photo, borderwidth=1, relief=tk.SOLID)
                lbl.pack()
                lbl.bind("<Button-1>", lambda e, h=h: self.preview.show(h))
            name = rec.get("name") or h
            ttk.Label(cell, text=f"{slot}\n{name[-22:]}", justify=tk.CENTER, font=("", 8),
                      wraplength=self.THUMB + 20).pack()
        self.thumb_refs[str(row)] = refs

    def on_variant_double(self, _event):
        variant = self._selected_variant()
        mi = variant.get("mergeInto") if variant else None
        if mi and self.tree.exists(mi["material"]):
            self.tree.selection_set(mi["material"])
            self.tree.see(mi["material"])
        elif mi:
            messagebox.showinfo("Hidden by filter", f"{mi['material']} is hidden by the current filter.")

    def on_slot_select(self, _event):
        sel = self.slot_tree.selection()
        if sel and sel[0] in self.slot_rows:
            self.preview.show(self.slot_rows[sel[0]])

    # ---- animations tab ----
    def on_anim_sort(self, col):
        self.anim_rev = (not self.anim_rev) if self.anim_sort == col else False
        self.anim_sort = col
        self.refresh_animations()

    def refresh_animations(self):
        self.anim_tree.delete(*self.anim_tree.get_children())
        self.frame_list.delete(0, tk.END)
        self.user_list.delete(0, tk.END)
        if not self.dict:
            return
        d = self.dict
        kind = self.kind_var.get()
        needle = self.anim_filter_var.get().strip().lower()

        rows = []
        if kind in ("all", "frame swap"):
            for key, a in d.animations.items():
                users = len({u[0] for u in d.anim_users.get(key, [])})
                rows.append(("frame swap", a.get("name", ""), key,
                             f"{a.get('fps', '?')} fps, {a.get('timeBase', '')} time, "
                             f"loop {a.get('frameCount', 0) / max(1, a.get('fps', 1)):.2f}s",
                             a.get("frameCount", ""), users, f"a:{key}", ("swap",)))
        if kind in ("all", "uv scroll"):
            for h in d.scroll_hashes:
                rec = d.textures[h]
                users = len({u[0] for u in d.tex_users.get(h, [])})
                rows.append(("uv scroll", rec.get("name") or "", h, scroll_text(rec), "", users,
                             f"s:{h}", ("scroll",)))
        if needle:
            rows = [r for r in rows if needle in f"{r[1]} {r[2]} {r[3]}".lower()]

        index = {"kind": 0, "name": 1, "key": 2, "details": 3, "frames": 4, "users": 5}[self.anim_sort]
        rows.sort(key=lambda r: (r[index] if isinstance(r[index], int) else str(r[index]).lower()),
                  reverse=self.anim_rev)
        for r in rows:
            self.anim_tree.insert("", tk.END, iid=r[6], values=r[:6], tags=r[7])
        swaps = sum(1 for r in rows if r[0] == "frame swap")
        self.anim_count_var.set(f"{swaps} frame swap, {len(rows) - swaps} uv scroll")

    def on_anim_select(self, _event):
        sel = self.anim_tree.selection()
        self.frame_list.delete(0, tk.END)
        self.user_list.delete(0, tk.END)
        if not sel:
            return
        d = self.dict
        kind, _, ident = sel[0].partition(":")

        if kind == "a":
            self.detail_title_var.set("Frames (click one to see it still)")
            anim = d.animations[ident]
            frames = anim.get("frames") or []
            for i, h in enumerate(frames):
                name = (d.attrs(h) or {}).get("name") or ""
                self.frame_list.insert(tk.END, f"{i + 1}: {h}  {name}")
            for material, variant, frame in sorted(d.anim_users.get(ident, []), key=lambda u: (u[2], u[0])):
                v = d.variant(material, variant) or {}
                mi = v.get("mergeInto")
                tail = f" -> {mi['material']}" if mi else ""
                self.user_list.insert(tk.END, f"{material} | v{variant} | frame {frame + 1}{tail}")
            if frames:
                self.anim_preview.show(frames[0])
        else:
            self.detail_title_var.set("UV scroll parameters")
            rec = d.textures[ident]
            for line in self._scroll_lines(rec):
                self.frame_list.insert(tk.END, line)
            for material, variant, slot in sorted(d.tex_users.get(ident, [])):
                self.user_list.insert(tk.END, f"{material} | v{variant} | {slot}")
            self.anim_preview.show(ident)

    @staticmethod
    def _scroll_lines(rec):
        sc = rec.get("scroll") or {}
        lines = [f"Texture: {rec.get('name')}  {rec.get('width', '?')}x{rec.get('height', '?')}",
                 f"Type: {sc.get('type')}   tiling: {rec.get('tiling') or '-'}   "
                 f"alpha: {rec.get('alphaUsage') or '-'} / {rec.get('alphaBlend') or '-'}"]
        if sc.get("type") == "OffsetScale":
            lines.append(f"Offset  raw ({sc.get('rawOffsetS')}, {sc.get('rawOffsetT')})  "
                         f"-> ({sc.get('offsetS', 0):.4f}, {sc.get('offsetT', 0):.4f})   (raw / -1024)")
            lines.append(f"Scale   raw ({sc.get('rawScaleS')}, {sc.get('rawScaleT')})  "
                         f"-> ({sc.get('scaleS', 0):.4f}, {sc.get('scaleT', 0):.4f})   (raw / 256)")
            lines.append("Static: no animation, only a fixed UV transform.")
        else:
            for axis, raw, speed in (("S", sc.get("rawSpeedS"), sc.get("speedS")),
                                     ("T", sc.get("rawSpeedT"), sc.get("speedT"))):
                loop = f"loop {1 / abs(speed):.2f}s" if speed else "no motion"
                lines.append(f"Speed {axis}  raw {raw}  -> {speed or 0:.4f} cycles/s   {loop}")
            if sc.get("type") == "Snap":
                step = sc.get("timeStep") or 0
                lines.append(f"Time step  raw {sc.get('rawTimeStep')}  -> {step:.4f}s   "
                             f"(the offset jumps once per step)")
        return lines

    def on_frame_select(self, _event):
        sel = self.anim_tree.selection()
        idx = self.frame_list.curselection()
        if not sel or not idx or not sel[0].startswith("a:"):
            return
        frames = self.dict.animations[sel[0][2:]].get("frames") or []
        if idx[0] < len(frames):
            self.anim_preview.show(frames[idx[0]], freeze=True)


def main():
    ap = argparse.ArgumentParser(description="Viewer for carbon_material_dictionary.json")
    ap.add_argument("file", nargs="?", help="carbon_material_dictionary.json")
    ap.add_argument("--textures", help="folder with dumped textures (.dds/.png)")
    args = ap.parse_args()
    App(args.file, args.textures).mainloop()


if __name__ == "__main__":
    main()
