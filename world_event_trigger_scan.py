#!/usr/bin/env python3
"""
world_event_trigger_scan_v2.py

Concrete scanner for Carbon's world-event trigger system:

    0x80036000  EventTriggerPack (nested)
      0x00036001  Header
      0x00036002  AABB tree
      0x00036003  EventTrigger records

IMPORTANT:
- bChunk::GetAlignedData(16) is the real data pointer.
- The bytes between the chunk payload start and the 16-byte-aligned
  data pointer are BCHUNK_ALIGNMENT_PADDING (0x11), NOT part of the
  structure.
- bChunk::GetAlignedSize(16) is the real structure byte count.
- The AABB tree is broad-phase spatial indexing. Its leaves reference
  concrete EventTrigger record indices; the useful semantic data lives
  in those records.

Usage:
    python world_event_trigger_scan_v2.py STREAML5RA.BUN
    python world_event_trigger_scan_v2.py STREAML5RA.BUN --max-packs 10
    python world_event_trigger_scan_v2.py STREAML5RA.BUN --leaf-samples 8
    python world_event_trigger_scan_v2.py STREAML5RA.BUN --out dump.txt
"""

import argparse
import mmap
import struct
import sys
from collections import Counter


CHUNK_IDS = {
    0x80036000: "event_trigger_pack",
    0x00036001: "event_trigger_header",
    0x00036002: "event_trigger_tree",
    0x00036003: "event_trigger_instances",
}

PACK_MIN_LEN = 16
PACK_MAX_LEN = 2_000_000

ALIGNMENT = 16
TREE_HEADER_SIZE = 0x10
TREE_NODE_SIZE = 0x30
TRIGGER_SIZE = 0x20

BCHUNK_ALIGNMENT_PADDING = 0x11111111


EVENT_IDS = {
    0x00010003: "CAR_ON_FERN",
    0x00010005: "VIEW_DRIVING_LINE",
    0x00010006: "ACTIVATE_TRAIN",
    0x00010007: "SOUND",
    0x00010008: "GUIDE_ARROW",
    0x00010009: "ACTIVATE_PLANE",
    0x00020000: "INITIATE_PURSUIT",
    0x00020001: "CALL_FOR_BACKUP",
    0x00020002: "CALL_FOR_ROADBLOCK",
    0x00020003: "STRATEGY_INITIATE",
    0x00020004: "COLLISION",
    0x00020005: "ANNOUNCE_ARREST",
    0x00020006: "STRATEGY_OUTCOME",
    0x00020007: "ROADBLOCK_UPDATE",
    0x00020008: "CANCEL_PURSUIT",
    0x00040000: "START_SIREN",
    0x00040001: "STOP_SIREN",
}

PARAMETERS = {
    0x001D5D4F: "TREE",
    0x2C15BD86: "TUNNEL_ENTRY",
    0x495F75B6: "INTERSECTION",
    0x72F66B23: "PILLAR",
    0xB2B5A6E3: "FOUNTAIN",
    0xB6BD9C95: "TRAFFIC_LIGHT",
    0xF3ABE1C2: "FREEWAY_SIGN",
    0xF40A48EF: "LAMPPOST",
}


def align_up(value, alignment=ALIGNMENT):
    return (value + alignment - 1) & ~(alignment - 1)


def aligned_data_offset(chunk_payload_offset, chunk_payload_end, alignment=ALIGNMENT):
    """
    Equivalent to:
        bChunk::GetAlignedData(16)

    Returns the absolute file offset of the real structure data.
    """
    data_offset = align_up(chunk_payload_offset, alignment)
    if data_offset > chunk_payload_end:
        return None
    return data_offset


def aligned_data_size(chunk_payload_offset, chunk_payload_end, alignment=ALIGNMENT):
    """
    Equivalent to:
        bChunk::GetAlignedSize(16)
    """
    data_offset = aligned_data_offset(
        chunk_payload_offset, chunk_payload_end, alignment
    )
    if data_offset is None:
        return 0
    return chunk_payload_end - data_offset


def find_candidates(mm, chunk_id, min_len, max_len):
    pattern = struct.pack("<I", chunk_id)
    pos = 0
    results = []
    while True:
        idx = mm.find(pattern, pos)
        if idx == -1:
            break
        pos = idx + 1
        if idx + 8 > len(mm):
            continue
        length = struct.unpack_from("<I", mm, idx + 4)[0]
        if min_len <= length <= max_len and idx + 8 + length <= len(mm):
            results.append((idx, length))
    return results


def decode_header(mm, data_offset, data_size):
    if data_size < 0x20:
        raise ValueError(f"header data too small: {data_size}")
    next_ptr, prev_ptr, version, scenery_section, num_triggers, endian_swapped, tree_ptr, arr_ptr = \
        struct.unpack_from("<8I", mm, data_offset)
    return {
        "next_ptr": next_ptr,
        "prev_ptr": prev_ptr,
        "version": version,
        "scenery_section": scenery_section,
        "num_triggers": num_triggers,
        "endian_swapped": endian_swapped,
        "tree_ptr": tree_ptr,
        "arr_ptr": arr_ptr,
    }


def decode_tree_header(mm, data_offset, data_size):
    if data_size < TREE_HEADER_SIZE:
        raise ValueError(f"tree data too small: {data_size}")
    node_array_ptr, num_leaf, num_parent, total_nodes, depth, pad1 = \
        struct.unpack_from("<IhhhhI", mm, data_offset)
    node_bytes = data_size - TREE_HEADER_SIZE
    real_node_count = node_bytes // TREE_NODE_SIZE
    remainder = node_bytes % TREE_NODE_SIZE
    return {
        "node_array_ptr": node_array_ptr,
        "num_leaf": num_leaf,
        "num_parent": num_parent,
        "total_nodes": total_nodes,
        "depth": depth,
        "pad1": pad1,
        "real_node_count": real_node_count,
        "node_remainder": remainder,
    }


def decode_tree_node(mm, node_offset):
    px, py, pz, parent_index, num_children, ex, ey, ez = \
        struct.unpack_from("<3fhh3f", mm, node_offset)
    children = struct.unpack_from("<10h", mm, node_offset + 0x1C)
    return {
        "position": (px, py, pz),
        "parent_index": parent_index,
        "num_children": num_children,
        "extent": (ex, ey, ez),
        "children": list(children),
    }


def decode_instance(mm, rec_off):
    name_hash, event_id, parameter, track_mask = struct.unpack_from(
        "<4I", mm, rec_off
    )
    px, py, pz, radius = struct.unpack_from(
        "<4f", mm, rec_off + 0x10
    )
    return {
        "name_hash": name_hash,
        "event_id": event_id,
        "parameter": parameter,
        "track_mask": track_mask,
        "position": (px, py, pz),
        "radius": radius,
    }


def trigger_label(trigger):
    ev = EVENT_IDS.get(trigger["event_id"], f"0x{trigger['event_id']:08X}")
    param = PARAMETERS.get(trigger["parameter"], f"0x{trigger['parameter']:08X}")
    x, y, z = trigger["position"]
    return (
        f"idx=? hash=0x{trigger['name_hash']:08X} "
        f"event={ev} param={param} mask={trigger['track_mask']} "
        f"pos=({x:.2f},{y:.2f},{z:.2f}) r={trigger['radius']:.2f}"
    )


def describe_trigger(index, trigger):
    ev = EVENT_IDS.get(trigger["event_id"], "?")
    param = PARAMETERS.get(trigger["parameter"], "?")
    x, y, z = trigger["position"]
    return (
        f"trigger[{index}] "
        f"hash=0x{trigger['name_hash']:08X} "
        f"event=0x{trigger['event_id']:08X}({ev}) "
        f"param=0x{trigger['parameter']:08X}({param}) "
        f"mask={trigger['track_mask']} "
        f"pos=({x:.2f},{y:.2f},{z:.2f}) "
        f"radius={trigger['radius']:.2f}"
    )


def walk_pack(
    p,
    mm,
    pack_offset,
    pack_length,
    max_instances_shown,
    leaf_samples,
):
    payload_start = pack_offset + 8
    payload_end = payload_start + pack_length

    p(f"event_trigger_pack @ 0x{pack_offset:08X}  payload_length={pack_length}")

    pos = payload_start
    header = None
    tree = None
    tree_nodes = None
    triggers = None

    while pos + 8 <= payload_end:
        chunk_id, chunk_len = struct.unpack_from("<2I", mm, pos)
        chunk_payload = pos + 8
        chunk_end = chunk_payload + chunk_len

        if chunk_end > payload_end:
            p(
                f"  [!] chunk 0x{chunk_id:08X} @ 0x{pos:08X} claims "
                f"length {chunk_len}, overruns pack end - stopping walk"
            )
            break

        data_offset = aligned_data_offset(chunk_payload, chunk_end, ALIGNMENT)
        data_size = aligned_data_size(chunk_payload, chunk_end, ALIGNMENT)

        if data_offset is None:
            p(f"  [!] chunk 0x{chunk_id:08X}: alignment exceeds payload")
            break

        padding_len = data_offset - chunk_payload
        padding_value = None
        if padding_len >= 4:
            padding_value = struct.unpack_from("<I", mm, chunk_payload)[0]

        name = CHUNK_IDS.get(chunk_id, f"unknown_0x{chunk_id:08X}")

        if chunk_id == 0x00036001:
            try:
                header = decode_header(mm, data_offset, data_size)
            except (struct.error, ValueError) as exc:
                p(f"  [!] header decode failed: {exc}")
                header = None
            else:
                p(
                    f"  header @ 0x{pos:08X}: data=0x{data_offset:08X} "
                    f"data_size={data_size} version={header['version']} "
                    f"scenery_section={header['scenery_section']} "
                    f"num_triggers={header['num_triggers']} "
                    f"endian_swapped={header['endian_swapped']}"
                )

        elif chunk_id == 0x00036002:
            try:
                tree = decode_tree_header(mm, data_offset, data_size)
            except (struct.error, ValueError) as exc:
                p(f"  [!] tree decode failed: {exc}")
                tree = None
            else:
                total_match = tree["real_node_count"] == tree["total_nodes"]
                rem_ok = tree["node_remainder"] == 0
                p(
                    f"  tree @ 0x{pos:08X}: data=0x{data_offset:08X} "
                    f"data_size={data_size} "
                    f"declared total_nodes={tree['total_nodes']} "
                    f"(leaf={tree['num_leaf']}, parent={tree['num_parent']}, "
                    f"depth={tree['depth']}) "
                    f"payload implies {tree['real_node_count']} nodes "
                    f"remainder={tree['node_remainder']} -> "
                    f"{'OK' if total_match and rem_ok else 'MISMATCH'}"
                )

        elif chunk_id == 0x00036003:
            if data_size % TRIGGER_SIZE != 0:
                p(
                    f"  [!] instances @ 0x{pos:08X}: aligned data size "
                    f"{data_size} is not a multiple of 0x20"
                )
            instance_count = data_size // TRIGGER_SIZE
            triggers = []
            for i in range(instance_count):
                try:
                    triggers.append(
                        decode_instance(mm, data_offset + i * TRIGGER_SIZE)
                    )
                except struct.error as exc:
                    p(f"  [!] instance[{i}] decode failed: {exc}")
                    break

            p(
                f"  instances @ 0x{pos:08X}: data=0x{data_offset:08X} "
                f"aligned_size={data_size} {len(triggers)} record(s)"
            )

            for i, trigger in enumerate(triggers[:max_instances_shown]):
                ev_label = EVENT_IDS.get(trigger["event_id"], "?")
                param_label = PARAMETERS.get(trigger["parameter"], "?")
                x, y, z = trigger["position"]
                p(
                    f"    [{i}] hash=0x{trigger['name_hash']:08X} "
                    f"event=0x{trigger['event_id']:08X}({ev_label}) "
                    f"param=0x{trigger['parameter']:08X}({param_label}) "
                    f"trackmask={trigger['track_mask']} "
                    f"pos=({x:.2f},{y:.2f},{z:.2f}) "
                    f"radius={trigger['radius']:.2f}"
                )

        else:
            p(
                f"  [?] {name} @ 0x{pos:08X}, length={chunk_len}, "
                f"aligned_data=0x{data_offset:08X}, aligned_size={data_size}"
            )

        # For debugging only: show that 0x11111111 bytes are alignment
        # padding, not part of the logical structure.
        if padding_len and padding_value == BCHUNK_ALIGNMENT_PADDING:
            p(
                f"      alignment padding: {padding_len} byte(s) "
                f"of 0x{BCHUNK_ALIGNMENT_PADDING:08X}"
            )
        elif padding_len:
            p(f"      alignment padding: {padding_len} byte(s)")

        pos = chunk_end

    # ---- Correlate actual tree leaves to actual trigger records. ----
    if tree is not None:
        actual_nodes = tree["real_node_count"]
        if actual_nodes > 0 and tree["node_remainder"] == 0:
            nodes = []
            node_base = None

            # Find the tree chunk again so we have its aligned data pointer.
            q = payload_start
            while q + 8 <= payload_end:
                cid, clen = struct.unpack_from("<2I", mm, q)
                cpay = q + 8
                cend = cpay + clen
                if cend > payload_end:
                    break
                if cid == 0x00036002:
                    node_base = aligned_data_offset(cpay, cend, ALIGNMENT)
                    break
                q = cend

            if node_base is not None:
                for i in range(actual_nodes):
                    nodes.append(
                        decode_tree_node(mm, node_base + TREE_HEADER_SIZE + i * TREE_NODE_SIZE)
                    )
                tree_nodes = nodes

                parent_count = sum(n["num_children"] > 0 for n in nodes)
                leaf_count = sum(n["num_children"] <= 0 for n in nodes)
                p(
                    f"  tree semantic scan: actual_parent_nodes={parent_count} "
                    f"actual_leaf_nodes={leaf_count}"
                )

                if tree["num_parent"] != parent_count or tree["num_leaf"] != leaf_count:
                    p(
                        f"    [!] tree header counts differ from decoded topology: "
                        f"header parent={tree['num_parent']} leaf={tree['num_leaf']}"
                    )

                invalid_parent_refs = 0
                invalid_leaf_refs = 0
                referenced_trigger_indices = []
                leaf_dumped = 0

                for ni, node in enumerate(nodes):
                    nchild = node["num_children"]
                    if nchild > 0:
                        for ref in node["children"][:nchild]:
                            if ref < 0 or ref >= actual_nodes:
                                invalid_parent_refs += 1
                    else:
                        hit_count = -nchild if nchild < 0 else 0
                        refs = node["children"][:hit_count]
                        for ref in refs:
                            if triggers is None or ref < 0 or ref >= len(triggers):
                                invalid_leaf_refs += 1
                            else:
                                referenced_trigger_indices.append(ref)

                        if hit_count and leaf_dumped < leaf_samples:
                            x, y, z = node["position"]
                            ex, ey, ez = node["extent"]
                            p(
                                f"    leaf node[{ni}]: hits={hit_count} "
                                f"center=({x:.2f},{y:.2f},{z:.2f}) "
                                f"extent=({ex:.2f},{ey:.2f},{ez:.2f}) "
                                f"trigger_refs={refs}"
                            )
                            if triggers is not None:
                                for ref in refs:
                                    if 0 <= ref < len(triggers):
                                        p(f"      -> {describe_trigger(ref, triggers[ref])}")
                            leaf_dumped += 1

                p(
                    f"  tree references: {len(referenced_trigger_indices)} "
                    f"leaf->trigger references; "
                    f"invalid_parent_refs={invalid_parent_refs} "
                    f"invalid_leaf_refs={invalid_leaf_refs}"
                )

                if triggers is not None:
                    unique_refs = set(referenced_trigger_indices)
                    missing_refs = [
                        i for i in range(len(triggers))
                        if i not in unique_refs
                    ]
                    duplicate_refs = (
                        len(referenced_trigger_indices) - len(unique_refs)
                    )

                    p(
                        f"  trigger coverage: {len(unique_refs)}/{len(triggers)} "
                        f"unique trigger records referenced by tree; "
                        f"duplicate_tree_refs={duplicate_refs}"
                    )
                    if missing_refs:
                        preview = missing_refs[:20]
                        suffix = " ..." if len(missing_refs) > 20 else ""
                        p(
                            f"    unreferenced trigger indices: "
                            f"{preview}{suffix}"
                        )

    if header is not None and triggers is not None:
        match = header["num_triggers"] == len(triggers)
        p(
            f"  header.num_triggers={header['num_triggers']} vs decoded "
            f"instance count={len(triggers)} -> {'OK' if match else 'MISMATCH'}"
        )

        event_counts = Counter(t["event_id"] for t in triggers)
        parameter_counts = Counter(t["parameter"] for t in triggers)

        if event_counts:
            p("  event summary:")
            for event_id, count in event_counts.most_common():
                p(
                    f"    0x{event_id:08X} "
                    f"{EVENT_IDS.get(event_id, '?'):<20} {count}"
                )

        if parameter_counts:
            p("  parameter summary:")
            for parameter, count in parameter_counts.most_common():
                p(
                    f"    0x{parameter:08X} "
                    f"{PARAMETERS.get(parameter, '?'):<20} {count}"
                )

    p()


def scan_file(args):
    out_fh = (
        open(args.out, "w", encoding="utf-8")
        if args.out
        else sys.stdout
    )

    def p(*a, **kw):
        print(*a, file=out_fh, **kw)

    try:
        with open(args.file, "rb") as f:
            mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
            try:
                p(
                    f"# Scanning {args.file} ({len(mm):,} bytes) "
                    f"for event_trigger_pack (0x80036000)"
                )
                p(
                    "# Child data is decoded at bChunk::GetAlignedData(16), "
                    "not raw chunk_payload."
                )
                p()

                packs = find_candidates(
                    mm,
                    0x80036000,
                    PACK_MIN_LEN,
                    PACK_MAX_LEN,
                )
                p(
                    f"event_trigger_pack: {len(packs)} candidate(s) found "
                    f"(length capped at {PACK_MAX_LEN:,} bytes)"
                )
                p()

                p(f"## Condensed offset table (first {args.summary_limit})")
                for offset, length in packs[:args.summary_limit]:
                    p(f"0x{offset:08X}   length={length}")
                p()

                p(
                    "## Detailed pack walks "
                    "(header -> aligned tree -> aligned instances -> "
                    "tree/trigger correlation)"
                )
                for offset, length in packs[:args.max_packs]:
                    p("=" * 76)
                    walk_pack(
                        p,
                        mm,
                        offset,
                        length,
                        args.max_instances_per_pack,
                        args.leaf_samples,
                    )

            finally:
                mm.close()
    finally:
        if args.out:
            out_fh.close()
            print(f"Wrote output to {args.out}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "file",
        help="Path to the stream file (e.g. STREAML5RA.BUN)",
    )
    ap.add_argument(
        "--max-packs",
        type=int,
        default=10,
        help="How many packs to show in full detail (default 10)",
    )
    ap.add_argument(
        "--max-instances-per-pack",
        type=int,
        default=5,
        help="Trigger records to print per pack (default 5)",
    )
    ap.add_argument(
        "--leaf-samples",
        type=int,
        default=5,
        help="Leaf nodes to expand with trigger references (default 5)",
    )
    ap.add_argument(
        "--summary-limit",
        type=int,
        default=200,
    )
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    scan_file(args)


if __name__ == "__main__":
    main()
