#!/usr/bin/env python3
"""
flare_scenery_scan.py - World positions of every scenery flare in Carbon (STREAML5RA.BUN).

Pipeline (layouts from hyperlinked + NFS-ModTools test branch, CarbonScenery.cs / CarbonSolidReader.cs):
  1. Solids   : container 0x80134010 -> header 0x00134011 (hash, marker count, pivot, name)
                                        markers 0x0013401A (N x 0x50 position_marker)
  2. Scenery  : container 0x80034100 -> 0x00034101 header (section number)
                                        0x00034102 infos     (0x48 each, solid keys[4])
                                        0x00034103 instances (0x60 each, position + 3x3 rotation)
  3. Expand   : every instance whose info's solid_keys[LOD] has markers yields one flare per marker.
                world = (marker.translation - pivot.translation) x instance.rotation + instance.position
                (hyperlib commit_flares / create_transform, row-vector convention)

Payload start is aligned to 0x10 by the game readers (AlignReader). The padding is the "prefix"
seen earlier: pad = (-payload_file_offset) % 16.
"""

import argparse
import json
import math
import mmap
import re
import struct
import sys
from collections import Counter, defaultdict

SOLID_HDR, SOLID_MARKERS = 0x00134011, 0x0013401A
SC_HDR, SC_INFOS, SC_INSTANCES = 0x00034101, 0x00034102, 0x00034103
SC_OVR_INFOS, SC_OVR_GROUPS = 0x00034108, 0x00034109  # override infos / named groups (siblings of 0x80034100)
MARKER_SIZE, INFO_SIZE, INST_SIZE = 0x50, 0x48, 0x60
M32 = 0xFFFFFFFF
MAX_DEPTH = 32

FLARE_TYPES = [
    "car_headlight", "car_brakelight", "car_traffic_brakelight", "car_reverse_light",
    "car_fog_light", "car_cop_light_red", "car_cop_light_blue", "car_cop_light_white",
    "car_cop_headlight_right", "car_cop_headlight_left", "car_cop_light_bright_red",
    "car_cop_light_bright_blue", "car_cop_light_orange", "lamppost", "catseye_orange",
    "catseye_red", "catseye_blue", "blinking_amber", "blinking_red", "blinking_green",
    "hand_flare", "sun_flare", "generic_1", "generic_2", "generic_3", "generic_4",
    "generic_5", "generic_6", "generic_7", "generic_8", "generic_9", "generic_10",
]
LAMPPOST, CATSEYE_ORANGE, SUN_FLARE = 13, 14, 21
UNI_DIRECTIONAL = {17, 18, 19, 29}  # blinking_amber/red/green, generic_8


def walk(mm, start, end, visit, anc=(), stats=None):
    off = start
    while off + 8 <= end:
        cid, size = struct.unpack_from("<II", mm, off)
        body = off + 8
        if body + size > end:
            if stats is not None:
                stats["bad"] += 1
            return
        if cid & 0x80000000:
            if len(anc) < MAX_DEPTH:
                walk(mm, body, body + size, visit, anc + ((off, cid),), stats)
        else:
            visit(off, cid, size, anc)
        off = body + size


def pad_of(off):
    return (-(off + 8)) % 16


def resolve_type(iparam, fparam):
    if fparam <= 0.0:
        return LAMPPOST if iparam == 6 else CATSEYE_ORANGE + iparam
    return SUN_FLARE + int(fparam)


def type_name(t):
    return FLARE_TYPES[t] if 0 <= t < len(FLARE_TYPES) else f"?{t}"


def marker_ok(mm, p):
    if mm[p + 0x1C: p + 0x20] != b"\0\0\0\0" or mm[p + 0x2C: p + 0x30] != b"\0\0\0\0":
        return False
    if mm[p + 0x3C: p + 0x40] != b"\0\0\0\0" or mm[p + 0x4C: p + 0x50] != b"\0\0\x80\x3f":
        return False
    vals = struct.unpack_from("<16f", mm, p + 0x10)
    return all(math.isfinite(v) and abs(v) < 1e7 for v in vals)


def parse_markers(mm, off, size):
    """Return (list of markers, padding) or None if the chunk does not fit."""
    pad = pad_of(off)
    body = size - pad
    if body < MARKER_SIZE or body % MARKER_SIZE:
        return None
    n = body // MARKER_SIZE
    base = off + 8 + pad
    out = []
    for i in range(n):
        p = base + i * MARKER_SIZE
        if not marker_ok(mm, p):
            return None
        key, iparam, fparam = struct.unpack_from("<Iif", mm, p)
        m = struct.unpack_from("<16f", mm, p + 0x10)
        out.append({"key": key, "iparam": iparam, "fparam": fparam,
                    "tint": bytes(mm[p + 0x0C: p + 0x10]).hex(), "pos": (m[12], m[13], m[14])})
    return out, pad


def parse_solid_header(mm, off, size):
    p = off + 8 + pad_of(off)
    if p + 0xA0 > off + 8 + size:
        return None
    version = mm[p + 0x0C]
    h = {
        "version": version,
        "hash": struct.unpack_from("<I", mm, p + 0x10)[0],
        "marker_count": mm[p + 0x1B],
        "pivot": struct.unpack_from("<3f", mm, p + 0x70),
    }
    end = off + 8 + size
    raw = bytes(mm[p + 0xA0: min(p + 0xA0 + 0x40, end)])
    h["name"] = raw.split(b"\0")[0].decode("ascii", "replace")
    return h


def split_records(size, rec, pad):
    """Pick padding 0 or aligned pad so that the body is a multiple of rec."""
    for cand in (0, pad):
        if size - cand >= 0 and (size - cand) % rec == 0:
            return cand
    return None


def bin_hash(text):
    h = M32
    for b in text.encode("cp1252", "replace"):
        h = (b + 33 * h) & M32
    return h


def parse_override_infos(mm, off, size, valid):
    """6-byte records: section u16, instance u16, flags u16. Pick the start (0 or 0x10-aligned)
    that gives the most records pointing at real (section, instance) pairs."""
    best = None
    for pad in sorted({0, pad_of(off)}):
        body = size - pad
        if body <= 0 or body % 6:
            continue
        recs = [struct.unpack_from("<HHH", mm, off + 8 + pad + i * 6) for i in range(body // 6)]
        score = sum(1 for sec, inst, _ in recs if inst < valid.get(sec, 0))
        if best is None or score > best[0]:
            best = (score, pad, recs)
    return best


def parse_groups(mm, off, size, n_infos):
    """Variable records (see SceneryGroupReader.cs). Tries both padding rules; the right one
    ends exactly at the chunk end with every override index in range."""
    end = off + 8 + size
    for pad in sorted({0, pad_of(off)}):
        for mode in ("always", "round"):
            p, out = off + 8 + pad, []
            ok = True
            while p + 0x14 <= end:
                key, num, cnt, bar, dthru, race = struct.unpack_from("<IHHBBH", mm, p + 8)
                q = p + 0x14
                if q + 2 * cnt > end:
                    ok = False
                    break
                idx = struct.unpack_from(f"<{cnt}H", mm, q) if cnt else ()
                if any(i >= n_infos for i in idx):
                    ok = False
                    break
                out.append({"key": key, "number": num, "barrier": bar, "drive_through": dthru,
                            "race_section": race, "indices": list(idx)})
                p = q + 2 * cnt
                p += (4 - (p % 4)) if mode == "always" else ((-p) % 4)
            if ok and p == end and out:
                return out, pad, mode
    return None


def resolve_names(path, keys):
    """Names for group keys from a hash list: lines 'name', '0xHASH name' or 'name 0xHASH'."""
    found, want = {}, set(keys)
    for line in open(path, encoding="latin-1", errors="replace"):
        t = line.strip()
        if not t or t.startswith("#"):
            continue
        m = re.search(r"0x([0-9A-Fa-f]{8})", t)
        if m:
            h = int(m.group(1), 16)
            rest = t.replace(m.group(0), "").strip(" \t,;:=")
            if h in want and rest:
                found[h] = rest
            continue
        for v in {t, t.upper(), t.lower()}:
            h = bin_hash(v)
            if h in want and h not in found:
                found[h] = t
    return found


def main():
    ap = argparse.ArgumentParser(description="Expand scenery flares to world positions.")
    ap.add_argument("file", help="STREAML5RA.BUN")
    ap.add_argument("--lod", type=int, default=2, help="solid_keys index to use (2 = LOD c, as the game does)")
    ap.add_argument("--sections", nargs="+", type=int, help="Only these section numbers")
    ap.add_argument("--json", help="Write all flares to this file, one JSON object per line")
    ap.add_argument("--tsv", help="Write all flares to this TSV file")
    ap.add_argument("--hashes", help="Hash name list (e.g. hashes_main) to name the override groups")
    ap.add_argument("--include-2600", action="store_true", help="Keep section 2600 (moving objects; skipped by default)")
    ap.add_argument("--max-print", type=int, default=20, help="Flares to print")
    ap.add_argument("--near", nargs=3, type=float, metavar=("X", "Y", "Z"), help="Print flares near this world position")
    ap.add_argument("--radius", type=float, default=30.0, help="Radius for --near (default 30)")
    args = ap.parse_args()

    with open(args.file, "rb") as f:
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)

    solid_parts = defaultdict(dict)   # parent offset -> {"hdr":..., "markers":..., "off":...}
    sc_parts = defaultdict(dict)      # parent offset -> {"section":..., "infos":[...], "instances":[...]}
    stats = {"bad": 0}
    misfit_markers = []
    misfit_sc = []
    ovr_parts = defaultdict(dict)  # parent offset -> {"infos": (off,size), "groups": (off,size)}

    def visit(off, cid, size, anc):
        if cid == SOLID_HDR and anc:
            h = parse_solid_header(mm, off, size)
            if h:
                solid_parts[anc[-1][0]]["hdr"] = h
        elif cid == SOLID_MARKERS and anc:
            r = parse_markers(mm, off, size)
            if r:
                solid_parts[anc[-1][0]]["markers"] = r[0]
                solid_parts[anc[-1][0]]["off"] = off
            else:
                misfit_markers.append((off, size))
        elif cid in (SC_OVR_INFOS, SC_OVR_GROUPS):
            ovr_parts[anc[-1][0] if anc else -1]["infos" if cid == SC_OVR_INFOS else "groups"] = (off, size)
        elif cid == SC_HDR and anc:
            p = off + 8
            if size >= 0x10:
                sc_parts[anc[-1][0]]["section"] = struct.unpack_from("<i", mm, p + 0x0C)[0]
        elif cid == SC_INFOS and anc:
            pad = split_records(size, INFO_SIZE, pad_of(off))
            if pad is None:
                misfit_sc.append(("infos", off, size))
                return
            base = off + 8 + pad
            infos = []
            for i in range((size - pad) // INFO_SIZE):
                q = base + i * INFO_SIZE
                name = bytes(mm[q: q + 24]).split(b"\0")[0].decode("ascii", "replace")
                keys = struct.unpack_from("<4I", mm, q + 0x18)
                infos.append((name, keys))
            sc_parts[anc[-1][0]]["infos"] = infos
        elif cid == SC_INSTANCES and anc:
            pad = split_records(size, INST_SIZE, pad_of(off))
            if pad is None:
                misfit_sc.append(("instances", off, size))
                return
            sc_parts[anc[-1][0]]["inst_off"] = (off + 8 + pad, (size - pad) // INST_SIZE)

    print("Walking chunk tree (heavy file, please wait)...")
    walk(mm, 0, len(mm), visit, (), stats)

    # ---- solids by hash
    solids = {}
    dup = 0
    count_mismatch = 0
    for part in solid_parts.values():
        h, mk = part.get("hdr"), part.get("markers")
        if not h or not mk:
            continue
        if h["marker_count"] != len(mk):
            count_mismatch += 1
        if h["hash"] in solids:
            dup += 1
            continue
        solids[h["hash"]] = {"name": h["name"], "pivot": h["pivot"], "markers": mk, "off": part["off"]}

    print(f"Solids with markers: {len(solids)} (duplicate hashes skipped: {dup}, header-vs-chunk count mismatches: {count_mismatch})")
    print(f"Marker chunks that did not fit: {len(misfit_markers)}; scenery chunks that did not fit: {len(misfit_sc)}")
    for off, size in misfit_markers[:5]:
        print(f"  marker misfit @0x{off:08X} size={size}")

    # ---- override groups: (section, instance) -> [(group key, flags)]
    valid = {}
    for part in sc_parts.values():
        if "section" in part and "inst_off" in part:
            valid[part["section"]] = part["inst_off"][1]
    override_map = defaultdict(list)
    all_groups = []
    for parent, part in ovr_parts.items():
        if "infos" not in part or "groups" not in part:
            continue
        best = parse_override_infos(mm, part["infos"][0], part["infos"][1], valid)
        if not best:
            print("Override infos chunk did not parse (size not a multiple of 6).")
            continue
        score, pad, recs = best
        g = parse_groups(mm, part["groups"][0], part["groups"][1], len(recs))
        if not g:
            print("Override groups chunk did not parse with any padding rule (send me its first bytes).")
            continue
        groups, gpad, gmode = g
        print(f"Override table: {len(recs)} entries ({score} point at real instances), {len(groups)} groups "
              f"(start pad {gpad}, padding rule '{gmode}')")
        for grp in groups:
            all_groups.append(grp)
            for ix in grp["indices"]:
                sec_o, inst_o, fl = recs[ix]
                override_map[(sec_o, inst_o)].append((grp["key"], fl))
    names = resolve_names(args.hashes, {g["key"] for g in all_groups}) if args.hashes and all_groups else {}
    if args.hashes and all_groups:
        print(f"Group names resolved from hash list: {len(names)} of {len({g['key'] for g in all_groups})}")

    def group_label(key):
        return f"0x{key:08X}({names[key]})" if key in names else f"0x{key:08X}"

    def group_plain(key):
        return names.get(key, f"0x{key:08X}")

    # ---- expand
    flares = []
    sections = 0
    total_instances = 0
    inst_with_flares = 0
    unresolved = Counter()
    types = Counter()
    per_section = Counter()
    only = set(args.sections) if args.sections else None

    for part in sc_parts.values():
        if "infos" not in part or "inst_off" not in part:
            continue
        sec = part.get("section", -1)
        if only and sec not in only:
            continue
        if sec == 2600 and not args.include_2600:
            continue
        sections += 1
        infos = part["infos"]
        base, n = part["inst_off"]
        for i in range(n):
            q = base + i * INST_SIZE
            total_instances += 1
            iflags = struct.unpack_from("<I", mm, q + 0x0C)[0]
            pos = struct.unpack_from("<3f", mm, q + 0x20)
            rot = struct.unpack_from("<9f", mm, q + 0x2C)
            guid = struct.unpack_from("<I", mm, q + 0x50)[0]
            info_no = struct.unpack_from("<h", mm, q + 0x54)[0]
            if not 0 <= info_no < len(infos):
                continue
            name, keys = infos[info_no]
            key = keys[args.lod]
            solid = solids.get(key)
            if solid is None:
                if key:
                    unresolved[name] += 1
                continue
            inst_with_flares += 1
            piv = solid["pivot"]
            for mi, mk in enumerate(solid["markers"]):
                lx, ly, lz = (mk["pos"][0] - piv[0], mk["pos"][1] - piv[1], mk["pos"][2] - piv[2])
                wx = lx * rot[0] + ly * rot[3] + lz * rot[6] + pos[0]
                wy = lx * rot[1] + ly * rot[4] + lz * rot[7] + pos[1]
                wz = lx * rot[2] + ly * rot[5] + lz * rot[8] + pos[2]
                t = resolve_type(mk["iparam"], mk["fparam"])
                ovr = override_map.get((sec, i), [])
                f = {
                    "override_groups": ",".join(group_label(k) for k, _ in ovr),
                    "override_names": ",".join(group_plain(k) for k, _ in ovr),
                    "override_flags": ",".join(f"0x{fl:04X}" for _, fl in ovr),
                    "in_override": 1 if ovr else 0,
                    "section": sec, "instance": i, "marker_index": mi, "tint_zero": mk["tint"] == "00000000",
                    "scenery_guid": guid, "scenery_name": name, "solid_key": key,
                    "solid_name": solid["name"], "instance_flags": iflags, "marker_key": mk["key"],
                    "type": t, "type_name": type_name(t), "iparam": mk["iparam"], "fparam": mk["fparam"],
                    "tint": mk["tint"], "position": [wx, wy, wz],
                }
                if t in UNI_DIRECTIONAL:
                    dx, dy = wx - pos[0], wy - pos[1]
                    L = math.hypot(dx, dy) or 1.0
                    f["direction"] = [dx / L, dy / L, 0.0]
                flares.append(f)
                types[f["type_name"]] += 1
                per_section[sec] += 1

    mm.close()

    print(f"\nScenery sections read: {sections}")
    print(f"Instances: {total_instances}, instances that produce flares: {inst_with_flares}")
    print(f"TOTAL FLARES: {len(flares)}")
    print("By type:")
    for k, v in types.most_common():
        print(f"  {k:<26} {v}")
    print("Top sections by flare count:")
    for sec, n in per_section.most_common(10):
        print(f"  section {sec:<6} {n}")
    in_ovr = [f for f in flares if f["in_override"]]
    print(f"Flares on instances that belong to an override group: {len(in_ovr)}")
    gcount = Counter()
    for f in in_ovr:
        for lab in f["override_groups"].split(","):
            gcount[lab] += 1
    for lab, n in gcount.most_common(10):
        print(f"  {lab:<44} {n}")
    print(f"Flares with an all-zero tint (vault colour used): {sum(1 for f in flares if f['tint_zero'])}")
    if unresolved:
        print(f"\nInstances whose LOD solid key was not found in this file: {sum(unresolved.values())} "
              f"({len(unresolved)} distinct infos). Top 10:")
        for k, v in unresolved.most_common(10):
            print(f"  {k:<28} {v}")

    if args.near:
        cx, cy, cz = args.near
        near = [f for f in flares
                if math.dist(f["position"], (cx, cy, cz)) <= args.radius]
        near.sort(key=lambda f: math.dist(f["position"], (cx, cy, cz)))
        print(f"\nFlares within {args.radius} of ({cx}, {cy}, {cz}): {len(near)}")
        for f in near[: args.max_print]:
            d = math.dist(f["position"], (cx, cy, cz))
            pos = ",".join(f"{v:.2f}" for v in f["position"])
            print(f"  d={d:6.2f} sec {f['section']:<5} {f['scenery_name']:<24} {f['type_name']:<18} ({pos})")

    print(f"\nFirst {min(args.max_print, len(flares))} flares:")
    print(f"{'Sec':<6} {'Scenery':<26} {'Type':<18} {'iP':<4} {'fP':<6} {'Tint':<9} World position")
    for f in flares[: args.max_print]:
        pos = ",".join(f"{v:.2f}" for v in f["position"])
        print(f"{f['section']:<6} {f['scenery_name']:<26} {f['type_name']:<18} {f['iparam']:<4} {f['fparam']:<6.1f} {f['tint']:<9} ({pos})")

    if args.json:
        with open(args.json, "w") as fh:
            for f in flares:
                fh.write(json.dumps(f) + "\n")
        print(f"\nWrote {len(flares)} flares to {args.json} (one per line)")
    if args.tsv:
        cols = ["section", "instance", "scenery_guid", "scenery_name", "solid_name", "marker_index", "type",
                "type_name", "iparam", "fparam", "tint", "x", "y", "z", "dir_x", "dir_y", "dir_z",
                "in_override", "override_groups", "override_names", "override_flags"]
        with open(args.tsv, "w") as fh:
            fh.write("\t".join(cols) + "\n")
            for f in flares:
                d = f.get("direction", ["", "", ""])
                row = [f["section"], f["instance"], f"0x{f['scenery_guid']:08X}", f["scenery_name"], f["solid_name"],
                       f["marker_index"], f["type"], f["type_name"], f["iparam"], f"{f['fparam']:g}", f["tint"],
                       *(f"{v:.4f}" for v in f["position"]),
                       *((f"{v:.4f}" if v != "" else "") for v in d),
                       f["in_override"], f["override_groups"], f["override_names"], f["override_flags"]]
                fh.write("\t".join(str(c) for c in row) + "\n")
        print(f"Wrote {len(flares)} flares to {args.tsv}")


if __name__ == "__main__":
    main()
