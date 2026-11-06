#!/usr/bin/env python3
"""uc_world_anim_dump.py - Undercover world_anim reader (read only), with object names.

Undercover counterpart of dump_worldanim_2400.py / dump_worldanim_allsections.py. The Carbon
tools assert a 120 byte rtnode; the UC rtnode payload is 140 bytes (136 bytes of data), and UC
has no frames chunk (0x37240) and no shared sections 2400/2600.

What it reads (STREAML8R_MW2.BUN, top level chunks in file order):
    0x00037220  header   4 x u32: (N, N, section, N) in the probe samples
    0x00037260  counts   u32 (1 in the probe samples)
    0x00037250  rtnode   136 data bytes, layout below
    0x00037270  endptr   u32 marker 0x69696969 ("iiii") in the probe samples
A group is one header followed by N x (counts, rtnode, endptr).

rtnode data layout (offsets from the data start; Carbon offsets minus 4 up to +0x58):
    +0x00 float[16]  matrix, row 3 = translation
    +0x40 u32 key | +0x44 i8 parent, u8 timescale, u8 flags, u8 pad
    +0x48 u32[4] solid keys | +0x58 u32 smackable key | +0x5C u32 guid (scenery GUID?)
    +0x60 float[3] vec A, u32 +0x6C | +0x70 float[3] vec B | +0x7C u16 type, u16 section
    +0x80 u32, +0x84 u32 (meaning unknown)
The meaning of vec A, vec B, type and the last two words is NOT known: this tool prints them
so they can be compared with what the objects do in the game.

Names: every node's guid is looked up in the scenery instances (0x34103, GUID at +0x2C, info
number s16 at +0x1E, position at +0x20) and the instance's info (0x34102, 0x48 bytes: name[24],
4 LOD solid keys at +0x18). The first solid key is looked up in the LOD keys independently, and
the two names are compared. The 0x34102/0x34103 field offsets come from UCGT and have not been
checked on our file: the report states how many guids were found and how far the node
translation is from the instance position, so a wrong offset shows up as a low hit rate.

Flags: the 0x34103 InstanceFlags word (+0x0C) and the four bytes at +0x3C (LodGroup, LoiFlags,
SwayMaxMultiplier, WSpeedMultiplier, order from UCGT) are decoded for every node. Bit names are
the Carbon hyper::instance_flags enum except bits 5, 18 and 19 (left neutral); that this enum is the same on Undercover is NOT verified,
so the report also counts every bit over ALL instances in the file, to show which bits are
unusual for the animated objects.

Written files (outputs/uc_world_anim/):
    uc_world_anim.txt   the report: group check, every node decoded with its names, histograms
    uc_world_anim.tsv   one row per node

Usage:
    python world_anim/uc_world_anim_dump.py STREAML8R_MW2.BUN
    python world_anim/uc_world_anim_dump.py STREAML8R_MW2.BUN --out outputs/uc_world_anim
"""
# stream bootstrap: make the shared library folders importable
import pathlib as _pl, sys as _sys
_sys.path[:0] = [str(_pl.Path(__file__).resolve().parents[1] / _d) for _d in ['common']]

import argparse
import math
import mmap
import pathlib
import struct
from collections import Counter, defaultdict

from nfs_chunk_walk import iter_chunks, split_payload

ID_HEADER = 0x00037220
ID_RTNODE = 0x00037250
ID_COUNTS = 0x00037260
ID_ENDPTR = 0x00037270
ID_SECTION = 0x80034100
ID_SC_HEADER = 0x00034101
ID_SC_INFO = 0x00034102
ID_SC_INST = 0x00034103

# Carbon hyper::instance_flags, bit 0 to 31 (names only, UC use unverified); bits 5, 18 and 19
# do not fit Undercover and are left neutral
FLAG_NAMES = [
    "exclude_split_screen", "exclude_main_view", "exclude_racing", "exclude_disable_rendering",
    "exclude_group_disable", "bit05", "include_rear_view", "include_reflection",
    "envmap_shadow", "chopped_roadway", "identity_matrix", "artwork_flipped",
    "reflection", "environment_map", "swayable", "enable_wind",
    "always_facing", "dont_receive_shadows", "bit18", "bit19",
    "cast_shadow_volume", "cast_shadow_map", "inverted_matrix", "flip_on_backwards_track",
    "reflect_in_ocean", "visible_further", "cast_shadow_map_mesh", "include_reflection_ng",
    "aux_lighting", "pc_platform", "collidable", "lightmapped",
]

RT_FMT = "<16fIbBBB4IIIfffIfffHHII"
RT_SIZE = struct.calcsize(RT_FMT)
assert RT_SIZE == 0x88, RT_SIZE

INFO_STRIDE = 0x48
INST_STRIDE = 0x60

RT_NAMES = [
    *["m%d" % i for i in range(16)],
    "key", "parent", "timescale", "flags", "pad0",
    "solid0", "solid1", "solid2", "solid3",
    "smackable", "guid",
    "ax", "ay", "az", "aw",
    "bx", "by", "bz",
    "type", "section", "tail0", "tail1",
]


def f4(x):
    return "%.4g" % x


def pattern(values):
    """Letters by first appearance: [a, b, b, c] -> ABBC."""
    seen = {}
    out = []
    for v in values:
        if v not in seen:
            seen[v] = chr(ord("A") + len(seen))
        out.append(seen[v])
    return "".join(out)


def axes(vec):
    return "".join(n if abs(v) > 1e-6 else "." for n, v in zip("xyz", vec))


def read_file(path):
    """One pass over the file. Returns (groups, sections, problems)."""
    groups = []
    cur = None
    sections = {}  # container offset -> {"section", "infos", "insts"}
    problems = []
    stats = {"inst_total": 0, "bit_counts": [0] * 32, "flag_words": Counter(), "sway_nonzero": 0}
    with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        for off, cid, length, is_cont, pstart, parent, depth, parent_off in iter_chunks(mm, problems=problems):
            if cid == ID_SECTION:
                sections[off] = {"section": None, "infos": [], "insts": []}
                continue
            if is_cont:
                continue

            if parent == ID_SECTION:
                sec = sections.get(parent_off)
                if sec is None or cid not in (ID_SC_HEADER, ID_SC_INFO, ID_SC_INST):
                    continue
                dstart, pad, _kind = split_payload(mm, pstart, length)
                dlen = length - pad
                if cid == ID_SC_HEADER and dlen >= 0x10:
                    sec["section"] = struct.unpack_from("<I", mm, dstart + 0x0C)[0]
                elif cid == ID_SC_INFO:
                    for i in range(dlen // INFO_STRIDE):
                        b = dstart + i * INFO_STRIDE
                        name = bytes(mm[b:b + 24]).split(b"\0")[0].decode("latin-1")
                        lod = struct.unpack_from("<4I", mm, b + 0x18)
                        sec["infos"].append((name, lod))
                elif cid == ID_SC_INST:
                    for i in range(dlen // INST_STRIDE):
                        b = dstart + i * INST_STRIDE
                        flags = struct.unpack_from("<I", mm, b + 0x0C)[0]
                        info_no = struct.unpack_from("<h", mm, b + 0x1E)[0]
                        pos = struct.unpack_from("<3f", mm, b + 0x20)
                        guid = struct.unpack_from("<I", mm, b + 0x2C)[0]
                        extra = struct.unpack_from("<4B", mm, b + 0x3C)  # LodGroup, LoiFlags, SwayMax, WSpeed
                        sec["insts"].append((guid, info_no, pos, flags, extra))
                        stats["inst_total"] += 1
                        stats["flag_words"][flags] += 1
                        if extra[2] or extra[3]:
                            stats["sway_nonzero"] += 1
                        for bit in range(32):
                            if flags >> bit & 1:
                                stats["bit_counts"][bit] += 1
                continue

            if parent != -1 or cid not in (ID_HEADER, ID_RTNODE, ID_COUNTS, ID_ENDPTR):
                continue

            dstart, pad, _kind = split_payload(mm, pstart, length)
            dlen = length - pad
            if cid == ID_HEADER:
                cur = {"offset": off, "header": None, "nodes": [], "counts": [], "endptrs": []}
                groups.append(cur)
                if dlen >= 16:
                    cur["header"] = struct.unpack_from("<4I", mm, dstart)
                else:
                    problems.append((off, "header data is %d bytes, expected 16" % dlen, 0))
                continue
            if cur is None:
                cur = {"offset": off, "header": None, "nodes": [], "counts": [], "endptrs": []}
                groups.append(cur)
            if cid == ID_COUNTS:
                cur["counts"].append(struct.unpack_from("<I", mm, dstart)[0] if dlen >= 4 else None)
            elif cid == ID_ENDPTR:
                cur["endptrs"].append(struct.unpack_from("<I", mm, dstart)[0] if dlen >= 4 else None)
            elif cid == ID_RTNODE:
                if dlen != RT_SIZE:
                    problems.append((off, "rtnode data is %d bytes, expected %d" % (dlen, RT_SIZE), 0))
                    continue
                rec = dict(zip(RT_NAMES, struct.unpack_from(RT_FMT, mm, dstart)))
                rec["offset"] = off
                cur["nodes"].append(rec)
    return groups, sections, problems, stats


def build_lookups(sections):
    infos_by_section = {}
    guid_map = defaultdict(list)    # guid -> [(section, instance index, instance)]
    lodkey_map = defaultdict(list)  # solid key -> [(section, info index, name)]
    for sec in sections.values():
        num = sec["section"]
        infos_by_section[num] = sec["infos"]
        for i, inst in enumerate(sec["insts"]):
            guid_map[inst[0]].append((num, i, inst))
        for i, (name, lod) in enumerate(sec["infos"]):
            for key in lod:
                if key:
                    lodkey_map[key].append((num, i, name))
    return infos_by_section, guid_map, lodkey_map


def resolve(node, infos_by_section, guid_map, lodkey_map):
    """Names for one node, by guid and by first solid key. Returns a dict."""
    out = {"guid_hits": 0, "guid_section": None, "guid_in_node_section": None, "inst_info": None,
           "name_guid": "", "inst_pos": None, "pos_dist": None, "inst_flags": None, "inst_extra": None,
           "names_solid": [], "agree": None}
    hits = guid_map.get(node["guid"], [])
    out["guid_hits"] = len(hits)
    if hits:
        pick = next((h for h in hits if h[0] == node["section"]), hits[0])
        sec, idx, inst = pick
        out["guid_section"] = sec
        out["guid_in_node_section"] = (sec == node["section"])
        out["inst_info"] = inst[1]
        infos = infos_by_section.get(sec, [])
        if 0 <= inst[1] < len(infos):
            out["name_guid"] = infos[inst[1]][0]
        out["inst_pos"] = inst[2]
        out["inst_flags"] = inst[3]
        out["inst_extra"] = inst[4]
        t = (node["m12"], node["m13"], node["m14"])
        out["pos_dist"] = math.sqrt(sum((a - b) ** 2 for a, b in zip(t, inst[2])))
    names = sorted({h[2] for h in lodkey_map.get(node["solid0"], [])})
    out["names_solid"] = names
    if out["name_guid"] and names:
        out["agree"] = out["name_guid"] in names
    return out


def flag_names(word):
    return [FLAG_NAMES[b] for b in range(32) if word >> b & 1]


def report(groups, sections, problems, stats, path):
    L = []
    infos_by_section, guid_map, lodkey_map = build_lookups(sections)
    nodes = []
    for gi, g in enumerate(groups):
        for ni, n in enumerate(g["nodes"]):
            n["group"] = gi
            n["index"] = ni
            n["header_section"] = g["header"][2] if g["header"] else None
            n["res"] = resolve(n, infos_by_section, guid_map, lodkey_map)
            nodes.append(n)

    L.append("=== %s ===" % path)
    L.append("%d groups, %d rtnodes; %d scenery sections read, %d guids, %d solid keys" % (
        len(groups), len(nodes), len(sections), len(guid_map), len(lodkey_map)))
    for p in problems[:10]:
        L.append("  problem at 0x%X: %s" % (p[0], p[1]))
    L.append("")

    # group check
    L.append("--- group check (header N, N, section, N versus the nodes that follow)")
    bad = 0
    sizes = Counter()
    for gi, g in enumerate(groups):
        h = g["header"]
        n = len(g["nodes"])
        sizes[n] += 1
        ok = h is not None and h[0] == h[1] == h[3] == n and len(g["counts"]) == n and len(g["endptrs"]) == n
        if not ok:
            bad += 1
            L.append("  group %d at 0x%X: header=%s nodes=%d counts=%d endptrs=%d" % (
                gi, g["offset"], h, n, len(g["counts"]), len(g["endptrs"])))
    L.append("groups that do not fit (N=N=N=nodes=counts=endptrs): %d of %d" % (bad, len(groups)))
    L.append("nodes per group: " + ", ".join("%d x %d" % (k, v) for k, v in sorted(sizes.items())))
    L.append("counts values: " + str(sorted(Counter(c for g in groups for c in g["counts"]).items())))
    L.append("endptr values: " + ", ".join("0x%08X x %d" % (k, v) for k, v in Counter(
        e for g in groups for e in g["endptrs"]).items()))
    hs = Counter(g["header"][2] for g in groups if g["header"])
    dup = [k for k, v in hs.items() if v > 1]
    L.append("header sections: %d distinct, duplicated: %s" % (len(hs), dup[:10]))
    L.append("")

    # fixed value assumptions
    L.append("--- assumptions (count of nodes where they hold, of %d)" % len(nodes))
    def holds(label, fn):
        L.append("  %-44s %d" % (label, sum(1 for n in nodes if fn(n))))
    holds("matrix w == 1.0 (m15)", lambda n: n["m15"] == 1.0)
    holds("parent -1, timescale 0, flags 0, pad 0", lambda n: (n["parent"], n["timescale"], n["flags"], n["pad0"]) == (-1, 0, 0, 0))
    holds("smackable == 0", lambda n: n["smackable"] == 0)
    holds("tail0 == 0 and tail1 == 1", lambda n: (n["tail0"], n["tail1"]) == (0, 1))
    holds("aw == 0 (word after vec A)", lambda n: n["aw"] == 0)
    holds("node section == header section", lambda n: n["section"] == n["header_section"])
    L.append("  parent/timescale/flags seen: " + str(Counter((n["parent"], n["timescale"], n["flags"]) for n in nodes).most_common(8)))
    L.append("  tail0/tail1 seen: " + str(Counter((n["tail0"], n["tail1"]) for n in nodes).most_common(8)))
    L.append("")

    # histograms
    L.append("--- type (+0x7C u16)")
    for t, c in sorted(Counter(n["type"] for n in nodes).items()):
        L.append("  type %d: %d nodes" % (t, c))
    L.append("--- vec A / vec B axes in use per type")
    for t in sorted({n["type"] for n in nodes}):
        sub = [n for n in nodes if n["type"] == t]
        pa = Counter(axes((n["ax"], n["ay"], n["az"])) for n in sub)
        pb = Counter(axes((n["bx"], n["by"], n["bz"])) for n in sub)
        L.append("  type %d  A: %s   B: %s" % (t, dict(pa), dict(pb)))
    L.append("--- distinct nonzero float values in A and B: " + ", ".join(
        f4(v) for v in sorted({abs(x) for n in nodes for x in (n["ax"], n["ay"], n["az"], n["bx"], n["by"], n["bz"]) if abs(x) > 1e-6})[:40]))
    L.append("--- solid key pattern (A = first key, letters by first appearance)")
    for k, v in Counter(pattern([n["solid0"], n["solid1"], n["solid2"], n["solid3"]]) for n in nodes).most_common():
        L.append("  %s: %d" % (k, v))
    rel = Counter()
    for n in nodes:
        s = [n["solid0"], n["solid1"], n["solid2"], n["solid3"]]
        rel["key == solid%s" % ",".join(str(i) for i, v in enumerate(s) if v == n["key"])
            if n["key"] in s else "key not among the solid keys"] += 1
    L.append("--- key (+0x40) versus solid keys: " + str(dict(rel)))
    L.append("")

    # name resolution
    got = [n for n in nodes if n["res"]["guid_hits"]]
    L.append("--- names")
    L.append("guid found in the scenery instances: %d of %d nodes" % (len(got), len(nodes)))
    L.append("  ... in the node's own section: %d" % sum(1 for n in got if n["res"]["guid_in_node_section"]))
    L.append("  ... guid matches more than one instance: %d" % sum(1 for n in got if n["res"]["guid_hits"] > 1))
    near = [n["res"]["pos_dist"] for n in got]
    L.append("  node translation to instance position: %d within 1 m, %d within 100 m, median %s m" % (
        sum(1 for d in near if d < 1.0), sum(1 for d in near if d < 100.0),
        f4(sorted(near)[len(near) // 2]) if near else "-"))
    L.append("first solid key found in an info LOD key: %d of %d nodes" % (
        sum(1 for n in nodes if n["res"]["names_solid"]), len(nodes)))
    L.append("name by guid agrees with name by solid key: %d of %d comparable" % (
        sum(1 for n in nodes if n["res"]["agree"] is True), sum(1 for n in nodes if n["res"]["agree"] is not None)))
    L.append("")

    # flags
    byname = defaultdict(list)
    for n in got:
        byname[n["res"]["name_guid"]].append(n)
    L.append("--- InstanceFlags per object (0x34103 +0x0C), bit names are Carbon's")
    for name, ns in sorted(byname.items()):
        words = Counter(n["res"]["inst_flags"] for n in ns)
        for w, c in words.most_common():
            L.append("  %-26s %3d x 0x%08X  %s" % (name, c, w, ",".join(flag_names(w)) or "-"))
    L.append("")
    L.append("--- bit counts: animated nodes (%d) versus all instances in the file (%d)" % (len(got), stats["inst_total"]))
    anim_bits = [sum(1 for n in got if n["res"]["inst_flags"] >> b & 1) for b in range(32)]
    for b in range(32):
        tot = stats["bit_counts"][b]
        L.append("  bit %2d %-24s anim %3d of %d   file %6d (%.1f%%)" % (
            b, FLAG_NAMES[b], anim_bits[b], len(got), tot, 100.0 * tot / max(1, stats["inst_total"])))
    L.append("distinct flag words in the file: %d; the 6 most common: %s" % (
        len(stats["flag_words"]), ", ".join("0x%08X x %d" % kv for kv in stats["flag_words"].most_common(6))))
    L.append("")
    L.append("--- LodGroup / LoiFlags / SwayMaxMultiplier / WSpeedMultiplier per object (bytes at +0x3C)")
    for name, ns in sorted(byname.items()):
        combos = Counter(tuple(n["res"]["inst_extra"]) for n in ns)
        L.append("  %-26s %s" % (name, ", ".join("%d x (%d,%d,%d,%d)" % ((c,) + k) for k, c in combos.most_common())))
    L.append("instances in the whole file with SwayMax or WSpeed nonzero: %d of %d" % (stats["sway_nonzero"], stats["inst_total"]))
    L.append("")

    # every node
    L.append("--- every node: group/index  section(header/node)  type  A  B  tail  name(guid)  | name(solid key)  | pos dist")
    for n in nodes:
        r = n["res"]
        L.append("g%03d.%d  sec %s/%s  t%d  A(%s %s %s)  B(%s %s %s)  tail %d,%d  guid %08X  flags %s  %s  | %s  | %s" % (
            n["group"], n["index"], n["header_section"], n["section"], n["type"],
            f4(n["ax"]), f4(n["ay"]), f4(n["az"]), f4(n["bx"]), f4(n["by"]), f4(n["bz"]),
            n["tail0"], n["tail1"], n["guid"],
            "0x%08X" % r["inst_flags"] if r["inst_flags"] is not None else "-",
            r["name_guid"] or "(guid not found)", ",".join(r["names_solid"][:3]) or "(key not found)",
            f4(r["pos_dist"]) if r["pos_dist"] is not None else "-"))
    return L, nodes


def write_tsv(nodes, path):
    cols = ["group", "index", "header_section", "section", "type", "parent", "timescale", "flags",
            "key", "solid0", "solid1", "solid2", "solid3", "smackable", "guid",
            "ax", "ay", "az", "aw", "bx", "by", "bz", "tail0", "tail1",
            "tx", "ty", "tz", "name_guid", "names_solid", "guid_hits", "inst_flags", "flag_names", "lod_group", "loi_flags",
            "sway_max", "wspeed", "pos_dist"]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\t".join(cols) + "\n")
        for n in nodes:
            r = n["res"]
            row = [n["group"], n["index"], n["header_section"], n["section"], n["type"], n["parent"],
                   n["timescale"], n["flags"], "0x%08X" % n["key"],
                   "0x%08X" % n["solid0"], "0x%08X" % n["solid1"], "0x%08X" % n["solid2"], "0x%08X" % n["solid3"],
                   "0x%08X" % n["smackable"], "0x%08X" % n["guid"],
                   f4(n["ax"]), f4(n["ay"]), f4(n["az"]), "0x%08X" % n["aw"], f4(n["bx"]), f4(n["by"]), f4(n["bz"]),
                   n["tail0"], n["tail1"], f4(n["m12"]), f4(n["m13"]), f4(n["m14"]),
                   r["name_guid"], "|".join(r["names_solid"]), r["guid_hits"],
                   "" if r["inst_flags"] is None else "0x%08X" % r["inst_flags"],
                   "" if r["inst_flags"] is None else ",".join(flag_names(r["inst_flags"])),
                   *(r["inst_extra"] or ("", "", "", "")),
                   "" if r["pos_dist"] is None else f4(r["pos_dist"])]
            f.write("\t".join(str(x) for x in row) + "\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file")
    ap.add_argument("--out", default="outputs/uc_world_anim")
    args = ap.parse_args()

    groups, sections, problems, stats = read_file(args.file)
    lines, nodes = report(groups, sections, problems, stats, args.file)
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "uc_world_anim.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    write_tsv(nodes, out / "uc_world_anim.tsv")
    # console: the summary part only, the node list is in the report
    for l in lines:
        if l.startswith("--- every node"):
            break
        print(l)
    print("\nWrote %s" % (out / "uc_world_anim.txt"))
    print("Wrote %s" % (out / "uc_world_anim.tsv"))


if __name__ == "__main__":
    main()
