# Traffic Node Editor & Exporter

Standalone Tkinter tool for building and iterating a BeamNG AI road network from the already-parsed NFS CARP / `WRoadNetwork` data. It does **not** import or modify `viewers/nfs_region_viewer.py`.

## Install into `eniweii/stream`

Copy these new files into the repository while preserving the folders:

```text
viewers/traffic_node_editor.py
region/traffic_corridors.py
beamng/traffic_node_exporter.py
tests/test_traffic_corridors.py
```

The app reuses existing `region/nfs_carp_parser.py` and `common/nfs_outputs.py`. Do not replace either of those.

## Run

From the repository root:

```bash
python viewers/traffic_node_editor.py
```

Or launch with a source file:

```bash
python viewers/traffic_node_editor.py "E:\Need for Speed Carbon\TRACKS\L5RA.BUN"
```

Choose the **main region/world BUN that contains the CARP road network**, not `STREAML5RA.BUN`. The app currently reads the selected file into memory in the same manner as the existing region parser.

## Current features

- Standalone UI with Traffic Editor, Rules & Patterns, and Exporter tabs.
- Reads the existing CARP parser output without modifying source records.
- Samples source Bezier segments from the stored endpoint/handle data.
- Derives editable profile-zone corridors using width, lateral offset, `middleZone`, and segment start/end inversion flags.
- Visualizes the source graph, color-coded corridors, direction arrows, and branch nodes.
- Editable source-type mapping: label, drivability, color, and export policy.
- Per-corridor overrides for classification, drivability, width scale, offset delta, direction, lane settings, `autoJunction`, and exclusion.
- Reusable patterns match source zone/profile signatures; later patterns win, local overrides have higher precedence.
- Saves state to `outputs/traffic_node_editor/traffic_overrides.json`; appends adjustment history to `outputs/traffic_node_editor/adjustment_log.jsonl`.
- Builds DecalRoads directly from the same per-zone geometry drawn in Zones mode. Every road carries a full ordered `nodes` array with per-node width; nodes are never exported as individual objects.
- Stitches lane zones by shared CARP source node and raw profile-zone identity, including simple continuations across `roadID` boundaries. Joins are topology-driven, not rejected because an offset endpoint differs by a fraction of a metre.
- Harmonizes endpoints of through lanes and decision connector zones by `(source node, raw profile zone)` so BeamNG `autoJunction` receives matching endpoints.
- Keeps junction decision connectors (`roadID = -1`) as individual curved DecalRoads so source turning movements are retained instead of being flattened or guessed.
- Exporter tab option **Combine lanes into one DecalRoad** (off by default, saved with the overrides). Per segment, the traffic lanes become ONE DecalRoad. Two-way segments get `lanesLeft`/`lanesRight` = the lanes on each side (uneven sides too, 4 left + 2 right), `oneWay=false`. One-sided segments (flagged one-way, or all lanes on one side) get `oneWay=true` with every lane on one road: `lanesRight=n` when traffic flows along the nodes, `lanesLeft=n` when it flows against them (node order is never reversed). The node is the centre of the whole carriageway and its width runs from the outer edge of the outermost lane to the outer edge of the outermost lane on the other side, so a median is a dead gap inside the width and is not exported as a road. Sidewalks, shoulders and any other zone between the lanes stay separate roads. Decision connectors, NoTraffic segments and lanes with an override keep the one-lane-per-road export. Counts and skip reasons are in the Exporter tab and the `.report.json`.
- Exporter tab **Node tolerance** (default 0.3 m, 0 = keep every node) and **Max node spacing** (default 25 m): after merging, every road's nodes are thinned (Douglas-Peucker over x/y/z and width) so straight pieces keep only their ends. Road ends and shared junction points never move. On L5RA.BUN this takes about 198k nodes to about 89k.
- Exporter tab **Connection radius, junctions and runoffs** (default 50 m, 0 = unlimited): a through road, stem or runoff piece is only made when its length is within the radius, so a connection goes to the nearest partner of its own kind instead of across the map. A junction with an arm that cannot be connected within the radius (a big interchange) keeps its original NFS connectors untouched. L5RA.BUN at 50 m: 430 of 479 junctions resolved, longest connection 50 m (134 m without the radius).
- Node thinning keeps a node when its width differs from the interpolated width by more than half a metre (half the difference), so width changes at junction profiles stay while small wobbles do not.
- Exporter tab **Node width scale, all roads** (default 1, saved with the overrides): multiplies the node width of every exported road, on top of the per-corridor Width scale; a value like 0.95 keeps roads clear of collision packs.
- Exporter tab **Road detail / subdivision** (default 1, saved with the overrides): written as the DecalRoad `detail` field. BeamNG's default of 0.1 subdivides the road far too finely for invisible AI roads (the dense rungs in the road editor); the hand-made West Coast USA AI roads use 1.
- Exporter tab **Junctions: roads cross / end on the through road, runoffs extended, no connectors** (off by default, needs *Combine lanes*): the way hand-made BeamNG AI maps (e.g. West Coast USA) build junctions. Per junction with 3+ arms that are combined roads (or single-lane roads), opposite arms with the same lanes become one through road (one-way arms only pair an entering with a leaving arm), a crossing pair shares one node, and every other arm becomes a stem that keeps its heading and ends on a node placed on the through road, so the AI chooses by itself (autoJunction). The traffic and median connectors are dropped. Runoffs (sidewalk/shoulder, zone type 3) stay their own roads: their connectors are dropped and their ends are extended so each end has a pair (opposite ends join, others run on to a joined pair or the nearest road, a last resort joins the nearest runoff end); the extension pieces are two-way. Junctions with an arm that is not a combined road keep their connectors. On L5RA.BUN: 462 of 479 junctions resolved, 8,795 roads become about 3,430.
- Exporter tab **Merge junction connectors into the roads they connect** (on by default): a Decision connector joins the road it leads into, or the one leading into it, when the link is unique. Branches (several connectors leaving or arriving at the same lane) stay split. L5RA.BUN: 9,877 roads become 8,795.
- Provides a persistent `overObjects` checkbox, enabled by default, in the Exporter tab.
- Exports BeamNG `DecalRoad` objects as line-delimited JSON plus a source manifest and diagnostics report.

## Export output

The exporter's output is modern `items.level.json` serialization: **one DecalRoad JSON object per line**, not one large JSON array. It writes a companion `.source_manifest.json` mapping generated roads back to source nodes, segments, profiles, and zones, plus a `.report.json` with counts and warnings.

The default export material is `road_invisible` because the first objective is AI pathing, not visual road replacement. `overObjects` defaults to enabled for a level whose physical road surface is provided by static meshes; toggle it off if the exported decals should project only to terrain. This setting is saved in `traffic_overrides.json`. Material and texture length are editable in the Exporter tab.

Zones mode is the geometry source for export; changing the UI to Corridors or Junctions mode does not change export geometry. The exporter stitches compatible zone paths into **one DecalRoad per continuous lane**, using shared source node IDs, raw profile-zone identity, travel orientation, and road-level settings. Each DecalRoad's `nodes` array contains all ordered sampled points from its connected pieces, and each node carries that zone's width. Source `Decision` junction connectors (`roadID = -1`) remain individual curved DecalRoads with harmonized approach endpoints and `autoJunction=true`, so BeamNG can connect the real turning curves. Ambiguous branch joins are left split rather than guessed.

## Important validation notes

This is a **first implementation**, not yet verified against the user's actual `L5RA.BUN` in BeamNG. Defaults are intentionally adjustable and must be treated as provisional while Carbon's profile-type mapping is refined.

- Default zone mapping: type 1 Traffic (`drivability=1`), type 3 Shoulder (`0.25`), type 4 Median (`0.10`), type 10 General road (`0.75`); other types excluded by default. Adjust these under Rules & Patterns.
- Eligible CARP `Decision` connectors are included by default so turning paths use the source's actual curves; the Exporter tab can turn them off for comparison. `NoTraffic` segments remain excluded unless a local `Force export` override deliberately includes a reviewed exception.
- Continuous source road chains become one DecalRoad per lane/corridor; ordinary lanes may still produce a large number of DecalRoad objects. The current provisional lane policy is `autoLanes=false`, `lanesLeft=0`, `lanesRight=1`, `oneWay=true`. Verify lateral AI offsets, width, direction, and `autoJunction` behavior in BeamNG on a small district before full export.
- The coordinate mapping is `(NFS x,y,z) -> (BeamNG x=z, y=-x, z=y+vertical_offset)`. The default `+150` vertical placement offset is applied by the exporter during final serialization exactly once, not baked into preview data. The vertical offset is editable and is recorded in the source manifest and report.
- Ordinary connections use BeamNG `autoJunction=true`. The editor shows branch nodes from the source graph; it does not generate a custom junction graph or `map.json` corrections. Set `autoJunction=false` locally for bad merges such as overpasses once validated.
- Profile endpoints with different zone counts are handled conservatively by generating corresponding indices present at both ends and reporting a warning. Type changes across a corridor are reported.
- This first pass does not split a single segment into multiple longitudinal pieces at every profile-type transition, nor does it infer traffic signal logic. Inspect warnings and preview before exporting a whole level.
- `DecalRoad` is a projected surface/navigation object, not collision geometry. Keep the existing scenery meshes and collision system for physical road surfaces.

## Tests

The conversion and serialization modules have synthetic tests (no game files needed):

Windows PowerShell:

```powershell
$env:PYTHONPATH = "region;beamng;common"
python -m unittest discover -s tests -v
```

Linux/macOS:

```bash
PYTHONPATH=region:beamng:common python -m unittest discover -s tests -v
```

A UI smoke test can be run under Xvfb on Linux, but loading and exporting an actual Carbon level and driving traffic in BeamNG remain required integration tests.
