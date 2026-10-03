#!/usr/bin/env python3
"""
track_path_zone_reader.py

Reader for Need for Speed Carbon / MW-family TrackPath data.

Scans a BUN/stream-like binary for:
    0x80034147 = TrackPathManager container
    0x0003414A = TrackPathZones data
    0x0003414D = TrackPathBarriers data

Zone records are VARIABLE-LENGTH.

TrackPathZone:
    +0x00  type                  u32
    +0x04  position              float[2]
    +0x0C  direction             float[2]
    +0x14  elevation             f32
    +0x18  zone_source           i8
    +0x19  cached_index          i8
    +0x1A  visit_info            i16
    +0x1C  user_data             u32/pointer (runtime field; preserve raw)
    +0x20  bbox_min              float[2]
    +0x28  bbox_max              float[2]
    +0x30  data[4]               i32[4]
    +0x40  num_points             i16
    +0x42  memory_image_size      i16
    +0x44  points[num_points]     float[2]

The game advances to the next record using MemoryImageSize.
The proven record-size rule is:
    MemoryImageSize == 0x44 + num_points * 8

Maximum struct size with 64 points:
    0x244 bytes

TrackPathBarrier is fixed 0x18 bytes:
    +0x00 points[0]              float[2]
    +0x08 points[1]              float[2]
    +0x10 enabled                i8
    +0x11 pad                    i8
    +0x12 player_barrier         i8
    +0x13 left_handed            i8
    +0x14 group_hash             u32

The original loader uses GetData()/GetSize() for these chunks, not
GetAlignedData(), so record data starts immediately after the 8-byte
chunk header.

Usage:
    python track_path_zone_reader.py L5RA.BUN
    python track_path_zone_reader.py L5RA.BUN --max-zones 20
    python track_path_zone_reader.py L5RA.BUN --json dump.json
"""

from __future__ import annotations

import argparse
import json
import math
import struct
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import BinaryIO


TRACK_PATH_MANAGER = 0x80034147
TRACK_PATH_ZONES = 0x0003414A
TRACK_PATH_BARRIERS = 0x0003414D

ZONE_MIN_SIZE = 0x44
ZONE_MAX_SIZE = 0x244
BARRIER_SIZE = 0x18

ZONE_TYPES = {
    0: "RESET",
    1: "RESET_TO_POINT",
    2: "GUIDED_RESET",
    3: "TUNNEL",
    4: "OVERPASS",
    5: "OVERPASS_SMALL",
    6: "STREAMER_PREDICTION",
    7: "GARAGE",
    8: "HIDDEN",
    9: "TRAFFIC_PATTERN",
    10: "DYNAMIC",
    11: "NEIGHBOURHOOD",
    12: "JUMP_CAM",
    13: "NO_COP_SPAWN",
    14: "PURSUIT_START",
}


@dataclass
class Zone:
    offset: int
    record_size: int
    type: int
    type_name: str
    position: tuple[float, float]
    direction: tuple[float, float]
    elevation: float
    zone_source: int
    cached_index: int
    visit_info: int
    user_data_raw: int
    bbox_min: tuple[float, float]
    bbox_max: tuple[float, float]
    data: tuple[int, int, int, int]
    num_points: int
    points: list[tuple[float, float]]
    declared_size_matches_formula: bool


@dataclass
class Barrier:
    offset: int
    p0: tuple[float, float]
    p1: tuple[float, float]
    enabled: int
    pad: int
    player_barrier: int
    left_handed: int
    group_hash: int


def align(value: int, alignment: int = 16) -> int:
    return (value + alignment - 1) & ~(alignment - 1)


def hex32(value: int) -> str:
    return f"0x{value:08X}"


def finite_vec2(v: tuple[float, float]) -> bool:
    return all(math.isfinite(x) for x in v)


def read_chunk_header(data: bytes, offset: int, endian: str = "<") -> tuple[int, int] | None:
    if offset < 0 or offset + 8 > len(data):
        return None
    chunk_id, chunk_size = struct.unpack_from(endian + "II", data, offset)
    return chunk_id, chunk_size


def plausible_chunk(offset: int, size: int, file_size: int) -> bool:
    return size >= 0 and offset + 8 + size <= file_size


def scan_chunk_ids(data: bytes, chunk_id: int, endian: str = "<") -> list[tuple[int, int]]:
    needle = struct.pack(endian + "I", chunk_id)
    results: list[tuple[int, int]] = []
    pos = 0
    while True:
        pos = data.find(needle, pos)
        if pos < 0:
            break
        hdr = read_chunk_header(data, pos, endian)
        if hdr is not None:
            found_id, size = hdr
            if found_id == chunk_id and plausible_chunk(pos, size, len(data)):
                results.append((pos, size))
        pos += 1
    return results


def parse_zone(data: bytes, offset: int, endian: str = "<") -> Zone:
    if offset + ZONE_MIN_SIZE > len(data):
        raise ValueError("zone header truncated")

    type_id = struct.unpack_from(endian + "I", data, offset + 0x00)[0]
    position = struct.unpack_from(endian + "2f", data, offset + 0x04)
    direction = struct.unpack_from(endian + "2f", data, offset + 0x0C)
    elevation = struct.unpack_from(endian + "f", data, offset + 0x14)[0]

    # The original loader does NOT endian-swap these byte-sized fields.
    zone_source = struct.unpack_from("b", data, offset + 0x18)[0]
    cached_index = struct.unpack_from("b", data, offset + 0x19)[0]
    visit_info = struct.unpack_from(endian + "h", data, offset + 0x1A)[0]

    # pUserData is a runtime pointer. Keep the raw 32-bit value only.
    user_data_raw = struct.unpack_from(endian + "I", data, offset + 0x1C)[0]

    bbox_min = struct.unpack_from(endian + "2f", data, offset + 0x20)
    bbox_max = struct.unpack_from(endian + "2f", data, offset + 0x28)
    values = struct.unpack_from(endian + "4i", data, offset + 0x30)

    num_points = struct.unpack_from(endian + "h", data, offset + 0x40)[0]
    memory_image_size = struct.unpack_from(endian + "h", data, offset + 0x42)[0]

    expected_size = ZONE_MIN_SIZE + max(num_points, 0) * 8
    if not (0 <= num_points <= 64):
        raise ValueError(
            f"invalid num_points={num_points} at 0x{offset:08X}"
        )

    if memory_image_size != expected_size:
        raise ValueError(
            f"memory_image_size=0x{memory_image_size:X}, "
            f"expected=0x{expected_size:X} "
            f"(num_points={num_points}) at 0x{offset:08X}"
        )

    if memory_image_size < ZONE_MIN_SIZE or memory_image_size > ZONE_MAX_SIZE:
        raise ValueError(
            f"invalid record size 0x{memory_image_size:X} at 0x{offset:08X}"
        )

    if offset + memory_image_size > len(data):
        raise ValueError(f"zone at 0x{offset:08X} overruns chunk/file")

    points: list[tuple[float, float]] = []
    point_off = offset + 0x44
    for _ in range(num_points):
        points.append(struct.unpack_from(endian + "2f", data, point_off))
        point_off += 8

    return Zone(
        offset=offset,
        record_size=memory_image_size,
        type=type_id,
        type_name=ZONE_TYPES.get(type_id, f"UNKNOWN_{type_id}"),
        position=position,
        direction=direction,
        elevation=elevation,
        zone_source=zone_source,
        cached_index=cached_index,
        visit_info=visit_info,
        user_data_raw=user_data_raw,
        bbox_min=bbox_min,
        bbox_max=bbox_max,
        data=values,
        num_points=num_points,
        points=points,
        declared_size_matches_formula=(memory_image_size == expected_size),
    )


def parse_zones(data: bytes, chunk_offset: int, chunk_size: int, endian: str = "<") -> list[Zone]:
    data_start = chunk_offset + 8
    data_end = data_start + chunk_size

    zones: list[Zone] = []
    pos = data_start

    while pos < data_end:
        remaining = data_end - pos
        if remaining < ZONE_MIN_SIZE:
            raise ValueError(
                f"trailing {remaining} bytes in zones chunk at 0x{chunk_offset:08X}"
            )

        zone = parse_zone(data, pos, endian)
        next_pos = pos + zone.record_size

        if next_pos <= pos or next_pos > data_end:
            raise ValueError(
                f"zone 0x{pos:08X} crosses chunk boundary "
                f"(next=0x{next_pos:08X}, end=0x{data_end:08X})"
            )

        zones.append(zone)
        pos = next_pos

    return zones


def parse_barriers(data: bytes, chunk_offset: int, chunk_size: int, endian: str = "<") -> list[Barrier]:
    if chunk_size % BARRIER_SIZE != 0:
        raise ValueError(
            f"barrier chunk size {chunk_size} is not divisible by 0x{BARRIER_SIZE:X}"
        )

    data_start = chunk_offset + 8
    count = chunk_size // BARRIER_SIZE
    barriers: list[Barrier] = []

    for i in range(count):
        off = data_start + i * BARRIER_SIZE
        p0 = struct.unpack_from(endian + "2f", data, off + 0x00)
        p1 = struct.unpack_from(endian + "2f", data, off + 0x08)
        enabled, pad, player_barrier, left_handed = struct.unpack_from(
            "4b", data, off + 0x10
        )
        group_hash = struct.unpack_from(endian + "I", data, off + 0x14)[0]

        barriers.append(
            Barrier(
                offset=off,
                p0=p0,
                p1=p1,
                enabled=enabled,
                pad=pad,
                player_barrier=player_barrier,
                left_handed=left_handed,
                group_hash=group_hash,
            )
        )

    return barriers


def print_zone(z: Zone, index: int) -> None:
    print(f"  zone[{index}] @ 0x{z.offset:08X} size=0x{z.record_size:X} "
          f"type={z.type} ({z.type_name})")
    print(f"    pos=({z.position[0]:.3f},{z.position[1]:.3f}) "
          f"dir=({z.direction[0]:.3f},{z.direction[1]:.3f}) "
          f"elevation={z.elevation:.3f}")
    print(f"    bbox=(({z.bbox_min[0]:.3f},{z.bbox_min[1]:.3f}) .. "
          f"({z.bbox_max[0]:.3f},{z.bbox_max[1]:.3f}))")
    print(f"    data={[hex32(x & 0xFFFFFFFF) for x in z.data]} "
          f"num_points={z.num_points}")
    print(f"    zone_source={z.zone_source} cached_index={z.cached_index} "
          f"visit_info={z.visit_info} user_data_raw={hex32(z.user_data_raw)}")
    if z.points:
        print(f"    first_point=({z.points[0][0]:.3f},{z.points[0][1]:.3f}) "
              f"last_point=({z.points[-1][0]:.3f},{z.points[-1][1]:.3f})")


def print_barrier(b: Barrier, index: int) -> None:
    print(f"  barrier[{index}] @ 0x{b.offset:08X} "
          f"p0=({b.p0[0]:.3f},{b.p0[1]:.3f}) "
          f"p1=({b.p1[0]:.3f},{b.p1[1]:.3f}) "
          f"enabled={b.enabled} player={b.player_barrier} "
          f"left_handed={b.left_handed} group={hex32(b.group_hash)}")


def summarize(zones: list[Zone], barriers: list[Barrier]) -> None:
    print("\nSUMMARY")
    print(f"  zones:    {len(zones)}")
    print(f"  barriers: {len(barriers)}")

    counts = Counter(z.type_name for z in zones)
    if counts:
        print("  zone types:")
        for name, count in sorted(counts.items(), key=lambda x: (x[0])):
            print(f"    {name:24s} {count}")

    point_counts = Counter(z.num_points for z in zones)
    if point_counts:
        print("  point counts:")
        for n, count in sorted(point_counts.items()):
            print(f"    {n:2d} points: {count}")

    group_counts = Counter(b.group_hash for b in barriers)
    if group_counts:
        print("  barrier groups:")
        for group, count in group_counts.most_common(20):
            print(f"    {hex32(group)} {count}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", type=Path)
    ap.add_argument("--max-zones", type=int, default=20)
    ap.add_argument("--max-barriers", type=int, default=20)
    ap.add_argument("--json", type=Path, default=None)
    args = ap.parse_args()

    data = args.file.read_bytes()
    endian = "<"

    print(f"# Scanning {args.file} ({len(data):,} bytes)")

    # --- TrackPathManager container ---
    manager_candidates = scan_chunk_ids(data, TRACK_PATH_MANAGER, endian)
    print(f"TrackPathManager candidates: {len(manager_candidates)}")

    all_zones: list[Zone] = []
    manager_records = []

    for manager_off, manager_size in manager_candidates:
        print("=" * 78)
        print(f"TRACK PATH MANAGER @ 0x{manager_off:08X} payload={manager_size:,}")

        payload_start = manager_off + 8
        payload_end = payload_start + manager_size

        child_pos = payload_start
        found_zone_chunk = None

        while child_pos + 8 <= payload_end:
            hdr = read_chunk_header(data, child_pos, endian)
            if hdr is None:
                break

            child_id, child_size = hdr
            child_end = child_pos + 8 + child_size
            if child_end > payload_end:
                print(f"  [!] child 0x{child_id:08X} overruns manager")
                break

            print(f"  child @ 0x{child_pos:08X} id={hex32(child_id)} size={child_size:,}")

            if child_id == TRACK_PATH_ZONES:
                found_zone_chunk = (child_pos, child_size)

            child_pos = child_end

        if found_zone_chunk is not None:
            zoff, zsize = found_zone_chunk
            zones = parse_zones(data, zoff, zsize, endian)
            all_zones.extend(zones)

            print(f"  zones: {len(zones)} variable-length records; "
                  f"sum=0x{sum(z.record_size for z in zones):X} "
                  f"chunk=0x{zsize:X}")
            for i, z in enumerate(zones[:args.max_zones]):
                print_zone(z, i)
            if len(zones) > args.max_zones:
                print(f"  ... {len(zones) - args.max_zones} more zones")

            manager_records.append({
                "offset": manager_off,
                "payload_size": manager_size,
                "zone_chunk_offset": zoff,
                "zone_chunk_size": zsize,
                "zone_count": len(zones),
                "zones": [asdict(z) for z in zones],
            })

    # --- Standalone barrier chunk(s) ---
    barrier_candidates = scan_chunk_ids(data, TRACK_PATH_BARRIERS, endian)
    print("\n" + "=" * 78)
    print(f"TrackPathBarriers candidates: {len(barrier_candidates)}")

    all_barriers: list[Barrier] = []
    barrier_records = []

    for off, size in barrier_candidates:
        if size == 0 or size % BARRIER_SIZE != 0:
            print(f"  skip @ 0x{off:08X}: size={size} not divisible by 0x{BARRIER_SIZE:X}")
            continue

        barriers = parse_barriers(data, off, size, endian)
        all_barriers.extend(barriers)

        print(f"  barriers @ 0x{off:08X}: {len(barriers)} records "
              f"(payload={size:,} bytes)")
        for i, b in enumerate(barriers[:args.max_barriers]):
            print_barrier(b, i)
        if len(barriers) > args.max_barriers:
            print(f"  ... {len(barriers) - args.max_barriers} more barriers")

        barrier_records.append({
            "offset": off,
            "payload_size": size,
            "barrier_count": len(barriers),
            "barriers": [asdict(b) for b in barriers],
        })

    summarize(all_zones, all_barriers)

    if not manager_candidates:
        print("\n[!] No TrackPathManager container found.")
    if not all_zones:
        print("[!] No TrackPathZones decoded.")
    if not all_barriers:
        print("[!] No TrackPathBarriers decoded.")

    if args.json:
        output = {
            "file": str(args.file),
            "file_size": len(data),
            "track_path_managers": manager_records,
            "barrier_chunks": barrier_records,
            "summary": {
                "zones": len(all_zones),
                "barriers": len(all_barriers),
                "zone_types": dict(Counter(z.type_name for z in all_zones)),
            },
        }
        args.json.write_text(json.dumps(output, indent=2), encoding="utf-8")
        print(f"\nWrote JSON: {args.json}")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, struct.error) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        raise SystemExit(2)
