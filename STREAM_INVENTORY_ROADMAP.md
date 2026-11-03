# Stream inventory roadmap (Carbon, ProStreet, Undercover)

Plan agreed 2026-11-03, no code yet. Findings from the inventory runs go in a separate
findings document later. Goal: know every byte of the stream file for each game and how
to read it, so it can be moved to BeamNG. This file is the plan. `common/chunk_registry.tsv`
stays the per-chunk ledger.

## Roles

- AssetDumper: meshes, materials, textures, LODs, and material naming (the UV/shader spec).
- stream (this repo): animations, emitters and other live scenery objects, region-file data,
  and the inventory of what exists in each file.
- beamng_stream: anything that writes a BeamNG format.

## What the sources cover

- hyperlinked: Carbon only (world_anim, texture info, flares, emitters, triggers, track path).
- UCGT: stream editor blocks (scenery, collision, CARP, region blocks, smoke spawners, heli
  sheet, vis curtains). No animation or emitter decoding.
- NFS-ModTools: per-game scenery, solid and TPK readers.
- nfsmw decomp: Most Wanted only; its animation structs do not match Carbon's.

So ProStreet and Undercover have no decomp. UCGT and NFS-ModTools give hints, real hex
decides, and the tool/output loop used for Carbon is the method.

## Already known

- Scenery chunks 0x34101, 0x34102, 0x34103 have the same IDs in all three games. The info
  record is 0x48 bytes in Undercover and 0x4C in ProStreet. ProStreet and Undercover instances
  also carry LodGroup, LodLevel, SwayMaxMultiplier, WSpeedMultiplier and PCA vertex-colour
  indices per LOD. Sway is wanted, so these need to reach the TSV.
- Texture packs: ProStreet uses the same TPK reader as Carbon (Version3Tpk), so scroll and
  frame swap should carry over. Undercover uses Version4Tpk, a different TextureStruct with
  the same scroll field names. Whether Undercover has a frame-swap pack is unknown.
- UCGT names some chunks the registry lists as unknown (unverified until hex-checked):
  0x34027 smoke spawners, 0x34159 heli sheet, 0x34185 vis curtains, 0x34104 scenery ads,
  0x34106 override hookups, 0x3B802 CARP world grid islands, 0x3B901 car collision bounds.
  Still unnamed: 0x34191 and 0x34111, 0x34113.
- UCGT constants are byte-reversed: its 0x46410300 is chunk 0x00034146. Read that way it names
  region-side rows the registry lists as unknown: 0x34110 offsets table, 0x34112 empty,
  0x34146 position markers, 0x34155 loading sections, 0x34156 elevation rules. UCGT calls
  0x34152 boundaries and 0x34153 relations, while the registry has 0x34153 as boundaries:
  unresolved, check per game.

## 1. Inventory design

- One ledger TSV per game under `common/registry/` (`carbon.tsv`, `prostreet.tsv`,
  `undercover.tsv`). The current `chunk_registry.tsv` becomes `carbon.tsv`.
- New columns: `parent`, `depth`, `count`, `size_min`, `size_max`, `layout`
  (hex-verified / source-only / unknown), `bytes_decoded`, `source`.
- Coverage per game: payload bytes with a verified layout versus total payload bytes, split
  by stream and region file. 100% means every byte is either decoded or kept raw.
- `common/nfs_chunk_inventory.py` already lists every chunk ID with counts and sizes. It needs
  parent/depth and TSV output, and its Carbon-specific watch list should become data in the
  ledger. `common/chunk_registry.py` gets a `--game` option.

## 2. Phases

1. Carbon close-out: run the inventory on the real stream and region files, reconcile with
   the ledger, label every chunk (stream or region), fill the unknown rows from the UCGT
   hints and hex-check them.
2. ProStreet: run the inventory, diff against Carbon (missing, new, resized chunks), then
   the families below.
3. Undercover: same, plus the Version4 texture pack differences.

Loop per chunk family: ledger row, `chunk_probe.py`, layout hypothesis from a source, hex
check on the real file, small reader, ledger row updated.

Family order in each game:

1. Scenery animation (Carbon: world_anim, 0x37220-0x37270). Does it exist, in which sections,
   is the rt_node the same size.
2. Texture animation: UV scroll (TextureStruct) and frame-swap pack (0x33312001/2,
   0xB3312000/4).
3. Emitters and live scenery objects: emitter library 0x3BC00, smoke spawners 0x34027, heli
   sheet 0x34159, vis curtains 0x34185, event triggers 0x80036000.
4. Instance fields not yet exported (flags, sway multipliers, LOD group/level, PCA indices),
   joined to AssetDumper output on Section + Instance.
5. Lights, flares, collision packs: confirm the layout per game.
6. Completeness: tree nodes, preculler, unknown chunks, kept raw.

Region-file chunks already have per-game parsers (`region/nfs_region_*.py`, including
Undercover). They go in the same ledger with `file = region` for labels and layout status; new
reader work there only for chunks no parser reads.

## 3. Material name spec (AssetDumper side)

The name carries the spec so BeamNG can assign effects per shader at runtime, and so Blender
never merges duplicate-datablock imports that use different shaders.

Current grammar in the fork's `test` branch (`ResolveMaterialExportName`):

- Carbon: `{diffuse}_{effect}`
- ProStreet: `{diffuse}_{effect}_{tag}`, where tag is a hash of the full captured texture set
- Undercover: `{diffuse}_{effect}_{comboTag}`, where the tag comes from the layer classifier

Proposed work, to settle per game with the inventory data:

1. Write the grammar down once per game: diffuse, effect, tag, and what each tag encodes.
2. Check that two materials with different UV routing can never share a name. The effect
   usually fixes the UV routing, but this needs verifying per effect from the vertex format
   tables (`shaderusages_legacy.txt` for ProStreet).
3. Keep the tag stable (hash inputs and order) so exports stay comparable between runs.
4. Stream-side input: which materials use scroll or frame swap, per game, so the animated
   ones keep separate names as in Carbon.

ProStreet notes: 8 TextureShaderUsage slots; ROAD has 3 real UV channels on one vertex set;
role tags seen are main, uv0, uv1, uv2, normal, normal slot2, lightmap (discarded).
Undercover notes: roles come from the name suffix, not slot position.

## 4. Current state (first inventory runs, 2026-11-03)

Inventories ran on Undercover (STREAML8R_MW2 and L8R_MW2) and ProStreet (AutobahnDrift). Presence
in the stream file, ProStreet / Undercover:

- world_anim: both. The rtnode is 140 bytes (Carbon 120). No frames chunk (0x37240) in
  Undercover; ProStreet has 17.
- Frame-swap pack (0x33312001/2, 0xB3312000/4): both.
- Emitters 0x3BC00: Undercover only. Smoke spawners 0x34027: both. Heli sheet 0x34159, vis
  curtains 0x34185 and 0x34104: Undercover only.
- Event triggers 0x80036000 and collision packs 0x3B801: both. 0x3B802 sits in the stream file in both.
- Large `0x00000000` filler chunks (21.9 MB in the ProStreet stream file): separate alignment
  padding from real gaps before computing coverage.
- Probe lessons: `chunk_probe.py` picked stride 0x44 for the rtnode, but a record is 0x88
  (a 4-byte 0x11 alignment pad plus 0x88 data); and it treats the first bytes of unaligned leaf
  chunks (frame-swap frames) as padding, hiding real data. Both are fixed in `chunk_probe.py`, and
  `chunk_probe_all.py` (whole file: nesting, examples per ID, index TSV as ledger seed) now does
  the first pass of every inventory run.

## 5. Next steps

1. Run `chunk_probe_all.py` on the real Carbon files (and `nfs_chunk_inventory.py` for the diff).
2. Decode the ProStreet and Undercover rtnode (probe with stride 0x88, compare with Carbon's
   layout; the 20 extra bytes include a fourth solid key and new float fields).
3. Probe the ProStreet frame-swap chunks the same way as Undercover's.
4. Label every unlabeled row in the per-game ledgers.
5. Material layers: dissect 0x134015, 0x134F01 and 0x134902 in Undercover for the layer
   classifier, and build the per-game material dictionaries that settle the UV and effect patterns.

## 6. Open questions

- Scroll timestep divisor and zero-speed behaviour were solved only empirically for Carbon.
- UCGT is an Undercover modding tool and its names may differ from the engine's.
- Console files (x360, PS3) are out of scope until the PC files are done.
