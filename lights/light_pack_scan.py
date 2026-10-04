#!/usr/bin/env python3
"""
light_pack_scan.py - Every light of every light pack (LightPack) in a stream file
(STREAML5RA.BUN), written to a TSV. Self-contained port of the AssetDumper light pack
reader (Common/Lights/LightPackReader.cs) and of the light data that the AssetDumper
scene export wrote into the COLLADA files (--export-lights).

Chunks (found at any depth):
  container 0x80135000 (one per scenery section), with the children
    0x00135001  header, 0x20 bytes (after the payload is aligned to 0x10):
                  +0x08 version u16   +0x0C section_number u32   +0x1C num_lights i32
    0x00135003  light list: N x 0x60-byte records (payload aligned to 0x10 first):
                  +0x00 name_hash u32
                  +0x04 type u8  +0x05 attenuation_type u8  +0x06 shape u8  +0x07 state u8
                  +0x08 exclude_name_hash u32   +0x0C color u32 (bytes R, G, B, A)
                  +0x10 position f32[3]   +0x1C size f32   +0x20 direction f32[3]
                  +0x2C intensity f32   +0x30 far_start f32   +0x34 far_end f32
                  +0x38 falloff f32   +0x3C section_number i16   +0x3E name char[34]
The alignment rule is the one the C# reader used: the payload of a child chunk starts
at the next file offset that is a multiple of 0x10, and the skipped bytes do not count
as payload.

Output: lights.tsv, one row per light. The AssetDumper reader dropped type,
attenuation_type, shape, state, exclude_name_hash, direction and the per-light section
number; they are all here. Columns:
  id (light_{pack section}_{index}, the id the DAE export used), chunk_offset,
  pack_section, pack_version, pack_num_lights, index, name_hash, name, type,
  attenuation_type, shape, state, exclude_name_hash, color (0xAABBGGRR), r, g, b (0..1),
  world_x, world_y, world_z, size, dir_x, dir_y, dir_z, intensity, far_start, far_end,
  falloff, light_section
For a Blender light the DAE export used energy = 100000 * intensity and radius = size.

Usage:
    python lights/light_pack_scan.py STREAML5RA.BUN
    python lights/light_pack_scan.py STREAML5RA.BUN --tsv E:/somewhere/lights.tsv
Default output: outputs/light_pack_scan/lights.tsv
The file is memory-mapped, so a very large stream file is not loaded into memory.
"""
# stream bootstrap: make the shared library folders importable
import pathlib as _pl, sys as _sys
_sys.path[:0] = [str(_pl.Path(__file__).resolve().parents[1] / _d) for _d in ['common', 'region', 'scenery', 'solids_materials']]

import argparse
import mmap
import struct
from decimal import Decimal

LIGHT_PACK_ID = 0x80135000
HEADER_ID = 0x00135001
LIGHT_LIST_ID = 0x00135003
CONTAINER_FLAG = 0x80000000
MAX_DEPTH = 32
HEADER_SIZE = 0x20
LIGHT_SIZE = 0x60

COLUMNS = ["id", "chunk_offset", "pack_section", "pack_version", "pack_num_lights", "index", "name_hash",
           "name", "type", "attenuation_type", "shape", "state", "exclude_name_hash", "color", "r", "g", "b",
           "world_x", "world_y", "world_z", "size", "dir_x", "dir_y", "dir_z", "intensity", "far_start",
           "far_end", "falloff", "light_section"]


def fmt_f32(value):
    """Shortest text that reads back as the same float32."""
    for digits in range(1, 10):
        text = "%.*g" % (digits, value)
        if struct.pack("<f", float(text)) == struct.pack("<f", value):
            if "e" in text:
                text = format(Decimal(text), "f")  # no exponent in the file
            return text
    return repr(value)


class Counts:
    def __init__(self):
        self.packs = 0
        self.lights = 0
        self.skipped_packs = 0


def scan_chunks(mm, start, end, rows, counts, depth=0):
    """Walks the chunk envelope (u32 type, u32 length, payload) in [start, end)."""
    pos = start
    while pos + 8 <= end:
        chunk_type, length = struct.unpack_from("<II", mm, pos)
        payload_start = pos + 8
        payload_end = payload_start + length

        if payload_end > end:
            print("WARNING: stopping scan at offset 0x%X: chunk declares length %d which runs past "
                  "the current container's end (truncated file or misparsed alignment before this point)"
                  % (pos, length))
            return False

        if chunk_type == LIGHT_PACK_ID:
            counts.packs += 1
            read_light_pack(mm, pos, payload_start, payload_end, rows, counts)
        elif chunk_type & CONTAINER_FLAG and depth < MAX_DEPTH:
            if not scan_chunks(mm, payload_start, payload_end, rows, counts, depth + 1):
                return False

        pos = payload_end
    return True


def aligned_payload(payload_start, length):
    """Start and size of a child payload after the 0x10 alignment (as BinaryUtil.AlignReader)."""
    start = (payload_start + 0xF) & ~0xF
    return start, length - (start - payload_start)


def read_light_pack(mm, pack_offset, start, end, rows, counts):
    """Reads the children of one 0x80135000 container."""
    header = None
    lights = None

    pos = start
    while pos + 8 <= end:
        chunk_id, length = struct.unpack_from("<II", mm, pos)
        payload_start = pos + 8
        next_pos = payload_start + length
        if next_pos > end:
            print("WARNING: light pack at 0x%X: child chunk at 0x%X runs past the container, stopping" % (pack_offset, pos))
            break

        payload, size = aligned_payload(payload_start, length)

        if chunk_id == HEADER_ID and size >= HEADER_SIZE:
            version = struct.unpack_from("<H", mm, payload + 0x08)[0]
            section_number = struct.unpack_from("<I", mm, payload + 0x0C)[0]
            num_lights = struct.unpack_from("<i", mm, payload + 0x1C)[0]
            header = (version, section_number, num_lights)
        elif chunk_id == LIGHT_LIST_ID:
            if size % LIGHT_SIZE != 0:
                print("WARNING: light pack at 0x%X: light list is %d bytes, not a multiple of 0x60 "
                      "(weirdly sized), skipping this pack" % (pack_offset, size))
                counts.skipped_packs += 1
                return
            lights = (payload, size // LIGHT_SIZE)

        pos = next_pos

    if header is None or lights is None:
        print("WARNING: light pack at 0x%X has no %s, skipping it"
              % (pack_offset, "header" if header is None else "light list"))
        counts.skipped_packs += 1
        return

    version, section_number, num_lights = header
    payload, count = lights
    if num_lights != count:
        print("WARNING: light pack at 0x%X: header says %d lights, the list holds %d (using the list)"
              % (pack_offset, num_lights, count))

    for index in range(count):
        p = payload + index * LIGHT_SIZE
        name_hash = struct.unpack_from("<I", mm, p)[0]
        light_type, attenuation_type, shape, state = struct.unpack_from("<4B", mm, p + 0x04)
        exclude_name_hash, color = struct.unpack_from("<2I", mm, p + 0x08)
        px, py, pz, size = struct.unpack_from("<4f", mm, p + 0x10)
        dx, dy, dz, intensity, far_start, far_end, falloff = struct.unpack_from("<7f", mm, p + 0x20)
        light_section = struct.unpack_from("<h", mm, p + 0x3C)[0]
        name = bytes(mm[p + 0x3E:p + 0x60]).split(b"\0")[0].decode("ascii", "replace")

        r, g, b = (color & 0xFF) / 255, ((color >> 8) & 0xFF) / 255, ((color >> 16) & 0xFF) / 255

        rows.append(["light_%d_%d" % (section_number, index), "0x%X" % pack_offset, section_number, version,
                     num_lights, index, "0x%08X" % name_hash, name, light_type, attenuation_type, shape, state,
                     "0x%08X" % exclude_name_hash, "0x%08X" % color, "%.6g" % r, "%.6g" % g, "%.6g" % b,
                     fmt_f32(px), fmt_f32(py), fmt_f32(pz), fmt_f32(size), fmt_f32(dx), fmt_f32(dy), fmt_f32(dz),
                     fmt_f32(intensity), fmt_f32(far_start), fmt_f32(far_end), fmt_f32(falloff), light_section])
        counts.lights += 1


def main():
    ap = argparse.ArgumentParser(description="Write every light pack light of a stream file to lights.tsv")
    ap.add_argument("file", help="Stream file (STREAML5RA.BUN)")
    ap.add_argument("--tsv", help="Output file (default outputs/light_pack_scan/lights.tsv)")
    args = ap.parse_args()

    from nfs_outputs import out_path
    tsv_path = args.tsv or out_path("light_pack_scan", "lights.tsv")

    print("Scanning %s for light packs (0x%X)" % (args.file, LIGHT_PACK_ID))

    rows = []
    counts = Counts()
    with open(args.file, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        scan_chunks(mm, 0, len(mm), rows, counts)

    with open(tsv_path, "w", encoding="utf-8", newline="") as out:
        out.write("\t".join(COLUMNS) + "\n")
        for row in rows:
            out.write("\t".join(str(v) for v in row) + "\n")

    print("Done. %d light pack(s) found (%d skipped), %d light(s) written. Wrote %s"
          % (counts.packs, counts.skipped_packs, counts.lights, tsv_path))
    if counts.packs == 0:
        print("WARNING: no light packs found in this file. Either it has none, or it is not a stream file of a "
              "game that uses 0x80135000 light packs.")


if __name__ == "__main__":
    main()
