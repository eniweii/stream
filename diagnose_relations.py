"""
Quick diagnostic for the unk1/dataCount question in ChunksRelated (see the
comment in nfs_region_prostreet.py). Reports, across a real region file, how
often the "visible" entries (index < unk1) vs. the "stale" entries (index >=
unk1, only present when unk1 < dataCount) fail to resolve to a real boundary
in the same file. If stale entries fail to resolve much more often than
visible ones, that's real evidence unk1 is the true live count and the
trailing entries are leftover/stale data rather than genuine relations.

This can't distinguish "stale data" from "legitimate cross-file reference"
by itself (both look like "doesn't resolve to a boundary in this file") -
but a strong skew between the two groups is still meaningful evidence either
way, and a file with zero stale entries just confirms there's nothing to
investigate there.

Usage: python diagnose_relations.py path/to/file.BUN [game]
"""
import sys

from nfs_region_parser import load_region_file


def diagnose(path, game='prostreet'):
    world = load_region_file(path, game=game)

    visible_total = visible_missing = 0
    stale_total = stale_missing = 0
    affected_records = 0

    for r in world.relations:
        visible = getattr(r, 'visible_related_chunk_ids', r.relatedChunkIDs)
        stale = getattr(r, 'stale_related_chunk_ids', [])
        if stale:
            affected_records += 1

        for i in visible:
            visible_total += 1
            if i not in world.by_id:
                visible_missing += 1

        for i in stale:
            stale_total += 1
            if i not in world.by_id:
                stale_missing += 1

    print(f"{len(world.relations)} relation records, {affected_records} with unk1 < dataCount")

    if visible_total:
        pct = 100 * visible_missing / visible_total
        print(f"visible entries (index < unk1):  {visible_missing}/{visible_total} "
              f"don't resolve to a boundary in this file ({pct:.1f}%)")

    if stale_total:
        pct = 100 * stale_missing / stale_total
        print(f"stale entries (index >= unk1):   {stale_missing}/{stale_total} "
              f"don't resolve to a boundary in this file ({pct:.1f}%)")
    else:
        print("no stale entries in this file (unk1 == dataCount on every record)")


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(f"Usage: python {sys.argv[0]} path/to/file.BUN [game]")
        sys.exit(1)

    diagnose(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else 'prostreet')
