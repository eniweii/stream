# Carbon flare datablock patch

Target repository: https://github.com/eniweii/stream

## What changed

Replace `beamng/flares/flare_lights_beamng.py` with the included version. It keeps the existing enriched TSV input but now writes:

- `nfsc.flares.datablocks.json`: reusable `LightFlareData` objects plus deduplicated `LightAnimData` objects.
- `items.level.json`: one `PointLight` JSON object per available L0/L1 flare layer, each wired to `flareType` and `flareScale`.
- `nfsc_flares_build_report.txt`: counts, texture-dimension fallbacks, and known approximations.

`tests/test_flare_lights_beamng.py` covers datablock generation, deduplication, height offset and override metadata, marker tint, blinking animation and empty layers.

## Run

From the repo root:

```bash
python flares/flare_scenery_scan.py STREAML5RA.BUN --hashes common/hashes_main.txt --tsv outputs/flares/flares.tsv
python flares/flare_report.py outputs/flares/flares.tsv light_flares_cg.yml --enriched outputs/flares/flares_full.tsv
python beamng/flares/flare_lights_beamng.py outputs/flares/flares_full.tsv
python -m unittest discover -s tests -p 'test_flare_lights_beamng.py'
```

The five sprites already live in `levels/nfsc/Assets/Flares/`: `HEADLIGHTFLAREINNER`, `HEADLIGHTFLAREOUTER`, `HEADLIGHTGLOW`, `HEADLIGHTCATSEYE`, and `LAMPPOSTFLARE`. The exporter checks that level directory relative to the repository root (and current working directory), reads dimensions from the existing assets, and reuses each actual filename and extension in `flareTexture`. `--texture-source-dir` is optional; it is only a supplemental image search path. `.dds` is the fallback extension if an asset cannot be found, and 256x256 is the configurable fallback size.

## Design notes

- Flares are screen-space visuals and do not add real illumination. Keep the PointLight intensity/radius as a separate porting choice.
- The current implementation writes one sprite element per flare datablock; each `elementDist` is `0.0` (over the source light), and `elementUseLightColor` is `true`.
- Each datablock is keyed by the used `L*_params` vault entry, not by individual placement. The used count therefore follows the entries present in the enriched TSV.
- `overallScale` approximates Carbon's `MinScale`/`Maxscale` by their midpoint multiplied by `--overall-scale`; Carbon's `MinSize`, `MaxSize`, and `Power` distance behavior does not have a one-to-one `LightFlareData` equivalent.
- A non-zero 4-byte marker tint is applied to the PointLight; a zero marker tint uses the vault layer's RGBA values.
- Blink cycles are amber 0.8s, red 1.0s, green 0.9s full-cycle durations, synchronized at phase zero; these correspond to Carbon's 0.4/0.5/0.45 second on/off half-periods. Runtime appearance still needs an in-game check.

## References checked

- Repository layout and emitter generator: `beamng/particles/build_beamng_particles.py` writes a unified name-keyed `nfsc.datablocks.json`; its `main` writes each placed node as JSONL.
- `flares/flare_report.py`: `TYPE_PARAMS`, `TEXTURES`, and `BLINK` are the source of truth for the `L0_*`/`L1_*` vault mapping and the three synchronized blink timers. The enriched schema uses `L0_params`/`L1_params` (plural).
- `beamng/flares/flare_lights_beamng.py` before replacement only emitted `PointLight` JSONL entries and used `sectionID` / `sectionOverrideName`.
- BeamNG light fields: https://documentation.beamng.com/modding/levels/level_classes/lights/ (documents `flareType`, `flareScale`, `animationType`, `animationPeriod`, and `animationPhase`).
- BeamNG datablocks: https://documentation.beamng.com/modding/levels/level_formats/datablocks/ (name-keyed `*.datablocks.json`, `LightFlareData`, `LightAnimData`, texture asset paths).
- Torque `LightFlareData` reference: https://t3ddocs.lukasj.org/scripting/class/classlightflaredata/ (pixel-space `elementRect`, `elementDist=0` at source light, maximum 10 elements).
- Torque `LightAnimData` reference: https://torque3d.readthedocs.io/en/3.5/script/class/LightAnimData.html (`brightnessA`, `brightnessZ`, `brightnessPeriod`, `brightnessKeys`, `brightnessSmooth`; `aza` with non-smooth transitions gives a stepped off/on/off pulse).

## Repository write limitation

The connected GitHub integration allowed reading the public repo, but rejected branch creation and file updates with HTTP 403. These files are a local patch bundle; they have not been committed to the GitHub repo or tested in BeamNG itself.
