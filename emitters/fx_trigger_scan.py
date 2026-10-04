#!/usr/bin/env python3
"""
fx_trigger_scan.py - World positions of every emitter trigger (WorldFXTrigger) in the
Carbon stream file (STREAML5RA.BUN). Self-contained port of the AssetDumper
`scan-fx-triggers` command (ExportFxTriggersCommand.cs, now removed), including the
rotation columns that command had gained.

Chunk: BCHUNK_SPEED_EMITTER_LIBRARY 0x0003BC00 (leaf chunk, found at any depth).
Layout (hand-decoded on one real chunk, at file offset 0x195A0B20 of one real
STREAML5RA.BUN; the other instances are unchecked):
  0x18-byte header, all int32:
    endian_swapped (0x11111111 in real data), version, library_count (always 0 on disk),
    section_number, record_count (the real count), section_ref
  then record_count flat 0x50-byte records:
    +0x00 group_key u32   +0x04 pad   +0x08 record_section_ref u32   +0x0C pad
    +0x10 rotation rows 0-2 of a 4x4 matrix, 12 f32 (x, y, z, pad per row; pad is 0)
    +0x40 matrix row 3: world_x f32   world_y f32   world_z f32   w f32 (1.0 in real data)
  The rows are the local axes in world space (row 2 = local Z, the cone axis).
A record whose position is NaN or larger than 1,000,000 in any axis is rejected.

Output: fx_triggers.tsv with the same columns the C# command wrote:
  chunk_offset  section_number  chunk_section_ref  record_section_ref  record_index
  group_key  group_name  world_x  world_y  world_z
  rot_r0x rot_r0y rot_r0z  rot_r1x rot_r1y rot_r1z  rot_r2x rot_r2y rot_r2z
group_name comes from the table below (MW's compiled-in effect names, same as the C#
command had), else UNKNOWN_0x........ . emitters/extract_emitters.py reads this file.

Usage:
    python emitters/fx_trigger_scan.py STREAML5RA.BUN
    python emitters/fx_trigger_scan.py STREAML5RA.BUN --tsv E:/somewhere/fx_triggers.tsv
Default output: outputs/fx_trigger_scan/fx_triggers.tsv

The file is memory-mapped and every chunk that is not the target is skipped by
offset, so a very large stream file is not loaded into memory.
"""
# stream bootstrap: make the shared library folders importable
import pathlib as _pl, sys as _sys
_sys.path[:0] = [str(_pl.Path(__file__).resolve().parents[1] / _d) for _d in ['common', 'region', 'scenery', 'solids_materials']]

import argparse
import math
import mmap
import struct
from decimal import Decimal

EMITTER_LIBRARY_ID = 0x0003BC00
CONTAINER_FLAG = 0x80000000
MAX_DEPTH = 32
ROT_COLS = ["rot_r%d%s" % (r, a) for r in range(3) for a in "xyz"]
HEADER_SIZE = 0x18
RECORD_SIZE = 0x50
MAX_RECORDS = 10000

# Real name <-> hash pairs from MW's compiled code (Generated/AttribSys/Classes/
# emittergroup_hash.h). Carbon effects missing here print as UNKNOWN_0x........ .
KNOWN_EFFECT_NAMES = {
    0xA13753EB: "car",
    0xFDA45513: "carsurface",
    0x490792B5: "debris",
    0xEEC2271A: "default",
    0x7CD7EF85: "destruction",
    0x33C586E2: "dust",
    0x3C04DF64: "environmental",
    0xCB223B96: "explosions",
    0x5E2FE5BC: "fire",
    0xB585F0E2: "fxcar_cop_damage1",
    0x3AEB075D: "fxcar_cop_death1",
    0x080847FC: "fxcar_coplightblue",
    0xB647956D: "fxcar_coplightred",
    0xCD495AF7: "fxcar_coplightwhite",
    0x9EE4CAC4: "fxcar_dusttrail1",
    0xC9FBD50F: "fxcar_engineblow1",
    0xB25D04FB: "fxcar_exhaust_bmw",
    0x59038C67: "fxcar_exhaust_bmw2",
    0x5FCBA64B: "fxcar_exhaust_drip",
    0xF74DFAAF: "fxcar_impactdebrisl",
    0xF991F8E8: "fxcar_impactl",
    0xF033A657: "fxcar_impactpavement",
    0x6B7916BD: "fxcar_nos",
    0x67F294DE: "fxcar_tireblow",
    0x3E898D27: "fxcs_sc_metal",
    0x84800B86: "FxCS_Sc_Stone",
    0x32CB8388: "FxCS_Sc_Wood",
    0xFD94EABE: "fxdust_lg_billow1",
    0xC3C0FA78: "fxdust_lg_fall",
    0x43BB74F8: "fxdust_med_billow1",
    0x34C14DE7: "fxdust_med_exup",
    0xD48711DB: "fxdust_med_fall",
    0x45F3B7ED: "fxenv_bird",
    0x39EDE226: "fxenv_birdblack01",
    0x4D9E7F58: "fxenv_blackbird02",
    0xFD888A4B: "fxenv_chimney1",
    0x08259CCF: "fxenv_dustmotes1",
    0xC8A23629: "fxenv_fog_fe1",
    0x48D22C27: "fxenv_fog_fe2",
    0x378D447E: "fxenv_fog1",
    0x0B9204CE: "fxenv_fog1thick",
    0xF8170EED: "fxenv_fog2",
    0x8010A402: "fxenv_fountain1",
    0x85676C22: "fxenv_fountain2",
    0x67306F57: "fxenv_fountain3",
    0x328CD168: "fxenv_leaffall_hvy",
    0x96AC4F78: "fxenv_leaves1",
    0x303DC26F: "fxenv_ripple1",
    0x476537C3: "fxenv_ripple2",
    0x4B54AD54: "fxenv_small_steam1",
    0x31FB2AB6: "fxenv_small_steam2",
    0xAA7DAB77: "fxenv_small_steam3",
    0x2F23057A: "fxenv_smokestack",
    0xF5ED0A9B: "fxenv_smokestack_blk",
    0xFD8995C1: "fxenv_smokestack_brn",
    0xFE17B5F9: "fxenv_smokestack_long",
    0x56447450: "fxex_carexplode_sm1",
    0xB6F25977: "fxex_gasstation",
    0xED95A4AE: "fxex_large1",
    0x4B8CEF14: "fxex_large2",
    0x8FED3B3E: "fxfire_lg_area1",
    0x9CD7D94E: "fxfire_sm1",
    0xD36AD240: "fxfire_trail1",
    0xB9FB31B1: "fxgame_flare_green",
    0x310805CF: "fxgame_flare_red",
    0x6699D23D: "fxgame_icongrp_chop",
    0x3CCB52C5: "fxgame_icongrp_circuit",
    0xC0087531: "fxgame_icongrp_drag",
    0xF9F71422: "fxgame_icongrp_hidecar",
    0xB8E326BF: "fxgame_icongrp_lapk",
    0xBE1EF064: "fxgame_icongrp_lot",
    0x7546C031: "fxgame_icongrp_pursuit",
    0x8A2709BE: "fxgame_icongrp_rivalr",
    0x6D018122: "fxgame_icongrp_safe",
    0x60BB33EC: "fxgame_icongrp_speedt",
    0xE1EB3B0F: "fxgame_icongrp_sprint",
    0x5A4699BF: "fxgame_icongrp_tollb",
    0x102985E8: "fxmis_coins1",
    0x53FDEF32: "fxmis_dustpuff",
    0xB14A8CF9: "fxmis_glass1",
    0x186E0544: "fxmis_grasshit",
    0xA6D64AC8: "fxmis_hitdust1",
    0x4A707043: "fxmis_leaffall1",
    0xAFFBBF1D: "fxmis_leafhit",
    0xFE2ED9D8: "fxmis_paper1",
    0x1C27E839: "fxmis_wooddust1",
    0x040D2469: "fxnis_extradust1",
    0x53B9B550: "fxnis_leafblast1",
    0x4E4E9BBE: "fxnis_leafblast2",
    0x5CFEB5AA: "fxnis_leafblast3",
    0x715D7EA4: "fxnis_leafblast4",
    0x0A2097E1: "fxnis_steamjet",
    0xB7A0AD8A: "fxsmk_md_trail",
    0x2DB37A81: "fxsmk_md_trail02",
    0xB6DB8816: "fxsprk_lg_dir",
    0x8566E9E3: "fxsprk_md_dir",
    0xEABB4FE1: "fxsprk_md_trail",
    0x95CDE6CB: "fxtd_dr_asphalt_leaves",
    0x0EAC606A: "fxtd_dr_asphalt_noleaves",
    0x98C0D7AC: "fxtd_dr_asphalt_wet",
    0x1129219D: "fxtd_dr_cobble",
    0x21A3994D: "fxtd_dr_cobble_wet",
    0xCC2D92BA: "fxtd_dr_dirt",
    0x66F77142: "fxtd_dr_grass",
    0x9E89DF3E: "fxtd_dr_grass_wet",
    0x6624AB85: "fxtd_dr_sand",
    0xB8366E92: "fxtd_dr_sand_wet",
    0xEDDF159F: "fxtd_fly_asphalt",
    0x87D2AFB4: "fxtd_fly_dirt",
    0x3FC1D096: "fxtd_hit_grass",
    0xD6BA7BBF: "fxtd_hit_sand",
    0xEAA2E866: "fxtd_sk_asphalt",
    0xD2F8D5BA: "fxtd_sk_asphalt_no_leaves",
    0x45861103: "fxtd_sk_cobble",
    0xE3FFD367: "fxtd_sk_sand",
    0x7790C105: "fxtd_sl_asphalt",
    0x1C9BDE03: "fxtd_sl_grass",
    0x437A9A0B: "fxwtr_fountain1",
    0x0582CC9E: "fxwtr_waterbarrel_L",
    0x898EE059: "fxwtr_waterbarrel_sm",
    0x5CEA9D46: "gameplay",
    0x8E6342C8: "nis",
    0x0B11B7B2: "smoke",
    0x2CAD8553: "sparks",
    0x0AEE9EE6: "terraindriving",
    0x5A2E0437: "water",
    0x7CCE05D6: "xeci_car",
    0xC14B9283: "xecs_solid_wall",
    0x9576CACA: "env_lavasparks1",
    0xA9FF07DB: "env_lavasplatter1",
    0x239F3CCF: "fxenv_chimney2",
    0x15006165: "fxenv_fog2thick",
    0x36FB97EF: "fxenv_fountain4",
    0xA5A82FEC: "fxenv_fountain5",
    0x0AEE710E: "fxenv_fountain6",
    0x31E6B159: "fxenv_fountain7",
    0x18333526: "fxenv_lava1",
    0x0978DA3C: "fxenv_moths1",
    0x73EF880E: "fxenv_smokestack_refinery",
    0x39C8DA21: "fxenv_torchfire1",
    0xEFA8FADF: "fxenv_torchfire2",
    0x7C9A286D: "fxenv_torchfire3",
    0xE016F4CB: "fxenv_torchfire4",
}


class Counts:
    def __init__(self):
        self.chunks = 0
        self.written = 0
        self.rejected = 0
        self.stopped_early = False


def fmt_f32(value):
    """Shortest text that reads back as the same float32 (the C# command printed the same way)."""
    for digits in range(1, 10):
        text = "%.*g" % (digits, value)
        if struct.pack("<f", float(text)) == struct.pack("<f", value):
            if "e" in text:
                # no exponent in the file: 4e+01 -> 40
                text = format(Decimal(text), "f")
            return text
    return repr(value)


def scan_chunks(mm, start, end, rows, counts, depth=0):
    """Walks the chunk envelope (u32 type, u32 length, payload) in [start, end)."""
    pos = start
    while pos + 8 <= end:
        chunk_type, length = struct.unpack_from("<II", mm, pos)
        payload_start = pos + 8
        payload_end = payload_start + length

        if payload_end > end:
            # Truncated or misaligned - stop instead of walking garbage
            print("WARNING: stopping scan at offset 0x%X: chunk declares length %d which runs past "
                  "the current container's end (truncated file or misparsed alignment before this point)"
                  % (pos, length))
            counts.stopped_early = True
            return False

        if chunk_type == EMITTER_LIBRARY_ID:
            counts.chunks += 1
            parse_emitter_library(mm, pos, payload_start, length, rows, counts)
        elif chunk_type & CONTAINER_FLAG and depth < MAX_DEPTH:
            if not scan_chunks(mm, payload_start, payload_end, rows, counts, depth + 1):
                return False

        pos = payload_end
    return True


def parse_emitter_library(mm, chunk_offset, payload_start, payload_length, rows, counts):
    payload_end = payload_start + payload_length

    if payload_length < HEADER_SIZE:
        print("WARNING: EmitterLibrary chunk at 0x%X is only %d bytes - too small for the 0x18-byte header, skipping"
              % (chunk_offset, payload_length))
        return

    endian_swapped, version, library_count, section_number, record_count, chunk_section_ref = \
        struct.unpack_from("<6i", mm, payload_start)

    if endian_swapped != 0x11111111:
        print("WARNING: EmitterLibrary chunk at 0x%X has EndianSwapped=0x%08X (expected 0x11111111) - "
              "this chunk may be a different shape or a big-endian console build"
              % (chunk_offset, endian_swapped & 0xFFFFFFFF))

    if library_count != 0:
        print("INFO: EmitterLibrary chunk at 0x%X: library_count field is %d, not the usual 0 - "
              "worth a closer look, not necessarily wrong" % (chunk_offset, library_count))

    if record_count < 0 or record_count > MAX_RECORDS:
        print("WARNING: EmitterLibrary chunk at 0x%X declares record_count=%d - rejecting it as implausible "
              "(wrong chunk match, or the layout is wrong for this file)" % (chunk_offset, record_count))
        return

    pos = payload_start + HEADER_SIZE
    for record_index in range(record_count):
        if pos + RECORD_SIZE > payload_end:
            print("WARNING: EmitterLibrary chunk at 0x%X: ran out of room at record %d of %d - "
                  "stopping this chunk here" % (chunk_offset, record_index, record_count))
            return

        group_key, _pad, record_section_ref = struct.unpack_from("<III", mm, pos)
        matrix = struct.unpack_from("<12f", mm, pos + 0x10)  # rows 0-2, 4 floats each (4th = pad)
        wx, wy, wz = struct.unpack_from("<3f", mm, pos + 0x40)
        pos += RECORD_SIZE

        if not all(math.isfinite(v) and abs(v) < 1_000_000 for v in (wx, wy, wz)):
            counts.rejected += 1
            print("WARNING: rejecting record %d at 0x%X: pos=(%s, %s, %s) does not look like real data "
                  "(if this happens on every record the layout is wrong)" % (record_index, chunk_offset, wx, wy, wz))
            continue

        counts.written += 1
        group_name = KNOWN_EFFECT_NAMES.get(group_key, "UNKNOWN_0x%08X" % group_key)
        rows.append(("0x%X" % chunk_offset, section_number, chunk_section_ref, record_section_ref,
                     record_index, "0x%08X" % group_key, group_name, fmt_f32(wx), fmt_f32(wy), fmt_f32(wz),
                     *[fmt_f32(matrix[4 * r + a]) for r in range(3) for a in range(3)]))


def main():
    ap = argparse.ArgumentParser(description="Write the emitter trigger positions of a stream file to fx_triggers.tsv")
    ap.add_argument("file", help="Stream file (STREAML5RA.BUN)")
    ap.add_argument("--tsv", help="Output file (default outputs/fx_trigger_scan/fx_triggers.tsv)")
    args = ap.parse_args()

    from nfs_outputs import out_path
    tsv_path = args.tsv or out_path("fx_trigger_scan", "fx_triggers.tsv")

    print("Scanning %s for EmitterLibrary (0x%X) chunks" % (args.file, EMITTER_LIBRARY_ID))

    rows = []
    counts = Counts()
    with open(args.file, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        scan_chunks(mm, 0, len(mm), rows, counts)

    with open(tsv_path, "w", encoding="utf-8", newline="") as out:
        out.write("\t".join(["chunk_offset", "section_number", "chunk_section_ref", "record_section_ref",
                             "record_index", "group_key", "group_name", "world_x", "world_y", "world_z"] + ROT_COLS) + "\n")
        for row in rows:
            out.write("\t".join(str(v) for v in row) + "\n")

    print("Done. %d EmitterLibrary chunk(s) found, %d record(s) written, %d rejected by sanity checks. Wrote %s"
          % (counts.chunks, counts.written, counts.rejected, tsv_path))
    if counts.chunks == 0:
        print("WARNING: no EmitterLibrary chunks found in this file. Either it has none, or the chunk ID or "
              "container assumptions are wrong for this game/file version.")


if __name__ == "__main__":
    main()
