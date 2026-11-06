#!/usr/bin/env python3
"""uc_stream_scenery.py - Undercover counterpart of nfs_stream_scenery.py (read only).

Writes the scenery middleman files for the whole map straight from the Undercover stream file
(STREAML8R_MW2.BUN), with the same first columns and the same text as the Carbon tool and
AssetDumper's SceneryManifestWriter, so tools that read scenery_instances.tsv by column name
can read these too. The UC columns come after the Carbon ones.

Why a separate tool: the UC scenery instance (0x34103, 0x60 bytes) is not laid out like the Carbon
one that nfs_stream_scenery.py reads. Layout used here (UCGT ChunkScenery_Instances; guid, info
number and position were checked on the 78 animated nodes of this file, 78 of 78 matched):
    +0x00 bbox min xyz | +0x0C u32 InstanceFlags | +0x10 bbox max xyz
    +0x1C s16 PrecullerInfoIndex | +0x1E s16 SceneryInfoNumber | +0x20 position xyz
    +0x2C u32 SceneryGUID | +0x30 matrix column 0 | +0x3C u8 LodGroup, LoiFlags,
    SwayMaxMultiplier, WSpeedMultiplier | +0x40 column 1 | +0x4C 2 x s16 PCA colour index
    | +0x50 column 2 | +0x5C 2 x s16 PCA colour index
Scenery info (0x34102, 0x48 bytes) is read like the Carbon one: name[24], 4 solid keys at +0x18,
radius f32 +0x38, flags u32 +0x3C, hierarchy hash u32 +0x40. NOT checked on this file; the report
prints a radius sanity check, and the "position inside its bounding box" count checks the
instance layout. The flag bit names are Carbon's hyper::instance_flags, except bits 5, 18 and 19
(Carbon: exclude_freeroam, low/high_platform_only), which do not fit Undercover and are written
as Bit05, Bit18 and Bit19. The other names are not verified either; the bit table in the report
shows what each bit is used on.

Files go to outputs/uc_stream_scenery/ (or --out DIR):
    scenery_infos.tsv      Section, Info, Name, SolidKey1-4, Radius, HierarchyHash, Flags
    scenery_instances.tsv  Section, Instance, Guid, Info, Name, Flags, FlagNames, BBoxMin/Max XYZ,
                           then PosX, PosY, PosZ, PrecullerInfo, LodGroup, LoiFlags,
                           SwayMaxMultiplier, WSpeedMultiplier, AnimType
                           (AnimType = the world_anim node type of the instance, empty if none)
    uc_scenery_flags.txt   checks, bit table, flag words, bytes, per object names

Usage:
    python scenery/uc_stream_scenery.py STREAML8R_MW2.BUN
    python scenery/uc_stream_scenery.py STREAML8R_MW2.BUN --out outputs/uc_stream_scenery
"""
# stream bootstrap: make the shared library folders importable
import pathlib as _pl, sys as _sys
_sys.path[:0] = [str(_pl.Path(__file__).resolve().parents[1] / _d) for _d in ['common', 'region', 'scenery', 'solids_materials']]

import argparse
import math
import mmap
import pathlib
import struct
from collections import Counter, defaultdict

from nfs_chunk_walk import iter_chunks, split_payload
from nfs_stream_scenery import INSTANCE_FLAG_NAMES, _clean, _hex, _net_float, _write_tsv

# Carbon's names, with the three bits that do not fit Undercover left neutral
UC_FLAG_NAMES = list(INSTANCE_FLAG_NAMES)
for _bit in (5, 18, 19):
    UC_FLAG_NAMES[_bit] = "bit%02d" % _bit


def uc_flag_names(flags):
    """Same text style as the Carbon tool (PascalCase, lowest bit first, '|', None for zero)."""
    names = [''.join(part.capitalize() for part in UC_FLAG_NAMES[b].split('_'))
             for b in range(32) if flags >> b & 1]
    return '|'.join(names) if names else 'None'


ID_SECTION = 0x80034100
ID_SC_HEADER = 0x00034101
ID_SC_INFO = 0x00034102
ID_SC_INST = 0x00034103
ID_RTNODE = 0x00037250

INFO_STRIDE = 0x48
INST_STRIDE = 0x60
RTNODE_SIZE = 0x88


def read_file(path):
    """One pass. Returns (sections, anim, problems): sections is a list of dicts with
    section number, infos and instances, anim maps (section, guid) to the world_anim type."""
    sections = {}  # container offset -> dict
    anim = {}
    problems = []
    with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        for off, cid, length, is_cont, pstart, parent, depth, parent_off in iter_chunks(mm, problems=problems):
            if cid == ID_SECTION:
                sections[off] = {"section": None, "infos": [], "insts": []}
                continue
            if is_cont:
                continue

            if parent == -1 and cid == ID_RTNODE:
                dstart, pad, _kind = split_payload(mm, pstart, length)
                if length - pad == RTNODE_SIZE:
                    guid = struct.unpack_from("<I", mm, dstart + 0x5C)[0]
                    node_type, node_section = struct.unpack_from("<HH", mm, dstart + 0x7C)
                    anim[(node_section, guid)] = node_type
                continue

            if parent != ID_SECTION or cid not in (ID_SC_HEADER, ID_SC_INFO, ID_SC_INST):
                continue
            sec = sections.get(parent_off)
            if sec is None:
                continue
            dstart, pad, _kind = split_payload(mm, pstart, length)
            dlen = length - pad
            if cid == ID_SC_HEADER and dlen >= 0x10:
                sec["section"] = struct.unpack_from("<i", mm, dstart + 0x0C)[0]
            elif cid == ID_SC_INFO:
                for i in range(dlen // INFO_STRIDE):
                    b = dstart + i * INFO_STRIDE
                    name = bytes(mm[b:b + 24]).split(b"\0")[0].decode("ascii", errors="replace")
                    keys = struct.unpack_from("<4I", mm, b + 0x18)
                    radius, flags, hier = struct.unpack_from("<fII", mm, b + 0x38)
                    sec["infos"].append((name, keys, radius, flags, hier))
            elif cid == ID_SC_INST:
                for i in range(dlen // INST_STRIDE):
                    b = dstart + i * INST_STRIDE
                    bmin = struct.unpack_from("<3f", mm, b + 0x00)
                    flags = struct.unpack_from("<I", mm, b + 0x0C)[0]
                    bmax = struct.unpack_from("<3f", mm, b + 0x10)
                    preculler, info_no = struct.unpack_from("<hh", mm, b + 0x1C)
                    pos = struct.unpack_from("<3f", mm, b + 0x20)
                    guid = struct.unpack_from("<I", mm, b + 0x2C)[0]
                    extra = struct.unpack_from("<4B", mm, b + 0x3C)
                    sec["insts"].append((i, guid, info_no, flags, bmin, bmax, pos, preculler, extra))
    return list(sections.values()), anim, problems


def pct(a, b):
    return "%.1f%%" % (100.0 * a / b) if b else "-"


def build(sections, anim):
    """Rows for both TSV files plus the numbers for the report."""
    stats = {
        "sections": 0, "infos": 0, "insts": 0, "no_section_number": 0, "dup_sections": 0,
        "bad_info_index": 0, "pos_in_bbox": 0, "bbox_ordered": 0, "dup_guid_in_section": 0,
        "anim_hits": 0,
        "bit_counts": [0] * 32, "bit_names": [Counter() for _ in range(32)],
        "words": Counter(), "word_names": defaultdict(Counter),
        "lod": Counter(), "loi": Counter(), "sway": Counter(), "wspeed": Counter(),
        "info_flags": Counter(), "radii": [], "anim_words": Counter(),
        "sway_names": Counter(),
    }
    info_rows, inst_rows = [], []
    seen_numbers = set()
    for sec in sorted(sections, key=lambda s: (s["section"] is None, s["section"] or 0)):
        num = sec["section"]
        if num is None:
            stats["no_section_number"] += 1
            continue
        if num in seen_numbers:
            stats["dup_sections"] += 1
        seen_numbers.add(num)
        stats["sections"] += 1
        infos = sec["infos"]
        stats["infos"] += len(infos)
        for idx, (name, keys, radius, flags, hier) in enumerate(infos):
            info_rows.append([num, idx, _clean(name), _hex(keys[0]), _hex(keys[1]), _hex(keys[2]),
                              _hex(keys[3]), _net_float(radius), _hex(hier), _hex(flags)])
            stats["info_flags"][flags] += 1
            if math.isfinite(radius):
                stats["radii"].append(radius)
        guids = Counter(i[1] for i in sec["insts"])
        stats["dup_guid_in_section"] += sum(1 for c in guids.values() if c > 1)
        for (number, guid, info_no, flags, bmin, bmax, pos, preculler, extra) in sec["insts"]:
            stats["insts"] += 1
            in_range = 0 <= info_no < len(infos)
            name = infos[info_no][0] if in_range else ""
            if not in_range:
                stats["bad_info_index"] += 1
            if all(a <= b for a, b in zip(bmin, bmax)):
                stats["bbox_ordered"] += 1
            if all(lo - 1.0 <= p <= hi + 1.0 for lo, p, hi in zip(bmin, pos, bmax)):
                stats["pos_in_bbox"] += 1
            anim_type = anim.get((num, guid))
            if anim_type is not None:
                stats["anim_hits"] += 1
                stats["anim_words"][flags] += 1
            stats["words"][flags] += 1
            stats["word_names"][flags][name] += 1
            for bit in range(32):
                if flags >> bit & 1:
                    stats["bit_counts"][bit] += 1
                    stats["bit_names"][bit][name] += 1
            stats["lod"][extra[0]] += 1
            stats["loi"][extra[1]] += 1
            stats["sway"][extra[2]] += 1
            stats["wspeed"][extra[3]] += 1
            if extra[2] or extra[3]:
                stats["sway_names"][name] += 1
            inst_rows.append([
                num, number, _hex(guid), info_no, _clean(name), _hex(flags), uc_flag_names(flags),
                *(_net_float(v) for v in bmin), *(_net_float(v) for v in bmax),
                *(_net_float(v) for v in pos), preculler, *extra,
                "" if anim_type is None else anim_type,
            ])
    return info_rows, inst_rows, stats


def top(counter, n=8):
    return ", ".join("%s x %d" % (k, v) for k, v in counter.most_common(n))


def report(stats, path):
    L = []
    total = stats["insts"]
    L.append("=== %s ===" % path)
    L.append("%d sections, %d scenery infos, %d instances; sections without a number: %d, duplicate section numbers: %d" % (
        stats["sections"], stats["infos"], total, stats["no_section_number"], stats["dup_sections"]))
    L.append("")
    L.append("--- checks (a wrong offset shows up as a low count)")
    L.append("  info number inside the section's info table: %d of %d (%s)" % (
        total - stats["bad_info_index"], total, pct(total - stats["bad_info_index"], total)))
    L.append("  bbox min <= bbox max on every axis:           %d of %d (%s)" % (
        stats["bbox_ordered"], total, pct(stats["bbox_ordered"], total)))
    L.append("  position inside its bbox (1 m slack):         %d of %d (%s)" % (
        stats["pos_in_bbox"], total, pct(stats["pos_in_bbox"], total)))
    L.append("  guid used twice inside one section:           %d guids" % stats["dup_guid_in_section"])
    radii = sorted(stats["radii"])
    if radii:
        L.append("  info radius (+0x38): %d finite, min %.4g, median %.4g, max %.4g; %d are > 0" % (
            len(radii), radii[0], radii[len(radii) // 2], radii[-1], sum(1 for r in radii if r > 0)))
    L.append("  instances that match a world_anim node (section + guid): %d" % stats["anim_hits"])
    L.append("")

    L.append("--- InstanceFlags bits over all %d instances (Carbon bit names except 5, 18, 19; UC use unverified)" % total)
    for b in range(32):
        c = stats["bit_counts"][b]
        names = top(stats["bit_names"][b], 3) if c else ""
        L.append("  bit %2d %-24s %7d (%6s)  %s" % (b, UC_FLAG_NAMES[b], c, pct(c, total), names))
    L.append("")

    L.append("--- most common flag words (%d distinct)" % len(stats["words"]))
    for word, c in stats["words"].most_common(30):
        L.append("  0x%08X %7d (%6s)  %s  | %s" % (word, c, pct(c, total), uc_flag_names(word),
                                                    top(stats["word_names"][word], 3)))
    L.append("")

    L.append("--- flag words of the world_anim instances")
    for word, c in stats["anim_words"].most_common():
        L.append("  0x%08X %4d  %s" % (word, c, uc_flag_names(word)))
    L.append("")

    L.append("--- bytes at +0x3C (UCGT order: LodGroup, LoiFlags, SwayMaxMultiplier, WSpeedMultiplier)")
    L.append("  LodGroup:          " + top(stats["lod"], 10))
    L.append("  LoiFlags:          " + top(stats["loi"], 10))
    L.append("  SwayMaxMultiplier: " + top(stats["sway"], 10))
    L.append("  WSpeedMultiplier:  " + top(stats["wspeed"], 10))
    L.append("  objects with a nonzero sway or speed byte: " + top(stats["sway_names"], 12))
    L.append("")

    L.append("--- scenery info flags (+0x3C of the info), %d distinct" % len(stats["info_flags"]))
    for word, c in stats["info_flags"].most_common(12):
        L.append("  0x%08X %6d" % (word, c))
    return L


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file")
    ap.add_argument("--out", default="outputs/uc_stream_scenery")
    args = ap.parse_args()

    sections, anim, problems = read_file(args.file)
    info_rows, inst_rows, stats = build(sections, anim)

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    _write_tsv(out / "scenery_infos.tsv",
               ["Section", "Info", "Name", "SolidKey1", "SolidKey2", "SolidKey3", "SolidKey4",
                "Radius", "HierarchyHash", "Flags"], info_rows)
    _write_tsv(out / "scenery_instances.tsv",
               ["Section", "Instance", "Guid", "Info", "Name", "Flags", "FlagNames",
                "BBoxMinX", "BBoxMinY", "BBoxMinZ", "BBoxMaxX", "BBoxMaxY", "BBoxMaxZ",
                "PosX", "PosY", "PosZ", "PrecullerInfo", "LodGroup", "LoiFlags",
                "SwayMaxMultiplier", "WSpeedMultiplier", "AnimType"], inst_rows)
    lines = report(stats, args.file)
    for p in problems[:10]:
        lines.append("walker problem at 0x%X: %s" % (p[0], p[1]))
    (out / "uc_scenery_flags.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print("\nWrote %s (%d rows)" % (out / "scenery_instances.tsv", len(inst_rows)))
    print("Wrote %s (%d rows)" % (out / "scenery_infos.tsv", len(info_rows)))
    print("Wrote %s" % (out / "uc_scenery_flags.txt"))


if __name__ == "__main__":
    main()
