#!/usr/bin/env python3
"""Audit Need for Speed Carbon BUN chunks related to roads and collision.

Scans STREAML5RA.BUN and optionally L5RA.BUN using the repository's generic
EAGL chunk envelope rules, but uses mmap so a ~1 GB stream file is not copied
into RAM. It maps WCollisionPack (0x0003B801) to its inner section number / CHOP,
checks 0x00034027 against the known MW smackable-record layout without assuming
Carbon must match it, and inventories candidate road/elevation/path chunks.

Outputs in --out:
  chunk_occurrences.tsv  relevant chunk IDs, offsets, sizes and containing CHOP
  chunk_census.tsv       counts/bytes by file and chunk ID
  chop_collision_map.tsv per-CHOP collision/smackable presence and totals
  collision_articles.tsv per-section 'ca' article geometry counts
  collision_surfaces.tsv per-article surface index -> stored surface hash
  smackables.tsv         individual records only when the MW layout check passes
  carp_tags.tsv         CARP contents of 0x3B800 / 0x3B801 containers
  report.txt            human-readable summary and parsing caveats

The script does NOT claim that 0x00034027 is a verified Carbon smackable format.
It will only report a MW-layout parse when header/size checks pass.
"""
from __future__ import annotations

import argparse
import csv
import mmap
import os
import struct
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

# On-disk IDs, as little-endian integers.
CHUNK_NAMES = {
    0x80034100: "ScenerySection container (CHOP context)",
    0x00034101: "SceneryHeader",
    0x00034102: "SceneryInfos",
    0x00034103: "SceneryInstances",
    0x00034108: "SceneryOverrideInfos (usually region file)",
    0x00034109: "SceneryGroups (usually region file)",
    0x0003B800: "CARP world road network / world grid",
    0x0003B801: "WCollisionPack (wall/barrier collision)",
    0x00034027: "smackable-spawner candidate (Carbon ID unverified)",
    0x0003414A: "TrackPath zones",
    0x0003414D: "TrackPath barriers",
    0x00034156: "Elevation polygons",
    0x00034159: "ambiguous: MW PC terrain ID; MW decomp names this HeliSheet",
    0x00034191: "TrackObjectBounds / streaming barrier candidate",
    0x00034180: "Trough container candidate",
    0x00034181: "Trough perimeter candidate",
    0x00034182: "Trough quad-tree nodes candidate",
    0x00034183: "Trough quad-tree entries candidate",
    0x00034184: "Wall clusters candidate",
    0x57944655: "TroughBoundary polygon (separate-file format candidate)",
}
WATCH_IDS = set(CHUNK_NAMES)
SCENERY_SECTION_ID = 0x80034100
SCENERY_HEADER_ID = 0x00034101
COLLISION_PACK_ID = 0x0003B801
ROAD_CARP_ID = 0x0003B800
SMACKABLE_ID = 0x00034027
CARP_TAG = 0x43415250
ARTI_TAG = 0x41727469
NAME_TAG = 0x4E616D65
TAG_MASK = 0xFFFF0000
NUMBERED_TAGS = {"ci", "ca", "co", "hk", "si"}


@dataclass
class Chunk:
    file: str
    offset: int
    chunk_id: int
    length: int
    payload: int
    end: int
    depth: int
    parents: tuple[int, ...] = ()
    chop_context: Optional[int] = None


@dataclass
class CarpEntry:
    offset: int
    tag: int
    type_flag: int
    data_length: int
    num_entries: int
    data_offset: int
    data_start: int = 0
    children: list["CarpEntry"] = field(default_factory=list)
    parse_error: str = ""

    @property
    def tag_name(self) -> str:
        return int_tag(self.tag)

    @property
    def tag_kind(self) -> str:
        high = (self.tag >> 16) & 0xFFFF
        chars = chr((high >> 8) & 0xFF) + chr(high & 0xFF)
        return chars if chars in NUMBERED_TAGS else self.tag_name


@dataclass
class PackResult:
    section: Optional[int]
    section_label: str
    offset: int = 0
    flags: int = 0
    inner_size: int = 0
    name: str = ""
    tags: Counter = field(default_factory=Counter)
    tag_bytes: Counter = field(default_factory=Counter)
    instances: int = 0
    objects: int = 0
    articles: int = 0
    strip_triangles: int = 0
    barrier_edges: int = 0
    surface_slots: int = 0
    article_rows: list[list] = field(default_factory=list)
    surface_rows: list[list] = field(default_factory=list)
    carp_rows: list[list] = field(default_factory=list)
    status: str = ""


def int_tag(value: int) -> str:
    return "".join(chr((value >> shift) & 0xFF) if 32 <= ((value >> shift) & 0xFF) < 127 else "."
                   for shift in (24, 16, 8, 0))


def chunk_name(chunk_id: int) -> str:
    return CHUNK_NAMES.get(chunk_id, f"unknown 0x{chunk_id:08X}")


def chop_label(section: Optional[int]) -> str:
    if section is None:
        return ""
    # Carbon's visible-section label formula reused from nfs_region_common.py:
    # letter = section // 100 + 'A' - 1, subsection = section % 100.
    if section >= 100:
        letter_ord = ord('A') + section // 100 - 1
        if ord('A') <= letter_ord <= ord('Z'):
            return f"{chr(letter_ord)}{section % 100}"
    return f"Section{section}"


def safe_u32(data, offset: int, default: Optional[int] = None) -> Optional[int]:
    if offset < 0 or offset + 4 > len(data):
        return default
    return struct.unpack_from("<I", data, offset)[0]


def direct_chunks(data, start: int, end: int) -> Iterator[tuple[int, int, int, int, int]]:
    """Yield direct children as (offset, id, length, payload_start, payload_end)."""
    pos = start
    while pos + 8 <= end:
        chunk_id, length = struct.unpack_from("<II", data, pos)
        payload = pos + 8
        payload_end = payload + length
        if payload_end > end or payload_end < payload:
            break
        yield pos, chunk_id, length, payload, payload_end
        if payload_end <= pos:
            break
        pos = payload_end


def immediate_chop_id(data, payload: int, end: int) -> Optional[int]:
    """Read the section ID in a 0x80034100 container's immediate 0x34101 child."""
    for _off, cid, length, cpay, cend in direct_chunks(data, payload, end):
        if cid == SCENERY_HEADER_ID and length >= 0x10 and cpay + 0x10 <= cend:
            value = struct.unpack_from("<I", data, cpay + 0x0C)[0]
            if value < 10000:
                return value
    return None


def walk_chunks(data, file_label: str, start: int = 0, end: Optional[int] = None,
                depth: int = 0, parents: tuple[int, ...] = (),
                chop_context: Optional[int] = None,
                max_depth: int = 48) -> Iterator[Chunk]:
    """Recursively scan Black Box's 8-byte ID/length chunks, using mmap slices only."""
    if end is None:
        end = len(data)
    if depth > max_depth:
        return
    pos = start
    while pos + 8 <= end:
        chunk_id, length = struct.unpack_from("<II", data, pos)
        payload = pos + 8
        chunk_end = payload + length
        if length > end or chunk_end > end or chunk_end < payload:
            # The shared repo walker stops at the first malformed envelope too.
            break
        own_context = chop_context
        if chunk_id == SCENERY_SECTION_ID:
            found = immediate_chop_id(data, payload, chunk_end)
            if found is not None:
                own_context = found
        yield Chunk(file_label, pos, chunk_id, length, payload, chunk_end, depth,
                    parents, own_context)
        if chunk_id & 0x80000000:
            yield from walk_chunks(data, file_label, payload, chunk_end, depth + 1,
                                   parents + (chunk_id,), own_context, max_depth)
        pos = chunk_end


def skip_1111_words(data, pos: int, end: int) -> int:
    while pos + 4 <= end and struct.unpack_from("<I", data, pos)[0] == 0x11111111:
        pos += 4
    return pos


def read_pack_root(data, chunk: Chunk) -> tuple[int, int, int, int, int]:
    """Return (header_pos, section, flags, last_address, root_entry_offset)."""
    pos = chunk.payload + 4  # inner block length before the aligned CARP header
    pos = skip_1111_words(data, pos, chunk.end)
    if pos + 16 > chunk.end:
        raise ValueError("pack header truncated after inner length/alignment")
    inner_size, section, flags, last_address = struct.unpack_from("<IIII", data, pos)
    root_offset = pos + 16
    if root_offset + 16 > chunk.end:
        raise ValueError("CARP root header falls outside chunk")
    return pos, section, flags, last_address, root_offset


def parse_carp_entry(data, offset: int, region_end: int, depth: int = 0,
                     max_depth: int = 20, node_budget: Optional[list[int]] = None) -> CarpEntry:
    if node_budget is None:
        node_budget = [100000]
    node_budget[0] -= 1
    if node_budget[0] < 0:
        raise ValueError("CARP parse node budget exceeded")
    if depth > max_depth or offset < 0 or offset + 16 > region_end:
        raise ValueError(f"invalid CARP entry header offset 0x{offset:X}")
    tag, packed, num_entries, data_offset = struct.unpack_from("<IIii", data, offset)
    type_flag = packed & 0xFF
    data_length = (packed >> 8) & 0xFFFFFF
    entry = CarpEntry(offset, tag, type_flag, data_length, num_entries, data_offset)
    if data_length:
        data_start = offset + data_offset
        if data_start < offset or data_start + data_length > region_end:
            entry.parse_error = "leaf range outside enclosing chunk"
            return entry
        entry.data_start = data_start
        return entry

    n = num_entries
    if tag == CARP_TAG:
        if type_flag == 42:
            n = 1
        elif type_flag == 122:
            n = 3
    if n < 0 or n > 10000:
        entry.parse_error = f"implausible child count {n}"
        return entry
    if data_offset < 0:
        entry.parse_error = f"negative child-table offset {data_offset}"
        return entry
    for i in range(n):
        child_offset = offset + 16 * (data_offset + i)
        try:
            child = parse_carp_entry(data, child_offset, region_end, depth + 1,
                                     max_depth, node_budget)
        except (ValueError, struct.error) as exc:
            entry.parse_error = str(exc)
            break
        entry.children.append(child)
    return entry


def all_carp_entries(entry: CarpEntry) -> Iterator[CarpEntry]:
    yield entry
    for child in entry.children:
        yield from all_carp_entries(child)


def strip_geometry_counts(data, article: CarpEntry, section: int, article_num: int,
                          rows: list[list], surface_rows: list[list]) -> tuple[int, int, int, int]:
    """Return (triangles, edges, surface slots, vertices) for one `ca` leaf."""
    start, length = article.data_start, article.data_length
    if length < 16:
        rows.append([section, article_num, length, "", "", "", "", "", "", "truncated < 16-byte header"])
        return 0, 0, 0, 0
    strips, strips_size, edges, edges_size, resolved, surface_count, surfaces_size, intermediate, flags = \
        struct.unpack_from("<HHHHBBHHh", data, start)
    needed = 16 + strips_size + edges_size + surface_count * 4
    if needed > length:
        rows.append([section, article_num, length, strips, edges, surface_count, "", "", flags,
                     f"payload lengths require {needed} bytes"])
        return 0, edges, surface_count, 0
    triangle_count = 0
    vertex_count_total = 0
    strip_errors = []
    for i in range(strips):
        sphere_pos = start + 16 + i * 16
        if sphere_pos + 16 > start + 16 + strips_size:
            strip_errors.append(f"sphere {i} outside strips area")
            break
        sx, sy, sz, radius, rel_offset = struct.unpack_from("<fffHH", data, sphere_pos)
        strip_pos = start + 16 + rel_offset
        if strip_pos + 16 > start + length:
            strip_errors.append(f"strip {i} start outside article")
            break
        vertex_count = struct.unpack_from("<H", data, strip_pos + 6)[0]
        if vertex_count < 3 or strip_pos + vertex_count * 8 > start + length:
            strip_errors.append(f"strip {i} has invalid vertex count {vertex_count}")
            continue
        vertex_count_total += vertex_count
        triangle_count += vertex_count - 2
    surface_start = start + 16 + strips_size + edges_size
    for si in range(surface_count):
        if surface_start + (si + 1) * 4 > start + length:
            break
        h = struct.unpack_from("<I", data, surface_start + si * 4)[0]
        surface_rows.append([section, article_num, si, f"0x{h:08X}", "unresolved hash; needs surface dictionary"])
    error = "; ".join(strip_errors)
    rows.append([section, article_num, length, strips, edges, surface_count,
                 triangle_count, vertex_count_total, f"0x{flags & 0xFFFF:04X}", error])
    return triangle_count, edges, surface_count, vertex_count_total


def parse_collision_pack(data, chunk: Chunk) -> PackResult:
    result = PackResult(None, "", offset=chunk.offset, status="unparsed")
    try:
        header_pos, section, flags, last_address, root_offset = read_pack_root(data, chunk)
        result.section = section
        result.section_label = chop_label(section)
        result.flags = flags
        result.inner_size = struct.unpack_from("<I", data, header_pos)[0]
        root = parse_carp_entry(data, root_offset, chunk.end)
        if root.tag != CARP_TAG:
            raise ValueError(f"root tag is {root.tag_name!r}, not CARP")
        arti = next((c for c in root.children if c.tag == ARTI_TAG), None)
        if arti is None:
            raise ValueError("CARP root has no Arti child")
        for child in arti.children:
            kind = child.tag_kind
            if kind == "Name" and child.data_length:
                raw = bytes(data[child.data_start:child.data_start + child.data_length]).split(b"\0", 1)[0]
                result.name = raw.decode("ascii", errors="replace")
            result.tags[kind] += 1
            result.tag_bytes[kind] += child.data_length
            result.carp_rows.append([chunk.file, f"0x{chunk.offset:X}", result.section,
                                     f"0x{child.tag:08X}", kind, child.data_length,
                                     child.num_entries, f"0x{child.offset:X}", child.parse_error])
            if not child.data_length:
                continue
            start = child.data_start
            if kind == "ci":
                stride = 0x40
                if child.num_entries > 0 and child.data_length % child.num_entries == 0:
                    candidate = child.data_length // child.num_entries
                    if candidate >= 0x40:
                        stride = candidate
                result.instances += child.data_length // stride
            elif kind == "co":
                stride = 0x70
                if child.data_length % stride != 0 and child.num_entries > 0 and child.data_length % child.num_entries == 0:
                    candidate = child.data_length // child.num_entries
                    if candidate >= 0x50:
                        stride = candidate
                result.objects += child.data_length // stride
            elif kind == "ca":
                article_num = child.tag & 0xFFFF
                result.articles += 1
                tri, edge, slots, _verts = strip_geometry_counts(
                    data, child, section, article_num, result.article_rows, result.surface_rows)
                result.strip_triangles += tri
                result.barrier_edges += edge
                result.surface_slots += slots
        result.status = "parsed"
    except (ValueError, struct.error, IndexError) as exc:
        result.status = f"ERROR: {exc}"
    return result


def skip_11_bytes(data, pos: int, end: int) -> int:
    while pos < end and data[pos] == 0x11:
        pos += 1
    return pos


def parse_smackable_candidate(data, chunk: Chunk) -> dict:
    """Try the MWSDK 0x34027 16-byte header + 64-byte record layout.

    Carbon is deliberately marked unverified. Only a matching version pair and
    a plausible record count create a 'MW-layout candidate' result.
    """
    end = chunk.end
    starts = []
    starts.append(chunk.payload)
    starts.append(skip_11_bytes(data, chunk.payload, end))
    if chunk.payload + 4 <= end:
        starts.append(chunk.payload + 4)
        starts.append(skip_11_bytes(data, chunk.payload + 4, end))
    # Remove duplicates while preserving order.
    starts = list(dict.fromkeys(starts))
    best = None
    for start in starts:
        if start + 16 > end:
            continue
        v0, v1 = struct.unpack_from("<II", data, start)
        section = struct.unpack_from("<h", data, start + 8)[0]
        first_local = struct.unpack_from("<h", data, start + 10)[0]
        rec_count = struct.unpack_from("<h", data, start + 12)[0]
        avail = (end - (start + 16)) // 64
        plausible_section = -1 <= section <= 4095
        plausible_count = 0 <= rec_count <= avail
        exactish = plausible_count and ((end - (start + 16) - rec_count * 64) < 32)
        version_match = (v0 == 0x0B and v1 == 0x0B)
        score = (version_match * 8) + (plausible_section * 2) + (exactish * 4) + (plausible_count * 2)
        cand = dict(start=start, version_a=v0, version_b=v1, section=section,
                    first_local=first_local, record_count=rec_count, available=avail,
                    exactish=exactish, version_match=version_match, score=score)
        if best is None or cand["score"] > best["score"]:
            best = cand
    if not best:
        return {"section": None, "records": 0, "status": "not parseable: header truncated"}
    decoded_records = []
    if (best["version_match"] and best["exactish"] and
            best["record_count"] <= best["available"] and best["section"] >= 0):
        status = "MW-layout candidate (not yet verified for Carbon)"
        records = best["record_count"]
        section = best["section"]
        for i in range(records):
            rp = best["start"] + 16 + i * 64
            direction = struct.unpack_from("<fff", data, rp + 0x04)
            position = struct.unpack_from("<fff", data, rp + 0x10)
            asset_hash = struct.unpack_from("<I", data, rp + 0x20)[0]
            asset_hash_repeat = struct.unpack_from("<I", data, rp + 0x24)[0]
            param_hash = struct.unpack_from("<I", data, rp + 0x28)[0]
            global_id = struct.unpack_from("<H", data, rp + 0x2C)[0]
            local_index = struct.unpack_from("<I", data, rp + 0x30)[0]
            scale = struct.unpack_from("<f", data, rp + 0x38)[0]
            decoded_records.append({"record_index": i, "direction": direction, "position": position,
                                    "asset_hash": asset_hash, "asset_hash_repeat": asset_hash_repeat,
                                    "param_hash": param_hash, "global_id": global_id,
                                    "local_index": local_index, "scale": scale})
    else:
        status = (f"unverified layout; best header v=0x{best['version_a']:X}/0x{best['version_b']:X}, "
                  f"count={best['record_count']}, available64={best['available']}")
        records = 0
        section = None
    return {**best, "section": section, "records": records, "record_data": decoded_records, "status": status}


def parse_generic_carp_chunk(data, chunk: Chunk) -> list[list]:
    """List CARP offset-table children for road-network and collision chunks.

    WCollisionPack has an additional 16-byte bChunkCarpHeader before the CARP
    root; the road-network chunk does not. Keep both layouts distinct.
    Output columns: File, ContainerOffset, SectionID, TagValue, TagName,
    TagKind, PayloadBytes, TableEntries, EntryOffset, ParseError.
    """
    rows: list[list] = []
    try:
        if chunk.chunk_id == COLLISION_PACK_ID:
            _header_pos, section, _flags, _last, root_offset = read_pack_root(data, chunk)
        else:
            section = None
            root_offset = chunk.payload + 4
            root_offset = skip_1111_words(data, root_offset, chunk.end)
        if root_offset + 16 > chunk.end:
            return rows
        root = parse_carp_entry(data, root_offset, chunk.end)
        for entry in all_carp_entries(root):
            if entry is root:
                continue
            rows.append([chunk.file, f"0x{chunk.offset:X}", section if section is not None else "",
                         f"0x{entry.tag:08X}", entry.tag_name, entry.tag_kind,
                         entry.data_length, entry.num_entries,
                         f"0x{entry.offset:X}", entry.parse_error])
    except (ValueError, struct.error, IndexError):
        pass
    return rows


def write_tsv(path: Path, header: list[str], rows: list[list]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, delimiter="\t", lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def scan_file(path: Path, label: str) -> dict:
    if not path.is_file():
        raise FileNotFoundError(path)
    occurrences: list[Chunk] = []
    census: Counter = Counter()
    census_bytes: Counter = Counter()
    chop_containers: set[int] = set()
    packs: list[PackResult] = []
    smack_rows: list[dict] = []
    carp_rows: list[list] = []
    file_size = path.stat().st_size
    if file_size == 0:
        raise ValueError(f"Empty input file: {path}")
    with path.open("rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        for chunk in walk_chunks(mm, label):
            if chunk.chunk_id == SCENERY_SECTION_ID and chunk.chop_context is not None:
                chop_containers.add(chunk.chop_context)
            if chunk.chunk_id in WATCH_IDS:
                occurrences.append(chunk)
                census[chunk.chunk_id] += 1
                census_bytes[chunk.chunk_id] += chunk.length
            if chunk.chunk_id == COLLISION_PACK_ID:
                packs.append(parse_collision_pack(mm, chunk))
            elif chunk.chunk_id == SMACKABLE_ID:
                parsed = parse_smackable_candidate(mm, chunk)
                parsed["file"] = label
                parsed["offset"] = chunk.offset
                parsed["payload_bytes"] = chunk.length
                parsed["parent_chop"] = chunk.chop_context
                parsed["parent_label"] = chop_label(chunk.chop_context)
                parsed["parent_ids"] = "/".join(f"0x{x:08X}" for x in chunk.parents)
                smack_rows.append(parsed)
            if chunk.chunk_id in (COLLISION_PACK_ID, ROAD_CARP_ID):
                carp_rows.extend(parse_generic_carp_chunk(mm, chunk))
    return dict(path=path, label=label, size=file_size, occurrences=occurrences,
                census=census, census_bytes=census_bytes, chop_containers=chop_containers,
                packs=packs, smack_rows=smack_rows, carp_rows=carp_rows)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("stream_bun", type=Path, help="STREAML5RA.BUN")
    ap.add_argument("--region", type=Path, help="optional L5RA.BUN; scans region/road-path chunks too")
    ap.add_argument("--out", type=Path, default=Path("outputs/carbon_collision_chop_audit"),
                    help="output directory (default: outputs/carbon_collision_chop_audit)")
    args = ap.parse_args(argv)
    inputs = [(args.stream_bun, "stream")]
    if args.region:
        inputs.append((args.region, "region"))
    scans = [scan_file(path, label) for path, label in inputs]
    args.out.mkdir(parents=True, exist_ok=True)

    # Chunk occurrence map: this allows a raw offset to be compared with the actual file.
    occurrence_rows = []
    census_rows = []
    for scan in scans:
        for chunk in scan["occurrences"]:
            occurrence_rows.append([
                scan["label"], f"0x{chunk.offset:X}", f"0x{chunk.chunk_id:08X}",
                chunk_name(chunk.chunk_id), chunk.length, chunk.depth,
                chop_label(chunk.chop_context), chunk.chop_context if chunk.chop_context is not None else "",
                "/".join(f"0x{x:08X}" for x in chunk.parents),
            ])
        for cid, count in scan["census"].items():
            census_rows.append([scan["label"], f"0x{cid:08X}", chunk_name(cid), count,
                                scan["census_bytes"][cid]])
    write_tsv(args.out / "chunk_occurrences.tsv",
              ["File", "Offset", "ChunkID", "Name", "PayloadBytes", "Depth", "ParentCHOP",
               "ParentSectionID", "ParentChunkIDs"], occurrence_rows)
    write_tsv(args.out / "chunk_census.tsv",
              ["File", "ChunkID", "Name", "Occurrences", "PayloadBytesTotal"],
              sorted(census_rows, key=lambda r: (r[0], r[1])))

    article_rows: list[list] = []
    surface_rows: list[list] = []
    carp_rows: list[list] = []
    smackable_record_rows: list[list] = []
    section_map: dict[tuple[str, int], dict] = {}
    for scan in scans:
        for section in scan["chop_containers"]:
            section_map.setdefault((scan["label"], section), {})["section_container"] = True
        for pack in scan["packs"]:
            article_rows.extend(pack.article_rows)
            surface_rows.extend(pack.surface_rows)
            if pack.section is None:
                continue
            row = section_map.setdefault((scan["label"], pack.section), {})
            row["collision_pack_count"] = row.get("collision_pack_count", 0) + 1
            row["collision_instances"] = row.get("collision_instances", 0) + pack.instances
            row["collision_articles"] = row.get("collision_articles", 0) + pack.articles
            row["strip_triangles"] = row.get("strip_triangles", 0) + pack.strip_triangles
            row["barrier_edges"] = row.get("barrier_edges", 0) + pack.barrier_edges
            row["collision_objects"] = row.get("collision_objects", 0) + pack.objects
            row["surface_slots"] = row.get("surface_slots", 0) + pack.surface_slots
            row.setdefault("collision_status", []).append(pack.status)
        for sm in scan["smack_rows"]:
            for rec in sm.get("record_data", []):
                smackable_record_rows.append([
                    scan["label"], f"0x{sm['offset']:X}", sm.get("section") if sm.get("section") is not None else "",
                    chop_label(sm.get("section")), rec["record_index"], rec["local_index"], rec["global_id"],
                    f"0x{rec['asset_hash']:08X}", f"0x{rec['asset_hash_repeat']:08X}",
                    f"0x{rec['param_hash']:08X}", *(f"{v:.6f}" for v in rec["position"]),
                    *(f"{v:.6f}" for v in rec["direction"]), f"{rec['scale']:.6f}",
                    "MW-layout candidate only; confirm Carbon semantics",
                ])
            section = sm.get("section")
            if section is None:
                section = sm.get("parent_chop")
            if section is None:
                continue
            row = section_map.setdefault((scan["label"], section), {})
            row["smackable_chunks"] = row.get("smackable_chunks", 0) + 1
            row["smackable_records"] = row.get("smackable_records", 0) + sm.get("records", 0)
            row.setdefault("smackable_status", []).append(sm.get("status", ""))
        # Add region road/path/elevation presence only when there is a per-CHOP context.
        for chunk in scan["occurrences"]:
            if chunk.chop_context is None:
                continue
            row = section_map.setdefault((scan["label"], chunk.chop_context), {})
            key = {
                ROAD_CARP_ID: "road_carp_chunks",
                0x0003414A: "trackpath_zone_chunks",
                0x0003414D: "trackpath_barrier_chunks",
                0x00034156: "elevation_chunks",
                0x00034159: "chunk_34159_count",
                0x00034191: "chunk_34191_count",
                SMACKABLE_ID: "smackable_raw_chunks",
                COLLISION_PACK_ID: "collision_pack_raw_chunks",
            }.get(chunk.chunk_id)
            if key:
                row[key] = row.get(key, 0) + 1

    for scan in scans:
        carp_rows.extend(scan["carp_rows"])
    write_tsv(args.out / "collision_articles.tsv",
              ["SectionID", "CHOP", "Article", "Bytes", "StripCount", "EdgeCount", "SurfaceCount",
               "Triangles", "Vertices", "Flags", "ParseNote"],
              [[r[0], chop_label(r[0]), *r[1:]] for r in sorted(article_rows, key=lambda r: (r[0], r[1]) )])
    write_tsv(args.out / "collision_surfaces.tsv",
              ["SectionID", "CHOP", "Article", "SurfaceIndex", "SurfaceHash", "Resolution"],
              [[r[0], chop_label(r[0]), *r[1:]] for r in sorted(surface_rows, key=lambda r: (r[0], r[1], r[2]) )])
    write_tsv(args.out / "carp_tags.tsv",
              ["File", "ContainerOffset", "SectionID", "TagValue", "TagName", "TagKind",
               "PayloadBytes", "TableEntries", "EntryOffset", "ParseError"], carp_rows)
    write_tsv(args.out / "smackables.tsv",
              ["File", "ChunkOffset", "SectionID", "CHOP", "RecordIndex", "LocalIndex", "GlobalID",
               "AssetHash", "AssetHashRepeat", "ParamHash", "PosX", "PosY", "PosZ",
               "DirX", "DirY", "DirZ", "Scale", "Interpretation"], smackable_record_rows)

    # Stable, broad section/chop inventory.
    all_keys = sorted(section_map, key=lambda k: (k[0], k[1]))
    map_rows = []
    for file_label, section in all_keys:
        row = section_map[(file_label, section)]
        map_rows.append([
            file_label, section, chop_label(section), int(bool(row.get("section_container"))),
            row.get("collision_pack_count", 0), row.get("collision_instances", 0),
            row.get("collision_articles", 0), row.get("strip_triangles", 0),
            row.get("barrier_edges", 0), row.get("collision_objects", 0), row.get("surface_slots", 0),
            row.get("smackable_chunks", 0), row.get("smackable_records", 0),
            row.get("smackable_raw_chunks", 0), row.get("collision_pack_raw_chunks", 0),
            row.get("road_carp_chunks", 0), row.get("trackpath_zone_chunks", 0),
            row.get("trackpath_barrier_chunks", 0), row.get("elevation_chunks", 0),
            row.get("chunk_34159_count", 0), row.get("chunk_34191_count", 0),
            "; ".join(sorted(set(row.get("collision_status", [])))),
            "; ".join(sorted(set(row.get("smackable_status", [])))),
        ])
    write_tsv(args.out / "chop_collision_map.tsv",
              ["File", "SectionID", "CHOP", "HasScenerySectionContainer", "CollisionPacks",
               "CollisionInstances", "CollisionArticles", "StripTriangles", "BarrierEdges", "CollisionObjects",
               "SurfaceHashSlots", "SmackableChunks", "SmackableRecords_MWLayoutCandidateOnly",
               "Raw34027Chunks", "Raw3B801Chunks", "RoadCARPChunks", "TrackPathZoneChunks",
               "TrackPathBarrierChunks", "ElevationChunks", "Chunk34159Count_Ambiguous",
               "Chunk34191Count", "CollisionParseStatus", "SmackableParseStatus"], map_rows)

    # Human-readable report including raw offsets for mismatches.
    report = []
    report.append("Carbon collision/CHOP audit")
    report.append("===========================")
    report.append("Files:")
    for scan in scans:
        report.append(f"  {scan['label']}: {scan['path']} ({scan['size']:,} bytes)")
        report.append(f"    chunks walked: {sum(scan['census'].values()):,} watched chunk(s); "
                      f"{len(scan['packs'])} WCollisionPack candidate(s); "
                      f"{len(scan['smack_rows'])} 0x34027 candidate(s)")
        for cid, count in sorted(scan["census"].items()):
            report.append(f"    {cid:08X} {chunk_name(cid)}: {count} occurrence(s), "
                          f"{scan['census_bytes'][cid]:,} payload bytes")
    report.append("")
    report.append("WCollisionPack parse results:")
    for scan in scans:
        for p in scan["packs"]:
            report.append(f"  {scan['label']} @0x{p.offset:X}: "
                          f"section={p.section} ({p.section_label}), tags={dict(p.tags)}, "
                          f"ci={p.instances}, ca={p.articles}, stripTris={p.strip_triangles}, "
                          f"edges={p.barrier_edges}, co={p.objects}, surfaceSlots={p.surface_slots}, status={p.status}")
    report.append("")
    report.append("0x00034027 candidate parses:")
    for scan in scans:
        for sm in scan["smack_rows"]:
            report.append(f"  {scan['label']} @0x{sm['offset']:X}: bytes={sm['payload_bytes']}, "
                          f"section={sm.get('section')} ({chop_label(sm.get('section'))}), "
                          f"records={sm.get('records', 0)}, parentCHOP={sm.get('parent_label', '')}, "
                          f"status={sm.get('status', '')}")
    report.append("")
    report.append("Interpretation rules:")
    report.append("  * WCollisionPack's inner section_number is authoritative for its CHOP mapping.")
    report.append("  * 0x34027 is treated as a candidate only; only a matching MW 0x0B/0x0B header is counted as a layout candidate.")
    report.append("  * 0x00034159 is ambiguous across game/builds: MW PC RE documents terrain in one build, while the MW source's SpeedChunks.hpp names it HeliSheet. Do not label Carbon ground collision from the ID alone.")
    report.append("  * Surface hashes in collision_surfaces.tsv are raw hashes; resolving them to physical names/grip requires the Carbon attribute/surface dictionary.")
    report.append("")
    report.append("Output files:")
    for name in ("chunk_occurrences.tsv", "chunk_census.tsv", "chop_collision_map.tsv", "collision_articles.tsv", "collision_surfaces.tsv", "smackables.tsv", "carp_tags.tsv"):
        report.append(f"  {name}")
    (args.out / "report.txt").write_text("\n".join(report) + "\n", encoding="utf-8")

    print("[collision_chop_audit] completed")
    print(f"[collision_chop_audit] output: {args.out.resolve()}")
    for scan in scans:
        print(f"[{scan['label']}] {scan['path'].name}: {len(scan['packs'])} WCollisionPack(s), "
              f"{len(scan['smack_rows'])} 0x34027 candidate(s)")
    print("[collision_chop_audit] inspect chop_collision_map.tsv and report.txt first")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
