#!/usr/bin/env python3
"""
carbon_plat_viewer.py

Window tool that reads every Carbon platform material entry (SolidObjectShadingGroup,
chunk 0x134B02, 0x90 bytes, 0x10 aligned) from STREAML5RA.BUN and shows all of it.
It complements carbon_material_viewer.py: that tool shows the dictionary, this one
shows the raw flags, so the unknown material meaning can be found by comparing
materials with what you see in the game.

Layout used (CarbonSolidReader.cs in NFS-ModTools, same chunk walk as nfs_solid_reader.py):
  0x80134010  solid object container
    0x134011  solid header (version byte at +0x0C, hash at +0x10, name at +0xA0)
    0x134012  texture table, 8 bytes per entry (hash u32 + 4 bytes skipped)
    0x80134100  plat container
      0x134B02  plat entries (0x90 bytes each)
      0x134C02  entry name (one per entry that has one, in order)
eStripEntry from the MW decomp is NOT the binary layout. Its flag names are used
only as the meaning of the flag bits, and they may be wrong (PS3 Carbon origin).

Rules kept:
  - raw flags, raw ids and every unknown field stay in the data
  - a texture id is never dropped. A separate slot state tells what the id is:
      texture / same_as_diffuse / none (0xFF) / out_of_range
  - alpha is three separate things: CLIP (flag), OpacityMapId (slot),
    GLOSSMAPINALPHA (flag). They are never merged.
  - there is no is_emissive. You tag materials yourself (E / N / C keys or the
    buttons) and the Flag bits tab shows which bit, if any, separates the two.

Material group = diffuse hash + effect id + raw flags.
Tags are saved to outputs/carbon_plat_viewer/material_tags.tsv after every change.

v2 additions (find what separates emissive from non-emissive in game):
  - Instances tab: every entry that uses the same diffuse texture, with solid hash, solid header
    fields and the chunk list of the solid. Select two or more rows to see only the fields that differ.
    "Dump selected" writes everything (raw record bytes, solid header bytes, chunk list, vertex
    color stats) to outputs/carbon_plat_viewer/.
  - Field values tab: every field value across all entries, with counts split by your tags.
  - File > Load material dictionary: adds the texture side (alpha usage, blend, tiling, render
    flags, scroll, frame swap) from carbon_material_dictionary.json.
  - Vertex color stats per entry (decoded with the layout code in nfs_solid_reader.py).
  - File > Load scenery TSVs: joins scenery_infos.tsv + scenery_instances.tsv (AssetDumper) on the
    solid hash (SolidKey1..4), so every solid shows which scenery instance flags it is placed with.
  - Flag names are CANDIDATES from the MW decomp (PS3 Carbon, "probably arent accurate").
    0x00200000 has no name. @0x44 is NOT treated as a flags copy: the Hyperlinked runtime struct
    has a pointer there (chunk_data, never filled in Hyperlinked, still a TODO there).

Usage: python carbon_plat_viewer.py [STREAML5RA.BUN] [--dict carbon_material_dictionary.json] [--scenery scenery_infos.tsv]
"""
# stream bootstrap: make the shared library folders importable
import pathlib as _pl, sys as _sys
_sys.path[:0] = [str(_pl.Path(__file__).resolve().parents[1] / _d) for _d in ['common', 'region', 'scenery', 'solids_materials']]

import csv
import json
import mmap
import os
import struct
import threading
import tkinter as tk
from enum import IntFlag
from tkinter import filedialog, messagebox, ttk

from nfs_hash_dictionary import load_dictionary
from nfs_outputs import out_dir, out_path

TOOL_NAME = "carbon_plat_viewer"

SOLID_OBJECT_CONTAINER = 0x80134010
CHUNK_SOLID_HEADER = 0x134011
CHUNK_SOLID_TEXTURES = 0x134012
CHUNK_PLAT_CONTAINER = 0x80134100
CHUNK_PLAT_VERTEX_BUFFER = 0x134B01
CHUNK_PLAT_MESH_ENTRIES = 0x134B02
CHUNK_PLAT_MESH_ENTRY_NAME = 0x134C02

ENTRY_SIZE = 0x90
SOLID_HEADER_VERSION = 0x16

# <3f 3f  5B B H  16s  H H I  I I  I I  6I  I I  5I  I  4I   = 0x90 bytes (144 bytes)
PLAT_FMT = struct.Struct("<3f 3f 5B B H 16s H H I I I I I 6I I I 5I I 4I")
assert PLAT_FMT.size == ENTRY_SIZE, f"plat entry struct is 0x{PLAT_FMT.size:X}, expected 0x90"

SLOTS = ("diffuse", "normal", "height", "specular", "opacity")

# InternalEffectId order from CarbonSolidReader.cs
EFFECT_NAMES = [
    "WorldShader", "WorldReflectShader", "WorldBoneShader", "WorldNormalMap", "CarShader",
    "CARNORMALMAP", "WorldMinShader", "FEShader", "FEMaskShader", "FilterShader",
    "ScreenFilterShader", "RainDropShader", "VisualTreatmentShader", "WorldPrelitShader",
    "ParticlesShader", "skyshader", "shadow_map_mesh", "CarShadowMapShader", "WorldDepthShader",
    "shadow_map_mesh_depth", "NormalMapNoFog", "InstanceMesh", "ScreenEffectShader", "HDRShader",
    "UCAP", "GLASS_REFLECT", "WATER", "RVMPIP", "GHOSTCAR",
]


class CarbonPlatFlags(IntFlag):
    SKINNED = 0x00000001
    UCAP = 0x00000002
    GLOSSMAPINALPHA = 0x00000004
    OCEAN = 0x00000008
    GLOSSYWINDOWS = 0x00000010
    SKY = 0x00000020
    ROAD_SPECULAR = 0x00000040
    LIGHT = 0x00000080
    ENVMAP = 0x00000100
    CLIP = 0x00000200
    DISABLE_RENDERING = 0x00000400
    DAMAGE_POLY = 0x00000800
    SINGLE_SIDED = 0x00001000
    PACKED_XYZ = 0x00002000
    HAS_NORMALS = 0x00004000
    STRIP_LOCALLY = 0x00008000
    NORMALMAP = 0x00020000
    HEIGHTMAP = 0x00040000
    SPECULARMAP = 0x00080000
    GLOSSINESSMAP = 0x00100000
    LIGHTNORMALMAP = 0x00800000
    LIGHTMAP = 0x02000000


FLAG_BITS = [(name, int(member)) for name, member in CarbonPlatFlags.__members__.items()]
KNOWN_MASK = 0
for _n, _v in FLAG_BITS:
    KNOWN_MASK |= _v
FLAG_NAME_BY_BIT = {v: n for n, v in FLAG_BITS}


def effect_name(effect_id):
    if 0 <= effect_id < len(EFFECT_NAMES):
        return EFFECT_NAMES[effect_id]
    return f"Effect_0x{effect_id:04X}"


def decode_flags(flags):
    """Known names first, then every bit that has no name as UNKNOWN_0x........"""
    out = [n for n, v in FLAG_BITS if flags & v]
    unmapped = flags & ~KNOWN_MASK
    for bit in range(32):
        if unmapped & (1 << bit):
            out.append(f"UNKNOWN_0x{1 << bit:08X}")
    return out


def hex_or_blank(value):
    return "" if value is None else f"0x{value:08X}"


# --------------------------------------------------------------------------
# data
# --------------------------------------------------------------------------

class CarbonPlatEntry:
    """One 0x90 record. Every field of the record is kept."""

    def __init__(self, u, offset, solid, index):
        self.stream_offset = offset
        self.solid = solid
        self.index = index
        self.name = ""
        self.group = None         # MaterialGroup, set by build_groups
        self.raw = b""            # the 0x90 record bytes, set by read_stream

        self.bounds_min = (u[0], u[1], u[2])
        self.bounds_max = (u[3], u[4], u[5])
        self.diffuse_map_id, self.normal_map_id, self.height_map_id = u[6], u[7], u[8]
        self.specular_map_id, self.opacity_map_id = u[9], u[10]
        self.light_material_number = u[11]
        self.unknown_1e = u[12]
        self.unknown_20 = u[13]
        self.effect_id = u[14]
        self.unknown_32 = u[15]
        self.unknown_34 = u[16]
        self.flags = u[17]
        self.texture_sort_key = u[18]
        self.num_verts = u[19]
        self.unknown_44 = u[20]
        self.unknown_48 = tuple(u[21:27])
        self.num_tris = u[27]
        self.index_offset = u[28]
        self.unknown_68 = tuple(u[29:34])
        self.num_indices = u[34]
        self.unknown_80 = tuple(u[35:39])

        table = solid.texture_hashes
        self.slot_state = {}
        for slot in SLOTS:
            sid = getattr(self, f"{slot}_map_id")
            setattr(self, f"{slot}_texture_hash", table[sid] if sid < len(table) else None)
            if sid == 0xFF:
                state = "none"
            elif sid >= len(table):
                state = "out_of_range"
            elif slot != "diffuse" and sid == self.diffuse_map_id:
                state = "same_as_diffuse"
            else:
                state = "texture"
            self.slot_state[slot] = state

    @property
    def effect_name(self):
        return effect_name(self.effect_id)

    @property
    def decoded_flags(self):
        return decode_flags(self.flags)

    has_normal_map = property(lambda s: bool(s.flags & CarbonPlatFlags.NORMALMAP))
    has_height_map = property(lambda s: bool(s.flags & CarbonPlatFlags.HEIGHTMAP))
    has_specular_map = property(lambda s: bool(s.flags & CarbonPlatFlags.SPECULARMAP))
    has_glossiness_map = property(lambda s: bool(s.flags & CarbonPlatFlags.GLOSSINESSMAP))
    has_light_normal_map = property(lambda s: bool(s.flags & CarbonPlatFlags.LIGHTNORMALMAP))
    has_lightmap = property(lambda s: bool(s.flags & CarbonPlatFlags.LIGHTMAP))
    has_envmap = property(lambda s: bool(s.flags & CarbonPlatFlags.ENVMAP))
    is_alpha_clip = property(lambda s: bool(s.flags & CarbonPlatFlags.CLIP))
    uses_alpha_for_gloss = property(lambda s: bool(s.flags & CarbonPlatFlags.GLOSSMAPINALPHA))
    uses_lighting = property(lambda s: bool(s.flags & CarbonPlatFlags.LIGHT))


class SolidObject:
    def __init__(self, offset):
        self.offset = offset
        self.name = ""
        self.hash = None
        self.header_ok = False
        self.header_raw = b""         # 0xA0 bytes of the solid header struct (before the name)
        self.header_flags = None      # u16 @0x0E
        self.num_polys = self.num_verts = None
        self.num_bones = self.num_tex = self.num_light_mats = self.num_markers = None
        self.end = offset
        self.chunks = []              # (chunk id, offset, length) of every chunk inside the solid
        self.vb_chunks = []           # (payload start, payload end) of every 0x134B01 chunk
        self.texture_hashes = []
        self.entries = []


def walk_chunks_recursive(mm, start=0, end=None):
    """
    Recursively walks all chunks and descends into any container chunk (bit 31 set).
    Yields (pos, cid, raw_id, length, is_container, pstart, pend).
    """
    pos = start
    limit = len(mm) if end is None else end
    while pos + 8 <= limit:
        # Skip zero padding between chunks
        if mm[pos:pos + 4] == b"\x00\x00\x00\x00":
            pos += 4
            continue

        raw_id = mm[pos:pos + 4]
        cid = int.from_bytes(raw_id, "little")
        length = struct.unpack_from("<I", mm, pos + 4)[0]
        pstart = pos + 8
        pend = pstart + length
        is_container = bool(cid & 0x80000000)

        yield pos, cid, raw_id, length, is_container, pstart, pend

        # Descend into container chunks
        if is_container and pend <= limit:
            yield from walk_chunks_recursive(mm, pstart, pend)

        pos = pend


def read_stream(path, progress=None):
    """Walks the stream file with full container recursion and returns (solid objects, warnings)."""
    objects, warnings = [], []
    compressed_chunks = 0
    size = os.path.getsize(path)

    with open(path, "rb") as f:
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            cur, cur_end, named, count = None, 0, 0, 0
            for offset, cid, raw_id, length, is_container, pstart, pend in walk_chunks_recursive(mm):
                count += 1
                if progress and count % 2000 == 0:
                    progress(offset, size)

                # Reset active solid when scanning past its container boundary
                if cur is not None and offset >= cur_end:
                    cur = None

                # Detect JDLZ compression chunks
                if cid in (0x00030210, 0x00030203, 0x0003A100) or (pstart + 4 <= pend and mm[pstart:pstart + 4] == b"JDLZ"):
                    compressed_chunks += 1
                    continue

                if cid == SOLID_OBJECT_CONTAINER:
                    cur = SolidObject(offset)
                    cur_end = pend
                    cur.end = pend
                    named = 0
                    objects.append(cur)
                    continue

                if cur is None:
                    continue

                cur.chunks.append((cid, offset, length))

                if cid == CHUNK_SOLID_HEADER:
                    pos = (pstart + 0xF) & ~0xF
                    if pos + 0xA1 <= pend and mm[pos + 0x0C] == SOLID_HEADER_VERSION:
                        cur.hash = struct.unpack_from("<I", mm, pos + 0x10)[0]
                        cur.header_raw = bytes(mm[pos:pos + 0xA0])
                        cur.header_flags = struct.unpack_from("<H", mm, pos + 0x0E)[0]
                        cur.num_polys, cur.num_verts = struct.unpack_from("<HH", mm, pos + 0x14)
                        (cur.num_bones, cur.num_tex, cur.num_light_mats,
                         cur.num_markers) = struct.unpack_from("<BBBB", mm, pos + 0x18)
                        end_str = mm.find(b"\x00", pos + 0xA0, pend)
                        end_str = pend if end_str == -1 else end_str
                        cur.name = bytes(mm[pos + 0xA0:end_str]).decode("ascii", errors="replace")
                        cur.header_ok = True
                    else:
                        warnings.append(f"0x{offset:08X}: solid header version is not 0x16, name skipped")

                elif cid == CHUNK_SOLID_TEXTURES:
                    for i in range(length // 8):
                        cur.texture_hashes.append(struct.unpack_from("<I", mm, pstart + i * 8)[0])

                elif cid == CHUNK_PLAT_MESH_ENTRIES:
                    pos = (pstart + 0xF) & ~0xF
                    if (pend - pos) % ENTRY_SIZE:
                        warnings.append(f"0x{offset:08X}: plat chunk size is not a multiple of 0x90")
                    for k in range((pend - pos) // ENTRY_SIZE):
                        rec = pos + k * ENTRY_SIZE
                        u = PLAT_FMT.unpack_from(mm, rec)
                        entry = CarbonPlatEntry(u, rec, cur, len(cur.entries))
                        entry.raw = bytes(mm[rec:rec + ENTRY_SIZE])
                        cur.entries.append(entry)

                elif cid == CHUNK_PLAT_VERTEX_BUFFER:
                    cur.vb_chunks.append(((pstart + 0xF) & ~0xF, pend))

                elif cid == CHUNK_PLAT_MESH_ENTRY_NAME:
                    if length > 0 and named < len(cur.entries):
                        end_str = mm.find(b"\x00", pstart, pend)
                        end_str = pend if end_str == -1 else end_str
                        cur.entries[named].name = bytes(mm[pstart:end_str]).decode("ascii", errors="replace")
                        named += 1

            if compressed_chunks > 0 and sum(len(o.entries) for o in objects) == 0:
                warnings.append(f"Found {compressed_chunks} JDLZ-compressed chunks. "
                                "This file stores its geometry compressed.")
        finally:
            mm.close()
    return objects, warnings


class MaterialGroup:
    """All entries with the same diffuse hash, effect id and raw flags."""

    def __init__(self, key, name):
        self.key = key                      # (diffuse hash or None, effect id, flags)
        self.name = name
        self.members = []                   # CarbonPlatEntry
        self.solids = set()

    @property
    def diffuse_hash(self):
        return self.key[0]

    @property
    def effect_id(self):
        return self.key[1]

    @property
    def flags(self):
        return self.key[2]

    @property
    def rep(self):
        return self.members[0]


def build_groups(objects, names):
    groups = {}
    for so in objects:
        for e in so.entries:
            key = (e.diffuse_texture_hash, e.effect_id, e.flags)
            g = groups.get(key)
            if g is None:
                base = names.get(e.diffuse_texture_hash) or hex_or_blank(e.diffuse_texture_hash) or "(no diffuse)"
                g = groups[key] = MaterialGroup(key, f"{base}_{e.effect_name}")
            g.members.append(e)
            g.solids.add(so.offset)
            e.group = g
    return sorted(groups.values(), key=lambda g: g.name.lower())


# --------------------------------------------------------------------------
# text and export
# --------------------------------------------------------------------------

def tex_text(e, slot, names):
    sid = getattr(e, f"{slot}_map_id")
    h = getattr(e, f"{slot}_texture_hash")
    state = e.slot_state[slot]
    if h is None:
        return f"id=0x{sid:02X}  hash=-  [{state}]"
    name = names.get(h)
    return f"id=0x{sid:02X}  hash=0x{h:08X}" + (f'  "{name}"' if name else "") + f"  [{state}]"


def dump_entry(e, names, tex=None):
    g = names.get(e.diffuse_texture_hash) if e.diffuse_texture_hash is not None else None
    title = e.name or (f"{g}_{e.effect_name}" if g else f"0x{e.texture_sort_key:08X}")
    flags = e.decoded_flags
    lines = [
        f"Material: {title}",
        f"Solid:    {e.solid.name or '[no name]'}  (entry {e.index}, record 0x{e.stream_offset:08X})",
        "",
        "Effect:",
        f"    {e.effect_name}  (id {e.effect_id})",
        "",
        "Textures:",
    ]
    lines += [f"    {slot:<9} = {tex_text(e, slot, names)}" for slot in SLOTS]
    lines += ["", f"Flags (raw 0x{e.flags:08X}):"]
    lines += [f"    {n}" for n in flags] if flags else ["    (none)"]
    lines += [
        "",
        "Render:",
        f"    alpha clip (CLIP)             {e.is_alpha_clip}",
        f"    opacity map id                0x{e.opacity_map_id:02X}  [{e.slot_state['opacity']}]",
        f"    gloss in alpha                {e.uses_alpha_for_gloss}",
        f"    envmap (ENVMAP)               {e.has_envmap}",
        f"    lighting (LIGHT)              {e.uses_lighting}",
        f"    lightmap (LIGHTMAP)           {e.has_lightmap}",
        f"    light normal map              {e.has_light_normal_map}",
        f"    light material number         {e.light_material_number} (0x{e.light_material_number:02X})",
        f"    sort key                      0x{e.texture_sort_key:08X}",
        f"    verts / tris / indices        {e.num_verts} / {e.num_tris} / {e.num_indices}",
        f"    index offset                  {e.index_offset}",
        "",
        "Unknown fields (names in brackets = field at that offset in the Hyperlinked runtime struct,",
        "which is a candidate only; a runtime pointer is not proven to be a disk value):",
        f"    @0x1E u16   0x{e.unknown_1e:04X}   [blend_matrix_indices start]",
        f"    @0x20 x16   {e.unknown_20.hex()}   [blend_matrix_indices, 16 bytes from 0x1E]",
        f"    @0x32 u16   0x{e.unknown_32:04X}   [shader_type high half]",
        f"    @0x34 u32   0x{e.unknown_34:08X}   [effect pointer]",
        f"    @0x44 u32   0x{e.unknown_44:08X}   [Unknown44 / ChunkDataCandidate, NOT a flags copy]",
        f"    @0x48 u32   0x{e.unknown_48[0]:08X}   [chunk_data_size candidate]",
        f"    @0x4C..0x5C {' '.join(f'{x:08X}' for x in e.unknown_48[1:])}   [unknown_4 .. unknown_8]",
        f"    @0x68..0x78 {' '.join(f'{x:08X}' for x in e.unknown_68)}   [idk_yet, file vb ptr, file vb size, d3d vb ptr, d3d vertex count]",
        f"    @0x80..0x8C {' '.join(f'{x:08X}' for x in e.unknown_80)}   [vlt name ptr, overridden material key, vertex data ptr, vertex offset]",
    ]
    if tex is not None:
        lines += ["", "Texture side (from the material dictionary):"]
        for slot in SLOTS:
            h = getattr(e, f"{slot}_texture_hash")
            if h is None or (slot != "diffuse" and e.slot_state[slot] != "texture"):
                continue
            lines.append(f"    {slot:<9} {tex_side_text(tex.get(h))}")
    s = e.solid
    lines += ["", "Solid:"]
    lines += [f"    {k:<26} {v}" for k, v in solid_fields(s)]
    return "\n".join(lines)


# Raw texture struct values written by AssetDumper under "raw" in the dictionary.
TEX_RAW_KEYS = ("classNameHash", "biasLevel", "renderingOrder", "applyAlphaSorting", "flags",
                "mipmapBiasType", "pad", "pad2", "pad3")
# Texture side values already decoded by the dictionary.
TEX_DECODED_KEYS = ("alphaUsage", "alphaBlend", "tiling", "renderFlags")


def tex_raw_text(v):
    return f"0x{v:02X}" if isinstance(v, int) else str(v)


def tex_side_text(rec):
    """One line for a dictionary texture record. Keys as written by AssetDumper's dictionary."""
    if not rec:
        return "(not in the loaded dictionary)"
    parts = [f"alpha {rec.get('alphaUsage') or '-'} / {rec.get('alphaBlend') or '-'}"]
    for key in ("tiling", "renderFlags", "width", "height"):
        if rec.get(key) is not None:
            parts.append(f"{key}={rec[key]}")
    sc = rec.get("scroll")
    if sc:
        parts.append("scroll=" + json.dumps(sc, sort_keys=True))
    fs = rec.get("frameSwap")
    if fs:
        parts.append("frameSwap=" + json.dumps(fs, sort_keys=True))
    raw = rec.get("raw")
    if raw:
        parts.append("raw: " + " ".join(f"{k}={tex_raw_text(raw[k])}" for k in TEX_RAW_KEYS if k in raw))
    return "  ".join(parts)


def load_scenery(infos_path):
    """Reads scenery_infos.tsv and the scenery_instances.tsv next to it.
    Returns {solid hash: {"count", "sections", "flags": {flag name: instances that have it}, "raw": {raw flags: n}}}."""
    inst_path = os.path.join(os.path.dirname(infos_path), "scenery_instances.tsv")
    keys_by_info = {}
    with open(infos_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f, delimiter="\t"):
            keys = []
            for k in ("SolidKey1", "SolidKey2", "SolidKey3", "SolidKey4"):
                v = to_hex(row.get(k))
                if v:
                    keys.append(v)
            keys_by_info[(row["Section"], row["Info"])] = keys
    out = {}
    with open(inst_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f, delimiter="\t"):
            names = [n for n in (row.get("FlagNames") or "").split("|") if n and n != "None"]
            raw = to_hex(row.get("Flags")) or 0
            for key in keys_by_info.get((row["Section"], row["Info"]), []):
                rec = out.setdefault(key, {"count": 0, "sections": set(), "flags": {}, "raw": {}})
                rec["count"] += 1
                rec["sections"].add(row["Section"])
                rec["raw"][raw] = rec["raw"].get(raw, 0) + 1
                for n in names:
                    rec["flags"][n] = rec["flags"].get(n, 0) + 1
    return out


def to_hex(text):
    try:
        return int(text, 16)
    except (TypeError, ValueError):
        return None


def scenery_lines(rec):
    """(label, text) pairs for the solid fields. rec is None when no scenery file is loaded."""
    if rec is None:
        return []
    if not rec["count"]:
        return [("scenery instances", "0 (no scenery info lists this solid as a SolidKey)")]
    n = rec["count"]
    always = sorted(k for k, c in rec["flags"].items() if c == n)
    some = sorted(f"{k} {c}/{n}" for k, c in rec["flags"].items() if c < n)
    return [("scenery instances", f"{n} in {len(rec['sections'])} sections"),
            ("instance flags on ALL", " ".join(always) or "(none)"),
            ("instance flags on SOME", " ".join(some) or "(none)"),
            ("instance raw flag values", " ".join(f"0x{k:08X}x{c}" for k, c in sorted(rec["raw"].items())[:6]))]


def solid_fields(s):
    """Solid level fields as (label, text). Used by the dump and by the difference view."""
    def h(v, w=2):
        return "-" if v is None else f"0x{v:0{w}X}"
    counts = {}
    for cid, _o, _l in s.chunks:
        counts[cid] = counts.get(cid, 0) + 1
    chunk_list = " ".join(f"{cid:X}" + (f"x{n}" if n > 1 else "") for cid, n in sorted(counts.items()))
    return [
        ("solid name", s.name or "[no name]"),
        ("solid hash", hex_or_blank(s.hash) or "-"),
        ("header flags (u16 @0x0E)", h(s.header_flags, 4)),
        ("num polys / verts", f"{s.num_polys} / {s.num_verts}"),
        ("num bones", str(s.num_bones)),
        ("num texture entries", str(s.num_tex)),
        ("num light materials", str(s.num_light_mats)),
        ("num position markers", str(s.num_markers)),
        ("entries in solid", str(len(s.entries))),
        ("chunk ids inside solid", chunk_list),
    ] + scenery_lines(getattr(s, "scenery", None))


def vertex_color_stats(path, solid, cache):
    """{entry index: text}. Colors are shown as 4 raw bytes (byte order is not assumed)."""
    if solid.offset in cache:
        return cache[solid.offset]
    result = {}
    solid.vmeans = {}                 # entry index -> (mean byte0, byte1, byte2, byte3), for the Field values tab
    try:
        import nfs_solid_reader as nsr
        with open(path, "rb") as f:
            vbs = []
            for a, b in solid.vb_chunks:
                f.seek(a)
                vbs.append(f.read(b - a))
        ns = nsr.SolidObject()
        last_fx, stream_index, total = None, 0, 0
        for e in solid.entries:
            m = nsr.SolidMaterial()
            m.effect_id, m.num_verts = e.effect_id, e.num_verts
            if ns.materials and e.effect_id != last_fx:
                stream_index += 1
            m.vertex_set_index = stream_index
            ns.materials.append(m)
            last_fx = e.effect_id
            total += e.num_verts
        nsr._assemble_vertices(ns, vbs, total)
        offsets = {}
        for e, m in zip(solid.entries, ns.materials):
            idx = m.vertex_set_index
            if idx >= len(ns.vertex_sets):
                result[e.index] = "no vertex set"
                continue
            off = offsets.get(idx, 0)
            verts = ns.vertex_sets[idx][off:off + (e.num_verts or len(ns.vertex_sets[idx]))]
            offsets[idx] = off + (e.num_verts or 0)
            cols = [v.color for v in verts if v.color is not None]
            if not cols:
                result[e.index] = "no color in this vertex layout"
                continue
            chan = [[(c >> (8 * k)) & 0xFF for c in cols] for k in range(4)]
            solid.vmeans[e.index] = tuple(sum(ch) / len(ch) for ch in chan)
            result[e.index] = (f"{len(cols)} verts, {len(set(cols))} distinct; byte0..3 min/max/mean: " +
                               "  ".join(f"{min(ch)}/{max(ch)}/{sum(ch) / len(ch):.0f}" for ch in chan))
    except Exception as ex:
        msg = f"vertex decode failed: {type(ex).__name__}: {ex}"
        result = {e.index: msg for e in solid.entries}
    cache[solid.offset] = result
    return result


def entry_fields(e, names, tex, vstats):
    """Ordered (label, text) pairs for one entry: solid fields, record fields, texture side, vertices."""
    out = list(solid_fields(e.solid))
    out += [("entry index", str(e.index)), ("entry name", e.name or "-"),
            ("effect", f"{e.effect_name} ({e.effect_id})"), ("flags raw", f"0x{e.flags:08X}"),
            ("flags decoded", " ".join(e.decoded_flags) or "(none)")]
    for slot in SLOTS:
        h = getattr(e, f"{slot}_texture_hash")
        out.append((f"{slot} slot", f"0x{getattr(e, f'{slot}_map_id'):02X} {hex_or_blank(h) or '-'} [{e.slot_state[slot]}]"))
    out += [("light material number", f"0x{e.light_material_number:02X}"),
            ("sort key", f"0x{e.texture_sort_key:08X}"),
            ("verts / tris / indices", f"{e.num_verts} / {e.num_tris} / {e.num_indices}"),
            ("index offset", str(e.index_offset)),
            ("unk @0x1E..0x2F", f"{e.unknown_1e:04X} {e.unknown_20.hex()}"),
            ("unk @0x32", f"0x{e.unknown_32:04X}"), ("unk @0x34", f"0x{e.unknown_34:08X}"),
            ("unk @0x44", f"0x{e.unknown_44:08X}"),
            ("unk @0x48", f"0x{e.unknown_48[0]:08X}"),
            ("unk @0x4C..0x5C", " ".join(f"{x:08X}" for x in e.unknown_48[1:])),
            ("unk @0x68..0x78", " ".join(f"{x:08X}" for x in e.unknown_68)),
            ("unk @0x80..0x8C", " ".join(f"{x:08X}" for x in e.unknown_80))]
    if tex is not None:
        h = e.diffuse_texture_hash
        out.append(("diffuse texture side", tex_side_text(tex.get(h)) if h is not None else "-"))
        if e.slot_state["normal"] == "texture":
            out.append(("normal texture side", tex_side_text(tex.get(e.normal_texture_hash))))
    if vstats is not None:
        out.append(("vertex colors", vstats.get(e.index, "-")))
    return out


def diff_text(entries, names, tex, vcache, path):
    """Only the fields that differ between the selected entries, then the ones that match."""
    rows = []
    for e in entries:
        vs = vertex_color_stats(path, e.solid, vcache) if path else None
        rows.append(entry_fields(e, names, tex, vs))
    labels = [k for k, _ in rows[0]]
    lines = [f"Compare {len(entries)} entries", ""]
    for n, e in enumerate(entries, 1):
        lines.append(f"  [{n}] {e.solid.name or '[no name]'}  entry {e.index}  record 0x{e.stream_offset:08X}")
    same = []
    lines += ["", "Fields that DIFFER:"]
    for i, label in enumerate(labels):
        vals = [r[i][1] if i < len(r) and r[i][0] == label else "-" for r in rows]
        if len(set(vals)) > 1:
            lines.append(f"  {label}")
            lines += [f"      [{n}] {v}" for n, v in enumerate(vals, 1)]
        else:
            same.append((label, vals[0]))
    lines += ["", "Fields that MATCH:"]
    lines += [f"  {k:<26} {v}" for k, v in same]
    return "\n".join(lines)


def full_dump_text(e, names, tex, vstats):
    """Everything known about one entry and its solid, for a text file."""
    parts = [dump_entry(e, names, tex), "", "Raw 0x90 record:"]
    for i in range(0, len(e.raw), 16):
        parts.append(f"    +0x{i:02X}  {e.raw[i:i + 16].hex(' ')}")
    parts += ["", f"Solid header raw ({len(e.solid.header_raw)} bytes):"]
    for i in range(0, len(e.solid.header_raw), 16):
        parts.append(f"    +0x{i:02X}  {e.solid.header_raw[i:i + 16].hex(' ')}")
    parts += ["", "Chunks inside the solid (id, stream offset, length):"]
    parts += [f"    0x{cid:08X}  0x{off:08X}  {ln}" for cid, off, ln in e.solid.chunks]
    if vstats is not None:
        parts += ["", f"Vertex colors: {vstats.get(e.index, '-')}"]
    return "\n".join(parts)


def distinct_text(members):
    """For one material group: the values the other fields take across its entries."""
    def seq(label, fn, fmt=str):
        vals = sorted({fn(m) for m in members}, key=lambda v: str(v))
        shown = ", ".join(fmt(v) for v in vals[:6]) + (f", ... ({len(vals)} values)" if len(vals) > 6 else "")
        return f"    {label:<22} {shown}"

    lines = [f"Across {len(members)} entries of this material:"]
    lines.append(seq("light material number", lambda m: m.light_material_number))
    lines.append(seq("sort key", lambda m: m.texture_sort_key, lambda v: f"0x{v:08X}"))
    for slot in SLOTS[1:]:
        lines.append(seq(f"{slot} slot", lambda m, s=slot: (getattr(m, f"{s}_map_id"), m.slot_state[s]),
                         lambda v: f"0x{v[0]:02X} {v[1]}"))
    lines.append(seq("unknown @0x1E", lambda m: m.unknown_1e, lambda v: f"0x{v:04X}"))
    lines.append(seq("unknown @0x20", lambda m: m.unknown_20.hex()))
    lines.append(seq("unknown @0x32", lambda m: m.unknown_32, lambda v: f"0x{v:04X}"))
    lines.append(seq("unknown @0x34", lambda m: m.unknown_34, lambda v: f"0x{v:08X}"))
    lines.append(seq("unknown @0x44", lambda m: m.unknown_44, lambda v: f"0x{v:08X}"))
    lines.append(seq("unknown @0x48", lambda m: m.unknown_48, lambda v: " ".join(f"{x:X}" for x in v)))
    lines.append(seq("unknown @0x68", lambda m: m.unknown_68, lambda v: " ".join(f"{x:X}" for x in v)))
    lines.append(seq("unknown @0x80", lambda m: m.unknown_80, lambda v: " ".join(f"{x:X}" for x in v)))
    return "\n".join(lines)


ENTRY_COLUMNS = [
    "Solid", "EntryIndex", "EntryName", "RecordOffset", "EffectId", "EffectName", "FlagsRaw", "FlagsDecoded",
    "DiffuseId", "DiffuseHash", "DiffuseName", "DiffuseState",
    "NormalId", "NormalHash", "NormalName", "NormalState",
    "HeightId", "HeightHash", "HeightName", "HeightState",
    "SpecularId", "SpecularHash", "SpecularName", "SpecularState",
    "OpacityId", "OpacityHash", "OpacityName", "OpacityState",
    "LightMaterialNumber", "SortKey", "NumVerts", "NumTris", "NumIndices", "IndexOffset",
    "BoundsMin", "BoundsMax",
    "HasNormalMap", "HasHeightMap", "HasSpecularMap", "HasGlossinessMap", "HasLightNormalMap",
    "HasLightmap", "HasEnvmap", "IsAlphaClip", "UsesAlphaForGloss", "UsesLighting",
    "Unk1E", "Unk20", "Unk32", "Unk34", "Unk44", "Unk48", "Unk68", "Unk80",
    "SolidHash", "SolidHeaderFlags", "SolidNumLightMaterials", "SolidNumMarkers", "SolidNumBones",
]


def entry_row(e, names):
    row = [e.solid.name, e.index, e.name, f"0x{e.stream_offset:08X}", e.effect_id, e.effect_name,
           f"0x{e.flags:08X}", "|".join(e.decoded_flags)]
    for slot in SLOTS:
        h = getattr(e, f"{slot}_texture_hash")
        row += [getattr(e, f"{slot}_map_id"), hex_or_blank(h), names.get(h, "") if h is not None else "",
                e.slot_state[slot]]
    row += [e.light_material_number, f"0x{e.texture_sort_key:08X}", e.num_verts, e.num_tris, e.num_indices,
            e.index_offset,
            " ".join(f"{v:.4f}" for v in e.bounds_min), " ".join(f"{v:.4f}" for v in e.bounds_max),
            e.has_normal_map, e.has_height_map, e.has_specular_map, e.has_glossiness_map,
            e.has_light_normal_map, e.has_lightmap, e.has_envmap, e.is_alpha_clip, e.uses_alpha_for_gloss,
            e.uses_lighting,
            f"0x{e.unknown_1e:04X}", e.unknown_20.hex(), f"0x{e.unknown_32:04X}", f"0x{e.unknown_34:08X}",
            f"0x{e.unknown_44:08X}", " ".join(f"{x:08X}" for x in e.unknown_48),
            " ".join(f"{x:08X}" for x in e.unknown_68), " ".join(f"{x:08X}" for x in e.unknown_80),
            hex_or_blank(e.solid.hash), "" if e.solid.header_flags is None else f"0x{e.solid.header_flags:04X}",
            e.solid.num_light_mats, e.solid.num_markers, e.solid.num_bones]
    return row


# --------------------------------------------------------------------------
# window
# --------------------------------------------------------------------------

TAG_EMISSIVE, TAG_NOT = "emissive", "not emissive"
FILTER_BITS = [("LIGHT", CarbonPlatFlags.LIGHT), ("LIGHTMAP", CarbonPlatFlags.LIGHTMAP),
               ("LIGHTNORMALMAP", CarbonPlatFlags.LIGHTNORMALMAP), ("CLIP", CarbonPlatFlags.CLIP),
               ("GLOSSMAPINALPHA", CarbonPlatFlags.GLOSSMAPINALPHA), ("ENVMAP", CarbonPlatFlags.ENVMAP)]
SHORT = {"texture": "tex", "same_as_diffuse": "=diff", "none": "none", "out_of_range": "OOR"}


def make_table(parent, cols):
    frame = ttk.Frame(parent)
    tree = ttk.Treeview(frame, columns=[c[0] for c in cols], show="headings", selectmode="extended")
    vsb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    hsb = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
    tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
    tree.grid(row=0, column=0, sticky="nsew")
    vsb.grid(row=0, column=1, sticky="ns")
    hsb.grid(row=1, column=0, sticky="ew")
    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)
    for key, title, width in cols:
        tree.heading(key, text=title)
        tree.column(key, width=width, minwidth=30, stretch=False)
    return frame, tree


class App(tk.Tk):
    def __init__(self, path=None, dict_path=None):
        super().__init__()
        self.title("Carbon Plat Entry Viewer")
        self.geometry("1700x900")
        self.objects, self.groups, self.names = [], [], {}
        self.tags = {}                       # group key -> tag (default for every entry of the group)
        self.entry_tags = {}                 # (solid hash, entry index) -> tag, overrides the group tag
        self.combo_filter = None
        self.sort_col, self.sort_rev = "name", False
        self.busy = False
        self.tex = None                      # {texture hash: dictionary record}, from the material dictionary
        self.scen = None                     # {solid hash: scenery instance summary}, from the scenery TSVs
        self.stream_path = None
        self.vcache = {}                     # solid offset -> vertex color stats
        self.by_diffuse = {}                 # diffuse hash -> [entries]
        self.inst_entries = []               # rows of the Instances tab
        self.entry_names = {}                # (solid hash, entry index) -> solid name, for the tags file
        self.fields_dirty = True

        self._build_menu()
        self._build_layout()
        self.load_tags()
        if dict_path:
            self.load_dictionary(dict_path)
        if path:
            self.after(100, lambda: self.start_load(path))

    def _build_menu(self):
        menubar = tk.Menu(self)
        m = tk.Menu(menubar, tearoff=0)
        m.add_command(label="Open stream file...", command=self.on_open, accelerator="Ctrl+O")
        m.add_command(label="Load material dictionary (JSON)...", command=self.on_load_dictionary)
        m.add_command(label="Load scenery TSVs (pick scenery_infos.tsv)...", command=self.on_load_scenery)
        m.add_separator()
        m.add_command(label="Dump selected instances (TXT)...", command=self.dump_selected)
        m.add_command(label="Export all entries (TSV)...", command=self.export_entries)
        m.add_command(label="Export materials (TSV)...", command=self.export_materials)
        m.add_separator()
        m.add_command(label="Quit", command=self.destroy)
        menubar.add_cascade(label="File", menu=m)
        self.config(menu=menubar)
        self.bind("<Control-o>", lambda e: self.on_open())

    def _build_layout(self):
        top = ttk.Frame(self, padding=(6, 6, 6, 0))
        top.pack(side=tk.TOP, fill=tk.X)
        ttk.Button(top, text="Open stream file...", command=self.on_open).pack(side=tk.LEFT)
        ttk.Label(top, text="Tag selected:").pack(side=tk.LEFT, padx=(16, 4))
        ttk.Button(top, text="Emissive (E)", command=lambda: self.tag_selected(TAG_EMISSIVE)).pack(side=tk.LEFT)
        ttk.Button(top, text="Not emissive (N)", command=lambda: self.tag_selected(TAG_NOT)).pack(side=tk.LEFT, padx=4)
        ttk.Button(top, text="Clear tag (C)", command=lambda: self.tag_selected(None)).pack(side=tk.LEFT)
        self.progress = ttk.Progressbar(top, orient="horizontal", mode="determinate", length=220)
        self.progress.pack(side=tk.RIGHT)

        flt = ttk.Frame(self, padding=(6, 4, 6, 0))
        flt.pack(side=tk.TOP, fill=tk.X)
        ttk.Label(flt, text="Name:").pack(side=tk.LEFT)
        self.text_var = tk.StringVar()
        self.text_var.trace_add("write", lambda *a: self.refresh_materials())
        ttk.Entry(flt, textvariable=self.text_var, width=22).pack(side=tk.LEFT, padx=(2, 8))
        ttk.Label(flt, text="Effect:").pack(side=tk.LEFT)
        self.effect_var = tk.StringVar(value="any")
        self.effect_box = ttk.Combobox(flt, textvariable=self.effect_var, width=18, state="readonly", values=["any"])
        self.effect_box.pack(side=tk.LEFT, padx=(2, 8))
        self.effect_box.bind("<<ComboboxSelected>>", lambda e: self.refresh_materials())
        self.bit_vars = {}
        for name, _bit in FILTER_BITS:
            ttk.Label(flt, text=name + ":").pack(side=tk.LEFT)
            var = tk.StringVar(value="any")
            box = ttk.Combobox(flt, textvariable=var, width=5, state="readonly", values=["any", "on", "off"])
            box.pack(side=tk.LEFT, padx=(2, 6))
            box.bind("<<ComboboxSelected>>", lambda e: self.refresh_materials())
            self.bit_vars[name] = var
        ttk.Label(flt, text="Tag:").pack(side=tk.LEFT)
        self.tag_var = tk.StringVar(value="any")
        box = ttk.Combobox(flt, textvariable=self.tag_var, width=12, state="readonly",
                           values=["any", "untagged", TAG_EMISSIVE, TAG_NOT])
        box.pack(side=tk.LEFT, padx=2)
        box.bind("<<ComboboxSelected>>", lambda e: self.refresh_materials())
        self.combo_label = ttk.Button(flt, text="", command=self.clear_combo_filter)

        info = ttk.Frame(self, padding=(6, 2, 6, 4))
        info.pack(side=tk.TOP, fill=tk.X)
        self.stats_var = tk.StringVar(value="No file loaded")
        ttk.Label(info, textvariable=self.stats_var).pack(side=tk.LEFT)

        paned = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True)

        self.nb = ttk.Notebook(paned)
        paned.add(self.nb, weight=3)
        self.after(200, lambda: paned.sashpos(0, 1000))

        mat_cols = [("name", "Material", 300), ("effect", "Effect", 130), ("flags", "Flags", 84),
                    ("LIGHT", "LIGHT", 48), ("LMAP", "LMAP", 48), ("LNRM", "LNRM", 48), ("CLIP", "CLIP", 44),
                    ("GLOSSA", "GLOSS_A", 56), ("ENV", "ENV", 40),
                    ("normal", "Normal", 54), ("height", "Height", 54), ("specular", "Spec", 54),
                    ("opacity", "Opac", 54), ("entries", "Entries", 52), ("solids", "Solids", 48),
                    ("tag", "Tag", 90)]
        f, self.mat_tree = make_table(self.nb, mat_cols)
        for key, *_ in mat_cols:
            self.mat_tree.heading(key, command=lambda k=key: self.on_sort(k))
        self.mat_tree.tag_configure(TAG_EMISSIVE, background="#fff3b0")
        self.mat_tree.tag_configure(TAG_NOT, background="#e6e6e6")
        self.mat_tree.bind("<<TreeviewSelect>>", self.on_select)
        self.mat_tree.bind("<Key-e>", lambda e: self.tag_selected(TAG_EMISSIVE))
        self.mat_tree.bind("<Key-n>", lambda e: self.tag_selected(TAG_NOT))
        self.mat_tree.bind("<Key-c>", lambda e: self.tag_selected(None))
        self.nb.add(f, text="Materials")

        combo_cols = [("effect", "Effect", 150), ("flags", "Flags", 90), ("decoded", "Decoded flags", 420),
                      ("materials", "Materials", 70), ("entries", "Entries", 60), ("emi", "Emissive", 60),
                      ("not", "Not", 50), ("verdict", "Reading", 200)]
        f, self.combo_tree = make_table(self.nb, combo_cols)
        self.combo_tree.bind("<Double-1>", self.on_combo_pick)
        self.nb.add(f, text="Flag combos (double-click to filter)")

        bit_cols = [("bit", "Bit", 90), ("name", "Name", 150), ("set_e", "Set: emissive", 90),
                    ("set_n", "Set: not", 70), ("clr_e", "Clear: emissive", 100), ("clr_n", "Clear: not", 80),
                    ("reading", "Reading", 300)]
        f, self.bit_tree = make_table(self.nb, bit_cols)
        self.nb.add(f, text="Flag bits (tagged materials only)")

        self.inst_frame = ttk.Frame(self.nb)
        bar = ttk.Frame(self.inst_frame)
        bar.pack(side=tk.TOP, fill=tk.X, padx=4, pady=2)
        self.inst_var = tk.StringVar(value="Select a material on the Materials tab")
        ttk.Label(bar, textvariable=self.inst_var).pack(side=tk.LEFT)
        ttk.Button(bar, text="Dump selected (TXT)", command=self.dump_selected).pack(side=tk.RIGHT)
        inst_cols = [("solid", "Solid", 210), ("shash", "Solid hash", 90), ("entry", "Entry", 44),
                     ("vlt", "Entry name (0x134C02)", 170),
                     ("effect", "Effect", 120), ("flags", "Flags", 84), ("hflags", "Hdr flags", 66),
                     ("nlm", "LightMats", 62), ("unk44", "@0x44", 84), ("sort", "Sort key", 84),
                     ("verts", "Verts", 50), ("tag", "Tag", 90)]
        f, self.inst_tree = make_table(self.inst_frame, inst_cols)
        f.pack(fill=tk.BOTH, expand=True)
        self.inst_tree.tag_configure(TAG_EMISSIVE, background="#fff3b0")
        self.inst_tree.tag_configure(TAG_NOT, background="#e6e6e6")
        self.inst_tree.bind("<<TreeviewSelect>>", self.on_inst_select)
        self.inst_tree.bind("<Key-e>", lambda e: self.tag_selected(TAG_EMISSIVE))
        self.inst_tree.bind("<Key-n>", lambda e: self.tag_selected(TAG_NOT))
        self.inst_tree.bind("<Key-c>", lambda e: self.tag_selected(None))
        self.nb.add(self.inst_frame, text="Instances (same diffuse; select 2+ to compare)")

        field_cols = [("field", "Field", 190), ("value", "Value", 330), ("entries", "Entries", 70),
                      ("emi", "Emissive", 64), ("not", "Not", 50), ("reading", "Value reading", 190),
                      ("sep", "Field reading", 200)]
        self.fields_frame, self.fields_tree = make_table(self.nb, field_cols)
        self.nb.add(self.fields_frame, text="Field values (all entries)")
        self.nb.bind("<<NotebookTabChanged>>", self.on_tab_changed)

        right = ttk.Frame(paned)
        self.detail = tk.Text(right, wrap="none", font=("Consolas", 9))
        dvs = ttk.Scrollbar(right, orient="vertical", command=self.detail.yview)
        dhs = ttk.Scrollbar(right, orient="horizontal", command=self.detail.xview)
        self.detail.configure(yscrollcommand=dvs.set, xscrollcommand=dhs.set)
        self.detail.grid(row=0, column=0, sticky="nsew")
        dvs.grid(row=0, column=1, sticky="ns")
        dhs.grid(row=1, column=0, sticky="ew")
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)
        paned.add(right, weight=2)

    def tags_path(self):
        return out_path(TOOL_NAME, "material_tags.tsv")

    def load_tags(self):
        p = self.tags_path()
        if not os.path.exists(p):
            return
        with open(p, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f, delimiter="\t"):
                if row.get("tag") not in (TAG_EMISSIVE, TAG_NOT):
                    continue
                try:
                    if row.get("scope") == "entry":
                        self.entry_tags[(int(row["solid_hash"], 16), int(row["entry_index"]))] = row["tag"]
                    else:                    # older files have no scope column: group tags
                        h = int(row["diffuse_hash"], 16) if row["diffuse_hash"] else None
                        self.tags[(h, int(row["effect_id"]), int(row["flags"], 16))] = row["tag"]
                except (KeyError, ValueError):
                    continue

    def save_tags(self):
        names = {g.key: g.name for g in self.groups}
        with open(self.tags_path(), "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f, delimiter="\t")
            w.writerow(["scope", "diffuse_hash", "effect_id", "flags", "solid_hash", "entry_index", "tag", "name"])
            for key, tag in sorted(self.tags.items(), key=lambda kv: str(kv[0])):
                w.writerow(["group", hex_or_blank(key[0]), key[1], f"0x{key[2]:08X}", "", "", tag, names.get(key, "")])
            for (sh, idx), tag in sorted(self.entry_tags.items(), key=lambda kv: str(kv[0])):
                w.writerow(["entry", "", "", "", hex_or_blank(sh), idx, tag, self.entry_names.get((sh, idx), "")])

    # ---- tag helpers: an entry tag beats the group tag ----
    def etag(self, e):
        return self.entry_tags.get((e.solid.hash, e.index)) or self.tags.get(e.group.key)

    def group_counts(self, g):
        """(emissive entries, not-emissive entries) of one group, effective tags."""
        gt = self.tags.get(g.key)
        if not self.entry_tags:
            n = len(g.members)
            return (n, 0) if gt == TAG_EMISSIVE else (0, n) if gt == TAG_NOT else (0, 0)
        emi = no = 0
        for e in g.members:
            t = self.etag(e)
            if t == TAG_EMISSIVE:
                emi += 1
            elif t == TAG_NOT:
                no += 1
        return emi, no

    def group_tag_label(self, g):
        emi, no = self.group_counts(g)
        if not emi and not no:
            return ""
        if emi == len(g.members):
            return TAG_EMISSIVE
        if no == len(g.members):
            return TAG_NOT
        return f"mixed {emi}E/{no}N"

    def tag_selected(self, tag):
        on_inst = self.nb.select() == str(self.inst_frame)
        if on_inst:                  # Instances tab: tag single entries (solids)
            sel = self.inst_tree.selection()
            for i in sel:
                e = self.inst_entries[int(i)]
                ek = (e.solid.hash, e.index)
                if tag is None:
                    self.entry_tags.pop(ek, None)
                else:
                    self.entry_tags[ek] = tag
                    self.entry_names[ek] = e.solid.name
        else:                        # Materials tab: tag a whole group, entry tags of its members are dropped
            sel = self.mat_tree.selection()
            for i in sel:
                g = self.groups[int(i)]
                for e in g.members:
                    self.entry_tags.pop((e.solid.hash, e.index), None)
                if tag is None:
                    self.tags.pop(g.key, None)
                else:
                    self.tags[g.key] = tag
        if not sel:
            return "break"
        self.save_tags()
        self.fields_dirty = True
        if on_inst:
            self.refresh_materials(keep=self.mat_tree.selection())
            self.fill_instances(keep=sel)
        else:
            self.refresh_materials(keep=sel)
            self.fill_instances(keep=())
        self.refresh_combos()
        self.refresh_bits()
        return "break"

    def on_open(self):
        path = filedialog.askopenfilename(title="Open Carbon stream file (STREAML5RA.BUN / L5RA.BUN)",
                                          filetypes=[("Stream bundle", "*.bun *.BUN"), ("All files", "*.*")])
        if path:
            self.start_load(path)

    def start_load(self, path):
        if self.busy:
            return
        self.busy = True
        self.progress["value"] = 0
        self.stats_var.set(f"Reading {os.path.basename(path)} ...")

        def progress(cur, total):
            self.after(0, lambda: self.progress.configure(value=int(cur * 100 / max(total, 1))))

        def worker():
            try:
                names = load_dictionary()
                objects, warnings = read_stream(path, progress)
                self.after(0, lambda: self.on_loaded(path, names, objects, warnings))
            except Exception as ex:
                msg = f"{type(ex).__name__}: {ex}"
                self.after(0, lambda: self.on_load_failed(msg))

        threading.Thread(target=worker, daemon=True).start()

    def on_load_failed(self, msg):
        self.busy = False
        self.stats_var.set("Read failed")
        messagebox.showerror("Read failed", msg)

    def on_loaded(self, path, names, objects, warnings):
        self.busy = False
        self.progress["value"] = 100
        self.names, self.objects = names, objects
        self.stream_path = path
        self.inst_group = None
        self.apply_scenery()
        self.vcache = {}
        self.fields_dirty = True
        self.groups = build_groups(objects, names)
        self.by_diffuse = {}
        for so in objects:
            for e in so.entries:
                self.by_diffuse.setdefault(e.diffuse_texture_hash, []).append(e)
        total = sum(len(o.entries) for o in objects)
        effects = sorted({effect_name(g.effect_id) for g in self.groups})
        self.effect_box.configure(values=["any"] + effects)
        self.effect_var.set("any")
        self.combo_filter = None
        self.stats_var.set(f"{os.path.basename(path)}: {len(objects)} solids, {total} plat entries, "
                           f"{len(self.groups)} materials, {len(names)} names known"
                           + (f", {len(warnings)} warnings" if warnings else ""))
        self.refresh_materials()
        self.refresh_combos()
        self.refresh_bits()
        if total == 0:
            msg = "No 0x134B02 entries found inside solid containers."
            if any("JDLZ" in w for w in warnings):
                msg += "\n\nThe file contains JDLZ-compressed chunks (standard for retail STREAM*.BUN files).\n" \
                       "Please decompress the stream file, or test with TRACKS\\L5RA.BUN / GLOBALB.BUN."
            messagebox.showwarning("No entries", msg)
        elif warnings:
            messagebox.showwarning("Warnings", "\n".join(warnings[:20]) + (f"\n... {len(warnings)} total" if len(warnings) > 20 else ""))

    def passes(self, g):
        text = self.text_var.get().strip().lower()
        if text and text not in g.name.lower() and not any(text in m.solid.name.lower() or text in m.name.lower()
                                                           for m in g.members[:200]):
            return False
        if self.effect_var.get() != "any" and effect_name(g.effect_id) != self.effect_var.get():
            return False
        for name, bit in FILTER_BITS:
            want = self.bit_vars[name].get()
            if want == "on" and not g.flags & bit:
                return False
            if want == "off" and g.flags & bit:
                return False
        tag = self.group_tag_label(g)
        want_tag = self.tag_var.get()
        if want_tag == "untagged" and tag:
            return False
        if want_tag in (TAG_EMISSIVE, TAG_NOT) and tag != want_tag:
            return False
        if self.combo_filter and (g.effect_id, g.flags) != self.combo_filter:
            return False
        return True

    def row_values(self, i, g):
        e = g.rep
        mark = lambda bit: "x" if g.flags & bit else ""
        return {
            "name": g.name, "effect": effect_name(g.effect_id), "flags": f"0x{g.flags:08X}",
            "LIGHT": mark(CarbonPlatFlags.LIGHT), "LMAP": mark(CarbonPlatFlags.LIGHTMAP),
            "LNRM": mark(CarbonPlatFlags.LIGHTNORMALMAP), "CLIP": mark(CarbonPlatFlags.CLIP),
            "GLOSSA": mark(CarbonPlatFlags.GLOSSMAPINALPHA), "ENV": mark(CarbonPlatFlags.ENVMAP),
            "normal": SHORT[e.slot_state["normal"]], "height": SHORT[e.slot_state["height"]],
            "specular": SHORT[e.slot_state["specular"]], "opacity": SHORT[e.slot_state["opacity"]],
            "entries": len(g.members), "solids": len(g.solids), "tag": self.group_tag_label(g),
        }

    def refresh_materials(self, keep=()):
        rows = [(i, g, self.row_values(i, g)) for i, g in enumerate(self.groups) if self.passes(g)]
        col = self.sort_col
        rows.sort(key=lambda r: (r[2][col] if isinstance(r[2][col], int) else str(r[2][col]).lower()),
                  reverse=self.sort_rev)
        self.mat_tree.delete(*self.mat_tree.get_children())
        cols = self.mat_tree["columns"]
        for i, g, v in rows:
            tag = v["tag"] if v["tag"] in (TAG_EMISSIVE, TAG_NOT) else None
            self.mat_tree.insert("", "end", iid=str(i), values=[v[c] for c in cols], tags=(tag,) if tag else ())
        still = [s for s in keep if self.mat_tree.exists(s)]
        if still:
            self.mat_tree.selection_set(still)
        if self.combo_filter:
            eff, fl = self.combo_filter
            self.combo_label.configure(text=f"Combo filter: {effect_name(eff)} 0x{fl:08X}  (click to clear)")
            self.combo_label.pack(side=tk.LEFT, padx=8)
        else:
            self.combo_label.pack_forget()

    def on_sort(self, col):
        self.sort_rev = (not self.sort_rev) if self.sort_col == col else False
        self.sort_col = col
        self.refresh_materials(keep=self.mat_tree.selection())

    def on_select(self, _event=None):
        if self.nb.select() in (str(self.inst_frame), str(self.fields_frame)):
            return                      # the other tabs own the detail pane while they are open
        sel = self.mat_tree.selection()
        self.detail.delete("1.0", tk.END)
        if not sel:
            return
        g = self.groups[int(sel[-1])]
        tag = self.group_tag_label(g)
        vs = vertex_color_stats(self.stream_path, g.rep.solid, self.vcache) if self.stream_path else {}
        self.fill_instances(g)
        parts = [f"Material group: {g.name}", f"Tag: {tag or '(none)'}",
                 f"Used by {len(g.solids)} solids, {len(g.members)} entries", "",
                 dump_entry(g.rep, self.names, self.tex), "",
                 f"Vertex colors (first entry): {vs.get(g.rep.index, '-')}", "",
                 distinct_text(g.members), "", "Used by (first 40):"]
        for m in g.members[:40]:
            parts.append(f"    {m.solid.name or '[no name]'}  entry {m.index}" + (f"  \"{m.name}\"" if m.name else ""))
        self.detail.insert(tk.END, "\n".join(parts))

    # ---- material dictionary (texture side) ----
    def on_load_dictionary(self):
        path = filedialog.askopenfilename(title="Open carbon_material_dictionary.json",
                                          filetypes=[("JSON", "*.json"), ("All files", "*.*")])
        if path:
            self.load_dictionary(path)

    def load_dictionary(self, path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            tex = {}
            for key, rec in (data.get("textures") or {}).items():
                try:
                    tex[int(key, 16)] = rec
                except ValueError:
                    continue
        except Exception as ex:
            messagebox.showerror("Dictionary failed", f"{type(ex).__name__}: {ex}")
            return
        self.tex = tex
        self.fields_dirty = True
        self.stats_var.set(f"{self.stats_var.get()}  |  dictionary: {len(tex)} textures")
        self.on_tab_changed()

    # ---- scenery TSVs (instance flags per solid) ----
    def on_load_scenery(self):
        path = filedialog.askopenfilename(title="Open scenery_infos.tsv (scenery_instances.tsv must be next to it)",
                                          filetypes=[("TSV", "*.tsv"), ("All files", "*.*")])
        if path:
            self.load_scenery_file(path)

    def load_scenery_file(self, path):
        try:
            self.scen = load_scenery(path)
        except Exception as ex:
            messagebox.showerror("Scenery TSVs failed", f"{type(ex).__name__}: {ex}")
            return
        self.apply_scenery()
        self.stats_var.set(f"{self.stats_var.get()}  |  scenery: {len(self.scen)} solid keys")

    def apply_scenery(self):
        if self.scen is None:
            return
        empty = {"count": 0, "sections": set(), "flags": {}, "raw": {}}
        for so in self.objects:
            so.scenery = self.scen.get(so.hash, empty)
        self.fields_dirty = True
        self.on_tab_changed()

    # ---- instances tab ----
    def fill_instances(self, g=None, keep=()):
        """Entries that use the same diffuse texture as group g (g=None: keep the current group)."""
        if g is not None:
            self.inst_group = g
        g = getattr(self, "inst_group", None)
        self.inst_tree.delete(*self.inst_tree.get_children())
        self.inst_entries = []
        if g is None:
            return
        entries = sorted(self.by_diffuse.get(g.diffuse_hash, []), key=lambda e: (e.solid.name, e.index))
        shown = entries[:5000]
        self.inst_entries = shown
        for i, e in enumerate(shown):
            tag = self.etag(e)
            self.inst_tree.insert("", "end", iid=str(i), tags=(tag,) if tag else (), values=[
                e.solid.name, hex_or_blank(e.solid.hash), e.index, e.name, e.effect_name, f"0x{e.flags:08X}",
                "" if e.solid.header_flags is None else f"0x{e.solid.header_flags:04X}",
                e.solid.num_light_mats, f"0x{e.unknown_44:08X}", f"0x{e.texture_sort_key:08X}", e.num_verts,
                tag or ""])
        base = self.names.get(g.diffuse_hash) or hex_or_blank(g.diffuse_hash) or "(no diffuse)"
        self.inst_var.set(f"{len(entries)} entries use diffuse {base}"
                          + (f" (first {len(shown)} shown)" if len(entries) > len(shown) else ""))
        still = [k for k in keep if self.inst_tree.exists(k)]
        if still:
            self.inst_tree.selection_set(still)

    def on_inst_select(self, _event=None):
        sel = self.inst_tree.selection()
        if not sel or self.nb.select() != str(self.inst_frame):
            return
        entries = [self.inst_entries[int(i)] for i in sel][:40]
        self.detail.delete("1.0", tk.END)
        if len(entries) == 1:
            e = entries[0]
            vs = vertex_color_stats(self.stream_path, e.solid, self.vcache) if self.stream_path else None
            text = full_dump_text(e, self.names, self.tex, vs)
        else:
            text = diff_text(entries, self.names, self.tex, self.vcache, self.stream_path)
            if len(sel) > 40:
                text = f"(first 40 of {len(sel)} selected rows compared)\n\n" + text
        self.detail.insert(tk.END, text)

    def on_tab_changed(self, _event=None):
        cur = self.nb.select()
        if cur == str(self.fields_frame):
            if self.fields_dirty:
                self.refresh_fields()
        elif cur == str(self.inst_frame):
            self.on_inst_select()
        else:
            self.on_select()

    def dump_selected(self):
        sel = self.inst_tree.selection() or self.inst_tree.get_children()
        entries = [self.inst_entries[int(i)] for i in sel][:300]
        if not entries:
            messagebox.showinfo("Nothing to dump", "Select a material, then rows on the Instances tab.")
            return
        h = entries[0].diffuse_texture_hash
        path = filedialog.asksaveasfilename(
            title="Dump selected instances", defaultextension=".txt", initialdir=str(out_dir(TOOL_NAME)),
            initialfile=f"dump_{h:08X}.txt" if h is not None else "dump.txt",
            filetypes=[("Text", "*.txt"), ("All files", "*.*")])
        if not path:
            return
        with open(path, "w", encoding="utf-8") as f:
            for e in entries:
                vs = vertex_color_stats(self.stream_path, e.solid, self.vcache) if self.stream_path else None
                f.write("=" * 100 + "\n" + full_dump_text(e, self.names, self.tex, vs) + "\n\n")
        self.stats_var.set(f"Dumped {len(entries)} entries to {os.path.basename(path)}")

    # ---- field values tab ----
    def field_extractors(self):
        fx = [("entry name vs diffuse name", self._name_match),
              ("effect", lambda e: f"{e.effect_name} ({e.effect_id})"),
              ("flags raw", lambda e: f"0x{e.flags:08X}"),
              ("light material number", lambda e: f"0x{e.light_material_number:02X}")]
        for slot in SLOTS[1:]:
            fx.append((f"{slot} slot state", lambda e, s=slot: e.slot_state[s]))
        fx += [("unk @0x1E..0x2F", lambda e: f"{e.unknown_1e:04X} {e.unknown_20.hex()}"),
               ("unk @0x32", lambda e: f"0x{e.unknown_32:04X}"),
               ("unk @0x34", lambda e: f"0x{e.unknown_34:08X}"),
               ("unk @0x44", lambda e: f"0x{e.unknown_44:08X}"),
               ("unk @0x48", lambda e: f"0x{e.unknown_48[0]:08X}"),
               ("unk @0x4C..0x5C", lambda e: " ".join(f"{x:08X}" for x in e.unknown_48[1:])),
               ("unk @0x68..0x78", lambda e: " ".join(f"{x:08X}" for x in e.unknown_68)),
               ("unk @0x80..0x8C", lambda e: " ".join(f"{x:08X}" for x in e.unknown_80)),
               ("solid header flags", lambda e: "-" if e.solid.header_flags is None else f"0x{e.solid.header_flags:04X}"),
               ("solid num bones", lambda e: str(e.solid.num_bones)),
               ("solid num light materials", lambda e: str(e.solid.num_light_mats)),
               ("solid num position markers", lambda e: str(e.solid.num_markers))]
        if self.tex is not None:
            for slot in ("diffuse", "normal"):
                for key in TEX_DECODED_KEYS:
                    fx.append((f"{slot} tex {key}", lambda e, s=slot, k=key: self._tex_value(e, s, k, False)))
                for key in TEX_RAW_KEYS:
                    fx.append((f"{slot} tex raw {key}", lambda e, s=slot, k=key: self._tex_value(e, s, k, True)))
        if self.scen is not None:
            names = sorted({k for so in self.objects for k in getattr(so, "scenery", {"flags": {}})["flags"]})
            fx.append(("scenery instances (count band)", lambda e: self._band(e.solid)))
            for nm in names:
                fx.append((f"instance flag {nm}", lambda e, n=nm: self._flag_state(e.solid, n)))
        ids = {}
        for so in self.objects:
            for cid in {c[0] for c in so.chunks}:
                ids[cid] = ids.get(cid, 0) + 1
        for cid, n in sorted(ids.items()):
            if n < len(self.objects):          # only chunks that some solids have and others do not
                fx.append((f"solid has chunk 0x{cid:X}",
                           lambda e, c=cid: "yes" if any(k[0] == c for k in e.solid.chunks) else "no"))
        return fx

    def _tex_value(self, e, slot, key, raw):
        """One dictionary value of the entry's diffuse or normal texture, as text."""
        if slot == "normal" and e.slot_state["normal"] != "texture":
            return "no normal texture"
        h = getattr(e, f"{slot}_texture_hash")
        rec = self.tex.get(h) if h is not None else None
        if not rec:
            return "not in dictionary"
        if raw:
            rec = rec.get("raw")
            if not rec:
                return "no raw data (re-run AssetDumper)"
        v = rec.get(key)
        return "-" if v is None else tex_raw_text(v)

    def _name_match(self, e):
        """Does the entry name (0x134C02) equal the name of the diffuse texture? Case is ignored."""
        base = self.names.get(e.diffuse_texture_hash)
        if not e.name:
            return "entry has no name"
        if not base:
            return "diffuse name unknown"
        return "same as diffuse" if e.name.lower() == base.lower() else "DIFFERENT from diffuse"

    @staticmethod
    def _band(solid):
        n = solid.scenery["count"]
        return "0" if n == 0 else "1" if n == 1 else "2-9" if n < 10 else "10+"

    @staticmethod
    def _flag_state(solid, name):
        rec = solid.scenery
        if not rec["count"]:
            return "no instances"
        c = rec["flags"].get(name, 0)
        return "all" if c == rec["count"] else "none" if c == 0 else "some"

    def refresh_fields(self):
        self.fields_dirty = False
        self.fields_tree.delete(*self.fields_tree.get_children())
        fx = self.field_extractors()
        counts = {name: {} for name, _ in fx}          # field -> value -> [entries, emissive, not]
        any_emi = any_not = False
        tagged_entries = []
        for g in self.groups:
            for e in g.members:
                tag = self.etag(e)
                any_emi = any_emi or tag == TAG_EMISSIVE
                any_not = any_not or tag == TAG_NOT
                if tag and len(tagged_entries) < 600:
                    tagged_entries.append((e, tag))
                for name, fn in fx:
                    c = counts[name].setdefault(fn(e), [0, 0, 0])
                    c[0] += 1
                    if tag == TAG_EMISSIVE:
                        c[1] += 1
                    elif tag == TAG_NOT:
                        c[2] += 1
        vc_name = "vertex color byte means /32 (tagged entries, max 600)"
        counts[vc_name] = {}
        if self.stream_path:
            for e, tag in tagged_entries:
                vertex_color_stats(self.stream_path, e.solid, self.vcache)
                m = getattr(e.solid, "vmeans", {}).get(e.index)
                if m:
                    c = counts[vc_name].setdefault("/".join(str(int(x) // 32) for x in m), [0, 0, 0])
                    c[0] += 1
                    c[1] += tag == TAG_EMISSIVE
                    c[2] += tag == TAG_NOT
        have_both = any_emi and any_not
        for name in [n for n, _ in fx] + [vc_name]:
            vals = counts[name]
            ev = {v for v, c in vals.items() if c[1]}
            nv = {v for v, c in vals.items() if c[2]}
            if have_both and ev and nv and ev.isdisjoint(nv):
                sep = "SEPARATES the tagged sets"
            elif have_both and (ev or nv):
                sep = "overlap: does not separate"
            else:
                sep = ""
            ranked = sorted(vals.items(), key=lambda kv: -kv[1][0])
            keep = [kv for kv in ranked[:40]] + [kv for kv in ranked[40:] if kv[1][1] or kv[1][2]]
            for v, c in keep:
                if have_both and c[1] and not c[2]:
                    reading = "only in emissive-tagged"
                elif have_both and c[2] and not c[1]:
                    reading = "only in not-emissive-tagged"
                else:
                    reading = ""
                self.fields_tree.insert("", "end", values=[name, v, c[0], c[1], c[2], reading, sep])
            if len(ranked) > len(keep):
                self.fields_tree.insert("", "end", values=[name, f"... {len(ranked) - len(keep)} more values", "", "", "", "", sep])

    def tag_counts(self, groups):
        e = n = 0
        for g in groups:
            ge, gn = self.group_counts(g)
            e, n = e + ge, n + gn
        return e, n

    def refresh_combos(self):
        combos = {}
        for g in self.groups:
            combos.setdefault((g.effect_id, g.flags), []).append(g)
        self.combo_tree.delete(*self.combo_tree.get_children())
        self.combo_keys = {}
        for n, (key, gs) in enumerate(sorted(combos.items(), key=lambda kv: -sum(len(g.members) for g in kv[1]))):
            e, no = self.tag_counts(gs)
            if e and no:
                reading = "MIXED: these flags do not decide"
            elif e:
                reading = "emissive"
            elif no:
                reading = "not emissive"
            else:
                reading = ""
            iid = str(n)
            self.combo_keys[iid] = key
            self.combo_tree.insert("", "end", iid=iid, values=[
                effect_name(key[0]), f"0x{key[1]:08X}", " | ".join(decode_flags(key[1])) or "(none)",
                len(gs), sum(len(g.members) for g in gs), e, no, reading])

    def on_combo_pick(self, _event):
        sel = self.combo_tree.selection()
        if not sel:
            return
        self.combo_filter = self.combo_keys[sel[0]]
        self.nb.select(0)
        self.refresh_materials()

    def clear_combo_filter(self):
        self.combo_filter = None
        self.refresh_materials()

    def refresh_bits(self):
        tagged = []                  # (group, emissive entries, not-emissive entries)
        for g in self.groups:
            ge, gn = self.group_counts(g)
            if ge or gn:
                tagged.append((g, ge, gn))
        self.bit_tree.delete(*self.bit_tree.get_children())
        if not tagged:
            return
        for bit in range(32):
            mask = 1 << bit
            if not any(g.flags & mask for g in self.groups):
                continue
            se = sum(1 for g, ge, gn in tagged if g.flags & mask and ge)
            sn = sum(1 for g, ge, gn in tagged if g.flags & mask and gn)
            ce = sum(1 for g, ge, gn in tagged if not g.flags & mask and ge)
            cn = sum(1 for g, ge, gn in tagged if not g.flags & mask and gn)
            if se and cn and not sn and not ce:
                reading = "separates: bit set = emissive (tagged data only)"
            elif sn and ce and not se and not cn:
                reading = "separates: bit set = not emissive (tagged data only)"
            else:
                reading = ""
            self.bit_tree.insert("", "end", values=[f"0x{mask:08X}", FLAG_NAME_BY_BIT.get(mask, "UNKNOWN"),
                                                    se, sn, ce, cn, reading])

    def _save_dialog(self, default):
        return filedialog.asksaveasfilename(title="Export TSV", defaultextension=".tsv",
                                            initialdir=str(out_dir(TOOL_NAME)), initialfile=default,
                                            filetypes=[("TSV", "*.tsv"), ("All files", "*.*")])

    def export_entries(self):
        if not self.objects:
            messagebox.showinfo("Nothing to export", "Open a stream file first.")
            return
        path = self._save_dialog("plat_entries.tsv")
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f, delimiter="\t")
            w.writerow(ENTRY_COLUMNS)
            for so in self.objects:
                for e in so.entries:
                    w.writerow(entry_row(e, self.names))
        self.stats_var.set(f"Exported entries to {os.path.basename(path)}")

    def export_materials(self):
        if not self.groups:
            messagebox.showinfo("Nothing to export", "Open a stream file first.")
            return
        path = self._save_dialog("plat_materials.tsv")
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f, delimiter="\t")
            w.writerow(["Material", "DiffuseHash", "EffectId", "Effect", "FlagsRaw", "FlagsDecoded",
                        "Entries", "Solids", "Tag"])
            for g in self.groups:
                w.writerow([g.name, hex_or_blank(g.diffuse_hash), g.effect_id, effect_name(g.effect_id),
                            f"0x{g.flags:08X}", "|".join(decode_flags(g.flags)), len(g.members), len(g.solids),
                            self.group_tag_label(g)])
        self.stats_var.set(f"Exported materials to {os.path.basename(path)}")


def main():
    args = _sys.argv[1:]
    dict_path = None
    if "--dict" in args:
        i = args.index("--dict")
        dict_path = args[i + 1] if i + 1 < len(args) else None
        del args[i:i + 2]
    scen_path = None
    if "--scenery" in args:
        i = args.index("--scenery")
        scen_path = args[i + 1] if i + 1 < len(args) else None
        del args[i:i + 2]
    app = App(args[0] if args else None, dict_path)
    if scen_path:
        app.load_scenery_file(scen_path)
    app.mainloop()


if __name__ == "__main__":
    main()