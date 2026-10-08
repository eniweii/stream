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
            ci     collision instance records, 0x40 bytes each (see below)
            ca     one collision article per block (triangle strips, edges, surfaces).
                   The MW decomp finds them as 'ca' (WCollisionArticle). Carbon has no
                   Havok, so 'hk' blocks are not expected; they are only counted
            co     collision objects (box and cylinder shapes), 0x70 bytes each in MW
            si     surface triggers (not read here)
The block tags are 'ci', 'ca', 'co' in the high 16 bits and a block number in the low
16 bits. Run this tool with --tags to list what a real file holds.

Collision instance record (0x40 bytes in the real Carbon file: 493696 bytes for 7714
articles, and the offset table says 14 entries for a 896 byte block; UCGT's 0x44 is
wrong for this file. Field names from the MW decomp):
    +0x00 fInvMatRow0Width  4f   row 0 of the inverse matrix, width in the 4th float
    +0x10 chunk_id          u16  UCGT calls it the chunk ID, the decomp calls it fIterStamp
    +0x12 flags             u16  fFlags
    +0x14 height            f
    +0x18 group_number      u16  scenery group that switches this instance (0 = always on)
    +0x1A instance_id       u16  fRenderInstanceInd: picks the article ('ca' block),
                                 NOT a scenery instance index
    +0x1C fCollisionArticle  u32  runtime pointer (zero on disk)
    +0x20 fInvMatRow2Length 4f   row 2 of the inverse matrix, length in the 4th float
    +0x30 fInvPosRadius     4f   position xyz, radius in the 4th float
A block whose stride is 0x44 (the UCGT layout) is still read: its fields after +0x1C sit
4 bytes later. The stride is block length / table entries, else 0x40.
The stored position is the INVERSE position (MW WCollisionInstance::CalcPosition):
    pos.x = -(P . row0)   pos.z = -(P . row2)   pos.y = -(P . (row2 x row0)) when
    flags & 3, else -P.y            (P = the stored position, rows 0 and 2 as stored)
true_position() does this. UCGT says row 0 is (1, 0, 0) and row 2 is (0, 0, 1) in every
file it saw; then the true position is the stored one with all three signs flipped.
UCGT also says the position is in a different axis order than the scenery positions;
match_to_scenery() picks the order from the data.

Instance flags (Hyperlinked collision.hpp): 0x01 y_vector_not_up, 0x02 dynamic,
0x04 disabled, 0x40 no_traffic, 0x80 no_cop. group_number != 0 gives an instance
the exclusion flags 0xC0 in MW (WCollisionAssets::SetExclusionFlags).

Article (the 'ca' block, MW WCollisionArticle, header 0x10):
    +0x00 u16 strip count     +0x02 u16 strips size (sphere table + strip data)
    +0x04 u16 edge count      +0x06 u16 edges size
    +0x08 u8 resolved         +0x09 u8 surface count     +0x0A u16 surfaces size
    +0x0C u16 intermediate    +0x0E i16 flags
    then strip count strip spheres (0x10 each: f32 x y z, u16 radius / 16, u16 offset),
    the strip data, the edges (0x20 each: min xyz, surface u8, flags u8, u16, max xyz,
    normalizer), and the surface hashes (u32 each).
    A strip sits at article start + offset + 0x10. Its vertices are 8 bytes: i16 x y z
    (divide by 128, add the sphere position) and a surface byte and flags byte. Vertex 0
    holds the vertex count in its last two bytes and vertex 1 the strip flags. The
    vertices are a triangle strip: count - 2 triangles.
World vertex = sum over j of local_j * row_j (rows of the instance matrix, row 1 = row 2 x
row 0 or (0, 1, 0)) + true position. For the usual identity rows: local + position.

Object (the 'co' block, MW CollisionObject, 0x70 bytes):
    +0x00 f32 x y z radius, +0x10 f32 dimensions x y z w, +0x20 u8 type, +0x21 u8 shape,
    +0x22 u16 flags, +0x24 u16 instance index (fRenderInstanceInd), +0x26 surface and
    surface flags, +0x28 8 bytes pad, +0x30 4x4 matrix.
    Hyperlinked object_flags: 0x01 dynamic, 0x02 vehicle, 0x04 character,
    0x08 player_controlled, 0x10 hench_controlled, 0x20 disabled, 0x40 unrenderable.

The article index of an instance is its instance_id. For an object the instance index
MAY be the scenery instance number: match_objects_to_scenery() tests that, and also
tests position and bounding box. No method is trusted before it prints a hit rate.
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
TAG_CA = 0x63610000                              # 'ca', one collision article per block number
TAG_CO = 0x636F0000                              # 'co', collision objects
TAG_HK = 0x686B0000                              # 'hk', Havok article (not expected on Carbon)
CI_RECORD_SIZE = 0x40
CO_RECORD_SIZE = 0x70
ARTICLE_HEADER_SIZE = 0x10
STRIP_SPHERE_SIZE = 0x10
VERTEX_SIZE = 8
EDGE_SIZE = 0x20

COLLISION_INSTANCE_FLAGS = {
    0x01: 'y_vector_not_up',
    0x02: 'dynamic',
    0x04: 'disabled',
    0x40: 'no_traffic',
    0x80: 'no_cop',
}

COLLISION_OBJECT_FLAGS = {
    0x01: 'dynamic',
    0x02: 'vehicle',
    0x04: 'character',
    0x08: 'player_controlled',
    0x10: 'hench_controlled',
    0x20: 'disabled',
    0x40: 'unrenderable',
}

# Settings for the matching functions
MATCH_DISTANCE = 1.5        # world units between a collision position and a scenery position
CALIBRATION_SAMPLE = 400    # collision instances or objects used to pick the axis order
MIN_MATCH_RATE = 0.5        # best axis order must match at least this share of the sample
AMBIGUITY_RATIO = 0.8       # and the second best must stay below this share of the best
INDEX_MARGIN = 2.0          # index method: the object may sit this far outside the bounding box
GEOMETRY_IOU = 0.3          # geometry method: article box and scenery box must overlap this much
GEOMETRY_MARGIN = 0.5       # both boxes are grown by this much before the overlap is measured
GEOMETRY_CELL = 64.0        # geometry method: scenery boxes are looked up within this distance


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
        self.position = position    # (x, y, z) as stored: the INVERSE position, see true_position()
        self.radius = radius
        self.inv_row0 = inv_row0
        self.inv_row2 = inv_row2


class CollisionStrip:
    __slots__ = ('sphere', 'radius', 'flags', 'vertices', 'surfaces')

    def __init__(self, sphere, radius, flags, vertices, surfaces):
        self.sphere = sphere          # (x, y, z) strip sphere position, article space
        self.radius = radius          # strip sphere radius in world units
        self.flags = flags            # strip flags: 1 up facing, 2 facing unknown
        self.vertices = vertices      # list of (x, y, z) in article space (vertex / 128 + sphere)
        self.surfaces = surfaces      # one surface byte per triangle (from its third vertex)


class CollisionEdge:
    __slots__ = ('minimum', 'maximum', 'surface', 'flags', 'normalizer')

    def __init__(self, minimum, maximum, surface, flags, normalizer):
        self.minimum = minimum
        self.maximum = maximum
        self.surface = surface
        self.flags = flags
        self.normalizer = normalizer


class CollisionArticle:
    __slots__ = ('number', 'length', 'strip_count', 'strips_size', 'edge_count', 'edges_size',
                 'resolved', 'surface_count', 'surfaces_size', 'intermediate', 'flags',
                 'strips', 'edges', 'surface_hashes', 'problem')

    def __init__(self, number, length):
        self.number = number            # block number: the instance_id of the instances that use it
        self.length = length
        self.strip_count = self.strips_size = self.edge_count = self.edges_size = 0
        self.resolved = self.surface_count = self.surfaces_size = 0
        self.intermediate = self.flags = 0
        self.strips = []
        self.edges = []
        self.surface_hashes = []
        self.problem = ''               # empty when the article parsed cleanly

    def vertex_count(self):
        return sum(len(strip.vertices) for strip in self.strips)

    def triangle_count(self):
        return sum(max(0, len(strip.vertices) - 2) for strip in self.strips)

    def bounds(self):
        """((min x, y, z), (max x, y, z)) over all strip vertices, or None."""
        points = [v for strip in self.strips for v in strip.vertices]
        if not points:
            return None
        return (tuple(min(p[i] for p in points) for i in range(3)),
                tuple(max(p[i] for p in points) for i in range(3)))


class CollisionObject:
    __slots__ = ('index', 'obj_type', 'shape', 'flags', 'instance_index', 'surface',
                 'surface_flags', 'position', 'radius', 'dimensions', 'matrix')

    def __init__(self, index, obj_type, shape, flags, instance_index, surface, surface_flags,
                 position, radius, dimensions, matrix):
        self.index = index
        self.obj_type = obj_type
        self.shape = shape
        self.flags = flags
        self.instance_index = instance_index    # fRenderInstanceInd in MW
        self.surface = surface
        self.surface_flags = surface_flags
        self.position = position                # (x, y, z), stored as is
        self.radius = radius
        self.dimensions = dimensions            # (x, y, z, w)
        self.matrix = matrix                    # 4 rows of 4 floats


class CollisionPack:
    __slots__ = ('offset', 'section_number', 'name', 'crp_size', 'flags', 'last_address',
                 'instances', 'objects', 'articles', 'hk_count', 'tags', 'object_stride')

    def __init__(self, offset, section_number, name, crp_size, flags, last_address,
                 instances, objects, articles, hk_count, tags, object_stride):
        self.offset = offset
        self.section_number = section_number
        self.name = name
        self.crp_size = crp_size
        self.flags = flags
        self.last_address = last_address
        self.instances = instances
        self.objects = objects
        self.articles = articles       # number -> CollisionArticle, from the 'ca' blocks
        self.hk_count = hk_count       # number of 'hk' blocks under Arti (Havok, not read)
        self.tags = tags               # tag name -> [block count, total bytes, (length, entries)]
        self.object_stride = object_stride

    @property
    def article_count(self):
        return len(self.articles)


def decode_collision_flags(flags):
    names = [name for bit, name in COLLISION_INSTANCE_FLAGS.items() if flags & bit]
    unknown = flags & ~sum(COLLISION_INSTANCE_FLAGS)
    for bit in range(16):
        if unknown & (1 << bit):
            names.append(f"bit_{bit}")
    return names


def decode_object_flags(flags):
    names = [name for bit, name in COLLISION_OBJECT_FLAGS.items() if flags & bit]
    unknown = flags & ~sum(COLLISION_OBJECT_FLAGS)
    for bit in range(16):
        if unknown & (1 << bit):
            names.append(f"bit_{bit}")
    return names


# ---- Instance transform (MW WCollisionInstance::CalcPosition and MakeMatrix) ----

def instance_rows(instance):
    """The three rows of the stored inverse matrix. Row 1 is row 2 x row 0 when
    flags & 3 is set, else (0, 1, 0)."""
    row0, row2 = instance.inv_row0, instance.inv_row2
    if instance.flags & 3:
        row1 = (row2[1] * row0[2] - row2[2] * row0[1],
                row2[2] * row0[0] - row2[0] * row0[2],
                row2[0] * row0[1] - row2[1] * row0[0])
    else:
        row1 = (0.0, 1.0, 0.0)
    return row0, row1, row2


def true_position(instance):
    """The real position of an instance. The stored value P is the inverse position,
    so the position is -(P . row) for each row of the matrix."""
    px = instance.position
    return tuple(-(px[0] * row[0] + px[1] * row[1] + px[2] * row[2])
                 for row in instance_rows(instance))


def local_to_world(instance, local, position=None):
    """Article space to world: row_i . local + position_i."""
    if position is None:
        position = true_position(instance)
    rows = instance_rows(instance)
    return tuple(rows[i][0] * local[0] + rows[i][1] * local[1] + rows[i][2] * local[2]
                 + position[i] for i in range(3))


# ---- Parsing ----

def _instance_stride(child):
    """Bytes per instance record: block length / table entries when that divides evenly
    and is 0x40 or more, else CI_RECORD_SIZE."""
    length = child.data_length
    if child.num_entries > 0 and length % child.num_entries == 0 \
            and length // child.num_entries >= CI_RECORD_SIZE:
        return length // child.num_entries
    return CI_RECORD_SIZE


def _parse_collision_instances(data, payload_start, length, stride=CI_RECORD_SIZE):
    instances = []
    shift = stride - CI_RECORD_SIZE   # 0 for the 0x40 layout; the fields after +0x1C move by it
    for index in range(length // stride):
        pos = payload_start + index * stride
        inv_row0 = struct.unpack_from('<fff', data, pos + 0x00)
        width = struct.unpack_from('<f', data, pos + 0x0C)[0]
        chunk_id, flags = struct.unpack_from('<HH', data, pos + 0x10)
        height = struct.unpack_from('<f', data, pos + 0x14)[0]
        group_number, instance_id = struct.unpack_from('<HH', data, pos + 0x18)
        inv_row2 = struct.unpack_from('<fff', data, pos + 0x20 + shift)
        length_value = struct.unpack_from('<f', data, pos + 0x2C + shift)[0]
        position = struct.unpack_from('<fff', data, pos + 0x30 + shift)
        radius = struct.unpack_from('<f', data, pos + 0x3C + shift)[0]
        instances.append(CollisionInstance(index, chunk_id, flags, group_number, instance_id,
                                           width, height, length_value, position, radius,
                                           inv_row0, inv_row2))
    return instances


def _object_stride(child):
    """MW uses 0x70 bytes per object. If the block does not divide by 0x70, the entry
    count of the offset table gives the stride."""
    length = child.data_length
    if length % CO_RECORD_SIZE == 0:
        return CO_RECORD_SIZE
    if child.num_entries > 0 and length % child.num_entries == 0 \
            and length // child.num_entries >= 0x50:
        return length // child.num_entries
    return CO_RECORD_SIZE


def _parse_collision_objects(data, payload_start, length, stride):
    objects = []
    for index in range(length // stride):
        pos = payload_start + index * stride
        x, y, z, radius = struct.unpack_from('<ffff', data, pos + 0x00)
        dimensions = struct.unpack_from('<ffff', data, pos + 0x10)
        obj_type, shape, flags, instance_index, surface, surface_flags = \
            struct.unpack_from('<BBHHBB', data, pos + 0x20)
        matrix_pos = pos + stride - 0x40      # the matrix is the last 0x40 bytes of a record
        matrix = tuple(struct.unpack_from('<ffff', data, matrix_pos + row * 16) for row in range(4))
        objects.append(CollisionObject(index, obj_type, shape, flags, instance_index, surface,
                                       surface_flags, (x, y, z), radius, dimensions, matrix))
    return objects


def _parse_collision_article(data, start, length, number):
    article = CollisionArticle(number, length)
    if length < ARTICLE_HEADER_SIZE:
        article.problem = f"block is {length} bytes, header needs {ARTICLE_HEADER_SIZE}"
        return article
    (article.strip_count, article.strips_size, article.edge_count, article.edges_size,
     article.resolved, article.surface_count, article.surfaces_size, article.intermediate,
     article.flags) = struct.unpack_from('<HHHHBBHHh', data, start)

    end = start + length
    needed = (ARTICLE_HEADER_SIZE + article.strips_size + article.edges_size
              + article.surface_count * 4)
    if needed > length:
        article.problem = f"sizes need {needed} bytes, block is {length}"
        return article
    if article.strip_count * STRIP_SPHERE_SIZE > article.strips_size:
        article.problem = f"{article.strip_count} strip spheres do not fit in {article.strips_size} bytes"
        return article

    # Strips: a sphere table, then strips that the spheres point to
    for i in range(article.strip_count):
        sphere_pos = start + ARTICLE_HEADER_SIZE + i * STRIP_SPHERE_SIZE
        sx, sy, sz, radius, offset = struct.unpack_from('<fffHH', data, sphere_pos)
        strip_pos = start + offset + ARTICLE_HEADER_SIZE
        if strip_pos + 2 * VERTEX_SIZE > end:
            article.problem = f"strip {i} starts outside the block"
            break
        vertex_count = struct.unpack_from('<H', data, strip_pos + 6)[0]
        strip_flags = struct.unpack_from('<H', data, strip_pos + VERTEX_SIZE + 6)[0]
        if vertex_count < 3 or strip_pos + vertex_count * VERTEX_SIZE > end:
            article.problem = f"strip {i} has {vertex_count} vertices, does not fit"
            break
        vertices = []
        for j in range(vertex_count):
            x, y, z = struct.unpack_from('<hhh', data, strip_pos + j * VERTEX_SIZE)
            vertices.append((x / 128.0 + sx, y / 128.0 + sy, z / 128.0 + sz))
        surfaces = [data[strip_pos + j * VERTEX_SIZE + 6] for j in range(2, vertex_count)]
        article.strips.append(CollisionStrip((sx, sy, sz), radius / 16.0, strip_flags,
                                             vertices, surfaces))

    # Edges (barriers)
    edges_start = start + ARTICLE_HEADER_SIZE + article.strips_size
    if article.edge_count:
        stride = article.edges_size // article.edge_count
        if stride != EDGE_SIZE:
            article.problem = article.problem or f"edge stride is {stride}, expected {EDGE_SIZE}"
        else:
            for i in range(article.edge_count):
                values = struct.unpack_from('<fffBBHffff', data, edges_start + i * EDGE_SIZE)
                article.edges.append(CollisionEdge(values[0:3], values[6:9], values[3], values[4],
                                                   values[9]))

    # Surface hashes
    surfaces_start = edges_start + article.edges_size
    if article.surface_count:
        article.surface_hashes = list(struct.unpack_from(f'<{article.surface_count}I', data,
                                                         surfaces_start))
    return article


NUMBERED_TAGS = ('ci', 'ca', 'co', 'hk', 'si')


def _tag_key(tag_type):
    """Readable name of an offset-table type. The tags of NUMBERED_TAGS carry a block
    number in the low 16 bits and give the two letters; other tags give all four."""
    high = (tag_type >> 16) & 0xFFFF
    letters = chr(high >> 8) + chr(high & 0xFF) if 0x20 <= high >> 8 < 0x7F and 0x20 <= high & 0xFF < 0x7F else ''
    if letters in NUMBERED_TAGS:
        return letters
    return ''.join(chr(c) if 0x20 <= c < 0x7F else '.'
                   for c in (tag_type >> 24 & 0xFF, tag_type >> 16 & 0xFF,
                             tag_type >> 8 & 0xFF, tag_type & 0xFF))


def _parse_collision_pack(data, offset, payload_start):
    pos = payload_start + 4   # skip the inner blockLength field
    while struct.unpack_from('<I', data, pos)[0] == 0x11111111:
        pos += 4
    crp_size, section_number, flags, last_address = struct.unpack_from('<IIII', data, pos)
    root = parse_carp(data, pos + 16)

    name = ''
    instances = []
    objects = []
    articles = {}
    hk_count = 0
    tags = {}
    object_stride = CO_RECORD_SIZE
    arti = root.get_entry(TAG_ARTI)
    if arti and arti.children:
        for child in arti.children:
            key = _tag_key(child.type)
            entry = tags.setdefault(key, [0, 0, (child.data_length, child.num_entries)])
            entry[0] += 1
            entry[1] += child.data_length
            kind = child.type & TAG_CI_MASK
            start = child.offset + child.data_offset
            if child.type == TAG_NAME:
                name = child.data or ''
            elif kind == TAG_CI and child.data_length:
                instances = _parse_collision_instances(data, start, child.data_length,
                                                       _instance_stride(child))
            elif kind == TAG_CO and child.data_length:
                object_stride = _object_stride(child)
                objects = _parse_collision_objects(data, start, child.data_length, object_stride)
            elif kind == TAG_CA and child.data_length:
                number = child.type & 0xFFFF
                articles[number] = _parse_collision_article(data, start, child.data_length, number)
            elif kind == TAG_HK:
                hk_count += 1
    return CollisionPack(offset, section_number, name, crp_size, flags, last_address,
                         instances, objects, articles, hk_count, tags, object_stride)


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
    objects = sum(len(p.objects) for p in packs)
    articles = sum(p.article_count for p in packs)
    hk = sum(p.hk_count for p in packs)
    return (f"{len(packs)} collision pack(s), {instances} collision instance(s), "
            f"{articles} article(s), {objects} object(s), {hk} hk block(s)")


def tag_census(packs):
    """tag name -> [block count, total bytes, number of packs with it, first (length, entries)]."""
    census = {}
    for pack in packs:
        for key, (count, size, example) in pack.tags.items():
            entry = census.setdefault(key, [0, 0, 0, example])
            entry[0] += count
            entry[1] += size
            entry[2] += 1
    return census


def census_lines(packs):
    lines = []
    census = tag_census(packs)
    for key in sorted(census):
        count, size, pack_count, example = census[key]
        lines.append(f"  {key:<6} {count:>7} block(s) {size:>10} byte(s) in {pack_count} pack(s); "
                     f"first block length {example[0]}, table entries {example[1]}")
    return lines


def article_problems(packs):
    return [(p.section_number, a.number, a.problem)
            for p in packs for a in p.articles.values() if a.problem]


# ---- Matching collision data to scenery instances ----

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


def _inside_box(point, bbox_min, bbox_max, margin):
    return all(bbox_min[i] - margin <= point[i] <= bbox_max[i] + margin for i in range(3))


def _best_orders(sample, hit):
    """sample: list of items. hit(item, order, signs) is True for a fit. Returns
    (rate, runner-up share of the best, order, signs) for the best axis order."""
    scores = []
    for order, signs in _axis_orders():
        hits = sum(1 for item in sample if hit(item, order, signs))
        scores.append((hits, order, signs))
    scores.sort(key=lambda s: s[0], reverse=True)
    best_hits, order, signs = scores[0]
    second = scores[1][0] / best_hits if best_hits else 1.0
    return best_hits / len(sample), second, order, signs


def _gate(rate, second, min_rate):
    """Empty text when the fit is good enough, else the reason it is not."""
    if rate < min_rate:
        return f"best fit is {rate:.0%}, below the {min_rate:.0%} threshold"
    if second >= AMBIGUITY_RATIO:
        return f"more than one axis order fits (runner-up is {second:.0%} of the best)"
    return ''


def _pairs(packs, scenery, need_objects):
    """Packs that have both collision data and scenery instances in the same section."""
    pairs = []
    for pack in packs:
        section = scenery.sections.get(pack.section_number)
        items = pack.objects if need_objects else pack.instances
        if section and items and section.instances:
            pairs.append((pack, section))
    return pairs


def _sample(items_per_pack):
    """items_per_pack: list of lists. Takes about CALIBRATION_SAMPLE items, evenly spread."""
    per_pack = max(1, CALIBRATION_SAMPLE // max(1, len(items_per_pack)))
    sample = []
    for items in items_per_pack:
        step = max(1, len(items) // per_pack)
        sample.extend(items[::step][:per_pack])
    return sample


def _box_iou(a_min, a_max, b_min, b_max, margin):
    """Volume overlap of two boxes, each grown by margin on every side (flat boxes
    would have no volume)."""
    inter = 1.0
    for i in range(3):
        low = max(a_min[i], b_min[i]) - margin
        high = min(a_max[i], b_max[i]) + margin
        if high <= low:
            return 0.0
        inter *= high - low
    volume_a = volume_b = 1.0
    for i in range(3):
        volume_a *= a_max[i] - a_min[i] + 2 * margin
        volume_b *= b_max[i] - b_min[i] + 2 * margin
    return inter / (volume_a + volume_b - inter)


def _article_corners(pack, instance):
    """The 8 corners of the article bounding box in world space, or None when the
    instance has no article with strips."""
    article = pack.articles.get(instance.instance_id)
    bounds = article.bounds() if article else None
    if bounds is None:
        return None
    low, high = bounds
    position = true_position(instance)
    return [local_to_world(instance, (x, y, z), position)
            for x in (low[0], high[0]) for y in (low[1], high[1]) for z in (low[2], high[2])]


def _mapped_box(corners, order, signs):
    points = [_apply_order(c, order, signs) for c in corners]
    return (tuple(min(p[i] for p in points) for i in range(3)),
            tuple(max(p[i] for p in points) for i in range(3)))


def _match_by_geometry(packs, scenery, min_rate, matches):
    """Method 1 for instances: the bounding box of the article (strip vertices) against the
    bounding box of a scenery instance, volume overlap >= GEOMETRY_IOU. Adds to matches
    and returns one report line."""
    prepared = []   # (pack, hash of scenery bbox centers, boxes by instance number, corners by index)
    for pack, section in _pairs(packs, scenery, need_objects=False):
        centers = [(inst.instance_number, tuple((inst.bbox_min[i] + inst.bbox_max[i]) / 2.0
                                                for i in range(3))) for inst in section.instances]
        boxes = {inst.instance_number: (inst.bbox_min, inst.bbox_max) for inst in section.instances}
        corners = {ci.index: _article_corners(pack, ci) for ci in pack.instances}
        prepared.append((pack, _build_hash(centers, GEOMETRY_CELL), boxes, corners))
    if not prepared:
        return "collision match (geometry): no section has both collision instances and scenery instances"

    def candidates(table, boxes, corners, order, signs):
        low, high = _mapped_box(corners, order, signs)
        center = tuple((low[i] + high[i]) / 2.0 for i in range(3))
        for _distance, number in _near(table, center, GEOMETRY_CELL, GEOMETRY_CELL):
            iou = _box_iou(low, high, boxes[number][0], boxes[number][1], GEOMETRY_MARGIN)
            if iou >= GEOMETRY_IOU:
                yield iou, number

    sample = _sample([[(table, boxes, corners[ci.index]) for ci in pack.instances
                       if corners[ci.index] is not None] for pack, table, boxes, corners in prepared])
    if not sample:
        return "collision match (geometry): no instance has an article with strips"
    rate, second, order, signs = _best_orders(
        sample, lambda item, o, sg: next(candidates(item[0], item[1], item[2], o, sg), None)
        is not None)
    text = (f"collision match (geometry): axis order {order}, signs {signs}, {rate:.0%} of "
            f"{len(sample)} sampled article boxes overlap a scenery box (>= {GEOMETRY_IOU:.0%})")
    reason = _gate(rate, second, min_rate)
    if reason:
        return f"{text}; NOT used: {reason}"

    placed = []
    total = 0
    for pack, table, boxes, corners in prepared:
        pairs_found = []
        for ci in pack.instances:
            if corners[ci.index] is None:
                continue
            for iou, number in candidates(table, boxes, corners[ci.index], order, signs):
                pairs_found.append((-iou, ci.index, number))
        pairs_found.sort()
        used_collision, used_scenery = set(), set()
        for negative_iou, collision_index, number in pairs_found:
            if collision_index in used_collision or number in used_scenery:
                continue
            used_collision.add(collision_index)
            used_scenery.add(number)
            matches[(pack.section_number, number)] = collision_index
            placed.append(-negative_iou)
        total += len(pack.instances)
    placed.sort()
    median = placed[len(placed) // 2] if placed else 0.0
    return f"{text}; used, {len(placed)} of {total} collision instance(s) matched, median overlap {median:.0%}"


def _match_by_position(packs, scenery, min_rate, matches):
    """Method 2 for instances: the true position against a scenery origin or bounding box
    middle (within MATCH_DISTANCE). Skips instances and scenery that method 1 placed.
    Adds to matches and returns one report line."""
    pairs = []   # (pack, tables for that section)
    for pack, section in _pairs(packs, scenery, need_objects=False):
        tables = {target: _build_hash(points, MATCH_DISTANCE)
                  for target, points in _scenery_points(section).items()}
        pairs.append((pack, tables))
    if not pairs:
        return "collision match (position): no section has both collision instances and scenery instances"

    sample = []   # (tables, true position)
    per_pack = max(1, CALIBRATION_SAMPLE // len(pairs))
    for pack, tables in pairs:
        step = max(1, len(pack.instances) // per_pack)
        for instance in pack.instances[::step][:per_pack]:
            sample.append((tables, true_position(instance)))

    best = None   # (rate, second, order, signs, target); origin wins a tie
    for target in ('origin', 'bbox_center'):
        result = _best_orders(sample, lambda item, order, signs, t=target: next(
            _near(item[0][t], _apply_order(item[1], order, signs), MATCH_DISTANCE,
                  MATCH_DISTANCE), None) is not None)
        if best is None or result[0] > best[0]:
            best = (*result, target)
    rate, second, order, signs, target = best

    text = (f"collision match (position): axis order {order}, signs {signs}, target {target}, "
            f"{rate:.0%} of {len(sample)} sampled instances fit")
    reason = _gate(rate, second, min_rate)
    if reason:
        return f"{text}; NOT used: {reason}"

    added = 0
    total = 0
    for pack, tables in pairs:
        candidates = []
        for instance in pack.instances:
            mapped = _apply_order(true_position(instance), order, signs)
            for distance, number in _near(tables[target], mapped, MATCH_DISTANCE, MATCH_DISTANCE):
                candidates.append((distance, instance.index, number))
        candidates.sort()
        used_collision = {index for (section, _n), index in matches.items()
                          if section == pack.section_number}
        used_scenery = {number for (section, number) in matches if section == pack.section_number}
        for distance, collision_index, number in candidates:
            if collision_index in used_collision or number in used_scenery:
                continue
            used_collision.add(collision_index)
            used_scenery.add(number)
            matches[(pack.section_number, number)] = collision_index
            added += 1
        total += len(pack.instances)
    return f"{text}; used, {added} more instance(s) placed of {total}"


def match_to_scenery(packs, scenery, min_rate=MIN_MATCH_RATE):
    """Finds which scenery instance each collision INSTANCE belongs to. Two methods, in
    this order; each prints its own hit rate and is used only when the rate reaches
    min_rate and the best axis order stands clear of the runner-up:
        geometry  the bounding box of the instance's article against the bounding box
                  of a scenery instance (volume overlap)
        position  the true position against a scenery origin or bounding box middle
    The axis order is picked from the data for each method. Returns (matches, reports).
    matches maps (section_number, scenery instance_number) to the index of a collision
    instance in that section's pack, and is empty when no method passes. reports is a
    list of console lines."""
    matches = {}
    reports = [_match_by_geometry(packs, scenery, min_rate, matches),
               _match_by_position(packs, scenery, min_rate, matches)]
    reports.append(f"collision match: {len(matches)} scenery instance(s) got a collision instance")
    return matches, reports


def match_by_group(packs, scenery):
    """The one link MW itself has between collision and scenery. A collision instance with
    group_number != 0 only counts while the scenery group of that number is enabled
    (WCollisionMgr: fGroupNumber == 0 || IsSceneryGroupEnabled(fGroupNumber)). The group
    number is SceneryGroup.group_number. Needs the groups, so the scenery must hold the
    region file too (scenery.merge). Returns (links, reports). links maps
    (section_number, scenery instance_number) to the list of collision instance indexes
    of that section's pack whose group_number is a group number touching that scenery
    instance through the group's overrides."""
    grouped = [(pack, ci) for pack in packs for ci in pack.instances if ci.group_number]
    total = sum(len(pack.instances) for pack in packs)
    if not scenery.groups:
        return {}, [f"collision groups: {len(grouped)} of {total} instance(s) have a group "
                    f"number; no scenery groups loaded (give the region file too)"]
    group_numbers = {g.group_number for g in scenery.groups}
    numbers = {ci.group_number for _pack, ci in grouped}
    known = numbers & group_numbers
    known_instances = sum(1 for _pack, ci in grouped if ci.group_number in group_numbers)

    links = {}
    same_section = 0
    for pack in packs:
        section = scenery.sections.get(pack.section_number)
        by_group = {}
        for ci in pack.instances:
            if ci.group_number:
                by_group.setdefault(ci.group_number, []).append(ci.index)
        if not by_group:
            continue
        touched = set()
        for _idx, override, groups in scenery.overrides_for_section(pack.section_number):
            for group in groups:
                if group.group_number in by_group:
                    touched.add(group.group_number)
                    links.setdefault((pack.section_number, override.instance_number), [])
                    for index in by_group[group.group_number]:
                        if index not in links[(pack.section_number, override.instance_number)]:
                            links[(pack.section_number, override.instance_number)].append(index)
        same_section += sum(len(by_group[n]) for n in touched)

    unused = len(group_numbers - numbers)
    reports = [
        f"collision groups: {len(grouped)} of {total} instance(s) have a group number "
        f"({len(numbers)} distinct); {len(known)} of those numbers are scenery group numbers "
        f"({known_instances} instance(s)); {unused} of {len(group_numbers)} scenery group "
        f"numbers have no collision instance",
        f"collision groups: {same_section} grouped instance(s) share a section with an override "
        f"of their group; {len(links)} scenery instance(s) linked by group number"]
    return links, reports


def match_objects_to_scenery(packs, scenery, min_rate=MIN_MATCH_RATE):
    """Finds which scenery instance each collision OBJECT belongs to, with three methods
    tried in this order. Each method prints its own hit rate and is used only when the
    rate reaches min_rate and the best axis order stands clear of the runner-up.
        index     the object's instance index is the scenery instance number, and the
                  object sits inside that instance's bounding box (INDEX_MARGIN)
        position  the object sits within MATCH_DISTANCE of a scenery origin or
                  bounding box middle
        inside    the object sits inside the bounding box of a scenery instance (the
                  smallest box wins)
    Returns (matches, reports). matches maps (section_number, scenery instance_number)
    to (method, object index). A scenery instance keeps the first method that claims it."""
    pairs = _pairs(packs, scenery, need_objects=True)
    if not pairs:
        return {}, ["object match: no section has both collision objects and scenery instances"]

    total_objects = sum(len(pack.objects) for pack, _section in pairs)
    matches = {}
    reports = []
    claimed = set()   # (section number, object index) already placed by an earlier method

    def place(method, section_number, scenery_number, object_index, distance):
        key = (section_number, scenery_number)
        if key in matches or (section_number, object_index) in claimed:
            return False
        matches[key] = (method, object_index, distance)
        claimed.add((section_number, object_index))
        return True

    # Method 1: index
    numbered = []   # (object, scenery instance it names)
    per_pack = []
    for pack, section in pairs:
        by_number = {inst.instance_number: inst for inst in section.instances}
        items = [(obj, by_number[obj.instance_index]) for obj in pack.objects
                 if obj.instance_index in by_number]
        numbered.extend(items)
        per_pack.append(items)
    named = len(numbered)
    sample = _sample([items for items in per_pack if items])
    if not sample:
        reports.append(f"object match (index): {named} of {total_objects} object(s) name an "
                       f"existing scenery instance number; method not testable")
    else:
        rate, second, order, signs = _best_orders(sample, lambda item, o, sg: _inside_box(
            _apply_order(item[0].position, o, sg), item[1].bbox_min, item[1].bbox_max, INDEX_MARGIN))
        reason = _gate(rate, second, min_rate)
        text = (f"object match (index): {named} of {total_objects} object(s) name an existing "
                f"scenery instance number; axis order {order}, signs {signs}, {rate:.0%} of "
                f"{len(sample)} sampled fit the named bounding box")
        if reason:
            reports.append(f"{text}; NOT used: {reason}")
        else:
            used = 0
            for pack, section in pairs:
                by_number = {inst.instance_number: inst for inst in section.instances}
                for obj in pack.objects:
                    inst = by_number.get(obj.instance_index)
                    if inst is None:
                        continue
                    mapped = _apply_order(obj.position, order, signs)
                    if _inside_box(mapped, inst.bbox_min, inst.bbox_max, INDEX_MARGIN):
                        used += place('index', pack.section_number, inst.instance_number,
                                      obj.index, 0.0)
            reports.append(f"{text}; used, {used} object(s) placed")

    # Method 2: position
    prepared = []   # (pack, tables)
    for pack, section in pairs:
        tables = {target: _build_hash(points, MATCH_DISTANCE)
                  for target, points in _scenery_points(section).items()}
        prepared.append((pack, tables))
    sample = _sample([[(tables, obj.position) for obj in pack.objects] for pack, tables in prepared])
    best = None
    for target in ('origin', 'bbox_center'):
        result = _best_orders(sample, lambda item, order, signs, t=target: next(
            _near(item[0][t], _apply_order(item[1], order, signs), MATCH_DISTANCE,
                  MATCH_DISTANCE), None) is not None)
        if best is None or result[0] > best[0]:
            best = (*result, target)
    rate, second, order, signs, target = best
    reason = _gate(rate, second, min_rate)
    text = (f"object match (position): axis order {order}, signs {signs}, target {target}, "
            f"{rate:.0%} of {len(sample)} sampled fit")
    if reason:
        reports.append(f"{text}; NOT used: {reason}")
    else:
        used = 0
        for pack, tables in prepared:
            candidates = []
            for obj in pack.objects:
                mapped = _apply_order(obj.position, order, signs)
                for distance, number in _near(tables[target], mapped, MATCH_DISTANCE,
                                              MATCH_DISTANCE):
                    candidates.append((distance, obj.index, number))
            candidates.sort()
            for distance, object_index, number in candidates:
                used += place('position', pack.section_number, number, object_index, distance)
        reports.append(f"{text}; used, {used} object(s) placed")

    # Method 3: inside a bounding box
    def containing(obj, section, order, signs):
        mapped = _apply_order(obj.position, order, signs)
        return [inst for inst in section.instances
                if _inside_box(mapped, inst.bbox_min, inst.bbox_max, MATCH_DISTANCE)]

    sections_by_pack = {id(pack): section for pack, section in pairs}
    sample = _sample([[(pack, obj) for obj in pack.objects] for pack, _section in pairs])
    rate, second, order, signs = _best_orders(sample, lambda item, o, sg: bool(
        containing(item[1], sections_by_pack[id(item[0])], o, sg)))
    reason = _gate(rate, second, min_rate)
    text = (f"object match (inside): axis order {order}, signs {signs}, {rate:.0%} of "
            f"{len(sample)} sampled sit inside a scenery bounding box")
    if reason:
        reports.append(f"{text}; NOT used: {reason}")
    else:
        used = 0
        for pack, section in pairs:
            for obj in pack.objects:
                hits = containing(obj, section, order, signs)
                if not hits:
                    continue
                hits.sort(key=lambda i: (i.bbox_max[0] - i.bbox_min[0])
                          * (i.bbox_max[1] - i.bbox_min[1]) * (i.bbox_max[2] - i.bbox_min[2]))
                for inst in hits:
                    if place('inside', pack.section_number, inst.instance_number, obj.index, 0.0):
                        used += 1
                        break
        reports.append(f"{text}; used, {used} object(s) placed")

    reports.append(f"object match: {len(matches)} scenery instance(s) got a collision object, "
                   f"{total_objects} object(s) in the sections tested")
    return {key: (method, index) for key, (method, index, _d) in matches.items()}, reports


# ---- TSV output ----

def _write_tsv(path, header, rows):
    with open(path, 'w', encoding='utf-8', newline='') as f:
        writer = csv.writer(f, delimiter='\t', lineterminator='\n')
        writer.writerow(header)
        writer.writerows(rows)


def write_collision_instances_tsv(packs, path):
    """One row per collision instance. Join to scenery_instances.tsv on
    Section + CollisionIndex (the CollisionIndex column of that file). PosX, PosY, PosZ
    are the stored (inverse) position; TrueX, TrueY, TrueZ are the real position."""
    rows = []
    for pack in sorted(packs, key=lambda p: p.section_number):
        for ci in pack.instances:
            true = true_position(ci)
            rows.append([
                pack.section_number, ci.index, ci.chunk_id, f"0x{ci.flags:04X}",
                '|'.join(decode_collision_flags(ci.flags)) or 'None',
                ci.group_number, ci.instance_id,
                f"{ci.width:.4f}", f"{ci.height:.4f}", f"{ci.length:.4f}",
                f"{ci.position[0]:.4f}", f"{ci.position[1]:.4f}", f"{ci.position[2]:.4f}",
                f"{ci.radius:.4f}",
                f"{true[0]:.4f}", f"{true[1]:.4f}", f"{true[2]:.4f}",
            ])
    _write_tsv(path, ['Section', 'Index', 'ChunkId', 'Flags', 'FlagNames', 'Group', 'InstanceId',
                      'Width', 'Height', 'Length', 'PosX', 'PosY', 'PosZ', 'Radius',
                      'TrueX', 'TrueY', 'TrueZ'], rows)
    return len(rows)


def write_collision_articles_tsv(packs, path):
    """One row per article ('ca' block). UsedBy is the number of instances in the same
    pack whose InstanceId is this article number."""
    rows = []
    for pack in sorted(packs, key=lambda p: p.section_number):
        used = {}
        for ci in pack.instances:
            used[ci.instance_id] = used.get(ci.instance_id, 0) + 1
        for number in sorted(pack.articles):
            article = pack.articles[number]
            bounds = article.bounds()
            low, high = bounds if bounds else (('', '', ''), ('', '', ''))
            fmt = lambda v: f"{v:.4f}" if v != '' else ''
            rows.append([
                pack.section_number, number, article.strip_count, article.edge_count,
                article.surface_count, article.flags, article.vertex_count(),
                article.triangle_count(), article.length, used.get(number, 0),
                *(fmt(v) for v in low), *(fmt(v) for v in high), article.problem,
            ])
    _write_tsv(path, ['Section', 'Article', 'Strips', 'Edges', 'Surfaces', 'Flags', 'Vertices',
                      'Triangles', 'Bytes', 'UsedBy', 'MinX', 'MinY', 'MinZ', 'MaxX', 'MaxY',
                      'MaxZ', 'Problem'], rows)
    return len(rows)


def write_collision_objects_tsv(packs, path):
    """One row per collision object ('co' block). Matrix rows are written as x,y,z."""
    rows = []
    for pack in sorted(packs, key=lambda p: p.section_number):
        for obj in pack.objects:
            rows.append([
                pack.section_number, obj.index, obj.obj_type, obj.shape, f"0x{obj.flags:04X}",
                '|'.join(decode_object_flags(obj.flags)) or 'None', obj.instance_index,
                obj.surface, obj.surface_flags,
                f"{obj.position[0]:.4f}", f"{obj.position[1]:.4f}", f"{obj.position[2]:.4f}",
                f"{obj.radius:.4f}", *(f"{v:.4f}" for v in obj.dimensions),
                *(','.join(f"{v:.4f}" for v in row[:3]) for row in obj.matrix),
            ])
    _write_tsv(path, ['Section', 'Index', 'Type', 'Shape', 'Flags', 'FlagNames', 'InstanceIndex',
                      'Surface', 'SurfaceFlags', 'PosX', 'PosY', 'PosZ', 'Radius', 'DimX', 'DimY',
                      'DimZ', 'DimW', 'MatRow0', 'MatRow1', 'MatRow2', 'MatRow3'], rows)
    return len(rows)


def write_tag_census_tsv(packs, path):
    census = tag_census(packs)
    rows = [[key, c[0], c[1], c[2], c[3][0], c[3][1]] for key, c in sorted(census.items())]
    _write_tsv(path, ['Tag', 'Blocks', 'Bytes', 'Packs', 'FirstLength', 'FirstEntries'], rows)
    return len(rows)


def write_collision_obj(pack, path):
    """One section as a Wavefront OBJ, to look at in Blender: every instance with an article
    becomes an object o ci<index>_ca<article>, strips as triangles in world space."""
    vertex_base = 1
    with open(path, 'w', encoding='utf-8', newline='') as f:
        f.write(f"# collision pack, section {pack.section_number}\n")
        for ci in pack.instances:
            article = pack.articles.get(ci.instance_id)
            if article is None or not article.strips:
                continue
            position = true_position(ci)
            f.write(f"o ci{ci.index}_ca{ci.instance_id}\n")
            for strip in article.strips:
                for vertex in strip.vertices:
                    world = local_to_world(ci, vertex, position)
                    f.write(f"v {world[0]:.4f} {world[1]:.4f} {world[2]:.4f}\n")
                for t in range(len(strip.vertices) - 2):
                    a, b, c = vertex_base + t, vertex_base + t + 1, vertex_base + t + 2
                    f.write(f"f {a} {b} {c}\n" if t % 2 == 0 else f"f {b} {a} {c}\n")
                vertex_base += len(strip.vertices)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Write the collision pack TSV files from the stream file.")
    ap.add_argument('stream_bun', help="stream file (STREAML5RA.BUN)")
    ap.add_argument('--out', help="output folder (default outputs/nfs_collision_pack/)")
    ap.add_argument('--match', action='store_true',
                    help="also match the instances and objects to the scenery instances of the "
                         "stream file and print the hit rate of each method")
    ap.add_argument('--region-bun', metavar='REGION',
                    help="region file (L5RA.BUN): with --match, loads the scenery groups so the "
                         "group numbers of the instances can be checked")
    ap.add_argument('--min-rate', type=float, default=MIN_MATCH_RATE,
                    help=f"hit rate a matching method needs to be used (default {MIN_MATCH_RATE})")
    ap.add_argument('--obj', type=int, metavar='SECTION',
                    help="also write the collision articles of this section as an OBJ file")
    args = ap.parse_args(argv)

    from pathlib import Path
    from nfs_outputs import out_dir

    packs = load_collision_packs(args.stream_bun)
    print(f"[nfs_collision_pack] {summary(packs)}")
    print("[nfs_collision_pack] blocks under Arti, all packs:")
    for line in census_lines(packs):
        print(line)
    missing = sum(1 for p in packs for ci in p.instances if ci.instance_id not in p.articles)
    shared = sum(1 for p in packs
                 for number in p.articles
                 if sum(1 for ci in p.instances if ci.instance_id == number) > 1)
    unused = sum(1 for p in packs for number in p.articles
                 if not any(ci.instance_id == number for ci in p.instances))
    print(f"[nfs_collision_pack] instances without an article: {missing}; articles used by "
          f"more than one instance: {shared}; articles no instance uses: {unused}")
    problems = article_problems(packs)
    if problems:
        print(f"[nfs_collision_pack] {len(problems)} article(s) did not parse cleanly, "
              f"first: section {problems[0][0]} article {problems[0][1]}: {problems[0][2]}")

    out = Path(args.out) if args.out else out_dir(TOOL_NAME)
    out.mkdir(parents=True, exist_ok=True)
    row_count = write_collision_instances_tsv(packs, out / 'collision_instances.tsv')
    print(f"[nfs_collision_pack] wrote collision_instances.tsv, {row_count} row(s), to {out}")
    row_count = write_collision_articles_tsv(packs, out / 'collision_articles.tsv')
    print(f"[nfs_collision_pack] wrote collision_articles.tsv, {row_count} row(s)")
    row_count = write_collision_objects_tsv(packs, out / 'collision_objects.tsv')
    print(f"[nfs_collision_pack] wrote collision_objects.tsv, {row_count} row(s)")
    write_tag_census_tsv(packs, out / 'collision_tags.tsv')

    if args.obj is not None:
        chosen = [p for p in packs if p.section_number == args.obj]
        if not chosen:
            print(f"[nfs_collision_pack] no collision pack for section {args.obj}")
        else:
            write_collision_obj(chosen[0], out / f"collision_section_{args.obj}.obj")
            print(f"[nfs_collision_pack] wrote collision_section_{args.obj}.obj")

    if args.match:
        from nfs_stream_scenery import load_stream_scenery
        scenery = load_stream_scenery(args.stream_bun)
        if args.region_bun:
            scenery.merge(load_stream_scenery(args.region_bun))
        _group_links, group_reports = match_by_group(packs, scenery)
        for line in group_reports:
            print(f"[nfs_collision_pack] {line}")
        _matches, reports = match_to_scenery(packs, scenery, args.min_rate)
        for line in reports:
            print(f"[nfs_collision_pack] {line}")
        _object_matches, reports = match_objects_to_scenery(packs, scenery, args.min_rate)
        for line in reports:
            print(f"[nfs_collision_pack] {line}")


if __name__ == '__main__':
    main()
