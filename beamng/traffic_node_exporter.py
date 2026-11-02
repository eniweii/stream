"""Export derived CARP corridors as BeamNG items.level.json line-delimited objects."""
from __future__ import annotations
import json
import re
from pathlib import Path


def sanitize_name(value):
    return re.sub(r"[^A-Za-z0-9_]+", "_", str(value)).strip("_") or "traffic_road"


def corridor_to_decalroad(corridor, *, material="road_invisible", texture_length=8.0,
                          render_priority=0, over_objects=None, vertical_offset=150.0):
    """Convert one already-derived corridor to a documented DecalRoad object.

    `corridor` is a complete continuous zone-derived path, already merged from
    connected source segments where topology permits. Its `nodes` array contains
    every sampled source point, with a width per node. Height offset is applied
    at serialization time so it is guaranteed to reach the final game file.
    """
    if not corridor.get("export", False):
        return None
    name = sanitize_name("carbon_ai_" + corridor["id"])
    # Corridor geometry stores BeamNG X/Y and the original NFS vertical height
    # without the project placement offset. Apply the +150 Z adjustment here,
    # exactly once, so the serialized items.level.json cannot silently omit it.
    offset = float(vertical_offset)
    if not (offset == offset and abs(offset) != float("inf")):
        raise ValueError("vertical_offset must be a finite number")
    export_nodes = []
    for node in corridor["nodes"]:
        if len(node) < 4:
            raise ValueError(f"Corridor {corridor.get('id', name)} has a malformed node: {node!r}")
        export_nodes.append([float(node[0]), float(node[1]), float(node[2]) + offset, float(node[3])])
    obj = {
        "name": name,
        "class": "DecalRoad",
        "material": material,
        "textureLength": float(texture_length),
        "renderPriority": int(render_priority),
        "drivability": float(corridor.get("drivability", 1.0)),
        "autoLanes": bool(corridor.get("auto_lanes", False)),
        "lanesLeft": int(corridor.get("lanes_left", 0)),
        "lanesRight": int(corridor.get("lanes_right", 1)),
        "oneWay": bool(corridor.get("one_way", True)),
        "flipDirection": False,
        "autoJunction": bool(corridor.get("auto_junction", True)),
        "useSubdivisions": True,
        "overObjects": bool(corridor.get("over_objects", False) if over_objects is None else over_objects),
        "improvedSpline": True,
        "smoothness": 0.5,
        "nodes": export_nodes,
        # Custom traceability keys are intentionally omitted; source metadata is
        # written to the sidecar so the game only receives supported fields.
    }
    return obj


def write_items_level(corridors, path, *, material="road_invisible", texture_length=8.0,
                      render_priority=0, over_objects=None, vertical_offset=150.0):
    """Write one JSON object per line (BeamNG items.level.json format)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for corridor in corridors:
            obj = corridor_to_decalroad(corridor, material=material,
                                        texture_length=texture_length,
                                        render_priority=render_priority,
                                        over_objects=over_objects,
                                        vertical_offset=vertical_offset)
            if obj is None:
                continue
            f.write(json.dumps(obj, separators=(",", ":"), allow_nan=False) + "\n")
            count += 1
    return count


def write_source_manifest(corridors, path, *, vertical_offset=150.0):
    """Write sidecar containing source IDs, per-zone lineage, and export offset."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for c in corridors:
        rows.append({k: c.get(k) for k in (
            "id", "source_key", "source_keys", "merged_corridor_count",
            "source_segment_indices", "source_segment_ids", "source_road_ids", "source_zone_indices", "source_zone_raw_endpoints",
            "road_id", "segment_index", "source_segment_id", "node_start", "node_end",
            "profile_start", "profile_end", "zone_index", "zone_raw_start", "zone_raw_end", "source_type", "source_type_end",
            "classification", "side", "drivability", "direction", "one_way", "auto_junction",
            "auto_lanes", "lanes_left", "lanes_right", "source_decision", "source_no_traffic",
            "force_export", "export", "offset_start", "offset_end", "width_start", "width_end",
            "over_objects", "segment_flags", "zone_endpoint_harmonized", "max_source_endpoint_gap")})
    payload = {
        "schema_version": 2,
        "geometry_source": "CARP profile zones as rendered in Zones mode; connected through-road pieces are merged by source graph topology",
        "coordinate_mapping": "NFS (x,y,z) -> BeamNG (z,-x,y+vertical_offset)",
        "vertical_offset": float(vertical_offset),
        "vertical_offset_applied_during_serialization": True,
        "corridors": rows,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return len(rows)
