"""
TroughBoundary.bin reader for Black Box NFS (Carbon). A separate file, not
inside the stream file or the region file.

The file is a flat run of chunks, no containers. Every chunk has the ID
0x57944655 (bytes 55 46 94 57 on disk) and holds ONE named 2D polygon: the
outline of a drivable area (a road mesh, a runoff, a shortcut, a drift area...)
or a hole cut out of one. Checked on the Carbon Collector's Edition file
(955,824 bytes): 808 chunks, every size adds up, the bounding box matches the
points on all 808.

Chunk layout (offsets from the start of the chunk, envelope included):
    +0x00 id u32 (0x57944655) | +0x04 size u32 (bytes after the envelope)
    +0x08 8 bytes, zero
    +0x10 name char[0x50], NUL padded (for example "RD_FWY_Road_01")
    +0x60 flags u32: 1 = trough, 256 = hole
    +0x64 index u16, 1-based, in file order
    +0x66 parent_index u16: own index for a trough, the trough it cuts for a hole
    +0x68 bbox 4f: min x, min y, max x, max y
    +0x78 num_points u32
    +0x7C points[num_points] 2f
    size == 0x74 + num_points * 8

Troughs wind counter-clockwise (positive area), holes clockwise (negative
area). 48 of the 808 polygons repeat their first point at the end, the others
do not. Only flags 1 and 256 exist in this file. There is no antitrough type
and no separate barrier record in it.

NOT YET CHECKED: which two axes the (x, y) points use. The region viewer draws
them in the same plane as the VisibleSections boundary polygons (the plane of
the TrackPath zones and barriers). Look at the overlay on a real region file.
"""
# stream bootstrap: make the shared library folders importable
import pathlib as _pl, sys as _sys
_sys.path[:0] = [str(_pl.Path(__file__).resolve().parents[1] / _d) for _d in ['common', 'region', 'scenery', 'solids_materials']]

import argparse
import csv
import struct

from nfs_region_common import walk_chunks

TOOL_NAME = 'nfs_trough_boundary'

TROUGH_ID = bytes.fromhex('55469457')   # 0x57944655, little-endian on disk

NAME_OFFSET = 0x08          # from the payload start
NAME_LENGTH = 0x50
FLAGS_OFFSET = 0x58
INDEX_OFFSET = 0x5C
PARENT_OFFSET = 0x5E
BBOX_OFFSET = 0x60
NUM_POINTS_OFFSET = 0x70
POINTS_OFFSET = 0x74

FLAG_TROUGH = 1
FLAG_HOLE = 256


class TroughPolygon:
    __slots__ = ('offset', 'name', 'flags', 'index', 'parent_index',
                 'bbox_min', 'bbox_max', 'points')

    def __init__(self, offset, name, flags, index, parent_index, bbox_min, bbox_max, points):
        self.offset = offset
        self.name = name
        self.flags = flags
        self.index = index
        self.parent_index = parent_index
        self.bbox_min = bbox_min
        self.bbox_max = bbox_max
        self.points = points  # list of (x, y)

    @property
    def is_hole(self):
        return self.flags == FLAG_HOLE

    @property
    def kind_name(self):
        if self.flags == FLAG_TROUGH:
            return 'trough'
        if self.flags == FLAG_HOLE:
            return 'hole'
        return f"flags_{self.flags}"

    def signed_area(self):
        """Shoelace area. Positive = counter-clockwise (trough), negative = clockwise (hole)."""
        total = 0.0
        count = len(self.points)
        for i in range(count):
            x0, y0 = self.points[i]
            x1, y1 = self.points[(i + 1) % count]
            total += x0 * y1 - x1 * y0
        return total / 2.0


class TroughBoundary:
    def __init__(self, polygons):
        self.polygons = polygons
        self.by_index = {p.index: p for p in polygons}

    def holes_of(self, polygon):
        return [p for p in self.polygons if p.is_hole and p.parent_index == polygon.index]

    def summary(self):
        holes = sum(1 for p in self.polygons if p.is_hole)
        points = sum(len(p.points) for p in self.polygons)
        return f"{len(self.polygons) - holes} trough(s), {holes} hole(s), {points} point(s)"


def _parse_trough(data, offset, payload_start, length):
    if length < POINTS_OFFSET:
        raise ValueError(f"trough chunk at 0x{offset:X} is too small ({length} bytes)")
    raw_name = data[payload_start + NAME_OFFSET:payload_start + NAME_OFFSET + NAME_LENGTH]
    name = raw_name.split(b'\x00')[0].decode('ascii', errors='replace')
    flags = struct.unpack_from('<I', data, payload_start + FLAGS_OFFSET)[0]
    index, parent_index = struct.unpack_from('<HH', data, payload_start + INDEX_OFFSET)
    min_x, min_y, max_x, max_y = struct.unpack_from('<ffff', data, payload_start + BBOX_OFFSET)
    num_points = struct.unpack_from('<I', data, payload_start + NUM_POINTS_OFFSET)[0]
    if POINTS_OFFSET + num_points * 8 != length:
        raise ValueError(f"trough chunk '{name}' at 0x{offset:X}: {num_points} point(s) "
                         f"do not match the chunk size {length}")
    points = [struct.unpack_from('<ff', data, payload_start + POINTS_OFFSET + 8 * i)
              for i in range(num_points)]
    return TroughPolygon(offset, name, flags, index, parent_index,
                         (min_x, min_y), (max_x, max_y), points)


def load_trough_boundary(path):
    data = open(path, 'rb').read()
    polygons = []
    for offset, raw_id, _id_hex, length, _is_container, payload_start in walk_chunks(data):
        if raw_id == TROUGH_ID:
            polygons.append(_parse_trough(data, offset, payload_start, length))
    return TroughBoundary(polygons)


# ---- TSV output ----

def _write_tsv(path, header, rows):
    with open(path, 'w', encoding='utf-8', newline='') as f:
        writer = csv.writer(f, delimiter='\t', lineterminator='\n')
        writer.writerow(header)
        writer.writerows(rows)


def write_troughs_tsv(trough_boundary, path):
    """One row per polygon (trough or hole)."""
    rows = []
    for p in trough_boundary.polygons:
        rows.append([
            p.index, p.name, p.kind_name, p.parent_index, len(p.points),
            f"{p.bbox_min[0]:.4f}", f"{p.bbox_min[1]:.4f}",
            f"{p.bbox_max[0]:.4f}", f"{p.bbox_max[1]:.4f}",
            f"{p.signed_area():.4f}",
        ])
    _write_tsv(path, ['Index', 'Name', 'Kind', 'Parent', 'Points',
                      'BBoxMinX', 'BBoxMinY', 'BBoxMaxX', 'BBoxMaxY', 'Area'], rows)
    return len(rows)


def write_trough_points_tsv(trough_boundary, path):
    """One row per point, in file order. Join to troughs.tsv on Index."""
    rows = []
    for p in trough_boundary.polygons:
        for point_index, (x, y) in enumerate(p.points):
            rows.append([p.index, point_index, f"{x:.4f}", f"{y:.4f}"])
    _write_tsv(path, ['Index', 'Point', 'X', 'Y'], rows)
    return len(rows)


def check_trough_boundary(trough_boundary):
    """Prints the checks that held on the file we tested. A message here on a
    new file means the layout is different there."""
    problems = 0
    for p in trough_boundary.polygons:
        if p.flags not in (FLAG_TROUGH, FLAG_HOLE):
            print(f"[nfs_trough_boundary] check: '{p.name}' has unknown flags {p.flags}")
            problems += 1
        if p.is_hole and p.parent_index not in trough_boundary.by_index:
            print(f"[nfs_trough_boundary] check: hole '{p.name}' has parent index "
                  f"{p.parent_index}, which does not exist")
            problems += 1
        xs = [pt[0] for pt in p.points]
        ys = [pt[1] for pt in p.points]
        if xs and (abs(min(xs) - p.bbox_min[0]) > 1.0 or abs(max(xs) - p.bbox_max[0]) > 1.0
                   or abs(min(ys) - p.bbox_min[1]) > 1.0 or abs(max(ys) - p.bbox_max[1]) > 1.0):
            print(f"[nfs_trough_boundary] check: '{p.name}' bounding box does not match its points")
            problems += 1
    if not problems:
        print("[nfs_trough_boundary] check: all polygons passed")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Write troughs.tsv and trough_points.tsv from TroughBoundary.bin.")
    ap.add_argument('trough_bin', help="TroughBoundary.bin")
    ap.add_argument('--out', help="output folder (default outputs/nfs_trough_boundary/)")
    args = ap.parse_args(argv)

    from pathlib import Path
    from nfs_outputs import out_dir

    trough_boundary = load_trough_boundary(args.trough_bin)
    print(f"[nfs_trough_boundary] {trough_boundary.summary()}")

    out = Path(args.out) if args.out else out_dir(TOOL_NAME)
    out.mkdir(parents=True, exist_ok=True)

    polygon_count = write_troughs_tsv(trough_boundary, out / 'troughs.tsv')
    point_count = write_trough_points_tsv(trough_boundary, out / 'trough_points.tsv')
    print(f"[nfs_trough_boundary] wrote troughs.tsv ({polygon_count} row(s)) and "
          f"trough_points.tsv ({point_count} row(s)) to {out}")

    check_trough_boundary(trough_boundary)


if __name__ == '__main__':
    main()
