#!/usr/bin/env python3
"""
uv_scroll_dump.py - dump UV-scroll animation values (ScrollType, speeds,
timestep, offset, scale, tiling) for every texture found in a Carbon/
ProStreet stream file, using the EXACT real on-disk layout from
Version3Tpk.cs's TextureStruct (Pack=1) - not a guess, this is ground
truth from the actual working reader.

TextureStruct is 88 bytes (0x58), immediately followed by a 1-byte name
length and that many name characters (no padding, no fixed record size).
Because of that, textures can't be found by a repeating-pattern brute
force scan like the other systems - this instead scans for the
TexChunkId container (0x33310004) and walks sequential variable-length
records inside it until the declared chunk length is exhausted, flagging
any candidate that doesn't land exactly on the end as a likely false
positive/misalignment.

Usage:
    python uv_scroll_dump.py STREAML5RA.BUN
    python uv_scroll_dump.py STREAML5RA.BUN --only-animated
    python uv_scroll_dump.py STREAML5RA.BUN --out uv_scroll.tsv
"""

import argparse
import mmap
import struct
import sys

TEX_CHUNK_ID = 0x33310004
STRUCT_SIZE = 0x58  # 88 bytes, confirmed above

SCROLL_TYPE_NAMES = {0: "none", 1: "smooth", 2: "snap", 3: "offset_scale"}
TILABLE_BITS = [(0x01, "u_repeat"), (0x02, "v_repeat"), (0x04, "u_mirror"), (0x08, "v_mirror")]


def tilable_str(v):
    if v == 0:
        return "clamp"
    return "|".join(name for bit, name in TILABLE_BITS if v & bit)


def parse_texture_struct(mm, off):
    # Field offsets within the 88-byte struct, per Version3Tpk.cs exactly.
    hash_ = struct.unpack_from("<I", mm, off + 0x0C)[0]
    width = struct.unpack_from("<H", mm, off + 0x28)[0]
    height = struct.unpack_from("<H", mm, off + 0x2A)[0]
    tilable_uv = mm[off + 0x33]
    scroll_type = mm[off + 0x36]
    scroll_timestep = struct.unpack_from("<h", mm, off + 0x3E)[0]
    scroll_speed_s = struct.unpack_from("<h", mm, off + 0x40)[0]
    scroll_speed_t = struct.unpack_from("<h", mm, off + 0x42)[0]
    offset_s = struct.unpack_from("<h", mm, off + 0x44)[0]
    offset_t = struct.unpack_from("<h", mm, off + 0x46)[0]
    scale_s = struct.unpack_from("<h", mm, off + 0x48)[0]
    scale_t = struct.unpack_from("<h", mm, off + 0x4A)[0]
    return {
        "hash": hash_, "width": width, "height": height,
        "tilable_uv": tilable_uv, "scroll_type": scroll_type,
        "scroll_timestep": scroll_timestep,
        "scroll_speed_s": scroll_speed_s, "scroll_speed_t": scroll_speed_t,
        "offset_s": offset_s, "offset_t": offset_t,
        "scale_s": scale_s, "scale_t": scale_t,
    }


def walk_tex_chunk(mm, payload_start, payload_len):
    """Yields (name, fields) for each texture record, or raises/returns
    early with a flag if the walk doesn't land exactly on the end."""
    pos = payload_start
    end = payload_start + payload_len
    textures = []
    while pos < end:
        if pos + STRUCT_SIZE + 1 > end:
            return textures, False  # ran out of room mid-record - misaligned
        fields = parse_texture_struct(mm, pos)
        name_len = mm[pos + STRUCT_SIZE]
        name_start = pos + STRUCT_SIZE + 1
        if name_start + name_len > end:
            return textures, False
        name = mm[name_start:name_start + name_len].decode("latin-1", errors="replace")
        textures.append((name, fields))
        pos = name_start + name_len
    return textures, pos == end


def find_tex_chunk_candidates(mm):
    pattern = struct.pack("<I", TEX_CHUNK_ID)
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
        if 0 < length <= 200_000_000 and idx + 8 + length <= len(mm):
            results.append((idx, length))
    return results


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", help="Path to the stream file (e.g. STREAML5RA.BUN)")
    ap.add_argument("--only-animated", action="store_true", help="Only print textures with scroll_type != 0")
    ap.add_argument("--out", default=None, help="Write TSV output to this file instead of stdout")
    args = ap.parse_args()

    out_fh = open(args.out, "w", encoding="utf-8") if args.out else sys.stdout

    def p(*a, **kw):
        print(*a, file=out_fh, **kw)

    with open(args.file, "rb") as f:
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)

        candidates = find_tex_chunk_candidates(mm)
        print(f"# Found {len(candidates)} TexChunkId (0x33310004) candidate(s)", file=sys.stderr)

        p("\t".join([
            "chunk_offset", "name", "hash", "width", "height",
            "scroll_type", "scroll_timestep", "scroll_speed_s", "scroll_speed_t",
            "offset_s", "offset_t", "scale_s", "scale_t", "tilable_uv",
        ]))

        total_textures = 0
        clean_chunks = 0
        for offset, length in candidates:
            payload_start = offset + 8
            textures, clean = walk_tex_chunk(mm, payload_start, length)
            if not clean:
                print(f"# candidate @ 0x{offset:08X}: walk did not land exactly on chunk end "
                      f"({len(textures)} record(s) read before misalignment) - likely a false positive, skipping",
                      file=sys.stderr)
                continue
            clean_chunks += 1
            for name, fields in textures:
                if args.only_animated and fields["scroll_type"] == 0:
                    continue
                total_textures += 1
                p("\t".join(str(x) for x in [
                    f"0x{offset:08X}", name, f"0x{fields['hash']:08X}",
                    fields["width"], fields["height"],
                    f"{fields['scroll_type']} ({SCROLL_TYPE_NAMES.get(fields['scroll_type'], '?')})",
                    fields["scroll_timestep"], fields["scroll_speed_s"], fields["scroll_speed_t"],
                    fields["offset_s"], fields["offset_t"], fields["scale_s"], fields["scale_t"],
                    tilable_str(fields["tilable_uv"]),
                ]))

        print(f"# {clean_chunks} clean TexChunkId chunk(s), {total_textures} texture row(s) printed",
              file=sys.stderr)

        mm.close()

    if args.out:
        out_fh.close()
        print(f"Wrote output to {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
