"""
World grid of the region file (L5RA.BUN): the CARP block 0x0003B800, root typeFlag 122,
child CDat. The MW decomp (WGrid::Init in World/Common/WGrid.cpp) reads it as:
    CDat
        CGrd   the grid: WGrid, 0x20 bytes used
        CGcn   the grid nodes, back to back (WGridNode, walked with TotalSize())
The game finds every static collision instance through this grid: a query turns a point
and a radius into grid nodes (x and z, FindNodes), and each node lists the instances it
touches. So the grid, not the scenery, says where a collision instance applies.

WGrid (World/Common/WGrid.h):
    +0x00 fMin 4f   corner of the grid, x and z are used
    +0x10 fEdgeSize f   +0x14 fInvEdgeSize f   +0x18 fNumRows u32   +0x1C fNumCols u32
    node index = row * fNumCols + col; col = (x - min x) / edge, row = (z - min z) / edge
WGridNode (World/Common/WGridNode.h), header size = pointer size + 16:
    +0x00 fDynElems pointer (zero on disk), 4 bytes in a 32 bit build
    +P+0  fNodeInd u16   +P+2 pad u16   +P+4 fElemCounts u8[4]   +P+8 fElemOffsets u16[4]
    then the element lists: u32 each, list t at (end of the header + offset t),
    fElemCounts[t] entries. TotalSize = header + 4 * sum(counts).
Element types: 0 instance, 1 trigger, 2 object, 3 road segment.
An instance element is a global index: section number in the high 16 bits, index into
that section's collision pack in the low 16 bits (WCollisionAssets::Instance).

NOT CHECKED on a real file: the pointer size (4 or 8; this reader tries both and keeps
the one whose nodes end exactly at the end of the block), the meaning of the 'Map'
child, and whether a file has more than one CGcn block. Run it and read the check lines.
"""
# stream bootstrap: make the shared library folders importable
import pathlib as _pl, sys as _sys
_sys.path[:0] = [str(_pl.Path(__file__).resolve().parents[1] / _d) for _d in ['common', 'region', 'scenery', 'solids_materials']]

import argparse
import csv
import struct

from nfs_carp_parser import find_carp_offset, parse_carp, TAG_CDAT, TAG_CGRD, _tag_to_int, _int_to_tag

TOOL_NAME = 'nfs_world_grid'

TAG_CGCN = _tag_to_int('CGcn')
ELEMENT_NAMES = ('instance', 'trigger', 'object', 'road_segment')
GRID_HEADER_SIZE = 0x20
POINTER_SIZES = (4, 8)


class GridNode:
    __slots__ = ('index', 'elements')

    def __init__(self, index, elements):
        self.index = index
        self.elements = elements   # four lists of u32, one per element type


class WorldGrid:
    __slots__ = ('minimum', 'edge_size', 'rows', 'cols', 'nodes', 'pointer_size', 'tags',
                 'problems')

    def __init__(self, minimum, edge_size, rows, cols):
        self.minimum = minimum          # (x, y, z, w)
        self.edge_size = edge_size
        self.rows = rows
        self.cols = cols
        self.nodes = []
        self.pointer_size = 0
        self.tags = {}                  # tag name -> [block count, total bytes, entries of the first]
        self.problems = []

    def cell(self, node_index):
        """(min x, min z, max x, max z) of a node."""
        row, col = divmod(node_index, self.cols)
        x0 = self.minimum[0] + col * self.edge_size
        z0 = self.minimum[2] + row * self.edge_size
        return x0, z0, x0 + self.edge_size, z0 + self.edge_size

    def element_counts(self):
        counts = [0, 0, 0, 0]
        for node in self.nodes:
            for t in range(4):
                counts[t] += len(node.elements[t])
        return counts

    def summary(self):
        counts = self.element_counts()
        return (f"grid {self.rows} row(s) x {self.cols} col(s), edge {self.edge_size:g}, min "
                f"({self.minimum[0]:g}, {self.minimum[2]:g}); {len(self.nodes)} node(s) with "
                f"data; elements: " + ', '.join(f"{n} {c}" for n, c in zip(ELEMENT_NAMES, counts)))


def _parse_nodes(data, start, length, node_limit, pointer_size):
    """Walks the nodes of one CGcn block. Returns (nodes, problems, clean). clean is True
    when the nodes end within 15 bytes of the end of the block and every node index and
    element offset is valid."""
    header = pointer_size + 16
    end = start + length
    pos = start
    nodes = []
    problems = []
    bad_offsets = 0
    while pos + header <= end:
        index = struct.unpack_from('<H', data, pos + pointer_size)[0]
        counts = struct.unpack_from('<4B', data, pos + pointer_size + 4)
        offsets = struct.unpack_from('<4H', data, pos + pointer_size + 8)
        total = header + 4 * sum(counts)
        if index >= node_limit or pos + total > end:
            problems.append(f"node at +{pos - start:#x}: index {index}, counts {counts}, "
                            f"size {total} do not fit")
            return nodes, problems, False
        expected = 0
        elements = []
        for t in range(4):
            if offsets[t] != expected:
                bad_offsets += 1
            expected += 4 * counts[t]
            list_start = pos + header + offsets[t]
            if counts[t] and list_start + 4 * counts[t] > end:
                problems.append(f"node {index}: element list {t} runs past the block")
                return nodes, problems, False
            elements.append(list(struct.unpack_from(f'<{counts[t]}I', data, list_start))
                            if counts[t] else [])
        nodes.append(GridNode(index, elements))
        pos += total
    if bad_offsets:
        problems.append(f"{bad_offsets} element offset(s) are not back to back")
    leftover = end - pos
    clean = leftover < 16 and not bad_offsets
    if leftover >= 16:
        problems.append(f"{leftover} byte(s) left after the last node")
    return nodes, problems, clean


def load_world_grid(path):
    """Reads the grid of a region file. Returns None when the file has no CDat block with
    a CGrd header."""
    data = open(path, 'rb').read()
    offset = find_carp_offset(data)
    if offset is None:
        return None
    root = parse_carp(data, offset)
    cdat = root.get_entry(TAG_CDAT)
    if cdat is None or not cdat.children:
        return None

    grid = None
    node_blocks = []   # (start, length, table entries) of every CGcn block
    for child in cdat.children:
        start = child.offset + child.data_offset
        if child.type == TAG_CGRD and child.data_length >= GRID_HEADER_SIZE and grid is None:
            minimum = struct.unpack_from('<4f', data, start)
            edge_size = struct.unpack_from('<f', data, start + 0x10)[0]
            rows, cols = struct.unpack_from('<II', data, start + 0x18)
            grid = WorldGrid(minimum, edge_size, rows, cols)
        elif child.type == TAG_CGCN:
            node_blocks.append((start, child.data_length, child.num_entries))
    if grid is None:
        return None
    for child in cdat.children:
        entry = grid.tags.setdefault(_int_to_tag(child.type), [0, 0, child.num_entries])
        entry[0] += 1
        entry[1] += child.data_length

    node_limit = grid.rows * grid.cols
    best = None   # (node count, pointer size, nodes, problems) of the longest partial parse
    for pointer_size in POINTER_SIZES:
        nodes, problems, clean = [], [], True
        for start, length, _entries in node_blocks:
            block_nodes, block_problems, block_clean = _parse_nodes(data, start, length,
                                                                    node_limit, pointer_size)
            nodes.extend(block_nodes)
            problems.extend(block_problems)
            clean = clean and block_clean
        if clean:
            grid.pointer_size = pointer_size
            grid.nodes = nodes
            grid.problems = problems
            return grid
        if best is None or len(nodes) > best[0]:
            best = (len(nodes), pointer_size, nodes,
                    [f"pointer size {pointer_size}: {p}" for p in problems])
    if best is not None:
        grid.pointer_size, grid.nodes, grid.problems = best[1], best[2], best[3]
    grid.problems.append("no pointer size gave nodes that end exactly at the end of the block; "
                         "the node list is the longest partial one")
    return grid


def element_section_index(raw):
    """Global instance index to (section number, index in that section's pack)."""
    return raw >> 16, raw & 0xFFFF


def write_grid_nodes_tsv(grid, path):
    rows = []
    for node in sorted(grid.nodes, key=lambda n: n.index):
        row, col = divmod(node.index, grid.cols)
        x0, z0, x1, z1 = grid.cell(node.index)
        rows.append([node.index, row, col, f"{x0:.2f}", f"{z0:.2f}", f"{x1:.2f}", f"{z1:.2f}",
                     *(len(e) for e in node.elements)])
    with open(path, 'w', encoding='utf-8', newline='') as f:
        writer = csv.writer(f, delimiter='\t', lineterminator='\n')
        writer.writerow(['Node', 'Row', 'Col', 'MinX', 'MinZ', 'MaxX', 'MaxZ', 'Instances',
                         'Triggers', 'Objects', 'RoadSegments'])
        writer.writerows(rows)
    return len(rows)


def write_grid_instances_tsv(grid, path):
    """One row per (node, instance element)."""
    count = 0
    with open(path, 'w', encoding='utf-8', newline='') as f:
        writer = csv.writer(f, delimiter='\t', lineterminator='\n')
        writer.writerow(['Node', 'Section', 'Index', 'Raw'])
        for node in sorted(grid.nodes, key=lambda n: n.index):
            for raw in node.elements[0]:
                section, index = element_section_index(raw)
                writer.writerow([node.index, section, index, f"0x{raw:08X}"])
                count += 1
    return count


def check_world_grid(grid, packs=None):
    """Prints what the grid says, and with the collision packs of the stream file, how it
    fits them: which instances the grid lists, and whether the true position of an
    instance lies in the grid cells that list it (this tests the axis order directly)."""
    print(f"[nfs_world_grid] {grid.summary()}")
    print(f"[nfs_world_grid] node pointer size {grid.pointer_size}; blocks under CDat:")
    for key, (count, size, entries) in sorted(grid.tags.items()):
        print(f"  {key:<6} {count:>4} block(s) {size:>10} byte(s), table entries {entries}")
    for problem in grid.problems:
        print(f"[nfs_world_grid] problem: {problem}")

    references = {}   # raw -> list of node indexes
    for node in grid.nodes:
        for raw in node.elements[0]:
            references.setdefault(raw, []).append(node.index)
    print(f"[nfs_world_grid] {len(references)} distinct instance(s) listed, "
          f"{sum(len(v) for v in references.values())} node reference(s)")
    if not packs:
        return

    from nfs_collision_pack import true_position, _axis_orders, _apply_order
    by_section = {pack.section_number: pack for pack in packs}
    found = []        # (raw, instance)
    no_pack = 0
    no_index = 0
    for raw in references:
        section, index = element_section_index(raw)
        pack = by_section.get(section)
        if pack is None:
            no_pack += 1
        elif index >= len(pack.instances):
            no_index += 1
        else:
            found.append((raw, pack.instances[index]))
    listed = {raw for raw, _ in found}
    total_instances = sum(len(pack.instances) for pack in packs)
    unlisted = sum(1 for pack in packs for ci in pack.instances
                   if ((pack.section_number << 16) | ci.index) not in references)
    print(f"[nfs_world_grid] listed instances: {len(found)} found in the packs, {no_pack} name a "
          f"section with no pack, {no_index} name an index past the end of a pack")
    print(f"[nfs_world_grid] instances in the packs that no node lists: {unlisted} of "
          f"{total_instances}")
    group_listed = sum(1 for _raw, ci in found if ci.group_number)
    group_total = sum(1 for pack in packs for ci in pack.instances if ci.group_number)
    print(f"[nfs_world_grid] instances with a group number: {group_total} in the packs, "
          f"{group_listed} listed by the grid")
    if found:
        per_instance = sorted(len(references[raw]) for raw, _ in found)
        print(f"[nfs_world_grid] nodes per listed instance: min {per_instance[0]}, "
              f"median {per_instance[len(per_instance) // 2]}, max {per_instance[-1]}")

    # Axis order: the true position (x, z) must lie in a cell that lists the instance,
    # within the instance radius.
    if not found:
        return
    best = []
    for order, signs in _axis_orders():
        hits = 0
        for raw, ci in found:
            x, _y, z = _apply_order(true_position(ci), order, signs)
            for node_index in references[raw]:
                x0, z0, x1, z1 = grid.cell(node_index)
                r = ci.radius
                if x0 - r <= x <= x1 + r and z0 - r <= z <= z1 + r:
                    hits += 1
                    break
        best.append((hits, order, signs))
    best.sort(key=lambda b: b[0], reverse=True)
    identity = next(h for h, o, s in best if o == (0, 1, 2) and s == (1.0, 1.0, 1.0))
    print(f"[nfs_world_grid] true position inside a listing cell, identity axes: "
          f"{identity / len(found):.0%}; best order {best[0][1]} signs {best[0][2]}: "
          f"{best[0][0] / len(found):.0%}; runner-up {best[1][0] / len(found):.0%}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Write the world grid TSV files from the region file.")
    ap.add_argument('region_bun', help="region file (L5RA.BUN), the one with the CARP world grid")
    ap.add_argument('--stream', help="stream file (STREAML5RA.BUN): also check the grid against "
                                     "its collision packs")
    ap.add_argument('--out', help="output folder (default outputs/nfs_world_grid/)")
    args = ap.parse_args(argv)

    from pathlib import Path
    from nfs_outputs import out_dir

    grid = load_world_grid(args.region_bun)
    if grid is None:
        print("[nfs_world_grid] no CarpWorldGrid with a CDat block in this file")
        return
    out = Path(args.out) if args.out else out_dir(TOOL_NAME)
    out.mkdir(parents=True, exist_ok=True)
    node_count = write_grid_nodes_tsv(grid, out / 'world_grid_nodes.tsv')
    element_count = write_grid_instances_tsv(grid, out / 'world_grid_instances.tsv')
    print(f"[nfs_world_grid] wrote world_grid_nodes.tsv ({node_count} row(s)) and "
          f"world_grid_instances.tsv ({element_count} row(s)) to {out}")

    packs = None
    if args.stream:
        from nfs_collision_pack import load_collision_packs
        packs = load_collision_packs(args.stream)
    check_world_grid(grid, packs)


if __name__ == '__main__':
    main()
