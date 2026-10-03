#!/usr/bin/env python3
"""
nfs_anim_name_match.py - Match scenery instance names (the same strings
that become Blender object names) to their raw world_anim rtnode data.

Replicates, read-only, exactly what AssetDumper/ExportBundleCommand.cs
does: read a 0x80034100 ScenerySection container (header/infos/instances
per Common/Scenery/CarbonScenery.cs), then look up each instance's
SceneryGuid in the worldAnimByGuid dictionary built from every rtnode
in the file (0x00037250 chunks, per Common/WorldAnimReader.cs).

Also dumps the FULL raw Flags byte (+0x4A) in hex/binary, not just the
three previously-named bits, since Need for Speed Most Wanted's real
CWorldAnimCtrl::AdvanceAnimTime (dbalatoni13/nfsmw,
Animation/WorldAnimCtrl.cpp) uses byte-flag bits 0x08 (linear),
0x10 (pingpong), 0x20 (loop) that we have not yet confirmed do or don't
share this same on-disk byte with is_library_anim(0x01)/
use_library_anim(0x02)/use_parent_anim(0x04).
"""

import argparse
import mmap
import struct
import sys
from collections import defaultdict

SCENERY_SECTION_CHUNK_ID = 0x80034100
SCENERY_HEADER_CHUNK_ID = 0x00034101
SCENERY_INFOS_CHUNK_ID = 0x00034102
SCENERY_INSTANCES_CHUNK_ID = 0x00034103

RTNODE_CHUNK_ID = 0x00037250
SENTINEL = 0x11111111

# --- world_anim rtnode (confirmed-correct layout; see dump_worldanim_allsections.py) ---
RTNODE_FMT = "<I16fIbBBBIIIIIhhhHIIhHBBh"
RTNODE_SIZE = struct.calcsize(RTNODE_FMT)
assert RTNODE_SIZE == 0x78, RTNODE_SIZE

RTNODE_FIELDS = [
    "sentinel",
    *[f"m{i}" for i in range(16)],
    "key", "parent_index", "time_scale", "flags", "pad0",
    "solid_key0", "solid_key1", "solid_key2",
    "smackable_key", "scenery_guid",
    "rot_speed_x", "rot_speed_y", "rot_speed_z",
    "section_number", "key_frame_count", "bits",
    "initial_angle", "unknown_0x6e", "use_parent_frames",
    "pad1", "pad2",
]

# --- scenery section structs (Common/Scenery/CarbonScenery.cs) ---
SCENERY_HEADER_FMT = "<qiiiqqqqq"
SCENERY_HEADER_SIZE = struct.calcsize(SCENERY_HEADER_FMT)
assert SCENERY_HEADER_SIZE == 0x3C, SCENERY_HEADER_SIZE  # 60

SCENERY_INFO_FMT = "<24s4I4IfIII"
SCENERY_INFO_SIZE = struct.calcsize(SCENERY_INFO_FMT)
assert SCENERY_INFO_SIZE == 0x48, SCENERY_INFO_SIZE  # 72

SCENERY_INSTANCE_FMT = "<3fI3fhh3f9fIhQH"
SCENERY_INSTANCE_SIZE = struct.calcsize(SCENERY_INSTANCE_FMT)
assert SCENERY_INSTANCE_SIZE == 0x60, SCENERY_INSTANCE_SIZE  # 96


def parse_rtnode(payload):
    vals = struct.unpack(RTNODE_FMT, payload)
    rec = dict(zip(RTNODE_FIELDS, vals))
    bits = rec["bits"]
    rec["bits_frame_count_field"] = bits & 0x3FFFFFFF
    rec["is_ping_pong"] = bool(bits & 0x40000000)
    rec["is_looping"] = bool(bits & 0x80000000)
    flags = rec["flags"]
    rec["is_library_anim"] = bool(flags & 0x01)
    rec["use_library_anim"] = bool(flags & 0x02)
    rec["use_parent_anim"] = bool(flags & 0x04)
    # Untested hypothesis, from real CWorldAnimCtrl::AdvanceAnimTime bit layout
    # (0x08 linear / 0x10 pingpong / 0x20 loop) - printed raw, not asserted.
    rec["flag_bit_0x08"] = bool(flags & 0x08)
    rec["flag_bit_0x10"] = bool(flags & 0x10)
    rec["flag_bit_0x20"] = bool(flags & 0x20)
    rec["flag_bit_0x40"] = bool(flags & 0x40)
    rec["flag_bit_0x80"] = bool(flags & 0x80)
    return rec


def scan_rtnodes(mm):
    pattern = struct.pack("<I", RTNODE_CHUNK_ID)
    pos = 0
    records = []
    while True:
        idx = mm.find(pattern, pos)
        if idx == -1:
            break
        pos = idx + 1
        if idx + 8 > len(mm):
            continue
        chunk_size = struct.unpack_from("<I", mm, idx + 4)[0]
        if chunk_size != RTNODE_SIZE:
            continue
        payload = mm[idx + 8: idx + 8 + chunk_size]
        if len(payload) != RTNODE_SIZE:
            continue
        rec = parse_rtnode(payload)
        if rec["sentinel"] != SENTINEL:
            continue
        records.append((idx, rec))
    return records


def build_world_anim_by_guid(rtnode_records):
    """Mirrors ExportBundleCommand.cs: GroupBy(SceneryGuid).First(), in
    file order, only for guid != 0."""
    by_guid = {}
    for _, rec in rtnode_records:
        guid = rec["scenery_guid"]
        if guid == 0:
            continue
        if guid not in by_guid:
            by_guid[guid] = rec
    return by_guid


def build_world_anim_by_key(rtnode_records):
    """Mirrors the confirmed find_library_tree logic: a borrowing node's
    solid_keys[0] is matched against a library node's own `key` field -
    NOT scenery_guid. Used to find the real motion source for a
    use_library_anim node whose own record looks like a placeholder."""
    by_key = {}
    for _, rec in rtnode_records:
        by_key[rec["key"]] = rec
    return by_key


def decode_name(raw24):
    return raw24.split(b"\x00", 1)[0].decode("latin-1", errors="replace")


def scan_scenery_sections(mm):
    pattern = struct.pack("<I", SCENERY_SECTION_CHUNK_ID)
    pos = 0
    sections = []
    while True:
        idx = mm.find(pattern, pos)
        if idx == -1:
            break
        pos = idx + 1
        if idx + 8 > len(mm):
            continue
        container_size = struct.unpack_from("<I", mm, idx + 4)[0]
        payload_start = idx + 8
        payload_end = payload_start + container_size
        if payload_end > len(mm):
            continue

        section_number = None
        infos = []
        instances = []

        sub_pos = payload_start
        while sub_pos < payload_end:
            if sub_pos + 8 > payload_end:
                break
            sub_id = struct.unpack_from("<I", mm, sub_pos)[0]
            sub_size = struct.unpack_from("<I", mm, sub_pos + 4)[0]
            sub_payload_start = sub_pos + 8
            sub_payload_end = sub_payload_start + sub_size
            if sub_payload_end > payload_end:
                break

            if sub_id == SCENERY_HEADER_CHUNK_ID and sub_size >= SCENERY_HEADER_SIZE:
                h = struct.unpack_from(SCENERY_HEADER_FMT, mm, sub_payload_start)
                section_number = h[2]  # Pointer1, Pointer2, SectionNumber, ...

            elif sub_id == SCENERY_INFOS_CHUNK_ID:
                count = sub_size // SCENERY_INFO_SIZE
                for i in range(count):
                    off = sub_payload_start + i * SCENERY_INFO_SIZE
                    vals = struct.unpack_from(SCENERY_INFO_FMT, mm, off)
                    name = decode_name(vals[0])
                    solid_key = vals[1]  # SolidMeshKey1
                    infos.append({"name": name, "solid_key": solid_key})

            elif sub_id == SCENERY_INSTANCES_CHUNK_ID:
                # ReadSceneryInstances aligns to 0x10 (absolute file offset)
                # before reading, and reduces the usable size accordingly.
                pad = (-sub_payload_start) % 0x10
                aligned_start = sub_payload_start + pad
                usable = sub_payload_end - aligned_start
                count = usable // SCENERY_INSTANCE_SIZE
                for i in range(count):
                    off = aligned_start + i * SCENERY_INSTANCE_SIZE
                    vals = struct.unpack_from(SCENERY_INSTANCE_FMT, mm, off)
                    # 3f(bbox_min) I(flags) 3f(bbox_max) h h(precull,light)
                    # 3f(position) 9f(rotation 3x3) I(guid) h(info#) Q H(pad)
                    position = vals[9:12]
                    rot9 = vals[12:21]
                    scenery_guid = vals[21]
                    info_index = vals[22]
                    instances.append({
                        "info_index": info_index,
                        "scenery_guid": scenery_guid,
                        "position": position,
                        "rotation3x3": rot9,
                    })

            sub_pos = sub_payload_end

        sections.append({
            "offset": idx,
            "section_number": section_number,
            "infos": infos,
            "instances": instances,
        })
    return sections


def print_rtnode_summary(rec, indent="    "):
    print(f"{indent}world_anim match: key=0x{rec['key']:08X} "
          f"section={rec['section_number']}")
    print(f"{indent}  flags byte = 0x{rec['flags']:02X} "
          f"(binary {rec['flags']:08b})")
    print(f"{indent}    is_library_anim(0x01)={rec['is_library_anim']} "
          f"use_library_anim(0x02)={rec['use_library_anim']} "
          f"use_parent_anim(0x04)={rec['use_parent_anim']}")
    print(f"{indent}    UNTESTED: bit0x08={rec['flag_bit_0x08']} "
          f"bit0x10={rec['flag_bit_0x10']} bit0x20={rec['flag_bit_0x20']} "
          f"bit0x40={rec['flag_bit_0x40']} bit0x80={rec['flag_bit_0x80']}")
    print(f"{indent}  bits u32 = 0x{rec['bits']:08X}  "
          f"is_ping_pong={rec['is_ping_pong']} is_looping={rec['is_looping']} "
          f"frame_count_bits={rec['bits_frame_count_field']}")
    print(f"{indent}  key_frame_count={rec['key_frame_count']}  "
          f"rotation_speed=({rec['rot_speed_x']},{rec['rot_speed_y']},"
          f"{rec['rot_speed_z']})  initial_angle={rec['initial_angle']}  "
          f"time_scale={rec['time_scale']}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("file", help="Path to stream file (e.g. STREAML5RA.BUN)")
    ap.add_argument("--section", type=int, default=None,
                     help="Only show this ScenerySection number")
    ap.add_argument("--name-contains", default=None,
                     help="Only show instances whose info name contains this "
                          "(case-insensitive substring, e.g. CarLotFlags)")
    args = ap.parse_args()

    try:
        f = open(args.file, "rb")
    except OSError as e:
        print(f"Error opening file '{args.file}': {e}", file=sys.stderr)
        sys.exit(1)

    with f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        rtnode_records = scan_rtnodes(mm)
        by_guid = build_world_anim_by_guid(rtnode_records)
        by_key = build_world_anim_by_key(rtnode_records)
        sections = scan_scenery_sections(mm)

    print(f"Found {len(sections)} scenery sections, "
          f"{len(rtnode_records)} rtnode records "
          f"({len(by_guid)} unique non-zero scenery guids)\n")

    name_filter = args.name_contains.lower() if args.name_contains else None

    for sec in sections:
        if args.section is not None and sec["section_number"] != args.section:
            continue
        shown_header = False
        for idx, inst in enumerate(sec["instances"]):
            info = None
            if 0 <= inst["info_index"] < len(sec["infos"]):
                info = sec["infos"][inst["info_index"]]
            name = info["name"] if info else "<no matching info>"

            if name_filter and name_filter not in name.lower():
                continue

            if not shown_header:
                print(f"=== ScenerySection {sec['section_number']} "
                      f"@ 0x{sec['offset']:08X} "
                      f"({len(sec['infos'])} infos, "
                      f"{len(sec['instances'])} instances) ===")
                shown_header = True

            print(f"  instance[{idx}] name='{name}' "
                  f"solid_key=0x{(info['solid_key'] if info else 0):08X} "
                  f"scenery_guid=0x{inst['scenery_guid']:08X}")
            print(f"    position={tuple(round(v, 2) for v in inst['position'])}")

            anim = by_guid.get(inst["scenery_guid"]) if inst["scenery_guid"] else None
            if anim:
                print_rtnode_summary(anim)
                if anim["solid_key0"] != anim["key"]:
                    lib = by_key.get(anim["solid_key0"])
                    if lib:
                        print("    --- library candidate (key == this node's solid_key0) ---")
                        print_rtnode_summary(lib, indent="      ")
                    else:
                        print(f"    (no rtnode anywhere has key == 0x{anim['solid_key0']:08X})")
            else:
                print("    world_anim match: NONE "
                      "(no rtnode anywhere in the file has this scenery_guid)")
            print()


if __name__ == "__main__":
    main()
