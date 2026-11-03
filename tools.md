# Tools

Rule: `stream` holds readers, viewers and plain middle files (TSV or JSON). Nothing here writes
an engine schema; BeamNG writers belong in `beamng_stream` (see "Pending move" at the end).
Every tool writes generated files to `outputs/<tool-name>/` (git ignores `outputs/`).
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
`flares/flare_export.py` | One pass: pick the stream file and the dump folder, scan, then enrich. Writes `outputs/flares/flares.tsv` and `flares_full.tsv`
`flares/flare_scan.py` | Diagnostic (kept for the newer games): flare packs (flare::pack, flare::instance)
`flares/flare_scenery_scan.py` | World positions of all scenery flares, with override group columns (`--hashes` names the groups)
`flares/flare_report.py` | Join flare TSV with vault `light_flares_cg.yml`. `--enriched` adds the `L0_*` / `L1_*` color columns
`flares/find_flare_hashes.py` | Diagnostic (kept for the newer games): find flare type table in the Carbon exe
**texture_anim/** |
`texture_anim/texture_anim_scan.py` | Scan TextureAnimPack chunks (frame-swap)
`texture_anim/uv_scroll_dump.py` | Dump UV scroll values per texture
**world_anim/** (diagnostics, to be replaced by a counting tool for the newer games) |
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
**region/** (region file) |
`region/nfs_region_parser.py` | Top-level region loader, per-game dispatch
`region/nfs_region_carbon.py`, `_mw`, `_prostreet`, `_undercover` | Per-game ChunksRelated structs
`region/nfs_carp_parser.py` | CARP world grid and road network
`region/road_lanes.py` | Plain helper for the viewer's lane view: lane strips per segment (zone sides, mirrored ends, zone pairing), the sampled centre curve of Curved segments, lane direction, one colour per lane type, and the profile census and segment text. No GUI, no engine transform
`region/export_road_network.py` | Write `road_network.json` (nodes, segments, roads, lane profiles as parsed, no transform). The viewer button calls it
`region/nfs_trackpath.py` | TrackPath zones and barriers
`region/nfs_trough_boundary.py` | TroughBoundary.bin (separate file): named drivable polygons and holes. Writes `troughs.tsv` and `trough_points.tsv`. Run on the real Carbon file
`region/nfs_collision_pack.py` | Collision packs of the stream file (chunk 0x3B801): instances (`ci`), articles (`ca`, strips and edges) and objects (`co`). Writes `collision_instances.tsv`, `collision_articles.tsv`, `collision_objects.tsv` and `collision_tags.tsv` (the tag census), and an OBJ of one section with `--obj`. `--match` (with `--region-bun L5RA.BUN` for the group numbers) prints the hit rate of each matching method (instances: article box against scenery box, then position; objects: index, position, inside); `nfs_stream_scenery.py --collision` uses the matches for `HasCollision`. Layouts from UCGT and the MW decomp, checked on synthetic data only: run it on the real file first
`region/nfs_world_grid.py` | World grid of the region file (CARP `CDat`: `CGrd` header, `CGcn` nodes). Writes `world_grid_nodes.tsv` and `world_grid_instances.tsv`. With `--stream` it checks which collision instances the grid lists and whether their true position lies in the listing cells. Layout from the MW decomp (WGrid, WGridNode); checked on synthetic data only: run it on the real file first
`region/export_zones.py` | Write `zones.json` (every TrackPathZone, raw region plane). The viewer button calls it
`region/nfs_collision_surfaces.py` | Collision surface census: surface hashes used by the collision packs, resolved names, triangle and edge counts. Writes `surface_mapping.tsv`
`region/export_sections.py` | Write `sections.json` (drivable boundaries + related IDs) from a region and a stream file. The viewer button uses it
**viewers/** |
`viewers/nfs_region_viewer.py` | Tkinter viewer. File menu (open region, stream, trough; Game), Settings menu (road network rotation, default 270; Lane legend window), Viewer and Export tabs. Layers: Nodes, Lanes (lane strips along the real curve of each road segment, arrows on traffic lanes, click shows the lanes at both ends, Profiles... census), Sections, Zones, Troughs, Collision: show/hide each, one active mode with its own options row and click selection, and a layer draw order (Raise/Lower). Section info is split into collapsible categories. Export tab: sections.json, zones.json, road_network.json. Tested under Xvfb with the real TroughBoundary.bin; not yet run on a real region file
`viewers/carbon_plat_viewer.py` | Tkinter viewer over every Carbon platform material entry (0x134B02) of the stream file; companion to `solids_materials/carbon_material_viewer.py`
`viewers/traffic_node_editor.py` | Standalone traffic node editor and exporter (moves to `beamng_stream`; see README_traffic_node_editor.md)
**beamng/** (reads middleman files, writes BeamNG formats; moves to `beamng_stream`) |
`beamng/particles/build_beamng_particles.py` | Step 3: `emitters_resolved.json` to BeamNG particle JSON
`beamng/traffic_node_exporter.py` | Derived corridors to `items.level.json` DecalRoad lines (used by the traffic node editor)
`beamng/flares/flare_lights_beamng.py` | Flare TSV to BeamNG PointLights (`items.level.json`), with `sectionID` and `sectionOverrideName`

## specconvert/ (standalone, not part of the stream pipeline)

Lives here only to avoid another repository. Reads and writes textures and `materials.json`; never reads game files.

Path | Purpose
--- | ---
`specconvert/specular_converter.py` | Specular DDS to roughness and metalness
`specconvert/ref_mask_converter.py` | REF DDS to a binary mask
`specconvert/illum_map_converter.py` | Illumination map conversion
`specconvert/flip_normal_green.py` | Invert the green channel of normal-map DDS files (needs texconv)
`specconvert/normal_ao_generator.py` | Normal and AO maps from REF masks, diffuse and existing normals
`specconvert/pipelines.py`, `specconvert/candidate_grid.py` | Diffuse-only height pipelines and the candidate picker used by the generator
`specconvert/materials_populator.py` | Batch edit of `materials.json`: texture matching, alpha/UV settings, emissive

## tests/ (synthetic, no game files needed)

`tests/test_collision_dae_export.py`, `tests/test_collision_surfaces.py`, `tests/test_flare_lights_beamng.py`, `tests/test_traffic_corridors.py`, `tests/test_traffic_topology.py`, `tests/test_road_network_export.py`

## Pending move to beamng_stream

These write BeamNG formats or use the BeamNG transform, so they leave `stream` once `beamng_stream` has them:
`beamng/` (all three scripts), `viewers/traffic_node_editor.py` with `README_traffic_node_editor.md`, `region/traffic_corridors.py`, `region/traffic_topology.py`, `region/nfs_collision_dae_export.py`, and the tests for them (`test_traffic_*`, `test_flare_lights_beamng`, `test_collision_dae_export`).
The editor will read `road_network.json` (`region/export_road_network.py`, the viewer's Export tab). Its tests (`test_road_network_export`) stay in `stream`.

## Flare pipeline (stream file to BeamNG lights)

    python flares/flare_export.py        (pickers: STREAML5RA.BUN, then the dump folder with light_flares_cg.yml; hashes from common/hashes_main.txt)
    python beamng/flares/flare_lights_beamng.py        (file picker opens in outputs/flares: choose flares_full.tsv; writes items.level.json next to it)
