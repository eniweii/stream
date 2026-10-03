"""
Most Wanted candidate structs for the Black Box region/world format.

Unlike ProStreet (hex-verified) and Undercover (ported from UCGT's tool),
this module is derived directly from MW's real decompiled source
(github.com/dbalatoni13/nfsmw, Speed/Indep/Src/World/VisibleSection.hpp) -
but has NOT been tested against an actual MW region/world file, because none
has been available to this project yet. The decompiled classes describe
MW's in-memory (runtime) layout; the on-disk chunk-serialized form could
reorder or drop fields the same way ProStreet's real on-disk layout ended up
differing from a naive port of Undercover's struct. Treat every offset here
as a hypothesis to verify against a real MW file, not a confirmed result.

Known chunk IDs for MW's VisibleSections container/blocks are NOT known -
BOUNDARIES_ID/RELATIONS_ID below are None until a real file lets us find them
by walking the chunk tree with nfs_region_common.walk_chunks() and matching
plausible record shapes against the struct guesses below.

---- VisibleSectionBoundary (from VisibleSection.hpp, total size 0xA4) ----
Source offsets are relative to the in-memory object, which starts with an
8-byte bTNode linked-list header (offset 0x0-0x7) that has no on-disk
equivalent - a chunk-serialized record would most likely start at what the
source calls offset 0x8:
    int16 SectionNumber        (source offset 0x8)
    int8  NumPoints            (0xA)
    int8  PanoramaBoundary     (0xB)
    bVector2 BBoxMin           (0xC, 2 floats)
    bVector2 BBoxMax           (0x14, 2 floats)
    bVector2 Centre            (0x1C, 2 floats)
    bVector2 Points[NumPoints] (0x24, 2 floats each, up to 16)
Notably smaller/simpler than UC/ProStreet's ChunkBoundary - no ID_over,
no elevationHash. PanoramaBoundary (int8) likely corresponds to the "type"
byte UC/ProStreet's struct has in the same relative position.

---- DrivableScenerySection (from VisibleSection.hpp, total size 0xA4) ----
Also excluding its own 8-byte bTNode header, and dropping pBoundary (a
runtime pointer at source offset 0x8 with no on-disk equivalent):
    int16 SectionNumber                        (source offset 0xC)
    int8  MostVisibleSections                  (0xE)
    int8  MaxVisibleSections                   (0xF)
    int16 NumVisibleSections                   (0x10)
    int16 VisibleSections[NumVisibleSections]  (0x12, capacity 72)
MostVisibleSections/MaxVisibleSections/NumVisibleSections is a plausible
match for ProStreet's dataCount/dataCapacity/unk1 trio (see
nfs_region_prostreet.py) - if MW's on-disk layout matches this closely, it
would mean ProStreet's still-unexplained "unk1" field is a peak/high-water
mark of related-section count, separate from the current count.
"""
import struct

BOUNDARIES_ID = None  # unknown - not yet found in a real MW file
RELATIONS_ID = None   # unknown - not yet found in a real MW file


class MWBoundaryCandidate:
    __slots__ = ('offset', 'SectionNumber', 'NumPoints', 'PanoramaBoundary',
                 'BBoxMin', 'BBoxMax', 'Centre', 'Points')

    def __init__(self, data, offset):
        self.offset = offset
        self.SectionNumber = struct.unpack_from('<h', data, offset)[0]
        self.NumPoints = data[offset + 2]
        self.PanoramaBoundary = data[offset + 3]
        bx0, by0, bx1, by1, cx, cy = struct.unpack_from('<ffffff', data, offset + 4)
        self.BBoxMin = (bx0, by0)
        self.BBoxMax = (bx1, by1)
        self.Centre = (cx, cy)
        pts = []
        p = offset + 28
        for _ in range(self.NumPoints):
            x, y = struct.unpack_from('<ff', data, p)
            pts.append((x, y))
            p += 8
        self.Points = pts

    @property
    def size(self):
        return 28 + self.NumPoints * 8


class MWRelatedCandidate:
    __slots__ = ('offset', 'SectionNumber', 'MostVisibleSections', 'MaxVisibleSections',
                 'NumVisibleSections', 'VisibleSections', 'size')

    def __init__(self, data, offset):
        self.offset = offset
        self.SectionNumber = struct.unpack_from('<h', data, offset)[0]
        self.MostVisibleSections = data[offset + 2]
        self.MaxVisibleSections = data[offset + 3]
        self.NumVisibleSections = struct.unpack_from('<h', data, offset + 4)[0]
        ids_start = offset + 6
        self.VisibleSections = [
            struct.unpack_from('<h', data, ids_start + 2 * i)[0]
            for i in range(self.NumVisibleSections)
        ]
        self.size = ids_start + self.MaxVisibleSections * 2 - offset


def parse_boundaries(data, payload_start, length):
    raise NotImplementedError(
        "MW boundary chunk ID and container layout are unknown - "
        "MWBoundaryCandidate is an untested hypothesis from decompiled source. "
        "Use nfs_region_common.walk_chunks() on a real MW file to find the "
        "real chunk IDs first, then hex-verify a record against this guess "
        "before trusting it.")


def parse_relations(data, payload_start, length):
    raise NotImplementedError(
        "MW relations chunk ID and container layout are unknown - "
        "MWRelatedCandidate is an untested hypothesis from decompiled source. "
        "Use nfs_region_common.walk_chunks() on a real MW file to find the "
        "real chunk IDs first, then hex-verify a record against this guess "
        "before trusting it.")
