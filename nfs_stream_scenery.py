"""
Reads the STREAM file's scenery data - per-section scenery instances, plus
the real named override-group system (scenery::group / scenery::override_info).

This is a DIFFERENT file and a different chunk-ID family (0x8003410x) from
the region/world file's VisibleSections (0x8003415x, chunk 0x80034150) that
nfs_region_common.py/nfs_region_parser.py already handle - e.g. L5RA.BUN
(region/world) vs STREAML5RA.BUN (stream). Both use the identical generic
chunk envelope, so this reuses nfs_region_common.walk_chunks() rather than
duplicating it.

Ported directly from the C# reader written for this project's AssetDumper
fork (Common/Scenery/CarbonScenery.cs + Common/Scenery/SceneryGroupReader.cs)
- byte offsets, chunk IDs, and struct shapes match that code exactly, not
independently re-derived here. See that code for the full reasoning
(confirmed against hyperlinked's real Carbon decompile).

Carbon/World09 only for now - SceneryInstanceInternal's 0x60-byte layout is
specific to those two games; other games have different struct shapes on
the C# side and would need their own version of _parse_scenery_instances.

NOT yet run against a real file - same caveat as everything else in this
project that hasn't been hex-verified yet. In particular: whether
scenery_override_infos/scenery_groups are really top-level siblings of the
per-section 0x80034100 containers, or nested one level deeper, is untested -
but since walk_chunks() recurses into every container unconditionally
(unlike the C# ChunkManager before its recent merge fix), this module finds
them either way without needing to know which.
"""
import struct

from nfs_region_common import walk_chunks
from nfs_hashing import bin_hash

# Confirmed real names for scenery::group keys, from hyperlinked's decompiled
# source (Common/assets/scenery.cpp + world.hpp) - hashed with bin_hash().
# Unlike the emitter/effect name table, there's no comprehensive dictionary
# compiled into the engine for this - only this one name is hardcoded
# (a permanent, engine-level group, not necessarily used by every level).
# Level-specific group names aren't in the decompiled engine source at all;
# use guess_group_name() below to test your own candidates against real keys.
KNOWN_GROUP_NAMES = {
    bin_hash("SCENERY_GROUP_DOOR"): "SCENERY_GROUP_DOOR",
}


def guess_group_name(candidate):
    """Hash a candidate string the same way the game does, for testing
    against real group keys found in a loaded file. Returns the hash."""
    return bin_hash(candidate)

SCENERY_SECTION_ID = bytes.fromhex('00410380')          # 0x80034100 (container, one per section)
SCENERY_HEADER_ID = bytes.fromhex('01410300')            # 0x00034101
SCENERY_INFOS_ID = bytes.fromhex('02410300')             # 0x00034102
SCENERY_INSTANCES_ID = bytes.fromhex('03410300')         # 0x00034103
SCENERY_OVERRIDE_INFOS_ID = bytes.fromhex('08410300')    # 0x00034108
SCENERY_GROUPS_ID = bytes.fromhex('09410300')            # 0x00034109

# hyperlinked's real instance_flags enum, bit 0 to bit 31 - see
# Common/Scenery/Data/ScenerySection.cs's SceneryInstanceFlags for the C# side.
INSTANCE_FLAG_NAMES = [
    'exclude_split_screen', 'exclude_main_view', 'exclude_racing',
    'exclude_disable_rendering', 'exclude_group_disable', 'exclude_freeroam',
    'include_rear_view', 'include_reflection', 'envmap_shadow', 'chopped_roadway',
    'identity_matrix', 'artwork_flipped', 'reflection', 'environment_map',
    'swayable', 'enable_wind',
    'always_facing', 'dont_receive_shadows', 'low_platform_only', 'high_platform_only',
    'cast_shadow_volume', 'cast_shadow_map', 'inverted_matrix', 'flip_on_backwards_track',
    'reflect_in_ocean', 'visible_further', 'cast_shadow_map_mesh', 'include_reflection_ng',
    'aux_lighting', 'pc_platform', 'collidable', 'lightmapped',
]


def decode_instance_flags(flags):
    return [name for i, name in enumerate(INSTANCE_FLAG_NAMES) if flags & (1 << i)]


class SceneryInfo:
    __slots__ = ('name', 'solid_key', 'flags')

    def __init__(self, name, solid_key, flags):
        self.name = name
        self.solid_key = solid_key
        self.flags = flags


class SceneryInstance:
    __slots__ = ('instance_number', 'info_index', 'position', 'rotation', 'scenery_guid', 'flags')

    def __init__(self, instance_number, info_index, position, rotation, scenery_guid, flags):
        self.instance_number = instance_number
        self.info_index = info_index
        self.position = position
        self.rotation = rotation  # 3x3 matrix as ((r0x,r0y,r0z),(r1x,r1y,r1z),(r2x,r2y,r2z))
        self.scenery_guid = scenery_guid
        self.flags = flags

    @property
    def flag_names(self):
        return decode_instance_flags(self.flags)


class SceneryOverrideInfo:
    __slots__ = ('section_number', 'instance_number', 'instance_flags')

    def __init__(self, section_number, instance_number, instance_flags):
        self.section_number = section_number
        self.instance_number = instance_number
        self.instance_flags = instance_flags


class SceneryGroup:
    __slots__ = ('key', 'group_number', 'barrier_flag', 'drive_through_barrier_flag',
                 'race_specific_section_number', 'override_indices')

    def __init__(self, key, group_number, barrier_flag, drive_through_barrier_flag,
                 race_specific_section_number, override_indices):
        self.key = key
        self.group_number = group_number
        self.barrier_flag = barrier_flag
        self.drive_through_barrier_flag = drive_through_barrier_flag
        self.race_specific_section_number = race_specific_section_number
        self.override_indices = override_indices

    @property
    def name(self):
        """Resolved name if known - checks the small compiled-in table
        first (100% certain, sourced straight from engine code), then falls
        back to the external hashes_main.txt dictionary if present (see
        nfs_hash_dictionary.py - optional companion file, not embedded).
        None if neither has it."""
        if self.key in KNOWN_GROUP_NAMES:
            return KNOWN_GROUP_NAMES[self.key]
        try:
            from nfs_hash_dictionary import resolve
            return resolve(self.key)
        except ImportError:
            return None

    def display_key(self):
        name = self.name
        return f"0x{self.key:08X} ({name})" if name else f"0x{self.key:08X}"


class StreamScenerySection:
    __slots__ = ('section_number', 'infos', 'instances')

    def __init__(self, section_number, infos, instances):
        self.section_number = section_number
        self.infos = infos          # list index == info_index
        self.instances = instances  # list index == instance_number

    def name_for(self, instance):
        """The real scenery object name for an instance, e.g. 'streetlamp_01',
        resolved via its info_index - or None if the index is out of range
        (shouldn't happen on well-formed data, but don't crash on it)."""
        if 0 <= instance.info_index < len(self.infos):
            return self.infos[instance.info_index].name
        return None


def _parse_scenery_instances(data, payload_start, length):
    """SceneryInstanceInternal, 0x60 bytes each (Carbon/World09 only). The
    chunk is 0x10-aligned before the instance array starts, matching
    BinaryUtil.AlignReader(br, 0x10) in CarbonScenery.cs - only pads if not
    already aligned."""
    pos = payload_start
    if pos % 0x10 != 0:
        pos += 0x10 - (pos % 0x10)

    end = payload_start + length
    instances = []
    instance_number = 0

    while pos + 0x60 <= end:
        instance_flags = struct.unpack_from('<I', data, pos + 0x0C)[0]
        px, py, pz = struct.unpack_from('<fff', data, pos + 0x20)
        r0 = struct.unpack_from('<fff', data, pos + 0x2C)
        r1 = struct.unpack_from('<fff', data, pos + 0x38)
        r2 = struct.unpack_from('<fff', data, pos + 0x44)
        scenery_guid = struct.unpack_from('<I', data, pos + 0x50)[0]
        info_index = struct.unpack_from('<h', data, pos + 0x54)[0]

        instances.append(SceneryInstance(
            instance_number=instance_number,
            info_index=info_index,
            position=(px, py, pz),
            rotation=(r0, r1, r2),
            scenery_guid=scenery_guid,
            flags=instance_flags,
        ))

        instance_number += 1
        pos += 0x60

    return instances


def _parse_scenery_infos(data, payload_start, length):
    """SceneryInfoStruct, 0x48 bytes each - fixed stride, NO alignment step
    (unlike instances). Name is a 24-byte fixed buffer, NUL-padded."""
    infos = []
    pos = payload_start
    end = payload_start + length

    while pos + 0x48 <= end:
        raw_name = data[pos:pos + 24]
        name = raw_name.split(b'\x00', 1)[0].decode('ascii', errors='replace')
        solid_key = struct.unpack_from('<I', data, pos + 24)[0]
        flags = struct.unpack_from('<I', data, pos + 60)[0]
        infos.append(SceneryInfo(name=name, solid_key=solid_key, flags=flags))
        pos += 0x48

    return infos


def _parse_scenery_section(data, payload_start, length):
    section_number = None
    infos = []
    instances = []

    for _offset, raw_id, _id_hex, chunk_length, _is_container, chunk_payload_start in \
            walk_chunks(data, payload_start, payload_start + length):
        if raw_id == SCENERY_HEADER_ID:
            # ScenerySectionHeader: SectionNumber is an int32 at offset 0x0C
            section_number = struct.unpack_from('<i', data, chunk_payload_start + 0x0C)[0]
        elif raw_id == SCENERY_INFOS_ID:
            infos = _parse_scenery_infos(data, chunk_payload_start, chunk_length)
        elif raw_id == SCENERY_INSTANCES_ID:
            instances = _parse_scenery_instances(data, chunk_payload_start, chunk_length)

    return StreamScenerySection(section_number, infos, instances)


def _parse_override_infos(data, payload_start, length):
    """SceneryOverrideInfo - flat array, 6 bytes each: section_number(u16),
    instance_number(u16), instance_flags(u16)."""
    count = length // 6
    return [
        SceneryOverrideInfo(*struct.unpack_from('<HHH', data, payload_start + i * 6))
        for i in range(count)
    ]


def _parse_groups(data, payload_start, length):
    """scenery::group - variable-length records. See
    Common/Scenery/SceneryGroupReader.cs for the full layout reasoning
    (intrusive linked-list pointers, the always-advance alignment quirk) -
    this is a direct port of that logic."""
    groups = []
    pos = payload_start
    end = payload_start + length

    while pos < end:
        pos += 8  # next_/prev_, meaningless on disk

        key = struct.unpack_from('<I', data, pos)[0]
        pos += 4
        group_number, override_count = struct.unpack_from('<HH', data, pos)
        pos += 4
        barrier_flag, drive_through_barrier_flag = struct.unpack_from('<BB', data, pos)
        pos += 2
        race_specific_section_number = struct.unpack_from('<H', data, pos)[0]
        pos += 2

        override_indices = [
            struct.unpack_from('<H', data, pos + 2 * i)[0]
            for i in range(override_count)
        ]
        pos += override_count * 2

        groups.append(SceneryGroup(
            key, group_number, barrier_flag, drive_through_barrier_flag,
            race_specific_section_number, override_indices,
        ))

        # Always advance to the NEXT multiple of 4, even if already aligned -
        # matches the real loader exactly (see SceneryGroupReader.cs).
        pos += 4 - (pos % 4)

    return groups


class StreamScenery:
    def __init__(self, sections, overrides, groups):
        self.sections = sections    # dict: section_number -> StreamScenerySection
        self.overrides = overrides  # list of SceneryOverrideInfo
        self.groups = groups        # list of SceneryGroup
        self._build_indices()

    def _build_indices(self):
        # Precomputed once, since a real file can have 20000+ overrides -
        # linear-scanning that per UI click/instance would be noticeably slow.
        self._override_section_numbers = frozenset(ov.section_number for ov in self.overrides)
        self._override_index_by_key = {}
        for idx, ov in enumerate(self.overrides):
            self._override_index_by_key.setdefault((ov.section_number, ov.instance_number), []).append(idx)
        self._groups_by_override_index = {}
        for g in self.groups:
            for idx in g.override_indices:
                if idx < len(self.overrides):
                    self._groups_by_override_index.setdefault(idx, []).append(g)

    def merge(self, other):
        """Combine another StreamScenery's data into this one, in place.
        Real files can carry only one half of this (instances OR overrides,
        not necessarily both - see e.g. a region file with overrides but no
        instances), so loading a second file merges rather than replaces,
        letting instance data from one file and override data from another
        be viewed together."""
        for section_number, section in other.sections.items():
            self.sections.setdefault(section_number, section)

        offset = len(self.overrides)
        self.overrides.extend(other.overrides)
        for g in other.groups:
            g.override_indices = [idx + offset for idx in g.override_indices]
        self.groups.extend(other.groups)

        self._build_indices()

    def has_data_for(self, section_number):
        """Whether this file has ANY scenery data - instances OR override
        records - for this section number. A region-only file (like L5RA.BUN
        can be) may carry the override system with zero instance placements,
        so checking `sections` alone isn't enough to answer 'is there
        anything here at all'."""
        return section_number in self.sections or section_number in self._override_section_numbers

    def groups_for(self, section_number, instance_number):
        """Group(s) with an override touching this specific instance."""
        hits = []
        for idx in self._override_index_by_key.get((section_number, instance_number), []):
            hits.extend(self._groups_by_override_index.get(idx, []))
        return hits

    def overrides_for_section(self, section_number):
        """(index, SceneryOverrideInfo, [SceneryGroup, ...]) for every
        override record belonging to this section - independent of whether
        there's matching instance data, so these show up even for a
        region-only file with no instances at all."""
        results = []
        for idx, ov in enumerate(self.overrides):
            if ov.section_number == section_number:
                results.append((idx, ov, self._groups_by_override_index.get(idx, [])))
        return results

    def groups_matching_name(self, candidate):
        """Hash a candidate name and return any real loaded groups whose key
        matches - lets you test your own guesses (level-specific group names
        aren't in the decompiled engine source, only SCENERY_GROUP_DOOR is)."""
        target = bin_hash(candidate)
        return [g for g in self.groups if g.key == target]

    def summary(self):
        total_instances = sum(len(s.instances) for s in self.sections.values())
        override_sections = sorted({ov.section_number for ov in self.overrides})
        return (f"{len(self.sections)} section(s), {total_instances} instance(s) total, "
                f"{len(self.overrides)} override record(s) across {len(override_sections)} "
                f"section(s), {len(self.groups)} group(s)")


def load_stream_scenery(path):
    # Whole-file read, same convention as nfs_region_parser.load_region_file -
    # fine for region-sized files, worth revisiting if a real stream file
    # turns out too large to comfortably fit in memory this way.
    data = open(path, 'rb').read()

    sections = {}
    overrides = []
    groups = []

    # walk_chunks recurses into every container unconditionally, so this finds
    # scenery_override_infos/scenery_groups whether they're top-level siblings
    # of 0x80034100 or nested somewhere else - no depth assumption needed.
    for _offset, raw_id, _id_hex, length, _is_container, payload_start in walk_chunks(data):
        if raw_id == SCENERY_SECTION_ID:
            section = _parse_scenery_section(data, payload_start, length)
            if section.section_number is not None:
                sections[section.section_number] = section
        elif raw_id == SCENERY_OVERRIDE_INFOS_ID:
            overrides.extend(_parse_override_infos(data, payload_start, length))
        elif raw_id == SCENERY_GROUPS_ID:
            groups.extend(_parse_groups(data, payload_start, length))

    result = StreamScenery(sections, overrides, groups)
    print(f"[nfs_stream_scenery] {result.summary()}")
    return result
