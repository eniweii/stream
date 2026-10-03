#!/usr/bin/env python3
"""
parse_2400_anims.py - Section 2400 Procedural Motion Parser

Complete 120-byte payload parser for Section 2400 (0x0960) WANIM_RTNODE entries.
Extracts 4x4 matrix basis vectors, entity hash linkages, procedural parameters,
and tail padding.
"""

import argparse
import mmap
import struct
import sys

CHUNK_RTNODE = 0x00037250
TARGET_SECTION = 2400  # 0x0960


def parse_2400_payload(payload):
    if len(payload) < 0x78:
        return None

    # +0x00: Header Type Signature (uint32)
    type_sig = struct.unpack_from("<I", payload, 0x00)[0]

    # +0x04..+0x43: 4x4 Transformation Matrix (16 floats, 64 bytes)
    m = struct.unpack_from("<16f", payload, 0x04)
    axis_x = (m[0], m[1], m[2])       # Matrix Row 0: Local X (Right)
    axis_y = (m[4], m[5], m[6])       # Matrix Row 1: Local Y (Up)
    axis_z = (m[8], m[9], m[10])      # Matrix Row 2: Local Z (Forward/Rotation Axis)
    base_pos = (m[12], m[13], m[14])  # Matrix Row 3: Translation Origin (X, Y, Z)

    # +0x44..+0x5F: Identifiers, Hashes & Scenery GUID
    key_hash = struct.unpack_from("<I", payload, 0x44)[0]
    node_flags = struct.unpack_from("<I", payload, 0x48)[0]
    parent_hash = struct.unpack_from("<I", payload, 0x4C)[0]
    sub_hash_1 = struct.unpack_from("<I", payload, 0x50)[0]
    sub_hash_2 = struct.unpack_from("<I", payload, 0x54)[0]
    padding_58 = struct.unpack_from("<I", payload, 0x58)[0]
    scenery_guid = struct.unpack_from("<I", payload, 0x5C)[0]

    # +0x60..+0x77: Procedural Parameters & Flags
    param_extra = struct.unpack_from("<I", payload, 0x60)[0]
    anim_speed = struct.unpack_from("<H", payload, 0x64)[0]
    section_number = struct.unpack_from("<H", payload, 0x66)[0]
    key_frame_count = struct.unpack_from("<I", payload, 0x68)[0]
    flags = struct.unpack_from("<I", payload, 0x6C)[0]
    param_phase = struct.unpack_from("<I", payload, 0x70)[0]
    tail_pad = struct.unpack_from("<I", payload, 0x74)[0]

    return {
        "type_sig": type_sig,
        "axis_x": axis_x,
        "axis_y": axis_y,
        "axis_z": axis_z,
        "base_pos": base_pos,
        "key_hash": key_hash,
        "node_flags": node_flags,
        "parent_hash": parent_hash,
        "sub_hash_1": sub_hash_1,
        "sub_hash_2": sub_hash_2,
        "padding_58": padding_58,
        "scenery_guid": scenery_guid,
        "param_extra": param_extra,
        "anim_speed": anim_speed,
        "section": section_number,
        "frames": key_frame_count,
        "flags": flags,
        "param_phase": param_phase,
        "tail_pad": tail_pad,
    }


def dump_hex_bytes(payload, offset_base):
    """Prints 120-byte rtnode payload in structured 16-byte hex rows."""
    print("  └─ Payload Hex Dump (120 bytes):")
    for i in range(0, len(payload), 16):
        row = payload[i : i + 16]
        hex_str = " ".join(f"{b:02X}" for b in row)
        ascii_str = "".join(chr(b) if 32 <= b <= 126 else "." for b in row)
        print(f"     0x{offset_base + i:08X} (+0x{i:02X}):  {hex_str:<48}  |{ascii_str}|")


def main():
    ap = argparse.ArgumentParser(
        description="Parse Section 2400 procedural world animations with full 120-byte payload alignment."
    )
    ap.add_argument("file", help="Path to stream file (e.g. STREAML5RA.BUN)")
    ap.add_argument(
        "--dump-hex", action="store_true", help="Dump raw hex payload for binary inspection"
    )
    args = ap.parse_args()

    try:
        f = open(args.file, "rb")
    except OSError as e:
        print(f"Error opening file '{args.file}': {e}", file=sys.stderr)
        sys.exit(1)

    with f:
        try:
            mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        except ValueError:
            print("Error: File is empty.", file=sys.stderr)
            sys.exit(1)

        rtnode_pattern = struct.pack("<I", CHUNK_RTNODE)
        pos = 0
        nodes = []

        while True:
            idx = mm.find(rtnode_pattern, pos)
            if idx == -1:
                break
            pos = idx + 1

            if idx + 8 > len(mm):
                continue

            length = struct.unpack_from("<I", mm, idx + 4)[0]
            if length != 0x78:
                continue

            payload = mm[idx + 8 : idx + 8 + length]
            node_data = parse_2400_payload(payload)

            if node_data and node_data["section"] == TARGET_SECTION:
                nodes.append((idx, payload, node_data))

        print("=== Section 2400 Procedural Animation Parser ===")
        print(f"File: {args.file}")
        print(f"Found {len(nodes)} Section 2400 objects\n")

        header_str = f"{'Offset':<12} {'Key Hash':<12} {'GUID':<12} {'Speed':<8} {'Extra':<8} {'Phase':<8} {'TailPad':<8} {'Position (X, Y, Z)':<30}"
        print(header_str)
        print("-" * len(header_str))

        for offset, payload, data in nodes:
            pos_str = f"({data['base_pos'][0]:.2f}, {data['base_pos'][1]:.2f}, {data['base_pos'][2]:.2f})"
            print(
                f"0x{offset:08X}   0x{data['key_hash']:08X}   0x{data['scenery_guid']:08X}   "
                f"{data['anim_speed']:<8} {data['param_extra']:<8} {data['param_phase']:<8} {data['tail_pad']:<8} {pos_str:<30}"
            )

            z_str = f"({data['axis_z'][0]:.4f}, {data['axis_z'][1]:.4f}, {data['axis_z'][2]:.4f})"
            print(f"  ├─ Rotation Axis Vector (Local Z): {z_str}")
            print(
                f"  ├─ Hashes: Parent=0x{data['parent_hash']:08X}, "
                f"Sub1=0x{data['sub_hash_1']:08X}, Sub2=0x{data['sub_hash_2']:08X}"
            )

            if args.dump_hex:
                dump_hex_bytes(payload, offset + 8)
                print()

        mm.close()


if __name__ == "__main__":
    main()