"""
CARP ("World Grid") container reader, and the RoadNetworkGroup blocks inside
it, ported directly from UCGT's real Java source
(fr.ni240sx.ucgt.streamEditor.streamBlocks.CARP.java) - this is UCGT's actual
working parser, not a decompiled guess, so field layouts here should be
trustworthy.

CONTAINER FORMAT IS DIFFERENT FROM VisibleSections' chunk envelope:
CARP does NOT use the 4-byte-ID + 4-byte-length RIFF-style envelope that
nfs_region_common.py's walk_chunks() handles. It uses its own offset-table
format: each HeaderEntry is 16 bytes -
    int32 type            (4-char ASCII tag, e.g. 'CARP', 'RNgp', 'RNnd'...)
    int32 packed           (byte 0 = typeFlag, bytes 1-3 = dataLength)
    int32 numEntries
    int32 dataOffset
All fields are little-endian, same as everything else in this format - the
tag bytes on disk are stored REVERSED (e.g. 'CARP' is literally the bytes
'P','R','A','C' in the file) specifically so a little-endian 4-byte read
reconstructs the same int as manually packing the ASCII string big-endian-
style (UCGT's own stringToInt(), which the comparisons use) - confirmed by
UCGT's own source comment ("0x43415250 PRAC => CARP") and by finding the
literal reversed bytes 'PRAC'/'RNgp'-reversed/etc. in a real file. Struct
unpacking with '<' handles this correctly with no manual byte-swapping
needed, since it produces the same int either way.

If dataLength == 0, this entry is a CONTAINER: its children's HeaderEntries
sit at `entry_start + 16 * (dataOffset + i)` for i in 0..numEntries (each
header slot is 16 bytes, contiguous). If dataLength != 0, this entry is a
LEAF: its actual payload data sits at `entry_start + dataOffset` (a plain
byte offset this time, not scaled by 16).

The root CARP entry's typeFlag tells you what kind of CARP block this is:
    42  = WCollision (1 child: "Arti", classic collision data)
    122 = WGrid (3 children: "Arti", "CDat" (grid), "RNgp" (RoadNetworkGroup))
Road network data only exists under a WGrid-type CARP block.

CONFIRMED PRESENT in the real ProStreet test file (L6R_AutobahnDrift.BUN):
found by searching for the reversed tag bytes (not the plain ASCII, per the
note above) - CARP root at offset 543424, RNgp at 543472, and RNhd/RNnd/RNpf/
RNrd/RNsg at 543536/543552/543568/543584/543600 respectively. Those offsets
land exactly where the HeaderEntry math above predicts (RNgp's children
table starts at RNgp_offset + 16*4, one slot per sub-block in that order),
which is strong structural confirmation this port is being applied correctly
- but the actual road node/segment/profile field VALUES haven't been
sanity-checked yet (e.g. against the file's known world-space coordinate
range) the way ChunkBoundary was for VisibleSections.
"""
# stream bootstrap: make the shared library folders importable
import pathlib as _pl, sys as _sys
_sys.path[:0] = [str(_pl.Path(__file__).resolve().parents[1] / _d) for _d in ['common', 'region', 'scenery', 'solids_materials']]

import struct


CARP_WORLDGRID_ID = (0x0003B800).to_bytes(4, 'little')  # BlockType.CarpWorldGrid


def find_carp_offset(data):
    """Locates the real CARP root offset in a .BUN file generically, via the
    normal RIFF-style chunk envelope (nfs_region_common.walk_chunks) rather
    than brute-force byte searching. CarpWorldGrid is a top-level chunk (a
    sibling of VisibleSections, not nested inside it) whose payload is:
    int32 blockLength (a second, inner length field - redundant with the
    outer chunk envelope's own length), then zero or more 0x11111111
    alignment filler ints (per UCGT's own comment, 'always 2x -> align 16?'
    though only one was present in the real ProStreet test file), then the
    CARP root itself starts. Returns None if no CarpWorldGrid chunk exists
    in this file (i.e. this file has no road network - common for per-
    streaming-region files that aren't the track's main world file)."""
    from nfs_region_common import walk_chunks
    for offset, raw_id, id_hex, length, is_container, payload_start in walk_chunks(data):
        if raw_id == CARP_WORLDGRID_ID:
            pos = payload_start + 4  # skip the inner blockLength field
            while struct.unpack_from('<I', data, pos)[0] == 0x11111111:
                pos += 4
            return pos
    return None


def _tag_to_int(tag):
    """Matches UCGT's stringToInt(): packs ASCII bytes big-endian into an int,
    e.g. 'CARP' -> 0x43415250."""
    v = 0
    for c in tag.encode('ascii'):
        v = (v << 8) | c
    return v


def _int_to_tag(value):
    return bytes([(value >> 24) & 0xff, (value >> 16) & 0xff,
                  (value >> 8) & 0xff, value & 0xff]).decode('ascii', errors='replace')


TAG_CARP = _tag_to_int('CARP')
TAG_ARTI = _tag_to_int('Arti')
TAG_NAME = _tag_to_int('Name')
TAG_CDAT = _tag_to_int('CDat')
TAG_CGRD = _tag_to_int('CGrd')
TAG_RNGP = _tag_to_int('RNgp')
TAG_RNHD = _tag_to_int('RNhd')
TAG_RNND = _tag_to_int('RNnd')
TAG_RNPF = _tag_to_int('RNpf')
TAG_RNRD = _tag_to_int('RNrd')
TAG_RNSG = _tag_to_int('RNsg')


class HeaderEntry:
    """One 16-byte offset-table record. `type` is read big-endian (matches
    UCGT's plain ByteBuffer.getInt(), which defaults to big-endian) so it
    compares directly against the ASCII tag ints above."""
    __slots__ = ('offset', 'type', 'type_flag', 'data_length', 'num_entries',
                 'data_offset', 'children', 'data')

    def __init__(self, data, offset):
        self.offset = offset
        self.type = struct.unpack_from('<i', data, offset)[0]
        v1 = struct.unpack_from('<I', data, offset + 4)[0]
        self.type_flag = v1 & 0xff
        self.data_length = (v1 >> 8) & 0xffffff
        self.num_entries = struct.unpack_from('<i', data, offset + 8)[0]
        self.data_offset = struct.unpack_from('<i', data, offset + 12)[0]
        self.children = None
        self.data = None

    def tag(self):
        return _int_to_tag(self.type)

    def read_recursive(self, data):
        if self.data_length == 0:
            self.children = []
            num = self.num_entries
            if self.type == TAG_CARP:
                if self.type_flag == 42:
                    num = 1
                elif self.type_flag == 122:
                    num = 3
            for i in range(num):
                child_offset = self.offset + 16 * (self.data_offset + i)
                child = HeaderEntry(data, child_offset)
                self.children.append(child)
                child.read_recursive(data)
        else:
            self.data = self._read_leaf(data)

    def _read_leaf(self, data):
        payload_start = self.offset + self.data_offset
        length = self.data_length
        t = self.type
        if t == TAG_NAME:
            end = data.index(b'\x00', payload_start)
            return data[payload_start:end].decode('ascii', errors='replace')
        if t == TAG_RNHD:
            return RoadNetworkHeader(data, payload_start)
        if t == TAG_RNND:
            return parse_road_nodes(data, payload_start, length)
        if t == TAG_RNPF:
            return parse_road_profiles(data, payload_start, length)
        if t == TAG_RNRD:
            return parse_roads(data, payload_start, length)
        if t == TAG_RNSG:
            return parse_road_segments(data, payload_start, length)
        # CGrd, cl, ci, hk and anything else - not needed for road network
        # display, left unparsed for now.
        return None

    def get_entry(self, tag_int):
        if not self.children:
            return None
        for c in self.children:
            if c.type == tag_int:
                return c
        return None

    def has_entry(self, tag_int):
        return self.get_entry(tag_int) is not None


def parse_carp(data, offset):
    """Parses a CARP block starting at `offset` (where the literal ASCII
    'CARP' bytes are). Returns the root HeaderEntry, fully read."""
    root = HeaderEntry(data, offset)
    if root.type != TAG_CARP:
        raise ValueError(
            f"parse_carp: no 'CARP' tag at offset {offset} (got {root.tag()!r}) - "
            f"wrong offset, or this file doesn't contain a CARP block here.")
    root.read_recursive(data)
    return root


def find_road_network(carp_root):
    """Given a parsed WGrid-type CARP root, returns the RNgp HeaderEntry, or
    None if this CARP block has no road network (e.g. a WCollision-type
    block, typeFlag 42)."""
    arti = carp_root.get_entry(TAG_ARTI)
    return carp_root.get_entry(TAG_RNGP)


# ---- Road network records (RoadNetworkGroup: RNhd/RNnd/RNpf/RNrd/RNsg) ----

class RoadNetworkHeader:
    __slots__ = ('numProfiles', 'numNodes', 'numSegments', 'numRoads')

    def __init__(self, data, offset):
        self.numProfiles = struct.unpack_from('<h', data, offset)[0]
        self.numNodes = struct.unpack_from('<h', data, offset + 2)[0]
        self.numSegments = struct.unpack_from('<h', data, offset + 4)[0]
        # offset+6: 0 (unused)
        self.numRoads = struct.unpack_from('<h', data, offset + 10)[0]
        # offset+12, +14: 0 (unused)


class RoadNode:
    __slots__ = ('position', 'index', 'profileIndex', 'numSegments', 'segmentIndex')
    SIZE = 32  # 3 floats (12) + index/profileIndex (4) + numSegments+pad (2) + 7 shorts (14)

    def __init__(self, data, offset):
        x, y, z = struct.unpack_from('<fff', data, offset)
        self.position = (x, y, z)
        self.index = struct.unpack_from('<h', data, offset + 12)[0]
        self.profileIndex = struct.unpack_from('<h', data, offset + 14)[0]
        # UCGT's Java source reads numSegments as a getShort() here, but hex
        # inspection of a real ProStreet file shows that's wrong: offset+16
        # is a single byte (matching the real segment count, e.g. 2 for a
        # normal mid-road node), offset+17 is a constant 0xAA padding byte
        # (not part of the count), and only the first numSegments entries of
        # segmentIndex are meaningful - unused slots are filled with 0xAAAA,
        # not zero. This mirrors the same "count byte + padding + partially-
        # filled fixed array" pattern found in ProStreet's ChunksRelated.
        self.numSegments = data[offset + 16]
        self.segmentIndex = [
            struct.unpack_from('<h', data, offset + 18 + 2 * i)[0] for i in range(7)
        ]


def parse_road_nodes(data, payload_start, length):
    end = payload_start + length
    pos = payload_start
    out = []
    while pos < end:
        out.append(RoadNode(data, pos))
        pos += RoadNode.SIZE
    return out


class RoadProfile:
    __slots__ = ('numZones', 'middleZone', 'lanes')
    SIZE = 64  # 2 bytes + 2 padding + 15 * 4-byte lanes

    def __init__(self, data, offset):
        self.numZones = data[offset]
        self.middleZone = data[offset + 1]
        # offset+2, +3: 0xAAAA padding
        self.lanes = [
            struct.unpack_from('<I', data, offset + 4 + 4 * i)[0] for i in range(15)
        ]

    def lane_type(self, lane_index):
        return self.lanes[lane_index] & 0xf

    @staticmethod
    def _signed14(raw):
        raw &= 0x3fff
        return raw - 0x4000 if raw & 0x2000 else raw

    def lane_width(self, lane_index):
        # bits [4:18), signed 14-bit * 100/8191 - matches MW's decompiled
        # WRoadLane::GetWidth() exactly (Speed/Indep/Src/World/WRoadElem.h).
        # The previous formula here was an unverified guess and didn't
        # sign-extend the field, which only happened to look right on a
        # symmetric profile where sign doesn't show up.
        raw = (self.lanes[lane_index] >> 4) & 0x3fff
        return self._signed14(raw) * (100.0 / 8191.0)

    def lane_offset(self, lane_index):
        # bits [18:32), signed 14-bit * 100/8191 - matches WRoadLane::GetOffset()
        raw = (self.lanes[lane_index] >> 18) & 0x3fff
        return self._signed14(raw) * (100.0 / 8191.0)

    def total_width(self):
        """Sum of all lane widths in this profile - a rough full-road-
        envelope width, NOT lane-accurate (doesn't use type/offset to tell
        paved traffic lanes apart from sidewalk/median/shoulder/parking
        zones - the lane Type enum's real numeric values are unconfirmed,
        only names survive in MW's decompiled source as dead comments - see
        blackbox-carp-road-network memory notes). Good enough for a first-
        pass width visualization, not for precise per-lane placement."""
        return sum(self.lane_width(i) for i in range(self.numZones))


def parse_road_profiles(data, payload_start, length):
    end = payload_start + length
    pos = payload_start
    out = []
    while pos < end:
        out.append(RoadProfile(data, pos))
        pos += RoadProfile.SIZE
    return out


class Road:
    __slots__ = ('scale', 'length', 'shortcut', 'minWidth', 'speechID')
    SIZE = 8

    def __init__(self, data, offset):
        self.scale = struct.unpack_from('<h', data, offset)[0]
        self.length = struct.unpack_from('<h', data, offset + 2)[0]
        self.shortcut = data[offset + 4]
        self.minWidth = data[offset + 5]
        self.speechID = struct.unpack_from('<h', data, offset + 6)[0]


def parse_roads(data, payload_start, length):
    end = payload_start + length
    pos = payload_start
    out = []
    while pos < end:
        out.append(Road(data, pos))
        pos += Road.SIZE
    return out


class RoadSegment:
    __slots__ = ('nodeStart', 'nodeEnd', 'length', 'roadID', 'index', 'flags',
                 'endHandleLength', 'startHandleLength', 'endHandle', 'startHandle')
    SIZE = 22  # 8 shorts (16) + 3 + 3 bytes of handles

    def __init__(self, data, offset):
        (self.nodeStart, self.nodeEnd, self.length, self.roadID, self.index,
         self.flags, self.endHandleLength, self.startHandleLength) = \
            struct.unpack_from('<8h', data, offset)
        self.endHandle = data[offset + 16:offset + 19]
        self.startHandle = data[offset + 19:offset + 22]


def parse_road_segments(data, payload_start, length):
    end = payload_start + length
    pos = payload_start
    out = []
    while pos < end:
        out.append(RoadSegment(data, pos))
        pos += RoadSegment.SIZE
    return out


class RoadNetwork:
    """Convenience bundle of the 5 RNgp sub-blocks, once located."""
    def __init__(self, header=None, nodes=None, profiles=None, roads=None, segments=None):
        self.header = header
        self.nodes = nodes or []
        self.profiles = profiles or []
        self.roads = roads or []
        self.segments = segments or []


def load_road_network_from_carp(data, carp_offset):
    """High-level helper: parses a CARP block at carp_offset and pulls out
    its RoadNetwork, if it has one (WGrid-type CARP blocks only)."""
    root = parse_carp(data, carp_offset)
    rngp = find_road_network(root)
    if rngp is None:
        return None

    def get_data(tag_int):
        e = rngp.get_entry(tag_int)
        return e.data if e else None

    return RoadNetwork(
        header=get_data(TAG_RNHD),
        nodes=get_data(TAG_RNND) or [],
        profiles=get_data(TAG_RNPF) or [],
        roads=get_data(TAG_RNRD) or [],
        segments=get_data(TAG_RNSG) or [],
    )
