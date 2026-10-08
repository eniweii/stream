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

WHERE THE DATA LIVES: the instances (0x80034100) are in the stream file. The
named override groups (0x34108 override infos, 0x34109 groups) are in the
REGION file (L5RA.BUN), which AssetDumper does not read. Load both and
merge() them (the command line below does it).

Command line - writes the scenery middleman files AssetDumper cannot write:
    python scenery/nfs_stream_scenery.py STREAML5RA.BUN L5RA.BUN
    python scenery/nfs_stream_scenery.py STREAML5RA.BUN L5RA.BUN \
        --instances E:/adexports/c/sections/scenery_instances.tsv
Files go to outputs/nfs_stream_scenery/ (or --out DIR):
    scenery_groups.tsv     one row per group (key, name, barrier flags, ...)
    scenery_overrides.tsv  one row per group and override (section, instance, flags)
    scenery_infos.tsv      one row per SceneryInfo (solid keys, radius, hierarchy
                           hash, flags). Same columns as AssetDumper's file
    scenery_instances.tsv  one row per instance (flags, flag names, bounding
                           box). Same columns as AssetDumper's file, so no
                           AssetDumper run is needed. With the region file, the
                           Groups and GroupNames columns are added at the end.
                           With --instances, AssetDumper's file is used instead
                           and the Groups columns are joined on Section + Instance
Checks are printed at the end (override indices outside the table, overrides
for sections or instances that do not exist in the loaded stream data).

NOT yet run against a real file - same caveat as everything else in this
project that hasn't been hex-verified yet. In particular: whether
scenery_override_infos/scenery_groups are really top-level siblings of the
per-section 0x80034100 containers, or nested one level deeper, is untested -
but since walk_chunks() recurses into every container unconditionally
(unlike the C# ChunkManager before its recent merge fix), this module finds
them either way without needing to know which.
"""
# stream bootstrap: make the shared library folders importable
import pathlib as _pl, sys as _sys
_sys.path[:0] = [str(_pl.Path(__file__).resolve().parents[1] / _d) for _d in ['common', 'region', 'scenery', 'solids_materials']]

import argparse
import csv
import math
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
    __slots__ = ('name', 'solid_key', 'flags', 'solid_keys', 'radius', 'hierarchy_hash')

    def __init__(self, name, solid_key, flags, solid_keys, radius, hierarchy_hash):
        self.name = name
        self.solid_key = solid_key
        self.flags = flags
        self.solid_keys = solid_keys  # the four LOD solid keys, as in SceneryInfoStruct
        self.radius = radius
        self.hierarchy_hash = hierarchy_hash


class SceneryInstance:
    __slots__ = ('instance_number', 'info_index', 'position', 'rotation', 'scenery_guid', 'flags',
                 'bbox_min', 'bbox_max')

    def __init__(self, instance_number, info_index, position, rotation, scenery_guid, flags,
                 bbox_min, bbox_max):
        self.instance_number = instance_number
        self.info_index = info_index
        self.position = position
        self.rotation = rotation  # 3x3 matrix as ((r0x,r0y,r0z),(r1x,r1y,r1z),(r2x,r2y,r2z))
        self.scenery_guid = scenery_guid
        self.flags = flags
        self.bbox_min = bbox_min
        self.bbox_max = bbox_max

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
        bbox_min = struct.unpack_from('<fff', data, pos + 0x00)
        instance_flags = struct.unpack_from('<I', data, pos + 0x0C)[0]
        bbox_max = struct.unpack_from('<fff', data, pos + 0x10)
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
            bbox_min=bbox_min,
            bbox_max=bbox_max,
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
        solid_keys = struct.unpack_from('<IIII', data, pos + 24)
        solid_key = solid_keys[0]
        radius = struct.unpack_from('<f', data, pos + 56)[0]
        flags = struct.unpack_from('<I', data, pos + 60)[0]
        hierarchy_hash = struct.unpack_from('<I', data, pos + 64)[0]
        infos.append(SceneryInfo(name=name, solid_key=solid_key, flags=flags,
                                 solid_keys=solid_keys, radius=radius, hierarchy_hash=hierarchy_hash))
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
    end = payload_start + length

    groups, final_pos = _parse_groups_with_padding(data, payload_start, end, True)
    if final_pos == end:
        return groups

    # The always-advance rule did not land exactly on the chunk end. Try the
    # other rule (pad only when not already aligned), the same fallback
    # flare_scenery_scan.py uses, and keep whichever one fits.
    other_groups, other_pos = _parse_groups_with_padding(data, payload_start, end, False)
    if other_pos == end:
        print("[nfs_stream_scenery] group chunk fits only with the 'pad only if unaligned' rule, using it")
        return other_groups

    print(f"[nfs_stream_scenery] WARNING: group chunk padding rule unclear (always-advance ends at "
          f"{final_pos}, other rule at {other_pos}, chunk ends at {end}); using always-advance")
    return groups


def _parse_groups_with_padding(data, payload_start, end, always_advance):
    """One pass over the group records. Returns (groups, final_pos). final_pos
    is -1 when the data ran out (wrong rule, so the next record was garbage)."""
    groups = []
    pos = payload_start

    try:
        while pos < end:
            pos = _read_one_group(data, pos, groups, always_advance)
    except struct.error:
        return groups, -1

    return groups, pos


def _read_one_group(data, pos, groups, always_advance):
    """Reads the group record at pos, appends it to groups, returns the
    position of the next record."""
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
    # matches the real loader exactly (see SceneryGroupReader.cs). The
    # other rule is only used when this one does not fit the chunk.
    if always_advance or pos % 4 != 0:
        pos += 4 - (pos % 4)

    return pos


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


# ---------------------------------------------------------------------------
# TSV export. AssetDumper writes scenery_infos.tsv and scenery_instances.tsv
# from the stream file. The override groups are in the region file, which
# AssetDumper does not read, so they are written here and joined to
# AssetDumper's scenery_instances.tsv on Section + Instance (Instance is the
# real on-disk instance number, the same number as in the DAE node id).
# ---------------------------------------------------------------------------

TOOL_NAME = 'nfs_stream_scenery'


def _hex(value):
    return f"0x{value:08X}"


def _write_tsv(path, header, rows):
    with open(path, 'w', encoding='utf-8', newline='') as f:
        writer = csv.writer(f, delimiter='\t', lineterminator='\n')
        writer.writerow(header)
        writer.writerows(rows)


def write_groups_tsv(scenery, path):
    """One row per group. Name is empty when the key is not in the dictionary."""
    rows = [
        (_hex(g.key), g.name or '', g.group_number, g.barrier_flag, g.drive_through_barrier_flag,
         g.race_specific_section_number, len(g.override_indices))
        for g in scenery.groups
    ]
    _write_tsv(path, ['Key', 'Name', 'GroupNumber', 'BarrierFlag', 'DriveThroughBarrierFlag',
                      'RaceSpecificSection', 'OverrideCount'], rows)


def write_overrides_tsv(scenery, path):
    """One row per group and override. FlagNames is the new live flag state the
    override sets (low 16 bits of the instance flags), names joined with '|'."""
    rows = []
    for g in scenery.groups:
        for idx in g.override_indices:
            if idx >= len(scenery.overrides):
                continue  # counted by check_scenery()
            ov = scenery.overrides[idx]
            rows.append((_hex(g.key), g.name or '', idx, ov.section_number, ov.instance_number,
                         f"0x{ov.instance_flags:04X}", '|'.join(decode_instance_flags(ov.instance_flags))))
    _write_tsv(path, ['GroupKey', 'GroupName', 'OverrideIndex', 'Section', 'Instance', 'Flags', 'FlagNames'], rows)


def write_instances_with_groups(scenery, instances_tsv, path):
    """AssetDumper's scenery_instances.tsv plus Groups (hex keys, '|' joined)
    and GroupNames (name, or the hex key when unknown). Returns the number of
    instances that belong to at least one group."""
    with open(instances_tsv, 'r', encoding='utf-8', newline='') as f:
        reader = csv.DictReader(f, delimiter='\t')
        fieldnames = [n for n in reader.fieldnames if n not in ('Groups', 'GroupNames')]
        rows = list(reader)

    with_groups = 0
    out_rows = []
    for row in rows:
        hits = scenery.groups_for(int(row['Section']), int(row['Instance']))
        if hits:
            with_groups += 1
        out = [row[n] for n in fieldnames]
        out.append('|'.join(_hex(g.key) for g in hits))
        out.append('|'.join(g.name or _hex(g.key) for g in hits))
        out_rows.append(out)

    _write_tsv(path, fieldnames + ['Groups', 'GroupNames'], out_rows)
    return with_groups


def _net_float(value):
    """Same text as C# float.ToString(CultureInfo.InvariantCulture) (.NET Core 3.0
    and later): the shortest digits that read back as the same float32. Fixed
    notation for exponents from -4 to 14, otherwise 'E+XX' / 'E-XX' (the upper
    limit is not checked above 1E+9, far outside any game coordinate). Used so
    the TSV files from this tool and from AssetDumper show the same text."""
    if value != value:
        return 'NaN'
    if math.isinf(value):
        return 'Infinity' if value > 0 else '-Infinity'
    if value == 0:
        return '-0' if math.copysign(1.0, value) < 0 else '0'

    text = ''
    for digits in range(1, 10):
        text = f"{value:.{digits - 1}e}"
        try:
            if struct.unpack('<f', struct.pack('<f', float(text)))[0] == value:
                break
        except OverflowError:
            pass  # too few digits near the float maximum: try one more digit

    mantissa, exponent_text = text.split('e')
    exponent = int(exponent_text)
    sign = '-' if mantissa.startswith('-') else ''
    digit_string = mantissa.lstrip('-').replace('.', '').rstrip('0') or '0'

    if -5 < exponent < 15:
        if exponent >= 0:
            digit_string = digit_string.ljust(exponent + 1, '0')
            whole, fraction = digit_string[:exponent + 1], digit_string[exponent + 1:]
            return sign + whole + ('.' + fraction if fraction else '')
        return sign + '0.' + '0' * (-exponent - 1) + digit_string

    scientific = digit_string[0] + ('.' + digit_string[1:] if len(digit_string) > 1 else '')
    return f"{sign}{scientific}E{'+' if exponent >= 0 else '-'}{abs(exponent):02d}"


def _csharp_flag_names(flags):
    """Flag names as C# SceneryInstanceFlags.ToString() writes them: PascalCase,
    lowest bit first, '|' between names, 'None' for zero."""
    names = [''.join(part.capitalize() for part in name.split('_'))
             for name in decode_instance_flags(flags)]
    return '|'.join(names) if names else 'None'


def _clean(value):
    # A tab or a line break inside a name would break the columns
    return (value or '').replace('\t', ' ').replace('\r', ' ').replace('\n', ' ')


def write_infos_tsv(scenery, path):
    """scenery_infos.tsv straight from the stream file. Same columns, same order
    and same text as SceneryManifestWriter.cs in AssetDumper. Returns the row count."""
    rows = []
    for section_number in sorted(scenery.sections):
        section = scenery.sections[section_number]
        for info_index, info in enumerate(section.infos):
            rows.append([
                section_number, info_index, _clean(info.name),
                _hex(info.solid_keys[0]), _hex(info.solid_keys[1]),
                _hex(info.solid_keys[2]), _hex(info.solid_keys[3]),
                _net_float(info.radius), _hex(info.hierarchy_hash), _hex(info.flags),
            ])

    _write_tsv(path, ['Section', 'Info', 'Name', 'SolidKey1', 'SolidKey2', 'SolidKey3',
                      'SolidKey4', 'Radius', 'HierarchyHash', 'Flags'], rows)
    return len(rows)


def write_instances_tsv(scenery, path, with_groups, collision_matches=None, object_matches=None,
                        group_links=None):
    """scenery_instances.tsv straight from the stream file. Same columns, same
    order and same text as SceneryManifestWriter.cs in AssetDumper. With
    with_groups (the region file is loaded), the Groups and GroupNames columns
    are added at the end, same as write_instances_with_groups. With
    collision_matches (from nfs_collision_pack.match_to_scenery) or object_matches
    (from nfs_collision_pack.match_objects_to_scenery), the HasCollision,
    CollisionIndex, CollisionObject and CollisionObjectMethod columns come last:
    HasCollision is 1 when an instance or an object matched; CollisionIndex is the
    index of the collision instance in that section's pack, CollisionObject the
    index of the collision object and CollisionObjectMethod the method that placed
    it (index, position or inside); empty cells when there is none. With group_links (from
    nfs_collision_pack.match_by_group) the CollisionGroupIndexes column is added after
    them: the indexes, joined with |, of the collision instances whose group number is a
    group touching this scenery instance (the link MW itself uses). The link is a
    match by index or position, see nfs_collision_pack. Returns the number of rows
    and the number of instances that belong to a group."""
    header = ['Section', 'Instance', 'Guid', 'Info', 'Name', 'Flags', 'FlagNames',
              'BBoxMinX', 'BBoxMinY', 'BBoxMinZ', 'BBoxMaxX', 'BBoxMaxY', 'BBoxMaxZ']
    if with_groups:
        header += ['Groups', 'GroupNames']
    with_collision = collision_matches is not None or object_matches is not None
    with_group_links = group_links is not None
    group_links = group_links or {}
    collision_matches = collision_matches or {}
    object_matches = object_matches or {}
    if with_collision:
        header += ['HasCollision', 'CollisionIndex', 'CollisionObject', 'CollisionObjectMethod']
    if with_group_links:
        header += ['CollisionGroupIndexes']

    rows = []
    in_group = 0
    for section_number in sorted(scenery.sections):
        section = scenery.sections[section_number]
        for instance in section.instances:
            row = [
                section_number, instance.instance_number, _hex(instance.scenery_guid),
                instance.info_index, _clean(section.name_for(instance)),
                _hex(instance.flags), _csharp_flag_names(instance.flags),
                *(_net_float(v) for v in instance.bbox_min),
                *(_net_float(v) for v in instance.bbox_max),
            ]
            if with_groups:
                hits = scenery.groups_for(section_number, instance.instance_number)
                if hits:
                    in_group += 1
                row.append('|'.join(_hex(g.key) for g in hits))
                row.append('|'.join(g.name or _hex(g.key) for g in hits))
            if with_collision:
                key = (section_number, instance.instance_number)
                collision_index = collision_matches.get(key)
                object_match = object_matches.get(key)
                row.append(0 if collision_index is None and object_match is None else 1)
                row.append('' if collision_index is None else collision_index)
                row.append('' if object_match is None else object_match[1])
                row.append('' if object_match is None else object_match[0])
            if with_group_links:
                row.append('|'.join(str(i) for i in group_links.get(
                    (section_number, instance.instance_number), [])))
            rows.append(row)

    _write_tsv(path, header, rows)
    return len(rows), in_group


def check_flags(scenery):
    """Prints how many instances have the flags we care about. A count of zero
    for every flag on a real file means the flag word is read from a wrong offset."""
    total = swayable = enable_wind = both = collidable = 0
    for section in scenery.sections.values():
        for instance in section.instances:
            total += 1
            is_sway = bool(instance.flags & (1 << 14))
            is_wind = bool(instance.flags & (1 << 15))
            swayable += is_sway
            enable_wind += is_wind
            both += is_sway and is_wind
            collidable += bool(instance.flags & (1 << 30))

    print(f"[nfs_stream_scenery] check: {total} instance(s), {swayable} swayable, "
          f"{enable_wind} enable_wind ({both} with both), {collidable} collidable")


def check_scenery(scenery):
    """Prints the sanity checks. A wrong read (for example the group padding
    rule) shows up as indices that point nowhere."""
    bad_index = 0
    no_section = 0
    past_end = 0
    for g in scenery.groups:
        for idx in g.override_indices:
            if idx >= len(scenery.overrides):
                bad_index += 1
                continue
            ov = scenery.overrides[idx]
            section = scenery.sections.get(ov.section_number)
            if section is None:
                no_section += 1
            elif ov.instance_number >= len(section.instances):
                past_end += 1

    in_group = {idx for g in scenery.groups for idx in g.override_indices}
    no_group = sum(1 for idx in range(len(scenery.overrides)) if idx not in in_group)

    print(f"[nfs_stream_scenery] check: {bad_index} group override index(es) outside the override table")
    print(f"[nfs_stream_scenery] check: {no_section} override(s) point to a section with no stream instances loaded")
    print(f"[nfs_stream_scenery] check: {past_end} override(s) point past the last instance of their section")
    print(f"[nfs_stream_scenery] check: {no_group} override record(s) belong to no group")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Write the scenery group TSV files (see the module docstring).")
    ap.add_argument('stream_bun', help="stream file (STREAML5RA.BUN): the scenery instances")
    ap.add_argument('region_bun', nargs='?', help="region file (L5RA.BUN): the override groups")
    ap.add_argument('--instances', help="AssetDumper's scenery_instances.tsv, to add the Groups columns "
                                        "(optional: without it, this tool writes its own scenery_instances.tsv)")
    ap.add_argument('--collision', action='store_true',
                    help="also read the collision packs (chunk 0x3B801) from the stream file, write "
                         "collision_instances.tsv and add HasCollision, CollisionIndex, "
                         "CollisionObject and CollisionObjectMethod to scenery_instances.tsv. "
                         "Reads the stream file a second time")
    ap.add_argument('--out', help="output folder (default outputs/nfs_stream_scenery/)")
    args = ap.parse_args(argv)

    from pathlib import Path
    from nfs_outputs import out_dir

    scenery = load_stream_scenery(args.stream_bun)
    if args.region_bun:
        scenery.merge(load_stream_scenery(args.region_bun))
    print(f"[nfs_stream_scenery] total: {scenery.summary()}")

    out = Path(args.out) if args.out else out_dir(TOOL_NAME)
    out.mkdir(parents=True, exist_ok=True)

    write_groups_tsv(scenery, out / 'scenery_groups.tsv')
    write_overrides_tsv(scenery, out / 'scenery_overrides.tsv')
    print(f"[nfs_stream_scenery] wrote scenery_groups.tsv and scenery_overrides.tsv to {out}")

    info_count = write_infos_tsv(scenery, out / 'scenery_infos.tsv')
    print(f"[nfs_stream_scenery] wrote scenery_infos.tsv, {info_count} info(s)")

    if args.instances:
        with_groups = write_instances_with_groups(scenery, args.instances, out / 'scenery_instances.tsv')
        print(f"[nfs_stream_scenery] wrote scenery_instances.tsv (from --instances), "
              f"{with_groups} instance(s) belong to a group")
    else:
        collision_matches = None
        object_matches = None
        group_links = None
        if args.collision:
            from nfs_collision_pack import (load_collision_packs, match_to_scenery,
                                            match_objects_to_scenery, match_by_group,
                                            write_collision_instances_tsv, summary as collision_summary)
            packs = load_collision_packs(args.stream_bun)
            print(f"[nfs_stream_scenery] {collision_summary(packs)}")
            write_collision_instances_tsv(packs, out / 'collision_instances.tsv')
            group_links, group_reports = match_by_group(packs, scenery)
            for line in group_reports:
                print(f"[nfs_stream_scenery] {line}")
            if not group_links:
                group_links = None
            collision_matches, reports = match_to_scenery(packs, scenery)
            for line in reports:
                print(f"[nfs_stream_scenery] {line}")
            object_matches, object_reports = match_objects_to_scenery(packs, scenery)
            for line in object_reports:
                print(f"[nfs_stream_scenery] {line}")
            if not collision_matches:
                collision_matches = None   # no usable match: no instance link
            if not object_matches:
                object_matches = None      # no usable match: no object link
        row_count, with_groups = write_instances_tsv(scenery, out / 'scenery_instances.tsv',
                                                     with_groups=bool(args.region_bun),
                                                     collision_matches=collision_matches,
                                                     object_matches=object_matches,
                                                     group_links=group_links)
        print(f"[nfs_stream_scenery] wrote scenery_instances.tsv, {row_count} instance(s)"
              + (f", {with_groups} belong to a group" if args.region_bun else ""))

    check_flags(scenery)
    check_scenery(scenery)


if __name__ == '__main__':
    main()
