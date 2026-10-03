#!/usr/bin/env python3
"""
extract_emitters.py - Step 1 of the Carbon -> BeamNG emitter port.

Reads (all from ONE folder you pick):
  emitterdata.yml    Attribulator unpack output (emitter layers)
  emittergroup.yml   Attribulator unpack output (groups of layers)
  fx_triggers.tsv    output of the AssetDumper scan-fx-triggers command
                     (if it is not in the folder, a file dialog asks for it)

Writes, into the same folder:
  emitters_resolved.json   groups + layers + placements (used groups only)
  textures_needed.txt      every texture name the placed layers use
  emitters_report.txt      counts, warnings, open-question answers

Needs:  pip install pyyaml      (tkinter ships with normal Python installs)
Usage:  python extract_emitters.py            -> folder picker window
        python extract_emitters.py <folder> [tsv_path] [textures_dir]   -> no window
        textures_dir (optional) = the emitter texture folder, e.g.
        levels/nfsc/Assets/Emitters. Its .dds names are hashed to name the
        hash-only textures in emitterdata.yml.
"""
import csv
import glob
import json
import os
import sys
from collections import Counter

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is missing. Run:  pip install pyyaml")

YAML_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


# ------------------------------------------------------- texture name hashes
def bstring_hash(name):
    """Black Box bStringHash: start 0xFFFFFFFF, h = h * 33 + char (name as is).
    The yml 'Texture.mEnum' is this hash of the upper-case texture name when
    Attribulator does not know the name. Texture packs use the same hash."""
    h = 0xFFFFFFFF
    for ch in name.encode("ascii", "ignore"):
        h = (h * 33 + ch) & 0xFFFFFFFF
    return h


# Names recovered by matching hashes from emitterdata.yml (index = mIndex).
KNOWN_TEXTURES = [
    "FX_FIRE01_ADDITIVE", "FX_FIRE01_BLEND", "FX_FIRE02_ADDITIVE",
    "FX_FIRE02_BLEND", "FX_FIRE03_ADDITIVE", "FX_HIDE_CAR_ICON",
    "FX_LEAF01_BLEND_ANIM", "FX_MILESTONE_SPEEDTRAP", "FX_MILESTONE_TOLLBOOTH",
    "FX_RINGWAVEA", "FX_WORLD_CARLOT", "FX_MODE_ICON_LAP_KNOCKOUT",
    "FX_MILESTONE_SPEEDTRAP_CAM", "FX_WATERSTRIPE01_BLEND",
    "FX_DEBRISSTONE1_BLEND",
]


def load_texture_names(folder, tex_dir=None):
    """{hash: name}. Sources: built-in list, <folder>/texture_names.txt
    (optional, one name per line, e.g. copied from NFS-TexEd) and every .dds
    file name found under tex_dir (the emitter texture folder, optional)."""
    names = list(KNOWN_TEXTURES)
    if tex_dir and os.path.isdir(tex_dir):
        for root, _dirs, files in os.walk(tex_dir):
            for fn in files:
                if fn.lower().endswith(".dds"):
                    names.append(os.path.splitext(fn)[0].upper())
    path = os.path.join(folder, "texture_names.txt")
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8-sig") as fh:
            for line in fh:
                n = line.strip().split("\t")[0].strip()
                if n.upper().endswith(".DDS"):
                    n = n[:-4]
                if n:
                    names.append(n.upper())
    return {bstring_hash(n): n for n in names}


# ----------------------------------------------------------------- helpers
def key_of(v):
    """Collection names can be plain text or a bare hex hash.
    YAML reads a bare 0x... as an integer, so turn it back into text."""
    if v is None or v == "":
        return ""
    if isinstance(v, int):
        return "0x%08X" % v
    return str(v)


def norm(v):
    """Vector dicts {X,Y,Z,W} become lists. Everything else passes through."""
    if isinstance(v, dict):
        if "X" in v and set(v) <= {"X", "Y", "Z", "W"}:
            return [v.get(a, 0) for a in "XYZW" if a in v]
        return {k: norm(x) for k, x in v.items()}
    if isinstance(v, list):
        return [norm(x) for x in v]
    return v


def load_collections(path):
    """Return {lowercase name: {name, parent, data}} for one Attribulator file."""
    with open(path, "r", encoding="utf-8-sig") as fh:
        doc = yaml.load(fh, Loader=YAML_LOADER)
    if isinstance(doc, dict):  # defensive: wrapped list
        doc = next((x for x in doc.values() if isinstance(x, list)), [])
    table = {}
    for item in doc or []:
        name = key_of(item.get("Name"))
        if name:
            table[name.lower()] = {
                "name": name,
                "parent": key_of(item.get("ParentName")),
                "data": item.get("Data") or {},
            }
    return table


def resolve(table, key, stats, seen=None):
    """Return the entry's fields with any parent fields it lacks filled in.
    Attribulator normally writes full data, so 'inherited' should stay 0."""
    entry = table.get(key.lower())
    if entry is None:
        return None
    seen = (seen or set()) | {key.lower()}
    data = dict(entry["data"])
    parent = entry["parent"]
    if parent and parent.lower() not in seen and parent.lower() in table:
        pdata = resolve(table, parent, stats, seen) or {}
        missing = [k for k in pdata if k not in data and k != "CollectionName"]
        if missing:
            stats["inherited"] += 1
            for k in missing:
                data[k] = pdata[k]
    return data


def decode_layer(name, data, tex_names=None, unresolved_tex=None):
    f = norm(data)
    colors = []
    for i in range(1, 5):
        v = f.get("Color%d" % i)
        if isinstance(v, int):  # RGBA, alpha in the LOW byte (VaultLib Colour.cs)
            colors.append([(v >> 24) & 255, (v >> 16) & 255, (v >> 8) & 255, v & 255])
    tex = f.get("Texture") or {}
    tex_name = key_of(tex.get("mEnum")) if isinstance(tex, dict) else ""
    if isinstance(tex, dict) and isinstance(tex.get("mEnum"), int) and tex.get("mEnum"):
        resolved = (tex_names or {}).get(tex["mEnum"])
        if resolved:
            tex_name = resolved
        elif unresolved_tex is not None:
            unresolved_tex.setdefault(tex_name, tex.get("mIndex"))
    return {
        "name": name,
        "texture": tex_name,
        "texture_index": tex.get("mIndex") if isinstance(tex, dict) else None,
        "colors_rgba": colors,
        "fields": f,  # every field, untouched, for step 3
    }


ROT_COLS = ["rot_r%d%s" % (r, a) for r in range(3) for a in "xyz"]


def read_rot(r):
    """9 floats (rows = local axes in world space) or None for an old TSV."""
    try:
        return [float(r[c]) for c in ROT_COLS]
    except (KeyError, ValueError, TypeError):
        return None


def rot_kind(rot):
    """'identity', 'yaw' (local Z stays up), 'tilted' or 'none' (no data)."""
    if rot is None:
        return "none"
    ident = [1, 0, 0, 0, 1, 0, 0, 0, 1]
    if all(abs(a - b) < 1e-3 for a, b in zip(rot, ident)):
        return "identity"
    if abs(rot[6]) < 1e-3 and abs(rot[7]) < 1e-3 and rot[8] > 1 - 1e-3:
        return "yaw"
    return "tilted"


def read_tsv(path):
    rows = []
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh, delimiter="\t"):
            try:
                rows.append({
                    "chunk_offset": r["chunk_offset"],
                    "sectionID": str(int(r["record_section_ref"])),  # section_number is 5 on every row
                    "chunk_section_ref": int(r["chunk_section_ref"]),
                    "record_section_ref": int(r["record_section_ref"]),
                    "record_index": int(r["record_index"]),
                    "group_key": "0x%08X" % int(r["group_key"], 0),
                    "tsv_group_name": r["group_name"].strip(),
                    "x": float(r["world_x"]),
                    "y": float(r["world_y"]),
                    "z": float(r["world_z"]),
                    "rot": read_rot(r),
                })
            except (KeyError, ValueError):
                continue
    return rows


# -------------------------------------------------------------------- core
def run(folder, tsv_path, tex_dir=None):
    stats = {"inherited": 0}
    warnings = []
    tex_names = load_texture_names(folder, tex_dir)
    unresolved_tex = {}
    ed = load_collections(os.path.join(folder, "emitterdata.yml"))
    eg = load_collections(os.path.join(folder, "emittergroup.yml"))
    rows = read_tsv(tsv_path)

    groups, layers, placements = {}, {}, []
    unresolved = Counter()
    group_cache = {}

    def get_group(tsv_name, hex_key):
        ck = (tsv_name.lower(), hex_key.lower())
        if ck in group_cache:
            return group_cache[ck]
        found = None
        for cand in (tsv_name, hex_key):  # name first, then the VLT hash
            if cand and cand.lower() in eg:
                found = eg[cand.lower()]["name"]
                break
        group_cache[ck] = found
        return found

    for r in rows:
        gname = get_group(r["tsv_group_name"], r["group_key"])
        if gname is None:
            unresolved["%s (%s)" % (r["tsv_group_name"], r["group_key"])] += 1
            gname = None
        elif gname not in groups:
            gdata = resolve(eg, gname, stats) or {}
            emitters = gdata.get("Emitters") or {}
            layer_names = []
            for ref in (emitters.get("Data") or []):
                ck = key_of(ref.get("CollectionKey"))
                cls = key_of(ref.get("ClassKey")).lower()
                if not ck:
                    continue
                if cls and cls != "emitterdata":
                    warnings.append("group %s: layer %s has class %s" % (gname, ck, cls))
                ldata = resolve(ed, ck, stats)
                if ldata is None:
                    warnings.append("group %s: layer %s not in emitterdata.yml" % (gname, ck))
                    continue
                lname = ed[ck.lower()]["name"]
                if lname not in layers:
                    layers[lname] = decode_layer(lname, ldata, tex_names, unresolved_tex)
                layer_names.append(lname)
            extra = {k: norm(v) for k, v in gdata.items()
                     if k not in ("Emitters", "CollectionName")}
            groups[gname] = {"layers": layer_names, "group_fields": extra}
        placements.append({
            "sectionID": r["sectionID"],
            "group": gname,
            "group_key": r["group_key"],
            "x": r["x"], "y": r["y"], "z": r["z"],
            "rot": r["rot"],
            "chunk_offset": r["chunk_offset"],
            "chunk_section_ref": r["chunk_section_ref"],
            "record_section_ref": r["record_section_ref"],
            "record_index": r["record_index"],
        })

    # ---- usage counts and texture list
    group_uses = Counter(p["group"] for p in placements if p["group"])
    tex_layers, tex_places = {}, Counter()
    for gname, g in groups.items():
        for lname in g["layers"]:
            t = layers[lname]["texture"] or "(none)"
            tex_layers.setdefault(t, set()).add(lname)
            tex_places[t] += group_uses[gname]

    out = {
        "meta": {
            "source_folder": folder,
            "tsv": tsv_path,
            "placement_count": len(placements),
            "group_count": len(groups),
            "layer_count": len(layers),
            "unresolved_placements": sum(unresolved.values()),
        },
        "groups": groups,
        "layers": layers,
        "placements": placements,
        "unresolved": dict(unresolved),
    }
    with open(os.path.join(folder, "emitters_resolved.json"), "w") as fh:
        json.dump(out, fh, indent=1)

    with open(os.path.join(folder, "textures_needed.txt"), "w") as fh:
        fh.write("texture_name\tmIndex\tlayers\tplacements\n")
        for t in sorted(tex_layers):
            idx = next((layers[l]["texture_index"] for l in tex_layers[t]), "")
            fh.write("%s\t%s\t%d\t%d\n" % (t, idx, len(tex_layers[t]), tex_places[t]))

    # ---- report (also answers the open design questions)
    with_ir = [n for n, g in groups.items()
               if any("intensity" in k.lower() for k in g["group_fields"])]
    cyc = sum(1 for l in layers.values()
              if (l["fields"].get("OffCycle") or 0) > 0 or (l["fields"].get("OnCycle") or 0) > 0)
    anim = Counter((l["fields"].get("TextureAnimation") or {}).get("AnimType", "?")
                   for l in layers.values())
    rot_counts = Counter(rot_kind(p["rot"]) for p in placements)
    tilted_groups = Counter(p["group"] or p["group_key"] for p in placements
                            if rot_kind(p["rot"]) == "tilted")
    lines = [
        "Files written to:          %s" % os.path.abspath(folder),
        "  (emitters_resolved.json, textures_needed.txt, emitters_report.txt)",
        "Placements read:           %d" % len(rows),
        "Distinct sectionIDs:       %d" % len({r["sectionID"] for r in rows}),
        "  matched to a group:      %d" % (len(rows) - sum(unresolved.values())),
        "  unresolved:              %d (%d distinct)" % (sum(unresolved.values()), len(unresolved)),
        "Groups used:               %d" % len(groups),
        "Layers used:               %d" % len(layers),
        "Textures used:             %d" % len(tex_layers),
        "Layers with on/off cycle:  %d" % cyc,
        "Layers that inherited fields from a parent: %d (0 = data was already full)" % stats["inherited"],
        "Texture AnimType counts:   %s" % dict(anim),
        "Groups with an intensity field: %d %s" % (len(with_ir), with_ir[:5]),
        "Group extra-field names:   %s" % sorted({k for g in groups.values() for k in g["group_fields"]}),
        "Placement rotations:       %s (none = old TSV without rot_* columns)" % dict(rot_counts),
        "Tilted placements by group: %s" % dict(tilted_groups),
        "Layers with RenderLinked:  %d" % sum(1 for l in layers.values() if l["fields"].get("RenderLinked")),
        "Texture folder scanned:   %s (%d known texture names in total)" % (tex_dir or "(none given)", len(tex_names)),
        "Hash-only textures still unnamed: %d %s" % (len(unresolved_tex), sorted(unresolved_tex.items(), key=lambda kv: kv[1] or 0)),
        "  (add their names to texture_names.txt in the folder; blend mode and file name need them)",
    ]
    if unresolved:
        lines.append("Unresolved (first 15): %s" % list(unresolved.items())[:15])
    lines += ["WARNING: " + w for w in warnings[:40]]
    report = "\n".join(lines)
    with open(os.path.join(folder, "emitters_report.txt"), "w") as fh:
        fh.write(report + "\n")
    return report


def find_tsv(folder):
    exact = os.path.join(folder, "fx_triggers.tsv")
    if os.path.isfile(exact):
        return exact
    found = glob.glob(os.path.join(folder, "*.tsv"))
    return found[0] if len(found) == 1 else None


def main():
    folder = sys.argv[1] if len(sys.argv) > 1 else None
    tsv = sys.argv[2] if len(sys.argv) > 2 else None
    tex_dir = sys.argv[3] if len(sys.argv) > 3 else None
    tk = None
    if folder is None:
        try:
            import tkinter as tk
            from tkinter import filedialog, messagebox
            root = tk.Tk()
            root.withdraw()
            folder = filedialog.askdirectory(
                title="Pick the folder with emitterdata.yml / emittergroup.yml")
            if not folder:
                return
        except ImportError:
            folder = input("Folder with the yml files: ").strip().strip('"')
    for need in ("emitterdata.yml", "emittergroup.yml"):
        if not os.path.isfile(os.path.join(folder, need)):
            sys.exit("Missing %s in %s" % (need, folder))
    if tsv is None:
        tsv = find_tsv(folder)
    if tsv is None:
        if tk:
            tsv = filedialog.askopenfilename(
                title="Pick fx_triggers.tsv",
                filetypes=[("TSV", "*.tsv"), ("All files", "*.*")])
        else:
            tsv = input("Path to fx_triggers.tsv: ").strip().strip('"')
        if not tsv:
            return
    if tex_dir is None and len(sys.argv) <= 3:
        if tk:
            tex_dir = filedialog.askdirectory(
                title="Optional: pick the emitter texture folder (Cancel to skip)") or None
        elif len(sys.argv) <= 1:
            tex_dir = input("Emitter texture folder (Enter to skip): ").strip().strip('"') or None
    report = run(folder, tsv, tex_dir)
    print(report)
    if tk:
        messagebox.showinfo("Emitter extract done", report[:1800])


if __name__ == "__main__":
    main()
