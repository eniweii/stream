"""
Top-level loader for Black Box NFS region/world .BUN files.

Dispatches to the right per-game module for the parts of the format that
differ between titles (currently just ChunksRelated / VisibleSections_
Relations - the chunk envelope and ChunkBoundary are shared and live in
nfs_region_common.py). See that module and each nfs_region_<game>.py for
what's actually verified vs. still a hypothesis per game.

`game` must be passed explicitly - there's no reliable way to detect it from
the filename. A level's track-code prefix (e.g. ProStreet's "L6R") identifies
the level, not the game.
"""
# stream bootstrap: make the shared library folders importable
import pathlib as _pl, sys as _sys
_sys.path[:0] = [str(_pl.Path(__file__).resolve().parents[1] / _d) for _d in ['common', 'region', 'scenery', 'solids_materials']]

from nfs_region_common import (
    VISIBLE_SECTIONS_ID, BOUNDARIES_ID, RELATIONS_ID, ELEVATION_ID, MANAGER_INFO_ID,
    walk_chunks, find_chunk, ChunkBoundary, parse_boundaries,
    ElevationRuleTriangle, parse_elevation, parse_manager_info, RegionWorld,
    section_letter, section_subsection, is_texture_section,
    is_library_section, is_regular_scenery_section, is_drivable_by_formula,
    format_section_label,
)

import nfs_region_prostreet
import nfs_region_undercover
import nfs_region_carbon
import nfs_region_mw

GAME_PARSERS = {
    'prostreet': nfs_region_prostreet,
    'undercover': nfs_region_undercover,
    'carbon': nfs_region_carbon,
    'mw': nfs_region_mw,
}


def get_label_functions(game):
    """Returns (section_letter_fn, format_section_label_fn) for this game.
    Falls back to nfs_region_common's MW/ProStreet-derived formula for any
    game module that doesn't define its own (currently just Undercover
    does, via UCGT's own divisor-1000 formula - see nfs_region_undercover.py)."""
    module = GAME_PARSERS.get(game)
    letter_fn = getattr(module, 'section_letter', section_letter)
    label_fn = getattr(module, 'format_section_label', format_section_label)
    return letter_fn, label_fn


def load_region_file(path, game='prostreet'):
    if game not in GAME_PARSERS:
        raise ValueError(f"Unknown game {game!r} - choose one of {sorted(GAME_PARSERS)}")
    parser_module = GAME_PARSERS[game]

    data = open(path, 'rb').read()

    vs = find_chunk(data, VISIBLE_SECTIONS_ID)
    if vs is None:
        raise ValueError("No VisibleSections (50410380) container found in this file")
    vs_start, vs_length = vs
    vs_end = vs_start + vs_length

    boundaries_hit = find_chunk(data, BOUNDARIES_ID, vs_start, vs_end)
    relations_hit = find_chunk(data, RELATIONS_ID, vs_start, vs_end)
    elevation_hit = find_chunk(data, ELEVATION_ID, vs_start, vs_end)
    manager_hit = find_chunk(data, MANAGER_INFO_ID, vs_start, vs_end)

    boundaries = parse_boundaries(data, *boundaries_hit) if boundaries_hit else []
    elevation = parse_elevation(data, *elevation_hit) if elevation_hit else []

    relations = []
    if relations_hit:
        try:
            relations = parser_module.parse_relations(data, *relations_hit)
        except (ValueError, NotImplementedError) as e:
            print(f"[load_region_file] WARNING: relations parsing failed for game={game!r}, "
                  f"proceeding with no relation data: {e}")
            relations = []

    lod_offset, drivable_section_ids = None, set()
    if manager_hit:
        try:
            lod_offset, drivable_section_ids = parse_manager_info(data, *manager_hit)
        except Exception as e:
            print(f"[load_region_file] WARNING: manager-info parsing failed for game={game!r}, "
                  f"proceeding without drivable-section data: {e}")

    road_network = None
    try:
        import nfs_carp_parser
        carp_offset = nfs_carp_parser.find_carp_offset(data)
        if carp_offset is not None:
            road_network = nfs_carp_parser.load_road_network_from_carp(data, carp_offset)
    except Exception as e:
        print(f"[load_region_file] WARNING: road network parsing failed for game={game!r}, "
              f"proceeding without it: {e}")

    track_paths = None
    try:
        import nfs_trackpath
        track_paths = nfs_trackpath.load_track_paths(data)
    except Exception as e:
        print(f"[load_region_file] WARNING: track path parsing failed for game={game!r}, "
              f"proceeding without it: {e}")

    return RegionWorld(boundaries, relations, elevation,
                        lod_offset=lod_offset, drivable_section_ids=drivable_section_ids,
                        road_network=road_network, track_paths=track_paths)


if __name__ == '__main__':
    import sys
    path = sys.argv[1]
    game = sys.argv[2] if len(sys.argv) > 2 else 'prostreet'
    w = load_region_file(path, game=game)
    print(f"[{game}] {len(w.boundaries)} boundaries, {len(w.relations)} relation records, "
          f"{len(w.elevation)} elevation triangles")
