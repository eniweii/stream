# Tools

Rule: every tool writes generated files to `outputs/<tool-name>/` (git ignores `outputs/`).
New tools use `common/nfs_outputs.py` for this. Older tools are listed in `tool_gaps.md`
with what they do not fully read.

Path | Purpose
--- | ---
**common/** |
`common/nfs_region_common.py` | Chunk envelope (ID + length, container bit), recursive walk, boundary structs
`common/nfs_hashing.py` | Black Box `bin` string hash
`common/nfs_hash_dictionary.py` | Reverse hash lookup from `hashes_main.txt`
`common/nfs_hash_lookup_gui.py` | GUI: resolve a hex hash with both NFS hash algorithms
`common/nfs_chunk_inventory.py` | List every distinct chunk ID in a file (count, sizes, first offset)
`common/nfs_outputs.py` | Helper: `out_dir(tool)` returns `outputs/<tool>/`
`common/chunk_registry.py` | Compare a file's chunk IDs with `chunk_registry.tsv` (unregistered, label mismatch, not found)
`common/chunk_probe.py` | Inspect one chunk ID: sizes, stride guess, per-column table, hex records
**scenery/** |
`scenery/nfs_stream_scenery.py` | Reader: scenery instances, override groups (0x34108/9)
`scenery/nfs_scenery_dae_scan.py` | Read per-section DAE nodes (identity and transform)
`scenery/nfs_scn_ref_match.py` | Match section DAEs to a Blender position dump
`scenery/nfs_scn_ref_report.py` | CSV: DAE-Blender identity, group membership, SceneryGuid
`scenery/nfs_scn_node_report.py` | CSV: one row per DAE node, keyed by `dae_node_id`
`scenery/blender_scenery_position_dump.py` | Run in Blender: dump object bounding-box centers
**solids_materials/** |
`solids_materials/nfs_solid_reader.py` | Reader: ObjectPack (0x80134000) solids, materials, geometry
`solids_materials/carbon_material_viewer.py` | GUI viewer for `carbon_material_dictionary.json`
**flares/** |
`flares/flare_scan.py` | Flare packs (flare::pack, flare::instance)
`flares/flare_marker_scan.py` | position_marker records inside solids
`flares/flare_scenery_scan.py` | World positions of all scenery flares
`flares/flare_report.py` | Join flare TSV with vault `light_flares_cg.yml`
`flares/find_flare_hashes.py` | Find flare type table in the Carbon exe
**texture_anim/** |
`texture_anim/texture_anim_scan.py` | Scan TextureAnimPack chunks (frame-swap)
`texture_anim/uv_scroll_dump.py` | Dump UV scroll values per texture
**world_anim/** |
`world_anim/world_anim_scan.py` | Brute-force scan of the world_anim chunk family
`world_anim/rtnode_section_scanner.py` | rtnode chunks, sections 2400 and 2600
`world_anim/parse_2400_anims.py` | Section 2400 rtnode parser (older field table)
`world_anim/parse_2600_anims.py` | Section 2600 rtnode and frames parser
`world_anim/dump_worldanim_2400.py` | Section 2400 diagnostic dump (confirmed struct)
`world_anim/dump_worldanim_allsections.py` | Whole-stream rtnode diagnostic by library/parent flags
`world_anim/nfs_anim_name_match.py` | Match instance names to rtnode data
**emitters/** |
`emitters/extract_emitters.py` | Step 1: resolve emitters from Attribulator yml and `fx_triggers.tsv`
`emitters/build_beamng_particles.py` | Step 3: write BeamNG particle JSON
`emitters/dump_fx_trigger_matrices.py` | Test the 0x30 block of WorldFXTrigger records
**triggers/** |
`triggers/world_event_trigger_scan.py` | Scanner for 0x80036000 event triggers
**region/** (region file, out of scope) |
`region/nfs_region_parser.py` | Top-level region loader, per-game dispatch
`region/nfs_region_carbon.py`, `_mw`, `_prostreet`, `_undercover` | Per-game ChunksRelated structs
`region/diagnose_relations.py` | Check unk1/dataCount in ChunksRelated
`region/nfs_carp_parser.py` | CARP world grid and road network
`region/nfs_trackpath.py` | TrackPath zones and barriers
`region/track_path_zone_reader.py` | Standalone TrackPath scanner
**viewers/** |
`viewers/nfs_region_viewer.py` | Tkinter viewer: boundaries and relations
`viewers/nfs_3d_viewer.py` | 3D viewer with camera-based streaming
