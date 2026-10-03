#!/usr/bin/env python3
"""
parse_2600_anims.py - Section 2600 Persistent Motion & Hex Dump Parser

Scans stream files for Section 2600 rtnode entries, locates forward-adjacent
WANIM_FRAMES (0x00037240) payloads, extracts translation coordinates, and optional hex dumps.
"""

import argparse
import mmap
import struct
import sys

CHUNK_RTNODE = 0x00037250
CHUNK_FRAMES = 0x00037240
TARGET_SECTION = 2600  # 0x0A28


def parse_rtnode_payload(payload):
    if len(payload) < 0x78:
        return None

    base_matrix = struct.unpack_from("<16f", payload, 0x04)
    key = struct.unpack_from("<I", payload, 0x44)[0]
    scenery_guid = struct.unpack_from("<I", payload, 0x5C)[0]
    section_number = struct.unpack_from("<H", payload, 0x66)[0]
    key_frame_count = struct.unpack_from("<I", payload, 0x68)[0]
    bits = struct.unpack_from("<I", payload, 0x6C)[0]

    return {
        "key": key,
        "scenery_guid": scenery_guid,
        "section_number": section_number,
        "key_frame_count": key_frame_count,
        "is_looping": bool(bits & 0x80000000),
        "base_pos": (base_matrix[12], base_matrix[13], base_matrix[14]),
    }


def find_forward_companion_frames(mm, rtnode_offset, rtnode_len):
    """Locates the WANIM_FRAMES (0x00037240) chunk immediately following WANIM_RTNODE."""
    expected_start = rtnode_offset + 8 + rtnode_len
    # Allow up to 64 bytes for alignment padding
    search_end = min(len(mm), expected_start + 64)

    pattern = struct.pack("<I", CHUNK_FRAMES)
    idx = mm.find(pattern, expected_start, search_end)

    if idx != -1 and idx + 8 <= len(mm):
        chunk_len = struct.unpack_from("<I", mm, idx + 4)[0]
        return idx + 8, chunk_len

    return None, 0


def read_frame_positions(mm, frame_payload_offset, frame_count, max_samples=3):
    """Reads translation vectors from keyframe 4x4 matrices."""
    samples = []
    if frame_count <= 0:
        return samples

    indices = [0]
    if frame_count > 1:
        indices.append(frame_count // 2)
        indices.append(frame_count - 1)

    indices = sorted(list(set(indices)))[:max_samples]

    for idx in indices:
        mat_offset = frame_payload_offset + (idx * 64)
        if mat_offset + 64 <= len(mm):
            mat = struct.unpack_from("<16f", mm, mat_offset)
            # Checked matrix indices [13, 14, 15] based on stream alignment
            samples.append((idx, mat[13], mat[14], mat[15]))

    return samples


def dump_hex_bytes(mm, offset, length, title="Hex Dump"):
    """Dumps formatted raw hexadecimal bytes with ASCII representations."""
    print(f"\n--- {title} @ 0x{offset:08X} ({length} bytes) ---")
    chunk = mm[offset : offset + length]
    for i in range(0, len(chunk), 16):
        row = chunk[i : i + 16]
        hex_str = " ".join(f"{b:02X}" for b in row)
        ascii_str = "".join(chr(b) if 32 <= b <= 126 else "." for b in row)
        print(f"0x{offset + i:08X}:  {hex_str:<48}  |{ascii_str}|")
    print("-" * 60)


def main():
    ap = argparse.ArgumentParser(description="Parse Section 2600 animated world objects and keyframe streams.")
    ap.add_argument("file", help="Path to stream file (e.g. STREAML5RA.BUN)")
    ap.add_argument("--dump-samples", action="store_true", help="Dump trajectory points (Start, Mid, End frames)")
    ap.add_argument("--dump-hex", action="store_true", help="Dump raw hex bytes for headers and keyframes")
    args = ap.parse_args()

    with open(args.file, "rb") as f:
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)

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
            node_data = parse_rtnode_payload(payload)

            if node_data and node_data["section_number"] == TARGET_SECTION:
                nodes.append((idx, length, node_data))

        print(f"=== Section 2600 Persistent Motion Parser ===")
        print(f"File: {args.file}")
        print(f"Found {len(nodes)} Section 2600 objects\n")

        print(f"{'Offset':<12} {'Key (Hash)':<12} {'GUID':<12} {'Frames':<8} {'Base Position (X, Y, Z)':<35}")
        print("-" * 85)

        for offset, rtnode_len, data in nodes:
            pos_str = f"({data['base_pos'][0]:.2f}, {data['base_pos'][1]:.2f}, {data['base_pos'][2]:.2f})"
            print(f"0x{offset:08X}   0x{data['key']:08X}   0x{data['scenery_guid']:08X}   {data['key_frame_count']:<8} {pos_str:<35}")

            if args.dump_hex:
                dump_hex_bytes(mm, offset, 0x80, title=f"RTNODE Header (Key 0x{data['key']:08X})")

            frame_off, frame_len = find_forward_companion_frames(mm, offset, rtnode_len)
            if frame_off:
                print(f"  └─ Companion Frame Chunk @ 0x{frame_off - 8:08X} ({frame_len} bytes)")

                if args.dump_samples and data["key_frame_count"] > 0:
                    samples = read_frame_positions(mm, frame_off, data["key_frame_count"])
                    for f_idx, x, y, z in samples:
                        print(f"     • Frame {f_idx:<5}: Pos = ({x:.2f}, {y:.2f}, {z:.2f})")

                if args.dump_hex:
                    dump_hex_bytes(mm, frame_off - 8, 0x50, title=f"Frame Chunk Header & Frame 0 Matrix")
            else:
                print("  └─ [!] Companion frame chunk not found immediately after RTNODE.")

            if args.dump_samples or args.dump_hex:
                print()

        mm.close()


if __name__ == "__main__":
    main()