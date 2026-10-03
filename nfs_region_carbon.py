"""
Carbon-specific piece of the Black Box region/world .BUN format:
ChunksRelated (VisibleSections_Relations).

VERIFIED against a real file: L5RA.BUN's relations block (offset 144792,
length 54380) parses as EXACTLY ProStreet's ChunksRelated struct (see
nfs_region_prostreet.py) - 572 records, consuming the block to its exact
declared end with zero leftover bytes. Carbon uses the identical layout,
right down to the same "unk1 occasionally differs from dataCount" quirk
(17 of 572 records here vs. 7 of 57 in the ProStreet test file - same
behavior, not a coincidence of one file).

ChunkBoundary (the shared struct in nfs_region_common.py) was independently
confirmed too: L5RA.BUN's boundaries block (offset 68912, length 67496)
parses to its exact declared end as well - 642 records, matching the count
already seen live in the viewer.

Carbon's section-letter/label formula is MW/ProStreet's (divisor 100, -1
offset) - already confirmed in nfs_region_common.py's docstring against
real Carbon debug screenshots (section 102 -> "A2", 1412 -> "N12"), so no
override is needed here the way Undercover needed its own divisor-1000
formula.

No struct here is Carbon-original - it's a straight reuse of ProStreet's,
now hex-verified against a real Carbon file rather than assumed.
"""
from nfs_region_common import ChunkBoundary, parse_boundaries  # re-exported for convenience
from nfs_region_prostreet import ChunksRelated, parse_relations  # re-exported - identical struct, verified above
