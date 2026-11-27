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
- Exporter tab **Connection radius, junctions and runoffs** (default 0 = unlimited, so every junction is resolved and every arm and runoff is extended until it meets its road; set e.g. 50 to keep far arms as connectors): a through road, stem or runoff piece is only made when its length is within the radius, so a connection goes to the nearest partner of its own kind instead of across the map. A junction with an arm that cannot be connected within the radius (a big interchange) keeps its original NFS connectors untouched. L5RA.BUN at 50 m: 430 of 479 junctions resolved, longest connection 50 m (134 m without the radius).
- Node thinning keeps a node when its width differs from the interpolated width by more than half a metre (half the difference), so width changes at junction profiles stay while small wobbles do not.
- Exporter tab **Node width scale, all roads** (default 1, saved with the overrides): multiplies the node width of every exported road, on top of the per-corridor Width scale; a value like 0.95 keeps roads clear of collision packs.
- Exporter tab **Road detail / subdivision** (default 1, saved with the overrides): written as the DecalRoad `detail` field. BeamNG's default of 0.1 subdivides the road far too finely for invisible AI roads (the dense rungs in the road editor); the hand-made West Coast USA AI roads use 1.
- Exporter tab **Junctions: roads cross / end on the through road, runoffs extended, no connectors** (off by default, needs *Combine lanes*): the way hand-made BeamNG AI maps (e.g. West Coast USA) build junctions. Per junction with 3+ arms that are combined roads (or single-lane roads), opposite arms with the same lanes become one through road (one-way arms only pair an entering with a leaving arm), a crossing pair shares one node, and every other arm becomes a stem that keeps its heading and ends on a node placed on the through road, so the AI chooses by itself (autoJunction). The traffic and median connectors are dropped. Runoffs (sidewalk/shoulder, zone type 3) stay their own roads: their connectors are dropped and their ends are extended so each end has a pair (opposite ends join, others run on to a joined pair or the nearest road, a last resort joins the nearest runoff end); the extension pieces are two-way. Junctions with an arm that is not a combined road keep their connectors. On L5RA.BUN: 462 of 479 junctions resolved, 8,795 roads become about 3,430.
- **Junction connectors are combined too** (part of *Combine lanes into one DecalRoad*): a Decision connector with several lanes becomes ONE road like its neighbours (NoTraffic segments never do), and a connector whose lanes meet in a point at an end is combined as well. In a junction that is left unresolved (big interchange, an arm beyond the connection radius) the connectors stay, but as one road per connector instead of a fan of single lanes, median-only connectors are dropped (dead gap), and the shoulder/sidewalk ends are extended onto the arms and connectors next to them (up to 140 m, not the connection radius) so every end keeps a pair and shoulders and general-road zones stay connected. L5RA.BUN at 50 m: 4,209 roads become 3,483, no segment is skipped, 11 runoff ends unpaired.
- **Keep shoulders and sidewalks only where they connect a shortcut (General road) to a main road** (off by default) and **Shoulder extension past the connection** (12 m): a shoulder or sidewalk (zone type 2/3) is kept where its width touches a General road (type 10) and only if it also touches a main road (type 1 traffic); it is a connection test, not a radius. The touching stretch is extended by the given metres at both ends so the roads overlap and BeamNG's autoJunction joins them; the rest of the shoulder is cut away (`_near0`, `_near1`, ... when a shoulder connects in several places) and a shoulder that connects nothing is removed. Runs after the junction step and before node thinning; saved with the overrides. L5RA.BUN with no radius: shoulders 323.7 km become 18.8 km (668 of 2,433 shoulder roads kept), and no shortcut end loses its neighbour.
- **Ignore sidewalks and shoulders** (on by default): zone types 2 and 3 are dropped before the lanes are combined, so no shoulder/sidewalk road, connector or junction runoff is exported (the shoulder filter above then has nothing left to do). **Extend General roads to the nearest traffic road** (on by default): a free end of a General road (type 10) that does not already touch a traffic road or another General road runs on along its heading to the first traffic road it hits, or straight to the nearest point of the nearest traffic road when the heading misses (nearest means within the connection radius, 300 m when that is 0). Each extension is its own road with the General road's lanes and winding, its width tapering from the road's end width to the traffic road's width (never wider than the road). **Overlap past the road they meet** (default 2 m, 0 = none): the extension ends this far past the middle of the traffic road, and junction stems (T junctions) run on this far past the through road, so the roads overlap and BeamNG's autoJunction can join them. Through roads are not changed. L5RA.BUN with width scale 0.9 and no radius: 88 of 522 General road ends extended (35 along the heading, 53 to the nearest point, 2.0 km, longest 67 m), 355 stems carry the overlap node. Counts are in the Exporter tab and the `.report.json`. **General road extensions follow the sidewalk / shoulder connector curve of their end** (off by default): the source joins a road end to its neighbours with Decision connector segments (sidewalk/shoulder zone types, never exported), which are the smooth curves seen in the Zones view. A free General road end that has such a connector on its own source node follows its curve to the node at the other end, when that end lies on or within 3 m of a traffic road, then ends on the traffic road's middle plus the overlap; among several connectors the one ending closest to the traffic road wins, then the shortest, and one longer than 3 x the straight distance + 30 m is skipped (it would run alongside the road). The connector is joined to the road end and to the traffic road's middle by smooth curves (Hermite curves matching the road's heading and the connector's own direction, no jumps), and a guided path that still turns more than 30 degrees at any node is dropped, so the extension never wrinkles the road. The extension always goes the same way as the road: a connector that sets off against the road's heading is skipped, and one that sets off the right way but hooks back against it (a shortcut that runs both ways) is not followed round the hook: the extension follows it only up to where the direction has turned 60 degrees, leaves it there and goes straight into the traffic road ahead (ray along the heading, else the nearest road in front), ending on its middle plus the overlap so autoJunction can join them. Ends without a usable connector keep the straight extension. L5RA.BUN (General road drivability 0.5, width scale 0.9): 95 of 204 free ends follow a connector (37 of them unhooked), 6 more had a connector that would wrinkle and stay straight, the rest stay straight.
- **Winding is kept in every mode.** One-lane roads whose traffic runs against the source segment order (the zones on the left of a segment) are written with the nodes in the source winding and `lanesLeft` = their lanes, `oneWay` on (blue lanes in the road editor), instead of reversed nodes with `lanesRight`; the same traffic, but BeamNG's autoJunction and Y junctions then see the real winding (no NoTraffic-like flip). This applies to the unmodified per-lane export and to every remaining per-lane road with Combine on; combined carriageways already follow the winding.
- **Shoulders, medians and lanes never merge into one road**, whatever the drivability rules say: the merge signature carries the zone role (shoulder/sidewalk, median, lane). With the same drivability for every type (e.g. Shoulder and Median at 1.0) a shoulder piece used to merge into a lane or General road chain and the chain took the type of its first piece. Traffic and General road may still continue into each other. The shoulder filter also judges contact on the widths after *Node width scale*.
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
