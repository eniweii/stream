# What the tools do not fully read

Source: reading the code in this repo (2026-10-03). Items marked (doc) come from a
docstring or comment, not from running the tool. No tool here was run on a real file for
this list. Byte ranges are offsets inside one record.

## scenery/

**nfs_stream_scenery.py** (Tier 1, items 1-3)
- Header 0x34101: only the section number (+0x0C) is read. The rest is dropped.
- SceneryInfo 0x34102 (0x48 bytes): reads name (24 bytes), the first solid key (+0x18), and flags (+0x3C).
  Not read: the other three solid keys (+0x1C, +0x20, +0x24), +0x28..+0x3B, +0x40..+0x47.
  `flares/flare_scenery_scan.py` reads `solid_keys[4]` on its own. This module does not.
- SceneryInstance 0x34103 (0x60 bytes): reads flags, position, rotation, guid, info index.
  Not read: +0x00..+0x0B, +0x10..+0x1F, +0x56..+0x5F.
- Other chunks inside a 0x80034100 section are skipped: 0x34105 tree nodes, 0x34107 preculler, 0x3410D.
- Override infos (0x34108) and groups (0x34109): every field is read. No gap.
- (doc) Not run against a real file.

**nfs_scenery_dae_scan.py** (doc) Not verified against a real exported .dae.
**nfs_scn_ref_match.py** (doc) First empirical check, not a finished pipeline.

## solids_materials/

**nfs_solid_reader.py** (Tier 1, item 4)
- Solid header 0xA0 bytes: reads version, hash, bounds, name. Pivot matrix and marker count are not exposed.
  `flare_scenery_scan.py` reads them itself.
- Shading group (144 bytes): reads bounds (+0x00..+0x17), six texture-slot bytes (+0x18..+0x1D),
  effect id (+0x30), flags (+0x38), sort key (+0x3C), vertex count (+0x40), triangle count (+0x60).
  The light material number (+0x1D) is read, then discarded. Everything else is unread:
  +0x1E..+0x2F, +0x32..+0x37, +0x44..+0x5F, +0x64..+0x8F.
- Texture list 0x134012: the first u32 of each 8-byte entry is read. The second u32 is dropped.
- Skipped on purpose (code comment): light materials, position markers (0x13401A), plat info, UCAP frame weights.
- (doc) Not run against a real file.

**carbon_material_viewer.py** shows `carbon_material_dictionary.json` from AssetDumper. It does not read the stream file.

## flares/

**flare_scenery_scan.py** (Tier 1, item 5)
- Reads one LOD only (`--lod`, default 2). Flares on solids in the other LODs are not found.
- Reads instance flags and writes them to the output, but does not apply them. A flare on an excluded instance still appears.
- Does not apply override groups, so a flare on a group-disabled instance still appears.
- Skips section 2600 unless `--include-2600` is set.
- Reads only 0x50-byte markers. The 44 chunks at size 92 are not explained (roadmap).

**flare_scan.py** Reads the layout in its docstring (pack 0x60, instance 0x30). The instance flags byte (+0x2D) is printed raw, not decoded.
**flare_report.py**, **find_flare_hashes.py** Work from the Carbon exe and the vault yml. They do not read the stream file.

## texture_anim/ (the gap we saw before)

**texture_anim_scan.py** (Tier 1, item 7)
- Scanner only. It prints text. There is no TSV or JSON output for a viewer or exporter.
- Header (0x33312001): reads name, key, frame count, fps, time base (up to +0x1F). Two zero gaps (8 and 12 bytes)
  in the header are not identified (doc). Nothing after +0x1F is read.
- Frames (0x33312002): the first u32 of each 12-byte entry is read as the frame hash. The other 8 bytes
  of each entry are not decoded.
- Containers 0xB3312000 and 0xB3312004: hex dump only. No decode.
- Frame hashes are not resolved to texture names.
- (doc) The docstring says to point it at a .TPK, not the stream file. The roadmap says the chunks are in STREAML5RA.BUN. Check which is right.

**uv_scroll_dump.py** Scroll and snap data is not dropped: it reads scroll type (including snap), timestep, speed S/T,
offset S/T, and scale S/T, and writes the raw values. The divisors that turn them into UV units are not confirmed (doc).
`texture_anim_scan.py` is frame-swap only and never touches scroll data. Reads 11 fields of the 88-byte TextureStruct (0x33310004): hash, size, tilable UV,
scroll type, timestep, speeds, offset, scale. Fields it does not read (none are scroll data): alpha blend type, alpha usage type, alpha sort,
bias level, flags, rendering order, mipmap bias. (The C# reader keeps these; see the audit.)

## world_anim/ (parser closed, playback open)

- (doc) `unknown_0x6e` is unconfirmed. The raw flags byte (+0x4A) bits 0x08, 0x10, 0x20 are printed but not confirmed.
- (doc) Open for playback: oscillation formula, delay source, timescale byte.
- (doc) Library and parent anim borrowing (`use_library_anim`, `use_parent_anim`) is only bucketed and counted. Nothing resolves the borrow.
- **parse_2600_anims.py** pairs a frames chunk with its rtnode only when the frames chunk comes right after. Other cases print a warning.

## emitters/

- The 0x30 block of each 0x50-byte WorldFXTrigger record is decoded: a 3x3 rotation matrix, position in row 3. The C# export writes the rotation columns. No gap.
- **extract_emitters.py** Does not read the stream file. It needs `fx_triggers.tsv` from the C# AssetDumper command and Attribulator yml files.
  Hash-only texture names stay unresolved unless you give it the texture folder.
- **build_beamng_particles.py** Skips layers with emission rate 0 and linked layers. Applies only the Z part of Accel (it counts the horizontal part as ignored).

## triggers/

**world_event_trigger_scan.py** Decoded as ambient audio triggers (set aside). The AABB tree (0x36002) leaves are sampled (`--leaf-samples`), not fully decoded.

## region/ (out of scope for the stream roadmap)

- **nfs_trackpath.py** Zone `user_data` is a runtime pointer, kept raw. `data[4]` is kept raw. Barriers are fully read.
- **nfs_carp_parser.py** (doc) Container offsets are confirmed on a ProStreet file. Field values are not checked.
- **nfs_region_undercover.py** (doc) Ported from UCGT. Not checked on a real Undercover file.
- **nfs_region_mw.py** (doc) Derived from decompiled source. Not hex-verified.
- **nfs_region_common.py** `ChunkBoundary` fields `unk2` and `unk3` have no known meaning.

## common/

**nfs_chunk_inventory.py** Prints only. Its watch list has 13 IDs. Use `chunk_registry.py` for coverage against all known IDs.

## What this means for the roadmap

| Roadmap item | Gap |
|---|---|
| T1.1 SceneryInfo | Solid keys 2-4 not read in `nfs_stream_scenery.py` |
| T1.2 SceneryInstance | Flags read, but three byte ranges unread; flare tool does not apply them |
| T1.4 Materials | Shading group bytes +0x1E..+0x2F and others unread; texture list second u32 dropped |
| T1.5 SolidMarkers | One LOD only; 44 size-92 chunks open; flags and groups not applied |
| T1.7 Texture anim | Scanner only: no structured output, frame entry bytes 4-11 and inst/pack containers unread |
| T2 unread chunks | 0x34105, 0x34107, 0x3410D, 0x135002, 0x3B801/2, unknown IDs have no tool yet |

## Removed as superseded (2026-10-03)

| Removed | Replaced by |
|---|---|
| `world_anim/parse_2400_anims.py` | `world_anim/dump_worldanim_2400.py` (confirmed rt_node layout; the old field table merged fields wrongly) |
| `world_anim/rtnode_section_scanner.py` | `world_anim/dump_worldanim_allsections.py` (same confirmed layout, all sections) |
| `region/track_path_zone_reader.py` | `region/nfs_trackpath.py` (same layout, used by the viewer) |
| `emitters/dump_fx_trigger_matrices.py` | Decoded: 0x30 block is a rotation matrix, exported by the C# fx-triggers command |
| `flares/flare_marker_scan.py` | `flares/flare_scenery_scan.py` (marker chunk 0x13401A confirmed). For the 44 size-92 chunks use `common/chunk_probe.py --size 92` |
