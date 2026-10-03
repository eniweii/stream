"""
ProStreet-specific piece of the Black Box region/world .BUN format:
ChunksRelated (VisibleSections_Relations).

VERIFIED, not guessed: hand hex-inspection of L6R_AutobahnDrift.BUN's
relations block (offset 431544, length 4436), confirmed by parsing all 57
records in the block this way and landing exactly on its declared end
(435980) with zero leftover/overrun bytes.

This differs from UCGT's documented Undercover struct (see
nfs_region_undercover.py) in three ways:
  - the third int32 ("type") is 0 here, not 11 like type1/type2
  - dataCount/dataCapacity are packed as two bytes (u8, u8), not two i16s
  - there's an extra i16 ("unk1") between dataCapacity and the ID array
  - the trailing unused field is i16, not i32

unk1 usually equals dataCount but not always (7 of 57 records in the test
file have unk1 < dataCount) - its real meaning is still unknown, but it does
NOT affect record size, which is driven by dataCapacity only:
    size = 20 + dataCapacity * 2

relatedChunkIDs commonly includes the record's own ID as an entry, and can
reference chunk IDs absent from this file's own Boundaries block entirely
(e.g. IDs 141-163, in a gap this file's own boundaries don't cover) - that's
expected (cross-region-file neighbor references), not a parse error. See
nfs_region_common.py's section-letter helpers for another confirmation of
this: some "missing" IDs (2401, 2501) decode as library/texture section
references, not garbage.
"""
import struct

from nfs_region_common import ChunkBoundary, parse_boundaries  # re-exported for convenience


class ChunksRelated:
    __slots__ = ('offset', 'type1', 'type2', 'type', 'ID', 'dataCount', 'dataCapacity',
                 'unk1', 'relatedChunkIDs', 'trailing', 'size',
                 'visible_related_chunk_ids', 'stale_related_chunk_ids')

    def __init__(self, data, offset):
        self.offset = offset
        self.type1, self.type2, self.type = struct.unpack_from('<iii', data, offset)
        self.ID = struct.unpack_from('<h', data, offset + 12)[0]
        self.dataCount = data[offset + 14]
        self.dataCapacity = data[offset + 15]
        self.unk1 = struct.unpack_from('<h', data, offset + 16)[0]
        ids_start = offset + 18
        self.relatedChunkIDs = [
            struct.unpack_from('<h', data, ids_start + 2 * i)[0]
            for i in range(self.dataCount)
        ]
        trailer_pos = ids_start + self.dataCapacity * 2
        self.trailing = struct.unpack_from('<h', data, trailer_pos)[0]
        self.size = (trailer_pos + 2) - offset

        # Untested hypothesis, not yet confirmed against a real file: when
        # unk1 < dataCount, unk1 might be the count that's actually
        # currently visible, with the remainder (dataCount - unk1) being
        # stale/leftover entries from a larger previously-used count rather
        # than real live relations. Doesn't change parsing (size is still
        # driven by dataCapacity only, same as before) - purely an
        # additional, optional way to look at the same already-parsed list.
        # To check this against a real file: see whether entries in
        # stale_related_chunk_ids resolve to real boundaries less often than
        # entries in visible_related_chunk_ids do.
        if 0 <= self.unk1 <= self.dataCount:
            self.visible_related_chunk_ids = self.relatedChunkIDs[:self.unk1]
            self.stale_related_chunk_ids = self.relatedChunkIDs[self.unk1:]
        else:
            # unk1 > dataCount has not been observed in any real file checked
            # so far - fall back to treating everything as visible rather
            # than guessing which entries to drop.
            self.visible_related_chunk_ids = self.relatedChunkIDs
            self.stale_related_chunk_ids = []


def parse_relations(data, payload_start, length):
    end = payload_start + length
    pos = payload_start
    out = []
    while pos < end:
        r = ChunksRelated(data, pos)
        if r.type1 != 11 or r.type2 != 11:
            raise ValueError(
                f"ChunksRelated record at offset {pos} has type1={r.type1} type2={r.type2} "
                f"(expected 11/11) - struct is misaligned here, discard this result.")
        out.append(r)
        pos += r.size
    if pos != end:
        raise ValueError(
            f"Relations block did not parse to its exact declared end ({pos} != {end}) - "
            f"struct mismatch, discard this result rather than use it.")
    return out
