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
`scenery/nfs_stream_scenery.py` | Reader: scenery instances (stream file), override groups 0x34108/9 (region file). Command line writes `scenery_groups.tsv`, `scenery_overrides.tsv`, `scenery_infos.tsv` and `scenery_instances.tsv` (same columns as AssetDumper's files, so no AssetDumper run is needed; the region file adds Groups columns). With `--instances`, it joins the Groups columns to AssetDumper's file instead
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
`flares/flare_scenery_scan.py` | World positions of all scenery flares, with override group columns (`--hashes` names the groups)
`flares/flare_report.py` | Join flare TSV with vault `light_flares_cg.yml`. `--enriched` adds the `L0_*` / `L1_*` color columns
`flares/find_flare_hashes.py` | Find flare type table in the Carbon exe
**texture_anim/** |
`texture_anim/texture_anim_scan.py` | Scan TextureAnimPack chunks (frame-swap)
`texture_anim/uv_scroll_dump.py` | Dump UV scroll values per texture
**world_anim/** |
`world_anim/world_anim_scan.py` | Brute-force scan of the world_anim chunk family
`world_anim/parse_2600_anims.py` | Section 2600 rtnode and frames parser
`world_anim/dump_worldanim_2400.py` | Section 2400 diagnostic dump (confirmed struct)
`world_anim/dump_worldanim_allsections.py` | Whole-stream rtnode diagnostic by library/parent flags
`world_anim/nfs_anim_name_match.py` | Match instance names to rtnode data
**emitters/** |
`emitters/fx_trigger_scan.py` | Self-contained: emitter trigger positions and rotations from the stream file (0x3BC00) to `fx_triggers.tsv`
`emitters/extract_emitters.py` | Step 1: resolve emitters from Attribulator yml and `fx_triggers.tsv`
**lights/** |
`lights/light_pack_scan.py` | Self-contained: every light pack light (0x80135000) from the stream file to `lights.tsv`, with all fields incl. direction, type, state
**triggers/** |
`triggers/world_event_trigger_scan.py` | Scanner for 0x80036000 event triggers
**region/** (region file, out of scope) |
`region/nfs_region_parser.py` | Top-level region loader, per-game dispatch
`region/nfs_region_carbon.py`, `_mw`, `_prostreet`, `_undercover` | Per-game ChunksRelated structs
`region/diagnose_relations.py` | Check unk1/dataCount in ChunksRelated
`region/nfs_carp_parser.py` | CARP world grid and road network
`region/nfs_trackpath.py` | TrackPath zones and barriers
`region/nfs_trough_boundary.py` | TroughBoundary.bin (separate file): named drivable polygons and holes. Writes `troughs.tsv` and `trough_points.tsv`. Run on the real Carbon file
`region/nfs_collision_pack.py` | Collision packs of the stream file (chunk 0x3B801): instances (`ci`), articles (`ca`, strips and edges) and objects (`co`). Writes `collision_instances.tsv`, `collision_articles.tsv`, `collision_objects.tsv` and `collision_tags.tsv` (the tag census), and an OBJ of one section with `--obj`. `--match` (with `--region-bun L5RA.BUN` for the group numbers) prints the hit rate of each matching method (instances: article box against scenery box, then position; objects: index, position, inside); `nfs_stream_scenery.py --collision` uses the matches for `HasCollision`. Layouts from UCGT and the MW decomp, checked on synthetic data only: run it on the real file first
`region/nfs_world_grid.py` | World grid of the region file (CARP `CDat`: `CGrd` header, `CGcn` nodes). Writes `world_grid_nodes.tsv` and `world_grid_instances.tsv`. With `--stream` it checks which collision instances the grid lists and whether their true position lies in the listing cells. Layout from the MW decomp (WGrid, WGridNode); checked on synthetic data only: run it on the real file first
`region/export_sections.py` | Write `sections.json` (drivable boundaries + related IDs) from a region and a stream file. The viewer button uses it
**viewers/** |
`viewers/nfs_region_viewer.py` | Tkinter viewer. File menu (open region, stream, trough; Game), Settings menu (road network rotation, default 270), Viewer and Export tabs. Four layers (Nodes, Sections, Zones, Troughs): show/hide each, one active mode with its own options row and click selection, and a layer draw order (Raise/Lower). Section info is split into collapsible categories. Export tab: sections.json (road node export planned). Tested under Xvfb with the real TroughBoundary.bin; not yet run on a real region file
**beamng/** (reads middleman files, writes BeamNG formats) |
`beamng/particles/build_beamng_particles.py` | Step 3: `emitters_resolved.json` to BeamNG particle JSON
`beamng/flares/flare_lights_beamng.py` | Flare TSV to BeamNG PointLights (`items.level.json`), with `sectionID` and `sectionOverrideName`

## Flare pipeline (stream file to BeamNG lights)

    python flares/flare_scenery_scan.py STREAML5RA.BUN --hashes common/hashes_main.txt --tsv outputs/flares/flares.tsv
    python flares/flare_report.py outputs/flares/flares.tsv light_flares_cg.yml --enriched outputs/flares/flares_full.tsv
    python beamng/flares/flare_lights_beamng.py        (file picker: choose flares_full.tsv; writes items.level.json next to it)
