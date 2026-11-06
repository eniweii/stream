#!/usr/bin/env python3
"""uc_material_dump.py - read every Undercover material the way AssetDumper would export it (read only).

Reads STREAML8R_MW2.BUN (solids = ObjectPack 0x80134000 -> 0x80134010, texture packs 0xB3300000) and, for
every shading group (0x134902, 0x100 bytes), prints/exports:

  * what AssetDumper does: effect id from MaterialAttribKey (EffectIdMapping), role resolution by texture NAME
    suffix (_B/_D/_N/_S/_M/_O/_I/_R), layer combo (single / single_M / layer_M / terrain_B), export name
    {diffuse}_{effect}_{combo}, normal/specular pairing by hash delta (+0x0A / +0x0F), the d/n/s/m/b/o/i/r
    layer block of its material logger, vertex bytes and UV channels its GetVertex consumes;
  * what the file itself says: the texture USAGE hash of every slot (TextureParamMaterial @0x8C, and the
    solid's texture list 0x134012 record +0x04), named with UCGT's TextureUsage (vlt_hash(NAME_SAMPLER)),
    the specific shader name (vlt_hash of UCGT's shader names) and UCGT's vertex format for that shader.

The two are compared in the report: usage versus suffix, AssetDumper layer versus usage layer, vertex size
and UV channels (UCGT format versus what GetVertex reads versus the stride found in the file).

Shading group (0x100 bytes, AssetDumper struct Material; offsets checked on the probe hex):
    +0x00 first idx | +0x04 last idx | +0x08 i32[10] TextureParam | +0x30 u8[10] TextureNumber (index into the
    solid's 0x134012 list, FF = none) | +0x3A u8 NumTextures | +0x3B u8 LightMaterialNumber | +0x3C u32 sort key
    | +0x40 u32 mesh flags | +0x44 i32 idx used | +0x48 u32 reduced idx | +0x4C vertex stream (40 bytes:
    size of vertex data @0x6C, stream @0x70, load index @0x71, i16 bytes-per-vertex+flag @0x72)
    | +0x84 u32 MaterialAttribKey | +0x88 u32 attrib pointer | +0x8C u32[10] usage hashes
    | +0xB4 u32[10] texture hashes

Files go to outputs/uc_materials/ (or --out DIR):
    uc_material_report.txt   the comparison, sections A to J
    uc_materials.tsv         one row per distinct material variant (shader + usage + texture set)
    uc_material_dump.txt     AssetDumper-style layer block per distinct export name, plus usage lines

Usage:
    python solids_materials/uc_material_dump.py STREAML8R_MW2.BUN
    python solids_materials/uc_material_dump.py STREAML8R_MW2.BUN --out outputs/uc_materials
"""
# stream bootstrap: make the shared library folders importable
import pathlib as _pl, sys as _sys
_sys.path[:0] = [str(_pl.Path(__file__).resolve().parents[1] / _d) for _d in ['common', 'solids_materials']]

import argparse
import mmap
import pathlib
import re
import struct
from collections import Counter, defaultdict

from nfs_chunk_walk import iter_chunks, split_payload
from nfs_hashing import vlt_hash
import uc_material_tables as T

ID_OBJECTPACK = 0x80134000
ID_SOLID = 0x80134010
ID_PLAT = 0x80134100
ID_SOLID_HEADER = 0x00134011
ID_SOLID_TEXTURES = 0x00134012
ID_SOLID_TEXTYPES = 0x00134015
ID_PLAT_MESH_ENTRIES = 0x00134902
ID_PLAT_MESH_NAME = 0x00134C02
ID_PLAT_SHADER_KEYS = 0x00134F01
ID_TPK_INFO_HEADER = 0xB3310000
ID_TPK_HASHES = 0x33310002
ID_TPK_TEXTURES = 0x33310004

MAT_SIZE = 0x100
TPK_FIXED = 0x58

# ---------------------------------------------------------------------------------------------------------
# what AssetDumper's UndercoverSolidReader.GetVertex consumes per effect: (bytes, uv read, uv skipped, layout)
# transcribed from the C# (Pos = Vector3 + 1 float = 16, short2n = 4, packed normal/tangent = 8)
AD_VERTEX = {}


def _grp(effects, size, uv_read, uv_skipped, layout):
    for e in effects.split():
        AD_VERTEX[e] = (size, uv_read, uv_skipped, layout)


_grp("mw2_constant mw2_constant_alpha_bias mw2_illuminated mw2_pano", 20, 1, 0, "Pos16 Tex0")
_grp("mw2_matte mw2_diffuse_spec mw2_branches mw2_glass_no_n mw2_diffuse_spec_illum diffuse_spec_2sided "
     "mw2_combo_refl mw2_dirt mw2_grass mw2_tunnel_illum mw2_diffuse_spec_alpha mw2_trunk "
     "mw2_matte_alpha mw2_dif_spec_a_bias mw2_dirt_overlay", 32, 1, 0, "Pos16 Col4 Tex0 Norm8")
_grp("mw2_normalmap mw2_normalmap_bias mw2_glass_refl normalmap2sided mw2_road_overlay mw2_road_refl_overlay mw2_rock",
     40, 1, 0, "Pos16 Col4 Tex0 Norm8 Tan8(skipped)")
_grp("mw2_grass_rock mw2_road_refl mw2_road_refl_tile mw2_road_tile mw2_dirt_rock",
     44, 2, 0, "Pos16 Col4 Tex0 Tex1 Norm8 Tan8(skipped)")
_grp("mw2_tunnel_road", 48, 1, 2, "Pos16 Col4 Tex0 Tex1(skipped) Tex2(skipped) Norm8 Tan8(skipped)")
_grp("mw2_road_refl_lite mw2_road_lite mw2_grass_dirt", 36, 1, 1, "Pos16 Col4 Tex0 Tex1(skipped) Norm8")
_grp("mw2_tunnel_wall", 28, 1, 1, "Pos16 Col4 Tex0 Tex1(skipped)")
_grp("mw2_foliage mw2_foliage_lod mw2_scrub mw2_scrub_lod mw2_ocean mw2_indicator", 24, 1, 0, "Pos16 Col4 Tex0")
_grp("mw2_sky", 24, 1, 0, "Pos16 Tex0(float2)")
_grp("mw2_texture_scroll mw2_car_heaven_default", 40, 1, 0, "Pos16 Col16(skipped) Tex0(float2)")
_grp("mw2_car_heaven", 48, 1, 0, "Pos16 Col16(skipped) Tex0(float2) Norm8")
_grp("mw2_carhvn_floor mw2_road", 44, 1, 1, "Pos16 Col4 Tex0 Tex1(skipped) Norm8 Tan8(skipped)")
_grp("car car_a car_a_nzw car_nm car_nm_a car_nm_v_s car_nm_v_s_a car_si car_si_a car_t car_t_a car_t_nm car_v",
     32, 1, 0, "Pos8 Tex0 Col4 Norm8 Tan8")
_grp("mw2_smokegeo", 24, 1, 0, "Pos12 Tex0 Norm8")
_grp("mw2_cardebris", 20, 0, 0, "Pos12 Norm8")
_grp("watersplash", 56, 1, 0, "Pos16 Tex0(float2) Norm16 Tan16")
_grp("worldbone worldbonenocull worldbonetransparency", 44, 1, 0, "Pos16 Tex0 Blend8(skipped) Norm8 Tan8")

# ---------------------------------------------------------------------------------------------------------
# UCGT shaderusages vertex format (PC) -> (bytes, uv channels)
_COMP = re.compile(r"^(Pos|Tex|Col|Norm|Tan|BlendWeight|BlendIndices|xf|xb)(\d+)(.*)$")


def ucgt_format(fmt):
    size, uv, comps = 0, 0, []
    for token in fmt.split("_"):
        m = _COMP.match(token)
        if not m:
            return None, None, [token + "?"]
        kind, num, sub = m.groups()
        if kind in ("xf", "xb"):
            n = int(num)
            b = n * 4 if kind == "xf" else n
        elif kind == "Pos":
            b = 12 if sub == "f3" else 8 if sub.startswith(("s", "h")) else 16
        elif kind == "Tex":
            b = 4 if sub.startswith(("s", "h")) else 8
            uv += 1
        elif kind == "Col":
            b = 16 if sub == "f4" else 4
        elif kind in ("Norm", "Tan"):
            b = 12 if sub == "f3" else 8 if sub == "s" else 4 if sub in ("d4n", "cmp3n") else 16
        else:  # BlendWeight / BlendIndices
            b = 4
        size += b
        comps.append("%s%d" % (token, b))
    return size, uv, comps


SHADER_BY_KEY = {}
for _name, (_fmt, _note) in T.SHADERS.items():
    _key = int(_name, 16) if _name.startswith("0x") else vlt_hash(_name)
    SHADER_BY_KEY[_key] = (_name, _fmt, _note)

USAGE_NAMES = dict(T.USAGE_NAMES)
for _base in ("DIFFUSE3", "NORMAL2", "NORMAL", "SPECULAR", "SPECULAR2", "BLEND", "OPACITY", "DIFFUSE", "DIFFUSE2"):
    USAGE_NAMES.setdefault(vlt_hash(_base + "_SAMPLER"), _base)

# ---------------------------------------------------------------------------------------------------------
# AssetDumper UndercoverLayerClassifier, ported as is
EXCEPT_PREFIXES = ("TRN_TRANSPARENT_",)


def ad_excepted(name):
    return name is not None and name.upper().startswith(EXCEPT_PREFIXES)


def ends(name, suffix):
    return name.upper().endswith(suffix)


def ad_resolve_diffuse(slot_hashes, diffuse_hash, name_of):
    for h in slot_hashes:
        n = name_of(h)
        if not ad_excepted(n) and ends(n, "_B"):
            return h
    for h in slot_hashes:
        n = name_of(h)
        if not ad_excepted(n) and ends(n, "_D"):
            return h
    return diffuse_hash


def ad_classify(slot_hashes, name_of):
    named = [n for n in (name_of(h) for h in slot_hashes) if not ad_excepted(n)]
    has_b = any(ends(n, "_B") for n in named)
    d = sum(1 for n in named if ends(n, "_D"))
    has_m = any(ends(n, "_M") for n in named)
    if has_b and d >= 2:
        return "terrain_B"
    if d >= 2 and has_m:
        return "layer_M"
    if d == 1 and has_m:
        return "single_M"
    if d == 1:
        return "single"
    return "unknown"


def ad_export_name(slot_hashes, diffuse_hash, name_of, effect_name, fallback):
    base = name_of(ad_resolve_diffuse(slot_hashes, diffuse_hash, name_of))
    if not base or base == "-":
        return fallback, ad_classify(slot_hashes, name_of)
    combo = ad_classify(slot_hashes, name_of)
    return "%s_%s_%s" % (base, effect_name, combo), combo


def ad_layer_block(export_name, combo, slot_hashes, diffuse_hash, name_of):
    """The d/n/s/m/b/o/i/r block of AssetDumper's material logger (commented out in the exporter)."""
    log = [export_name]
    named = [(h, name_of(h)) for h in slot_hashes]
    named = [(h, n) for h, n in named if n != "-" and not ad_excepted(n)]
    diffuses = [t for t in named if ends(t[1], "_D")]
    normals = [t for t in named if ends(t[1], "_N")]
    speculars = [t for t in named if ends(t[1], "_S")]

    def first(suffix):
        return next((n for h, n in named if ends(n, suffix)), None)

    data_map, blend, opacity, illum, refl = first("_M"), first("_B"), first("_O"), first("_I"), first("_R")

    def paired(h, cands, delta):
        return next((n for ch, n in cands if ch == (h + delta) & 0xFFFFFFFF), None)

    def layer(header, diffuse, m=None, b=None, o=None, i=None, r=None):
        log.append(header)
        if diffuse is not None:
            log.append("  d %s" % diffuse[1])
            n = paired(diffuse[0], normals, 0x0A)
            if n:
                log.append("  n %s" % n)
            s = paired(diffuse[0], speculars, 0x0F)
            if s:
                log.append("  s %s" % s)
        for tag, val in (("m", m), ("b", b), ("o", o), ("i", i), ("r", r)):
            if val:
                log.append("  %s %s" % (tag, val))

    crash = None
    at = lambda lst, i: lst[i] if i < len(lst) else None
    if combo == "terrain_B":
        layer("LayerA", at(diffuses, 0))
        layer("LayerB", at(diffuses, 1), b=blend)
    elif combo == "layer_M":
        resolved = ad_resolve_diffuse(slot_hashes, diffuse_hash, name_of)
        overlay = next((d for d in diffuses if d[0] == resolved), None) or diffuses[0]
        base = next((d for d in diffuses if d[0] != overlay[0]), None)
        if base is None:
            crash = "layer_M with two _D slots of the same hash: C# diffuses.First(...) throws"
            base = overlay
        layer("Overlay", overlay, m=data_map)
        layer("BaseLayer", base, o=opacity, i=illum, r=refl)
    elif combo == "single_M":
        layer("BaseLayer", at(diffuses, 0), m=data_map, o=opacity, i=illum, r=refl)
    elif combo == "unknown":
        for idx, h in enumerate(slot_hashes):
            if h and name_of(h) != "-":
                layer("Slot_%02d" % idx, (h, name_of(h)))
    else:
        layer("BaseLayer", at(diffuses, 0), o=opacity, i=illum, r=refl)
    return log, crash


def suffix_of(name):
    if name == "-":
        return "(no name)"
    m = re.search(r"_([A-Za-z]{1,2})$", name)
    return "_" + m.group(1).upper() if m else "(other)"


# ---------------------------------------------------------------------------------------------------------
def parse_material(buf, off):
    g = lambda fmt, o: struct.unpack_from(fmt, buf, off + o)
    return {
        "first_index": g("<i", 0x00)[0], "last_index": g("<i", 0x04)[0],
        "tparam": g("<10i", 0x08), "tnum": g("<10B", 0x30),
        "num_tex": g("<B", 0x3A)[0], "light": g("<B", 0x3B)[0], "sort_key": g("<I", 0x3C)[0],
        "mesh_flags": g("<I", 0x40)[0], "idx_used": g("<i", 0x44)[0], "reduced": g("<I", 0x48)[0],
        "vs_size": g("<I", 0x6C)[0], "vs_stream": g("<B", 0x70)[0], "vs_load": g("<B", 0x71)[0],
        "vs_bytes": g("<h", 0x72)[0] & 0x7FFF,
        "attr_key": g("<I", 0x84)[0], "attr_ptr": g("<I", 0x88)[0],
        "usage": g("<10I", 0x8C), "texhash": g("<10I", 0xB4),
    }


def cstring(buf, start, end):
    stop = buf.find(b"\0", start, end)
    return bytes(buf[start:end if stop < 0 else stop]).decode("latin-1")


def read_file(path):
    """One pass: solids with their texture list, usage table and shading groups; texture names by hash."""
    solids = {}      # container offset -> dict
    plat_owner = {}  # plat container offset -> solid
    tpk_count = {}   # tpk header container offset -> number of textures
    tex_names = {}   # texture hash -> name
    tex_class = {}
    problems = []
    with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        for off, cid, length, is_cont, pstart, parent, depth, parent_off in iter_chunks(mm, problems=problems):
            if cid == ID_SOLID:
                solids[off] = {"off": off, "name": "", "hash": 0, "textures": [], "type_names": [],
                               "mats": [], "mat_names": [], "plat_keys": []}
                continue
            if cid == ID_PLAT:
                if parent_off in solids:
                    plat_owner[off] = solids[parent_off]
                continue
            if is_cont:
                continue

            if parent == ID_TPK_INFO_HEADER and cid in (ID_TPK_HASHES, ID_TPK_TEXTURES):
                dstart, pad, _k = split_payload(mm, pstart, length)
                dlen = length - pad
                if cid == ID_TPK_HASHES:
                    tpk_count[parent_off] = dlen // 8
                else:
                    p, end = dstart, dstart + dlen
                    for _ in range(tpk_count.get(parent_off, 0)):
                        if p + TPK_FIXED + 1 > end:
                            problems.append((off, "texture struct runs past the chunk", 0))
                            break
                        h, cls = struct.unpack_from("<II", mm, p + 0x0C)
                        n = mm[p + TPK_FIXED]
                        tex_names[h] = cstring(mm, p + TPK_FIXED + 1, p + TPK_FIXED + 1 + n)
                        tex_class[h] = cls
                        p += TPK_FIXED + 1 + n
                continue

            solid = None
            if parent == ID_SOLID:
                solid = solids.get(parent_off)
            elif parent == ID_PLAT:
                solid = plat_owner.get(parent_off)
            if solid is None or cid not in (ID_SOLID_HEADER, ID_SOLID_TEXTURES, ID_SOLID_TEXTYPES,
                                            ID_PLAT_MESH_ENTRIES, ID_PLAT_MESH_NAME, ID_PLAT_SHADER_KEYS):
                continue
            dstart, pad, _k = split_payload(mm, pstart, length)
            dlen = length - pad

            if cid == ID_SOLID_HEADER and dlen >= 201:
                solid["hash"] = struct.unpack_from("<I", mm, dstart + 0x10)[0]
                solid["name"] = cstring(mm, dstart + 200, dstart + dlen)
            elif cid == ID_SOLID_TEXTURES:
                for i in range(dlen // 12):
                    solid["textures"].append(struct.unpack_from("<III", mm, dstart + i * 12))
            elif cid == ID_SOLID_TEXTYPES and dlen >= 8:
                n = struct.unpack_from("<I", mm, dstart)[0]
                base = 4 + 4 * n
                if 0 < n < 64 and base <= dlen:
                    for i in range(n):
                        o = struct.unpack_from("<I", mm, dstart + 4 + 4 * i)[0]
                        if base + o < dlen:
                            solid["type_names"].append(cstring(mm, dstart + base + o, dstart + dlen))
            elif cid == ID_PLAT_MESH_ENTRIES:
                for i in range(dlen // MAT_SIZE):
                    solid["mats"].append(parse_material(mm, dstart + i * MAT_SIZE))
            elif cid == ID_PLAT_MESH_NAME and dlen > 0:
                solid["mat_names"].append(cstring(mm, dstart, dstart + dlen))
            elif cid == ID_PLAT_SHADER_KEYS and dlen >= 4:
                n = struct.unpack_from("<I", mm, dstart)[0]
                if 4 + 4 * n <= dlen:
                    solid["plat_keys"] = list(struct.unpack_from("<%dI" % n, mm, dstart + 4))
    return list(solids.values()), tex_names, tex_class, problems


# ---------------------------------------------------------------------------------------------------------
def top(counter, n=8):
    return ", ".join("%s x %d" % (k, v) for k, v in counter.most_common(n))


def usage_name(h):
    if h == 0:
        return "-"
    return USAGE_NAMES.get(h, "0x%08X" % h)


def analyse(solids, tex_names):
    name_of = lambda h: tex_names.get(h, "-") if h else "-"
    variants = {}
    S = {  # statistics
        "solids": len(solids), "mats": 0, "unmapped_keys": Counter(), "slot_mismatch": 0, "slot_checked": 0,
        "tparam": Counter(), "mesh_flags": Counter(), "light": Counter(), "sortkey_is_diffuse": 0,
        "sortkey_checked": 0, "no_usage_hash": 0, "crash": Counter(), "crash_example": {},
        "shader": defaultdict(lambda: {"n": 0, "strides": Counter(), "sigs": Counter(), "ex": None, "ucgt": None}),
        "xtab": Counter(), "usage_slots": Counter(), "combo": Counter(), "combo_sigs": defaultdict(Counter),
        "twolayer_xtab": Counter(), "ad_vs_usage_diffuse": Counter(), "type_vs_usage": Counter(),
        "typetable_missing": 0, "unnamed_slots": 0, "slots": 0, "unknown_usage": Counter(),
        "name_variants": defaultdict(set), "name_count": Counter(), "keys_vs_plat": [0, 0],
    }
    dump_blocks = {}
    rows = []
    for solid in solids:
        used_usages = set()
        for mi, m in enumerate(solid["mats"]):
            S["mats"] += 1
            key = m["attr_key"]
            effect = T.EFFECT_KEYS.get(key)
            if effect is None:
                S["unmapped_keys"][key] += 1
                effect = "none"
                S["crash"]["EffectIdMapping[key] throws KeyNotFound"] += 1
                S["crash_example"].setdefault("EffectIdMapping[key] throws KeyNotFound", "%s (0x%08X)" % (solid["name"], key))
            shader = SHADER_BY_KEY.get(key)
            shader_name = shader[0] if shader else "(unknown 0x%08X)" % key

            slots = []
            for i in range(10):
                if m["texhash"][i] or m["usage"][i]:
                    slots.append((i, m["usage"][i], m["texhash"][i]))
            slot_hashes = list(m["texhash"])
            names_by_slot = [(i, usage_name(u), name_of(h)) for i, u, h in slots]
            sig = "+".join(u for _i, u, _n in names_by_slot) or "(no textures)"

            # consistency of the three places a texture is named
            for k in range(min(m["num_tex"], 10)):
                idx = m["tnum"][k]
                S["slot_checked"] += 1
                if idx == 0xFF or idx >= len(solid["textures"]):
                    S["slot_mismatch"] += 1
                    continue
                th, tu, _p = solid["textures"][idx]
                if th != m["texhash"][k] or tu != m["usage"][k]:
                    S["slot_mismatch"] += 1
            for i in range(10):
                S["tparam"][m["tparam"][i]] += 1 if m["texhash"][i] else 0
            S["mesh_flags"][m["mesh_flags"]] += 1
            S["light"][m["light"]] += 1
            diffuse_hash = 0
            if m["tnum"][0] != 0xFF and m["tnum"][0] < len(solid["textures"]):
                diffuse_hash = solid["textures"][m["tnum"][0]][0]
            S["sortkey_checked"] += 1
            S["sortkey_is_diffuse"] += 1 if m["sort_key"] == diffuse_hash else 0

            fallback = solid["mat_names"][mi] if mi < len(solid["mat_names"]) else "TextureMTL-0x%08X" % diffuse_hash
            export, combo = ad_export_name(slot_hashes, diffuse_hash, name_of, effect, fallback)
            block, crash = ad_layer_block(export, combo, slot_hashes, diffuse_hash, name_of)
            if crash:
                S["crash"][crash] += 1
                S["crash_example"].setdefault(crash, "%s / %s" % (solid["name"], export))

            # usage statistics
            for i, u, h in slots:
                un = usage_name(u)
                nm = name_of(h)
                S["slots"] += 1
                S["usage_slots"][un] += 1
                S["xtab"][(un, suffix_of(nm))] += 1
                if nm == "-":
                    S["unnamed_slots"] += 1
                if u and u not in USAGE_NAMES:
                    S["unknown_usage"][u] += 1
                if not u:
                    S["no_usage_hash"] += 1
                used_usages.add(un)
            S["combo"][combo] += 1
            S["combo_sigs"][combo][sig] += 1
            # AssetDumper's overlay/base against the usage of the same texture
            if combo == "layer_M":
                resolved = ad_resolve_diffuse(slot_hashes, diffuse_hash, name_of)
                for i, u, h in slots:
                    if name_of(h).upper().endswith("_D") and not ad_excepted(name_of(h)):
                        S["twolayer_xtab"][("Overlay" if h == resolved else "BaseLayer", usage_name(u))] += 1
            # AssetDumper's resolved diffuse against the usage of that slot
            resolved = ad_resolve_diffuse(slot_hashes, diffuse_hash, name_of)
            ru = next((usage_name(u) for i, u, h in slots if h == resolved), "(not a slot)")
            S["ad_vs_usage_diffuse"][(combo, ru)] += 1

            # vertex: stride in the file against UCGT and AssetDumper
            stride = m["vs_bytes"]
            ucgt = ucgt_format(shader[1]) if shader else (None, None, [])
            ad = AD_VERTEX.get(effect)
            sh = S["shader"][key]
            sh["n"] += 1
            sh["strides"][stride] += 1
            sh["sigs"][sig] += 1
            sh["effect"] = effect
            sh["name"] = shader_name
            sh["ucgt"] = ucgt
            sh["ad"] = ad
            sh["ex"] = sh["ex"] or solid["name"]
            if solid["plat_keys"]:
                S["keys_vs_plat"][0 if mi < len(solid["plat_keys"]) and solid["plat_keys"][mi] == key else 1] += 1

            # distinct variants and export name collisions
            vkey = (key, tuple((u, h) for _i, u, h in slots), stride)
            v = variants.get(vkey)
            if v is None:
                v = variants[vkey] = {"n": 0, "solid": solid["name"], "mi": mi, "export": export, "combo": combo,
                                      "effect": effect, "shader": shader_name, "stride": stride, "sig": sig,
                                      "slots": names_by_slot, "ucgt": ucgt, "ad": ad,
                                      "light": m["light"], "mesh_flags": m["mesh_flags"]}
            v["n"] += 1
            S["name_variants"][export].add(vkey)
            S["name_count"][export] += 1
            if export not in dump_blocks:
                dump_blocks[export] = (block, "%s / material %d" % (solid["name"], mi), sig,
                                       [(u, n) for _i, u, n in names_by_slot], shader_name, stride, ucgt, ad)
        for t in solid["type_names"]:
            S["type_vs_usage"][t in used_usages] += 1
    return variants, S, dump_blocks


def vertex_verdict(stride_counter, ucgt, ad):
    flags = []
    strides = sorted(stride_counter)
    if ucgt and ucgt[0] is not None:
        flags.append("UCGT %d %s" % (ucgt[0], "ok" if strides == [ucgt[0]] else "DIFFERS"))
    if ad:
        flags.append("AD %d %s" % (ad[0], "ok" if strides == [ad[0]] else "DIFFERS"))
    if ucgt and ad and ucgt[0] is not None and ucgt[1] is not None:
        if ucgt[1] != ad[1] + ad[2]:
            flags.append("UVs UCGT %d vs AD %d+%d" % (ucgt[1], ad[1], ad[2]))
        elif ad[2]:
            flags.append("AD skips %d UV" % ad[2])
    return "; ".join(flags)


def make_report(path, solids, tex_names, problems, variants, S):
    L = []
    L.append("=== %s ===" % path)
    L.append("A. parse: %d solids, %d shading groups, %d texture names from the texture packs, %d distinct variants" % (
        S["solids"], S["mats"], len(tex_names), len(variants)))
    for p in problems[:5]:
        L.append("   problem at 0x%X: %s" % (p[0], p[1]))
    L.append("   shader key not in AssetDumper's EffectIdMapping (AssetDumper throws): %s" % (
        ", ".join("0x%08X x %d" % kv for kv in S["unmapped_keys"].most_common(10)) or "none"))
    L.append("   materials whose attrib key equals the 0x134F01 list entry: %d, differs: %d" % tuple(S["keys_vs_plat"]))
    L.append("   texture slots that cannot be tied to a texture name in the packs: %d of %d" % (S["unnamed_slots"], S["slots"]))
    L.append("   slots with a texture hash but no usage hash: %d" % S["no_usage_hash"])
    L.append("   TextureNumber/solid list versus the slot arrays (hash or usage differs): %d of %d" % (
        S["slot_mismatch"], S["slot_checked"]))
    L.append("   unknown usage hashes: %s" % (", ".join("0x%08X x %d" % kv for kv in S["unknown_usage"].most_common(10)) or "none"))
    L.append("   TextureParam (+0x08) values on used slots: %s" % top(S["tparam"], 10))
    L.append("   TextureSortKey equals the diffuse texture hash: %d of %d" % (S["sortkey_is_diffuse"], S["sortkey_checked"]))
    L.append("   LightMaterialNumber: %s" % top(S["light"], 8))
    L.append("   mesh flags: %s" % ", ".join("0x%08X x %d" % kv for kv in S["mesh_flags"].most_common(10)))
    L.append("   AssetDumper would crash on: %s" % (
        "; ".join("%s x %d (e.g. %s)" % (k, v, S["crash_example"].get(k)) for k, v in S["crash"].items()) or "nothing found"))
    L.append("")

    L.append("B. shaders: AssetDumper effect versus UCGT shader name, and vertex size (file stride versus UCGT versus AssetDumper)")
    for key, sh in sorted(S["shader"].items(), key=lambda kv: -kv[1]["n"]):
        L.append("  0x%08X %-26s -> AD %-24s %6d groups  strides %s  %s" % (
            key, sh["name"], sh["effect"], sh["n"], dict(sh["strides"]), vertex_verdict(sh["strides"], sh["ucgt"], sh["ad"])))
        L.append("      usage signatures: %s" % top(sh["sigs"], 4))
    L.append("")

    L.append("C. usage census (slots): " + top(S["usage_slots"], 40))
    L.append("")
    L.append("D. usage versus the texture name suffix AssetDumper relies on (slots)")
    by_usage = defaultdict(Counter)
    for (u, s), c in S["xtab"].items():
        by_usage[u][s] = c
    for u, cnt in sorted(by_usage.items(), key=lambda kv: -sum(kv[1].values())):
        L.append("  %-18s %s" % (u, top(cnt, 8)))
    L.append("")

    L.append("E. AssetDumper combo versus usage signature")
    for combo, c in S["combo"].most_common():
        L.append("  %-10s %6d  %s" % (combo, c, top(S["combo_sigs"][combo], 5)))
    L.append("   layer_M: AssetDumper layer of each _D texture versus its usage: " + top(S["twolayer_xtab"], 10))
    L.append("   usage of the slot AssetDumper resolves as THE diffuse, per combo: " + top(S["ad_vs_usage_diffuse"], 14))
    L.append("   solid texture type table (0x134015) entry used by some slot of the solid: %s" % dict(S["type_vs_usage"]))
    L.append("")

    L.append("F. export name collisions (same AssetDumper name, different shader/usage/texture set)")
    coll = [(n, len(v), S["name_count"][n]) for n, v in S["name_variants"].items() if len(v) > 1]
    L.append("  %d of %d export names cover more than one variant; first 25 by size:" % (len(coll), len(S["name_variants"])))
    for n, nv, cnt in sorted(coll, key=lambda t: -t[2])[:25]:
        sigs = Counter(variants[k]["sig"] for k in S["name_variants"][n])
        L.append("  %-60s %3d variants, %5d groups  %s" % (n[:60], nv, cnt, top(sigs, 3)))
    return L


def write_outputs(out, path, solids, tex_names, problems, variants, S, dump_blocks):
    out.mkdir(parents=True, exist_ok=True)
    report = make_report(path, solids, tex_names, problems, variants, S)
    (out / "uc_material_report.txt").write_text("\n".join(report) + "\n", encoding="utf-8")

    cols = ["ExportName", "Combo", "Effect", "Shader", "Stride", "UcgtBytes", "UcgtUV", "AdBytes", "AdUV",
            "UsageSignature", "SlotsByUsage", "Count", "ExampleSolid", "ExampleMaterial", "LightMaterial", "MeshFlags"]
    with open(out / "uc_materials.tsv", "w", encoding="utf-8") as f:
        f.write("\t".join(cols) + "\n")
        for v in sorted(variants.values(), key=lambda v: (-v["n"], v["export"])):
            uc = v["ucgt"] or (None, None, [])
            ad = v["ad"]
            slots = ";".join("%s=%s" % (u, n) for _i, u, n in v["slots"])
            f.write("\t".join(str(x) for x in [
                v["export"], v["combo"], v["effect"], v["shader"], v["stride"],
                "" if uc[0] is None else uc[0], "" if uc[1] is None else uc[1],
                "" if not ad else ad[0], "" if not ad else "%d+%d" % (ad[1], ad[2]),
                v["sig"], slots, v["n"], v["solid"], v["mi"], v["light"], "0x%08X" % v["mesh_flags"]]) + "\n")

    with open(out / "uc_material_dump.txt", "w", encoding="utf-8") as f:
        for export in sorted(dump_blocks):
            block, example, sig, slots, shader, stride, ucgt, ad = dump_blocks[export]
            f.write("\n".join(block) + "\n")
            f.write("  [usage] %s | shader %s | stride %d | e.g. %s\n" % (sig, shader, stride, example))
            for u, n in slots:
                f.write("  [slot] %-16s %s\n" % (u, n))
            if ucgt and ucgt[0] is not None:
                f.write("  [vertex] UCGT %d bytes, %d UV | AD %s\n" % (
                    ucgt[0], ucgt[1], "-" if not ad else "%d bytes, %d read + %d skipped (%s)" % (ad[0], ad[1], ad[2], ad[3])))
            f.write("\n")
    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file")
    ap.add_argument("--out", default="outputs/uc_materials")
    args = ap.parse_args()

    solids, tex_names, _tex_class, problems = read_file(args.file)
    variants, S, dump_blocks = analyse(solids, tex_names)
    out = pathlib.Path(args.out)
    report = write_outputs(out, args.file, solids, tex_names, problems, variants, S, dump_blocks)
    print("\n".join(report[:40]))
    print("\nWrote %s" % (out / "uc_material_report.txt"))
    print("Wrote %s (%d variants)" % (out / "uc_materials.tsv", len(variants)))
    print("Wrote %s (%d export names)" % (out / "uc_material_dump.txt", len(dump_blocks)))


if __name__ == "__main__":
    main()
