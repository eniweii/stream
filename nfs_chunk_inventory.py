"""
Walks an entire file (region or stream, doesn't matter which) and reports
every distinct chunk ID found - count, total/min/max size, first offset.
Meant for cases where the real file is too large/impractical to upload:
run this locally, paste the printed report instead.

Also specifically flags the four chunk IDs currently pending confirmation
(see pending-decode-topics.md) so it's immediately obvious whether they
exist in a given file, without having to eyeball the full listing.

Usage: python nfs_chunk_inventory.py path/to/file.BUN [path/to/another.BUN ...]
"""
import sys
from collections import defaultdict

from nfs_region_common import walk_chunks

# The four pending topics, called out explicitly in the report if found.
WATCH_LIST = {
    0x0003BC00: "WorldFXTrigger / EmitterLibrary (emitter placement)",
    0x80034147: "TrackPathZone container (BCHUNK_TRACK_PATH_MANAGER)",
    0x0003414A: "TrackPathZone zones",
    0x0003414D: "TrackPathZone barriers",
    0x00037220: "world_anim header",
    0x00037240: "world_anim frames",
    0x00037250: "world_anim rtnode",
    0x00037260: "world_anim counts",
    0x00037270: "world_anim endptr",
    0x80036000: "event_trigger_pack container (World triggers)",
    0x00036001: "event_trigger_pack_header",
    0x00036002: "event_trigger_tree_nodes",
    0x00036003: "event_trigger_instances",
}


def inventory(path):
    data = open(path, 'rb').read()

    counts = defaultdict(int)
    total_size = defaultdict(int)
    min_size = {}
    max_size = {}
    first_offset = {}

    for offset, raw_id, _id_hex, length, _is_container, _payload_start in walk_chunks(data):
        chunk_id = int.from_bytes(raw_id, 'little')
        counts[chunk_id] += 1
        total_size[chunk_id] += length
        min_size[chunk_id] = min(min_size.get(chunk_id, length), length)
        max_size[chunk_id] = max(max_size.get(chunk_id, length), length)
        first_offset.setdefault(chunk_id, offset)

    print(f"\n=== {path} ({len(data)} bytes total) ===")
    print(f"{len(counts)} distinct chunk ID(s) found\n")

    print("--- Watch list (pending decode topics) ---")
    any_found = False
    for chunk_id, label in WATCH_LIST.items():
        if chunk_id in counts:
            any_found = True
            print(f"  FOUND  0x{chunk_id:08X}  {label}  "
                  f"(x{counts[chunk_id]}, sizes {min_size[chunk_id]}-{max_size[chunk_id]}, "
                  f"first at 0x{first_offset[chunk_id]:X})")
        else:
            print(f"  ----   0x{chunk_id:08X}  {label}")
    if not any_found:
        print("  (none of the watch-list IDs were found in this file)")

    print("\n--- Full chunk inventory (sorted by ID) ---")
    for chunk_id in sorted(counts):
        watch_tag = f"  <-- {WATCH_LIST[chunk_id]}" if chunk_id in WATCH_LIST else ""
        print(f"  0x{chunk_id:08X}  x{counts[chunk_id]:<6} "
              f"total={total_size[chunk_id]:<10} "
              f"sizes={min_size[chunk_id]}-{max_size[chunk_id]:<8} "
              f"first_offset=0x{first_offset[chunk_id]:X}{watch_tag}")


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(f"Usage: python {sys.argv[0]} path/to/file.BUN [path/to/another.BUN ...]")
        sys.exit(1)

    for path in sys.argv[1:]:
        inventory(path)
