"""
Undercover-specific piece of the Black Box region/world .BUN format:
ChunksRelated (VisibleSections_Relations).

Ported directly from NI240SX's UCGT (github.com/NI240SX/UCGT) documented
layout. NOT independently verified against a real Undercover .BUN file the
way ProStreet's struct was (see nfs_region_prostreet.py) - this is UCGT's
own documented layout, kept as a literal port, unmodified by the ProStreet
investigation.

If a real UC file produces bad parses here (raises, or doesn't consume the
block to its exact declared end), don't assume this is right just because
it's sourced from UCGT - the ProStreet struct looked plausible at a glance
too and turned out to differ in several fields once actually hex-inspected.
Treat a failure here the same way: hex-dump a record at the reported offset
and re-derive from the real bytes.
"""
# stream bootstrap: make the shared library folders importable
import pathlib as _pl, sys as _sys
_sys.path[:0] = [str(_pl.Path(__file__).resolve().parents[1] / _d) for _d in ['common', 'region', 'scenery', 'solids_materials']]

import struct

from nfs_region_common import ChunkBoundary, parse_boundaries  # re-exported for convenience


class ChunksRelated:
    __slots__ = ('offset', 'type1', 'type2', 'type', 'ID', 'relatedChunkIDs', 'size')

    def __init__(self, data, offset):
        self.offset = offset
        self.type1, self.type2, self.type = struct.unpack_from('<iii', data, offset)
        self.ID = struct.unpack_from('<h', data, offset + 12)[0]
        # offset+14: unused/duplicate short in the source, skipped
        dataCapacity = struct.unpack_from('<h', data, offset + 16)[0]
        dataCount = struct.unpack_from('<h', data, offset + 18)[0]
        ids = []
        p = offset + 20
        for _ in range(dataCount):
            ids.append(struct.unpack_from('<h', data, p)[0])
            p += 2
        p += (dataCapacity - dataCount) * 2  # filler shorts
        p += 4  # trailing unused int
        self.relatedChunkIDs = ids
        self.size = p - offset


def parse_relations(data, payload_start, length):
    end = payload_start + length
    pos = payload_start
    out = []
    while pos < end:
        r = ChunksRelated(data, pos)
        if r.type1 != 11 or r.type2 != 11:
            raise ValueError(
                f"ChunksRelated record at offset {pos} has type1={r.type1} type2={r.type2} "
                f"(expected 11/11) - struct does not match this file's actual layout.")
        out.append(r)
        pos += r.size
    if pos != end:
        raise ValueError(
            f"Relations block did not parse to its exact declared end ({pos} != {end}) - "
            f"struct mismatch, discard this result rather than use it.")
    return out


# ---- Undercover's own section-letter naming (from UCGT source itself) ----
# UCGT's StreamController.java, VisualController.java, and StreamInfo.java
# all independently define the same toString():
#     (char)('A' + ID/1000) + "" + ID%1000
# This is NOT the same formula as nfs_region_common.section_letter() (which
# is MW/ProStreet's real decompiled formula: divisor 100, with a -1 offset
# so the letter starts at 'A' for the 100-199 range). Undercover's own tool
# uses divisor 1000 with NO offset (letter starts at 'A' for the 0-999
# range) - so an Undercover section can have up to 999 subsections per
# letter, not 99. Do not reuse the common-module formula for Undercover IDs:
# e.g. ID 3238 under the common formula misparses as chr(32+64)='`'
# (backtick) with subsection 38, when the real answer (per UCGT) is letter
# 'D' (238 // 1000... - 'A'+3238//1000='D'), subsection 238 -> "D238".
def section_letter(section_id):
    return chr(ord('A') + section_id // 1000)


def section_subsection(section_id):
    return section_id % 1000


def format_section_label(section_id):
    return f"{section_letter(section_id)}{section_subsection(section_id)}"
