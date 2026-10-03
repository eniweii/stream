"""
TrackPath zones and barriers for Black Box NFS region files (Carbon/MW family).

Chunks (all live in the REGION file, for example L5RA.BUN, not the stream file):
    0x80034147  TRACK_PATH_MANAGER   (container)
    0x0003414A  TRACK_PATH_ZONES     (variable-length zone records)
    0x0003414D  TRACK_PATH_BARRIERS  (fixed 0x18-byte records)

Zone record layout (hex-verified on two real records, see the project notes):
    +0x00 type u32 | +0x04 position 2f | +0x0C direction 2f | +0x14 elevation f
    +0x18 zone_source i8 | +0x19 cached_index i8 | +0x1A visit_info i16
    +0x1C user_data u32 (runtime pointer, raw) | +0x20 bbox_min 2f | +0x28 bbox_max 2f
    +0x30 data[4] i32 | +0x40 num_points i16 | +0x42 memory_image_size i16
    +0x44 points[num_points] 2f
    memory_image_size == 0x44 + num_points * 8. The next record starts that
    many bytes later.

Barrier record layout (fixed 0x18 bytes):
    +0x00 p0 2f | +0x08 p1 2f | +0x10 enabled i8, pad i8, player_barrier i8,
    left_handed i8 | +0x14 group_hash u32

The 2D points use the same plane as the VisibleSections boundary polygons.
"""
import struct

from nfs_region_common import walk_chunks

TRACK_PATH_MANAGER_ID = bytes.fromhex('47410380')   # 0x80034147, little-endian on disk
TRACK_PATH_ZONES_ID = bytes.fromhex('4a410300')     # 0x0003414A
TRACK_PATH_BARRIERS_ID = bytes.fromhex('4d410300')  # 0x0003414D

ZONE_MIN_SIZE = 0x44
ZONE_MAX_POINTS = 64
BARRIER_SIZE = 0x18

ZONE_TYPES = {
    0: 'RESET',
    1: 'RESET_TO_POINT',
    2: 'GUIDED_RESET',
    3: 'TUNNEL',
    4: 'OVERPASS',
    5: 'OVERPASS_SMALL',
    6: 'STREAMER_PREDICTION',
    7: 'GARAGE',
    8: 'HIDDEN',
    9: 'TRAFFIC_PATTERN',
    10: 'DYNAMIC',
    11: 'NEIGHBOURHOOD',
    12: 'JUMP_CAM',
    13: 'NO_COP_SPAWN',
    14: 'PURSUIT_START',
}
STREAMER_PREDICTION = 6


def zone_type_name(type_id):
    return ZONE_TYPES.get(type_id, f'UNKNOWN_{type_id}')


class TrackPathZone:
    __slots__ = ('offset', 'record_size', 'type', 'position', 'direction', 'elevation',
                 'zone_source', 'cached_index', 'visit_info', 'user_data_raw',
                 'bbox_min', 'bbox_max', 'data', 'num_points', 'points')

    def __init__(self, data, offset):
        if offset + ZONE_MIN_SIZE > len(data):
            raise ValueError(f"zone header truncated at 0x{offset:08X}")
        self.offset = offset
        self.type = struct.unpack_from('<I', data, offset)[0]
        self.position = struct.unpack_from('<2f', data, offset + 0x04)
        self.direction = struct.unpack_from('<2f', data, offset + 0x0C)
        self.elevation = struct.unpack_from('<f', data, offset + 0x14)[0]
        self.zone_source = struct.unpack_from('<b', data, offset + 0x18)[0]
        self.cached_index = struct.unpack_from('<b', data, offset + 0x19)[0]
        self.visit_info = struct.unpack_from('<h', data, offset + 0x1A)[0]
        self.user_data_raw = struct.unpack_from('<I', data, offset + 0x1C)[0]
        self.bbox_min = struct.unpack_from('<2f', data, offset + 0x20)
        self.bbox_max = struct.unpack_from('<2f', data, offset + 0x28)
        self.data = struct.unpack_from('<4i', data, offset + 0x30)
        self.num_points = struct.unpack_from('<h', data, offset + 0x40)[0]
        self.record_size = struct.unpack_from('<h', data, offset + 0x42)[0]

        if not (0 <= self.num_points <= ZONE_MAX_POINTS):
            raise ValueError(f"invalid num_points={self.num_points} at 0x{offset:08X}")
        expected = ZONE_MIN_SIZE + self.num_points * 8
        if self.record_size != expected:
            raise ValueError(f"memory_image_size=0x{self.record_size:X}, expected 0x{expected:X} "
                             f"(num_points={self.num_points}) at 0x{offset:08X}")
        if offset + self.record_size > len(data):
            raise ValueError(f"zone at 0x{offset:08X} overruns the file")
        self.points = [struct.unpack_from('<2f', data, offset + 0x44 + 8 * i)
                       for i in range(self.num_points)]

    @property
    def type_name(self):
        return zone_type_name(self.type)

    def contains(self, x, y):
        """True if (x, y) lies inside the zone. Uses the bbox first. With 3 or
        more points, it then tests the polygon. With fewer points, the bbox
        is the whole test."""
        if not (self.bbox_min[0] <= x <= self.bbox_max[0]
                and self.bbox_min[1] <= y <= self.bbox_max[1]):
            return False
        if self.num_points < 3:
            return True
        inside = False
        pts = self.points
        n = len(pts)
        for i in range(n):
            x1, y1 = pts[i]
            x2, y2 = pts[(i + 1) % n]
            if ((y1 > y) != (y2 > y)) and (x < (x2 - x1) * (y - y1) / (y2 - y1 + 1e-12) + x1):
                inside = not inside
        return inside


class TrackPathBarrier:
    __slots__ = ('offset', 'p0', 'p1', 'enabled', 'pad', 'player_barrier',
                 'left_handed', 'group_hash')

    def __init__(self, data, offset):
        self.offset = offset
        self.p0 = struct.unpack_from('<2f', data, offset)
        self.p1 = struct.unpack_from('<2f', data, offset + 0x08)
        (self.enabled, self.pad, self.player_barrier,
         self.left_handed) = struct.unpack_from('<4b', data, offset + 0x10)
        self.group_hash = struct.unpack_from('<I', data, offset + 0x14)[0]

    def group_name(self):
        """Name from the external hash dictionary, or None. Never raises."""
        if not self.group_hash:
            return None
        try:
            import nfs_hash_dictionary
            return nfs_hash_dictionary.resolve(self.group_hash)
        except Exception:
            return None


def parse_zones(data, payload_start, length):
    """Walks the records by their own memory_image_size. On a bad record, it
    warns and keeps the records read so far, so one bad record does not lose
    the whole layer."""
    end = payload_start + length
    pos = payload_start
    zones = []
    while pos < end:
        if end - pos < ZONE_MIN_SIZE:
            print(f"[parse_zones] WARNING: {end - pos} trailing bytes at 0x{pos:08X}")
            break
        try:
            z = TrackPathZone(data, pos)
        except ValueError as e:
            print(f"[parse_zones] WARNING: stopped after {len(zones)} zone(s): {e}")
            break
        if pos + z.record_size > end:
            print(f"[parse_zones] WARNING: zone at 0x{pos:08X} crosses the chunk end")
            break
        zones.append(z)
        pos += z.record_size
    return zones


def parse_barriers(data, payload_start, length):
    if length % BARRIER_SIZE:
        print(f"[parse_barriers] WARNING: chunk length {length} does not divide by "
              f"0x{BARRIER_SIZE:X}. Extra bytes are ignored.")
    count = length // BARRIER_SIZE
    return [TrackPathBarrier(data, payload_start + i * BARRIER_SIZE) for i in range(count)]


class TrackPaths:
    def __init__(self, zones, barriers):
        self.zones = zones
        self.barriers = barriers


def load_track_paths(data):
    """Returns a TrackPaths bundle, or None if the file has neither chunk."""
    zones, barriers = [], []
    found = False
    for offset, raw_id, id_hex, length, is_container, payload_start in walk_chunks(data):
        if raw_id == TRACK_PATH_ZONES_ID:
            found = True
            zones.extend(parse_zones(data, payload_start, length))
        elif raw_id == TRACK_PATH_BARRIERS_ID:
            found = True
            barriers.extend(parse_barriers(data, payload_start, length))
    return TrackPaths(zones, barriers) if found else None
