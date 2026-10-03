"""
Shared, game-agnostic pieces of the Black Box NFS region/world .BUN parser.

Covers:
- the generic chunk envelope (RIFF-like: 4-byte ID + 4-byte LE length +
  payload; high bit of the ID's last raw byte marks a container), walked
  recursively
- ChunkBoundary / ElevationRuleTriangle, confirmed byte-identical between
  Undercover and ProStreet - see nfs_region_prostreet.py for what is NOT
  shared (ChunksRelated)
- the VisibleSection "letter" naming scheme, taken directly from Need for
  Speed Most Wanted's decompiled source
  (github.com/dbalatoni13/nfsmw, Speed/Indep/Src/World/VisibleSection.hpp):
  GetScenerySectionLetter/IsTextureSection/IsLibrarySection. This is NOT the
  same thing as UCGT's separate StreamChunksOffsets naming scheme
  (25000=Z, 24000=Y, ... a different, coarser world-streaming-chunk system)
  - don't confuse the two letter schemes.
  Verified against a real ProStreet file: relatedChunkIDs 2401 and 2501 (from
  L6R_AutobahnDrift.BUN's relations block) decode to letters 'X' and 'Y',
  which per MW's IsLibrarySection/IsTextureSection are exactly "library" and
  "texture" section categories - a section legitimately referencing the
  library asset and texture set it needs, not parser noise.
- RegionWorld, the parsed-file container every per-game parser returns
"""
import struct

VISIBLE_SECTIONS_ID = bytes.fromhex('50410380')   # BCHUNK_SPEED_VISIBLE_SECTION_CHUNKS (container)
BOUNDARIES_ID = bytes.fromhex('52410300')          # BCHUNK_SPEED_VISIBLE_SECTION_BOUNDARIES
MANAGER_INFO_ID = bytes.fromhex('51410300')        # BCHUNK_SPEED_VISIBLE_SECTION_MANAGER_INFO
# Naming correction: what this project originally called "VisibleSections_
# Relations" is, per UCGT's own bChunkID.java, BCHUNK_SPEED_VISIBLE_SECTION_
# DRIVABLE (0x00034153) - i.e. it's the per-drivable-section list (each
# record's ID is a drivable section, and its array is that section's set of
# concurrently-visible sections), not a generic adjacency/relations concept.
# Kept as RELATIONS_ID (with this alias) so existing per-game modules don't
# need renaming.
RELATIONS_ID = bytes.fromhex('53410300')
DRIVABLE_ID = RELATIONS_ID
ELEVATION_ID = bytes.fromhex('56410300')           # BCHUNK_SPEED_ELEV_POLYS


def walk_chunks(data, start=0, end=None):
    """Yields (offset, raw_id, id_hex, length, is_container, payload_start)
    for every chunk at this level, recursing automatically into containers."""
    if end is None:
        end = len(data)
    pos = start
    while pos + 8 <= end:
        raw_id = data[pos:pos + 4]
        length = int.from_bytes(data[pos + 4:pos + 8], 'little')
        is_container = bool(raw_id[3] & 0x80)
        payload_start = pos + 8
        payload_end = payload_start + length
        if payload_end > end or length < 0:
            break  # truncated/misaligned - stop rather than misparse further
        yield (pos, raw_id, raw_id.hex(), length, is_container, payload_start)
        if is_container:
            yield from walk_chunks(data, payload_start, payload_end)
        pos = payload_end


def find_chunk(data, target_id_bytes, start=0, end=None):
    """Returns (payload_start, length) of the first matching chunk, or None."""
    for offset, raw_id, id_hex, length, is_container, payload_start in walk_chunks(data, start, end):
        if raw_id == target_id_bytes:
            return payload_start, length
    return None


# ---- ChunkBoundary (VisibleSections_Boundaries) ----
# Confirmed byte-exact identical between Undercover (UCGT's documented layout)
# and ProStreet (verified against a real L6R_AutobahnDrift.BUN file: 93
# records parsed, consuming the block's declared length exactly).
class ChunkBoundary:
    __slots__ = ('offset', 'type1', 'type2', 'ID', 'numPoints', 'type', 'ID_over',
                 'unk2', 'elevationHash', 'unk3', 'boundsMin', 'boundsMax', 'pos', 'points')

    def __init__(self, data, offset):
        self.offset = offset
        self.type1, self.type2 = struct.unpack_from('<ii', data, offset)
        self.ID = struct.unpack_from('<h', data, offset + 8)[0]
        self.numPoints = data[offset + 10]
        self.type = data[offset + 11]
        self.ID_over = struct.unpack_from('<h', data, offset + 12)[0]
        self.unk2 = struct.unpack_from('<h', data, offset + 14)[0]
        self.elevationHash, self.unk3 = struct.unpack_from('<ii', data, offset + 16)
        bx0, by0, bx1, by1, px, py = struct.unpack_from('<ffffff', data, offset + 24)
        self.boundsMin = (bx0, by0)
        self.boundsMax = (bx1, by1)
        self.pos = (px, py)
        pts = []
        p = offset + 48
        for _ in range(self.numPoints):
            x, y = struct.unpack_from('<ff', data, p)
            pts.append((x, y))
            p += 8
        self.points = pts

    @property
    def size(self):
        return 48 + self.numPoints * 8


def parse_boundaries(data, payload_start, length):
    end = payload_start + length
    pos = payload_start
    out = []
    while pos < end:
        b = ChunkBoundary(data, pos)
        out.append(b)
        pos += b.size
    return out


# ---- ElevationRuleTriangle (ChunksElevationRules) ----
class ElevationRuleTriangle:
    __slots__ = ('offset', 'hash', 'points')
    SIZE = 64  # fixed-size record: 16-byte header + 3 * 16-byte points

    def __init__(self, data, offset):
        self.offset = offset
        self.hash = struct.unpack_from('<i', data, offset)[0]
        pts = []
        p = offset + 16
        for _ in range(3):
            x, y, z = struct.unpack_from('<fff', data, p)
            pts.append((x, y, z))
            p += 16  # 3 floats + 1 padding int
        self.points = pts


def parse_elevation(data, payload_start, length):
    end = payload_start + length
    pos = payload_start
    out = []
    while pos + ElevationRuleTriangle.SIZE <= end:
        out.append(ElevationRuleTriangle(data, pos))
        pos += ElevationRuleTriangle.SIZE
    if pos != end:
        print(f"[parse_elevation] WARNING: {end - pos} leftover bytes "
              f"(block length {length} doesn't divide evenly by {ElevationRuleTriangle.SIZE}) "
              f"- struct likely doesn't apply to this block as-is")
    return out


# ---- Section letter / category scheme (from MW's decompiled VisibleSection.hpp) ----

def section_letter(section_number):
    """letter = section_number // 100 + 'A' - 1, per MW's GetScenerySectionLetter.
    This is MW/ProStreet's formula specifically (divisor 100, subsections
    capped at 0-99). Undercover uses a DIFFERENT formula (divisor 1000, no
    -1 offset, subsections up to 999) - see nfs_region_undercover.py's own
    section_letter()/format_section_label(), which override these for that
    game. Don't apply this function to Undercover section IDs."""
    return chr(section_number // 100 + ord('A') - 1)


def section_subsection(section_number):
    """Per MW's GetScenerySubsectionNumber."""
    return section_number % 100


def is_texture_section(section_number):
    """Per MW's IsTextureSection (letter Y or W)."""
    return section_letter(section_number) in ('Y', 'W')


def is_library_section(section_number):
    """Per MW's IsLibrarySection (letter X or U)."""
    return section_letter(section_number) in ('X', 'U')


def is_regular_scenery_section(section_number):
    """Per MW's IsRegularScenerySection (letter A through T)."""
    letter = section_letter(section_number)
    return 'A' <= letter < 'U'


def short_section_label(section_number):
    """The letter+subsection form the game's own debug UI actually shows,
    e.g. 102 -> 'A2', 1412 -> 'N12' (confirmed against real Carbon debug
    screenshots: section 102 displayed as 'A2', section 1412 as 'N12')."""
    return f"{section_letter(section_number)}{section_subsection(section_number)}"


def format_section_label(section_number):
    """Full label with the game's actual short form alongside it, e.g.
    101 -> 'A101 (A1)', 2401 -> 'X2401 (X1) [library]'."""
    letter = section_letter(section_number)
    label = f"{letter}{section_number} ({short_section_label(section_number)})"
    if is_library_section(section_number):
        label += " [library]"
    elif is_texture_section(section_number):
        label += " [texture]"
    return label


# ---- VisibleSectionManagerInfo (BCHUNK_SPEED_VISIBLE_SECTION_MANAGER_INFO) ----
# Per MW's decompiled struct: int32 LODOffset, then a DrivableSectionsInRegion
# (int32 NumSections, int16 Sections[400] fixed-capacity - only the first
# NumSections entries are meaningful). LODOffset is per-region, not a fixed
# engine constant - MW's own compiled default is 10, but the real ProStreet
# test file (L6R_AutobahnDrift.BUN) uses 40. Verified: this chunk's
# NumSections/Sections list matches - element for element - the set of IDs
# that own a record in the VISIBLE_SECTION_DRIVABLE chunk (57 of 57 match).
def parse_manager_info(data, payload_start, length):
    lod_offset = struct.unpack_from('<i', data, payload_start)[0]
    num_sections = struct.unpack_from('<i', data, payload_start + 4)[0]
    sections = [
        struct.unpack_from('<h', data, payload_start + 8 + 2 * i)[0]
        for i in range(num_sections)
    ]
    return lod_offset, set(sections)


def is_drivable_by_formula(section_number, lod_offset):
    """Per MW's IsScenerySectionDrivable: a regular scenery section (letter
    A-T) whose subsection number falls in (0, lod_offset). Needs the real
    per-region lod_offset (from parse_manager_info) - MW's own compiled
    default of 10 is wrong for at least one real ProStreet file (uses 40)."""
    if not is_regular_scenery_section(section_number):
        return False
    sub = section_subsection(section_number)
    return 0 < sub < lod_offset


class RegionWorld:
    def __init__(self, boundaries, relations, elevation, lod_offset=None,
                 drivable_section_ids=None, road_network=None, track_paths=None):
        self.boundaries = boundaries
        self.relations = relations
        self.elevation = elevation
        self.lod_offset = lod_offset
        self.drivable_section_ids = drivable_section_ids or set()
        self.road_network = road_network  # nfs_carp_parser.RoadNetwork, or None
        self.track_paths = track_paths    # nfs_trackpath.TrackPaths, or None
        self.by_id = {b.ID: b for b in boundaries}
        self.relations_by_id = {r.ID: r for r in relations}

    def is_drivable(self, section_number):
        """True/False if this file has drivable-section data to answer from
        (the explicit list, preferred, or MW's ID-pattern formula as a
        fallback via lod_offset). None if neither is available."""
        if self.drivable_section_ids:
            return section_number in self.drivable_section_ids
        if self.lod_offset is not None:
            return is_drivable_by_formula(section_number, self.lod_offset)
        return None
