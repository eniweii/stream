"""
Collision packs of the stream file (STREAML5RA.BUN): chunk 0x0003B801, one per
scenery section. Layout from UCGT's working Java code (ChunkCollisions.java and
CARP.java in fr.ni240sx.ucgt.streamEditor.streamBlocks). The chunk ID is the
same one the MW decomp calls BCHUNK_CARP_WCOLLISIONPACK and Hyperlinked calls
world_collision_assets.

Chunk payload:
    int32 blockLength (inner, same job as the envelope size)
    0x11111111 filler ints (alignment to 16)
    bChunkCarpHeader, 16 bytes: crp_size, section_number, flags, last_address
    CARP root (the offset-table container of nfs_carp_parser.py), typeFlag 42
        Arti
            Name   the pack name
            ci     collision instance records, 0x44 bytes each
            hk     one Havok collision article per block (not read here)
            si     surface triggers (not read here)

Collision instance record (0x44 bytes, field names from the MW decomp):
    +0x00 fInvMatRow0Width  4f   row 0 of the inverse matrix, width in the 4th float
    +0x10 chunk_id          u16  UCGT calls it the chunk ID, the decomp calls it fIterStamp
    +0x12 flags             u16  fFlags
    +0x14 height            f
    +0x18 group_number      u16  scenery group that switches this instance (0 = always on)
    +0x1A instance_id       u16  fRenderInstanceInd: picks the article (hk block),
                                 NOT a scenery instance index
    +0x1C two runtime pointers (zero on disk)
    +0x24 fInvMatRow2Length 4f   row 2 of the inverse matrix, length in the 4th float
    +0x34 fInvPosRadius     4f   position xyz, radius in the 4th float
In every file UCGT looked at, row 0 is (1, 0, 0) and row 2 is (0, 0, 1), so the
instances are axis-aligned boxes. UCGT says the position is in a different axis
order than the scenery positions (position x about the section y, position z
about minus the section x, position y about minus the altitude).

Instance flags (Hyperlinked collision.hpp): 0x01 y_vector_not_up, 0x02 dynamic,
0x04 disabled, 0x40 no_traffic, 0x80 no_cop.

NOT PROVEN: which scenery instance a collision instance belongs to. No index
joins them. match_to_scenery() below compares positions and picks the axis
order from the data. It prints how well it did. Read that line.
"""
# stream bootstrap: make the shared library folders importable
import pathlib as _pl, sys as _sys
_sys.path[:0] = [str(_pl.Path(__file__).resolve().parents[1] / _d) for _d in ['common', 'region', 'scenery', 'solids_materials']]

import argparse
import csv
import itertools
import struct

from nfs_region_common import walk_chunks
from nfs_carp_parser import parse_carp, TAG_ARTI, TAG_NAME

TOOL_NAME = 'nfs_collision_pack'

COLLISION_PACK_ID = bytes.fromhex('01b80300')   # 0x0003B801, little-endian on disk
TAG_CI_MASK = 0xFFFF0000
TAG_CI = 0x63690000                              # 'ci', the low 16 bits are a block number
CI_RECORD_SIZE = 0x44

COLLISION_INSTANCE_FLAGS = {
    0x01: 'y_vector_not_up',
    0x02: 'dynamic',
    0x04: 'disabled',
    0x40: 'no_traffic',
    0x80: 'no_cop',
}

# Settings for match_to_scenery()
MATCH_DISTANCE = 1.5        # world units between a collision position and a scenery position
CALIBRATION_SAMPLE = 400    # collision instances used to pick the axis order
MIN_MATCH_RATE = 0.5        # best axis order must match at least this share of the sample
AMBIGUITY_RATIO = 0.8       # and the second best must stay below this share of the best


class CollisionInstance:
    __slots__ = ('index', 'chunk_id', 'flags', 'group_number', 'instance_id',
                 'width', 'height', 'length', 'position', 'radius', 'inv_row0', 'inv_row2')

    def __init__(self, index, chunk_id, flags, group_number, instance_id,
                 width, height, length, position, radius, inv_row0, inv_row2):
        self.index = index
        self.chunk_id = chunk_id
        self.flags = flags
        self.group_number = group_number
        self.instance_id = instance_id
        self.width = width
        self.height = height
        self.length = length
        self.position = position    # (x, y, z) as stored, see the module docstring
        self.radius = radius
        self.inv_row0 = inv_row0
        self.inv_row2 = inv_row2


class CollisionPack:
    __slots__ = ('offset', 'section_number', 'name', 'crp_size', 'flags', 'last_address',
                 'instances', 'article_count')

    def __init__(self, offset, section_number, name, crp_size, flags, last_address,
                 instances, article_count):
        self.offset = offset
        self.section_number = section_number
        self.name = name
        self.crp_size = crp_size
        self.flags = flags
        self.last_address = last_address
        self.instances = instances
        self.article_count = article_count   # number of 'hk' entries under Arti


def decode_collision_flags(flags):
    names = [name for bit, name in COLLISION_INSTANCE_FLAGS.items() if flags & bit]
    unknown = flags & ~sum(COLLISION_INSTANCE_FLAGS)
    for bit in range(16):
        if unknown & (1 << bit):
            names.append(f"bit_{bit}")
    return names


def _parse_collision_instances(data, payload_start, length):
    instances = []
    for index in range(length // CI_RECORD_SIZE):
        pos = payload_start + index * CI_RECORD_SIZE
        inv_row0 = struct.unpack_from('<fff', data, pos + 0x00)
        width = struct.unpack_from('<f', data, pos + 0x0C)[0]
        chunk_id, flags = struct.unpack_from('<HH', data, pos + 0x10)
        height = struct.unpack_from('<f', data, pos + 0x14)[0]
        group_number, instance_id = struct.unpack_from('<HH', data, pos + 0x18)
        inv_row2 = struct.unpack_from('<fff', data, pos + 0x24)
        length_value = struct.unpack_from('<f', data, pos + 0x30)[0]
        position = struct.unpack_from('<fff', data, pos + 0x34)
        radius = struct.unpack_from('<f', data, pos + 0x40)[0]
        instances.append(CollisionInstance(index, chunk_id, flags, group_number, instance_id,
                                           width, height, length_value, position, radius,
                                           inv_row0, inv_row2))
    return instances


def _parse_collision_pack(data, offset, payload_start):
    pos = payload_start + 4   # skip the inner blockLength field
    while struct.unpack_from('<I', data, pos)[0] == 0x11111111:
        pos += 4
    crp_size, section_number, flags, last_address = struct.unpack_from('<IIII', data, pos)
    root = parse_carp(data, pos + 16)

    name = ''
    instances = []
    article_count = 0
    arti = root.get_entry(TAG_ARTI)
    if arti and arti.children:
        for child in arti.children:
            if child.type == TAG_NAME:
                name = child.data or ''
            elif (child.type & TAG_CI_MASK) == TAG_CI and child.data_length:
                instances = _parse_collision_instances(
                    data, child.offset + child.data_offset, child.data_length)
            elif (child.type & TAG_CI_MASK) == 0x686B0000:   # 'hk'
                article_count += 1
    return CollisionPack(offset, section_number, name, crp_size, flags, last_address,
                         instances, article_count)


def load_collision_packs(path):
    # Whole-file read, same convention as nfs_stream_scenery.load_stream_scenery
    data = open(path, 'rb').read()
    packs = []
    for offset, raw_id, _id_hex, _length, _is_container, payload_start in walk_chunks(data):
        if raw_id == COLLISION_PACK_ID:
            packs.append(_parse_collision_pack(data, offset, payload_start))
    return packs


def summary(packs):
    instances = sum(len(p.instances) for p in packs)
    articles = sum(p.article_count for p in packs)
    return f"{len(packs)} collision pack(s), {instances} collision instance(s), {articles} article(s)"


# ---- Matching collision instances to scenery instances (position only) ----

def _hash_key(point, cell):
    return (int(point[0] // cell), int(point[1] // cell), int(point[2] // cell))


def _build_hash(points, cell):
    table = {}
    for number, point in points:
        table.setdefault(_hash_key(point, cell), []).append((number, point))
    return table


def _near(table, point, cell, max_distance):
    """Yields (distance, number) for every point within max_distance."""
    kx, ky, kz = _hash_key(point, cell)
    for dx, dy, dz in itertools.product((-1, 0, 1), repeat=3):
        for number, other in table.get((kx + dx, ky + dy, kz + dz), ()):
            distance = ((point[0] - other[0]) ** 2 + (point[1] - other[1]) ** 2
                        + (point[2] - other[2]) ** 2) ** 0.5
            if distance <= max_distance:
                yield distance, number


def _axis_orders():
    """All 48 ways to reorder the three stored position values and flip their signs."""
    for order in itertools.permutations(range(3)):
        for signs in itertools.product((1.0, -1.0), repeat=3):
            yield order, signs


def _apply_order(position, order, signs):
    return (signs[0] * position[order[0]], signs[1] * position[order[1]],
            signs[2] * position[order[2]])


def _scenery_points(section):
    """Per target: the origin, and the middle of the bounding box."""
    origins, centers = [], []
    for instance in section.instances:
        origins.append((instance.instance_number, instance.position))
        centers.append((instance.instance_number, tuple(
            (instance.bbox_min[i] + instance.bbox_max[i]) / 2.0 for i in range(3))))
    return {'origin': origins, 'bbox_center': centers}


def match_to_scenery(packs, scenery):
    """Finds which scenery instance sits where each collision instance sits.
    Returns (matches, report). matches maps (section_number, scenery
    instance_number) to the index of a collision instance in that section's
    pack. It is empty when no axis order matches well enough. report is one
    line of text for the console.

    The axis order and the target (scenery origin or bounding box middle) are
    picked from the data: the one that puts the most sampled collision
    instances within MATCH_DISTANCE of a scenery instance wins."""
    pairs = []   # (pack, tables for that section)
    for pack in packs:
        section = scenery.sections.get(pack.section_number)
        if section and pack.instances and section.instances:
            tables = {}
            for target, points in _scenery_points(section).items():
                tables[target] = _build_hash(points, MATCH_DISTANCE)
            pairs.append((pack, tables))
    if not pairs:
        return {}, "collision match: no section has both collision instances and scenery instances"

    sample = []   # (tables, position)
    per_pack = max(1, CALIBRATION_SAMPLE // len(pairs))
    for pack, tables in pairs:
        step = max(1, len(pack.instances) // per_pack)
        for instance in pack.instances[::step][:per_pack]:
            sample.append((tables, instance.position))

    scores = []   # one entry per axis order: its best target (origin wins a tie)
    for order, signs in _axis_orders():
        best_for_order = None
        for target in ('origin', 'bbox_center'):
            hits = 0
            for tables, position in sample:
                mapped = _apply_order(position, order, signs)
                if next(_near(tables[target], mapped, MATCH_DISTANCE, MATCH_DISTANCE), None):
                    hits += 1
            if best_for_order is None or hits > best_for_order[0]:
                best_for_order = (hits, order, signs, target)
        scores.append(best_for_order)
    scores.sort(key=lambda s: s[0], reverse=True)
    best_hits, order, signs, target = scores[0]
    rate = best_hits / len(sample)
    second = scores[1][0] / best_hits if best_hits else 1.0

    description = f"axis order {order}, signs {signs}, target {target}"
    if rate < MIN_MATCH_RATE:
        return {}, (f"collision match: no axis order fits ({description} was best with "
                    f"{rate:.0%} of {len(sample)} sampled instances); HasCollision not written")
    if second >= AMBIGUITY_RATIO:
        return {}, (f"collision match: more than one axis order fits equally well "
                    f"({description} {rate:.0%}, runner-up {second:.0%} of that); "
                    f"HasCollision not written")

    matches = {}
    matched = 0
    total = 0
    for pack, tables in pairs:
        candidates = []
        for instance in pack.instances:
            mapped = _apply_order(instance.position, order, signs)
            for distance, number in _near(tables[target], mapped, MATCH_DISTANCE, MATCH_DISTANCE):
                candidates.append((distance, instance.index, number))
        candidates.sort()
        used_collision, used_scenery = set(), set()
        for distance, collision_index, number in candidates:
            if collision_index in used_collision or number in used_scenery:
                continue
            used_collision.add(collision_index)
            used_scenery.add(number)
            matches[(pack.section_number, number)] = collision_index
        matched += len(used_collision)
        total += len(pack.instances)

    return matches, (f"collision match: {description}, {rate:.0%} of the sample fit; "
                     f"{matched} of {total} collision instance(s) matched to a scenery instance")


# ---- TSV output ----

def _write_tsv(path, header, rows):
    with open(path, 'w', encoding='utf-8', newline='') as f:
        writer = csv.writer(f, delimiter='\t', lineterminator='\n')
        writer.writerow(header)
        writer.writerows(rows)


def write_collision_instances_tsv(packs, path):
    """One row per collision instance. Join to scenery_instances.tsv on
    Section + CollisionIndex (the CollisionIndex column of that file)."""
    rows = []
    for pack in sorted(packs, key=lambda p: p.section_number):
        for ci in pack.instances:
            rows.append([
                pack.section_number, ci.index, ci.chunk_id, f"0x{ci.flags:04X}",
                '|'.join(decode_collision_flags(ci.flags)) or 'None',
                ci.group_number, ci.instance_id,
                f"{ci.width:.4f}", f"{ci.height:.4f}", f"{ci.length:.4f}",
                f"{ci.position[0]:.4f}", f"{ci.position[1]:.4f}", f"{ci.position[2]:.4f}",
                f"{ci.radius:.4f}",
            ])
    _write_tsv(path, ['Section', 'Index', 'ChunkId', 'Flags', 'FlagNames', 'Group', 'InstanceId',
                      'Width', 'Height', 'Length', 'PosX', 'PosY', 'PosZ', 'Radius'], rows)
    return len(rows)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Write collision_instances.tsv from the stream file.")
    ap.add_argument('stream_bun', help="stream file (STREAML5RA.BUN)")
    ap.add_argument('--out', help="output folder (default outputs/nfs_collision_pack/)")
    args = ap.parse_args(argv)

    from pathlib import Path
    from nfs_outputs import out_dir

    packs = load_collision_packs(args.stream_bun)
    print(f"[nfs_collision_pack] {summary(packs)}")

    out = Path(args.out) if args.out else out_dir(TOOL_NAME)
    out.mkdir(parents=True, exist_ok=True)
    row_count = write_collision_instances_tsv(packs, out / 'collision_instances.tsv')
    print(f"[nfs_collision_pack] wrote collision_instances.tsv, {row_count} row(s), to {out}")


if __name__ == '__main__':
    main()
