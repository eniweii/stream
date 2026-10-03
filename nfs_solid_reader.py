"""
Reads Carbon's solid/mesh geometry directly from the stream file -
ObjectPackChunk (0x80134000): per-solid header, texture references,
materials/shading-groups, and actual vertex/index geometry.

Ported from Common/Geometry/CarbonSolidReader.cs and its base
SolidReader<T,M> class in this project's AssetDumper (C#) fork - chunk IDs,
struct offsets, and the full effect-id vertex layout table are a direct
translation of that already-verified code, not independently re-derived
here. Carbon/World09 only for now (same byte-identical-struct finding
already established for materials/scenery in this project).

Structurally: 0x80134000 contains a header section (0x80134001, filename/
group name/streaming table - not needed for geometry, skipped) followed by
one 0x80134010 container per solid object, each itself containing a header,
texture list, and a nested 0x80134100 (Plat) container with the actual
materials/vertex-buffer/index data. walk_chunks() recurses into all of this
automatically; this module just needs to track which solid object is
"current" as 0x80134010 markers go by; the C# reader tracks this the same
way via an explicit state stack; here it falls out for free by watching the
chunk IDs, since none of the ID values collide between object-level and
Plat-level content.

NOT yet run against a real file.
"""
import struct

from nfs_region_common import walk_chunks

OBJECT_PACK_CHUNK = 0x80134000
SOLID_OBJECT_CONTAINER = 0x80134010
PLAT_CONTAINER = 0x80134100

CHUNK_SOLID_HEADER = 0x134011
CHUNK_SOLID_TEXTURES = 0x134012
CHUNK_SOLID_MORPH_TARGETS = 0x13401D

CHUNK_PLAT_MESH_ENTRIES = 0x134B02
CHUNK_PLAT_VERTEX_BUFFER = 0x134B01
CHUNK_PLAT_INDICES = 0x134B03
CHUNK_PLAT_MESH_ENTRY_NAME = 0x134C02

# Real InternalEffectId enum values (CarbonSolidReader.cs). Only the ones
# GetVertex actually decodes are given a vertex-layout entry below; anything
# else raises rather than guessing at an unverified layout - matches the C#
# reader's own behavior exactly (it throws on an unhandled effect id too).
EFFECT_WORLD_SHADER = 0
EFFECT_WORLD_REFLECT_SHADER = 1
EFFECT_WORLD_BONE_SHADER = 2
EFFECT_WORLD_NORMAL_MAP = 3
EFFECT_CAR_SHADER = 4
EFFECT_CAR_NORMAL_MAP = 5
EFFECT_SKYSHADER = 15
EFFECT_UCAP = 24
EFFECT_GLASS_REFLECT = 25
EFFECT_WATER = 26


class SolidMaterial:
    def __init__(self):
        self.flags = 0
        self.min_point = (0.0, 0.0, 0.0)
        self.max_point = (0.0, 0.0, 0.0)
        self.num_verts = 0
        self.num_indices = 0
        self.diffuse_hash = None
        self.normal_hash = None
        self.specular_hash = None
        self.height_hash = None
        self.opacity_hash = None
        self.effect_id = 0
        self.sort_key = 0
        self.vertex_set_index = 0
        self.indices = []
        self.name = None


class Vertex:
    __slots__ = ('position', 'normal', 'color', 'texcoords', 'tangent', 'blend_weight', 'blend_indices')

    def __init__(self):
        self.position = None
        self.normal = None
        self.color = None
        self.texcoords = None
        self.tangent = None
        self.blend_weight = None
        self.blend_indices = None


class SolidObject:
    def __init__(self):
        self.name = None
        self.hash = None
        self.min_point = (0.0, 0.0, 0.0)
        self.max_point = (0.0, 0.0, 0.0)
        self.texture_hashes = []
        self.materials = []
        self.vertex_sets = []     # list (per vertex-set/stream) of list of Vertex
        self.morph_targets = []   # (name_hash, blend_amount)


def _align16(pos):
    return pos if pos % 0x10 == 0 else pos + (0x10 - pos % 0x10)


def _read_solid_header(data, pos):
    # pos must already be 0x10-aligned by the caller
    version = data[pos + 0x0C]
    if version != 0x16:
        raise ValueError(f"Solid header version 0x{version:X} (expected 0x16) - struct is misaligned here")
    hash_ = struct.unpack_from('<I', data, pos + 0x10)[0]
    bounds_min = struct.unpack_from('<fff', data, pos + 0x20)
    bounds_max = struct.unpack_from('<fff', data, pos + 0x30)
    name_start = pos + 0xA0
    name_end = data.index(b'\x00', name_start)
    name = data[name_start:name_end].decode('ascii', errors='replace')
    return name, hash_, bounds_min, bounds_max


def _read_shading_group(data, pos):
    """SolidObjectShadingGroup, 144 bytes - see CarbonSolidReader.cs, already
    fully hex-verified earlier in this project's Carbon materials work."""
    m = SolidMaterial()
    m.min_point = struct.unpack_from('<fff', data, pos + 0x00)
    m.max_point = struct.unpack_from('<fff', data, pos + 0x0C)
    diffuse_id, normal_id, height_id, specular_id, opacity_id, _light_mat_num = \
        struct.unpack_from('<BBBBBB', data, pos + 0x18)
    m.effect_id = struct.unpack_from('<H', data, pos + 0x30)[0]
    m.flags = struct.unpack_from('<I', data, pos + 0x38)[0]
    m.sort_key = struct.unpack_from('<I', data, pos + 0x3C)[0]
    m.num_verts = struct.unpack_from('<I', data, pos + 0x40)[0]
    num_tris = struct.unpack_from('<I', data, pos + 0x60)[0]
    m.num_indices = num_tris * 3
    return m, diffuse_id, normal_id, height_id, specular_id, opacity_id


def _read_vertex(data, pos, effect_id):
    """Direct translation of CarbonSolidReader.GetVertex's switch - field
    order and sizes per effect id, including the two unexplained padding
    skips (skyshader's extra 8 bytes, UCAP's trailing 8x vec4) that the C#
    reader also just skips without a known meaning."""
    v = Vertex()
    p = pos

    def vec3():
        nonlocal p
        val = struct.unpack_from('<fff', data, p)
        p += 12
        return val

    def vec2():
        nonlocal p
        val = struct.unpack_from('<ff', data, p)
        p += 8
        return val

    def u32():
        nonlocal p
        val = struct.unpack_from('<I', data, p)[0]
        p += 4
        return val

    if effect_id == EFFECT_WORLD_SHADER:
        v.position = vec3(); v.normal = vec3(); v.color = u32(); v.texcoords = vec2()
    elif effect_id == EFFECT_SKYSHADER:
        v.position = vec3(); v.normal = vec3(); v.color = u32(); v.texcoords = vec2()
        p += 8  # unexplained extra D3DDECLUSAGE_TEXCOORD element, same as the C# reader
    elif effect_id in (EFFECT_WORLD_NORMAL_MAP, EFFECT_WORLD_REFLECT_SHADER):
        v.position = vec3(); v.normal = vec3(); v.color = u32(); v.texcoords = vec2()
        v.tangent = vec3(); p += 4  # skip tangent's W component
    elif effect_id == EFFECT_CAR_SHADER:
        v.position = vec3(); v.normal = vec3(); v.color = u32(); v.texcoords = vec2(); v.tangent = vec3()
    elif effect_id == EFFECT_CAR_NORMAL_MAP:
        v.position = vec3(); v.texcoords = vec2(); v.color = u32(); v.normal = vec3(); v.tangent = vec3()
    elif effect_id == EFFECT_GLASS_REFLECT:
        v.position = vec3(); v.texcoords = vec2(); v.color = u32(); v.normal = vec3()
    elif effect_id == EFFECT_WATER:
        v.position = vec3(); v.color = u32(); v.texcoords = vec2(); v.normal = vec3()
    elif effect_id == EFFECT_WORLD_BONE_SHADER:
        v.position = vec3(); v.normal = vec3(); v.color = u32(); v.texcoords = vec2()
        v.blend_weight = vec3(); v.blend_indices = vec3(); v.tangent = vec3()
    elif effect_id == EFFECT_UCAP:
        v.position = vec3(); v.texcoords = vec2(); v.blend_weight = vec3(); v.blend_indices = vec3()
        p += 8 * 16  # 8x vec4, unknown purpose - same as the C# reader
    else:
        raise ValueError(f"Effect id {effect_id} has no known decoded vertex layout")

    return v


def _assemble_vertices(solid, vertex_buffers, num_vertices_total):
    """Direct translation of SolidReader<T,M>.PostProcessSolid - stride is
    derived per buffer (buffer length / vertex count claimed against it),
    not looked up from a table."""
    num_buffers = len(vertex_buffers)
    if num_buffers == 0:
        return

    vb_counts = [0] * num_buffers
    if num_buffers == 1:
        vb_counts[0] = num_vertices_total
    else:
        for m in solid.materials:
            if m.vertex_set_index < num_buffers:
                vb_counts[m.vertex_set_index] += m.num_verts

    vb_offsets = [0] * num_buffers
    vb_arrays = [[None] * vb_counts[i] for i in range(num_buffers)]

    for m in solid.materials:
        idx = m.vertex_set_index
        if idx >= num_buffers or vb_counts[idx] == 0:
            continue
        stride = len(vertex_buffers[idx]) // vb_counts[idx]
        num_verts = m.num_verts if m.num_verts else vb_counts[idx]
        offset = vb_offsets[idx]
        if offset >= vb_counts[idx]:
            continue
        buf = vertex_buffers[idx]
        pos = offset * stride
        for i in range(num_verts):
            vb_arrays[idx][offset + i] = _read_vertex(buf, pos, m.effect_id)
            pos += stride  # trust the declared stride, not however many bytes
                            # _read_vertex itself consumed - matches the C#
                            # reader's own assumption
        vb_offsets[idx] += num_verts

    solid.vertex_sets = [[v for v in arr if v is not None] for arr in vb_arrays]


def read_object_pack(data, payload_start, length):
    """Returns a list of SolidObject, one per 0x80134010 found."""
    objects = []
    current = None
    vertex_buffers = []
    num_vertices_total = 0
    named_material_index = 0
    stream_index = 0
    last_effect_id = None

    def finish_current():
        if current is not None:
            _assemble_vertices(current, vertex_buffers, num_vertices_total)

    for _offset, raw_id, _id_hex, chunk_length, _is_container, chunk_payload_start in \
            walk_chunks(data, payload_start, payload_start + length):
        chunk_id = int.from_bytes(raw_id, 'little')

        if chunk_id == SOLID_OBJECT_CONTAINER:
            finish_current()
            current = SolidObject()
            objects.append(current)
            vertex_buffers = []
            num_vertices_total = 0
            named_material_index = 0
            stream_index = 0
            last_effect_id = None
            continue

        if current is None or chunk_id == PLAT_CONTAINER:
            # Either header-section content (filename/group name/streaming
            # table - not needed for geometry) or the Plat container marker
            # itself, whose children are already being recursed into.
            continue

        if chunk_id == CHUNK_SOLID_HEADER:
            pos = _align16(chunk_payload_start)
            name, hash_, bmin, bmax = _read_solid_header(data, pos)
            current.name, current.hash = name, hash_
            current.min_point, current.max_point = bmin, bmax

        elif chunk_id == CHUNK_SOLID_TEXTURES:
            for i in range(chunk_length // 8):
                current.texture_hashes.append(struct.unpack_from('<I', data, chunk_payload_start + i * 8)[0])

        elif chunk_id == CHUNK_SOLID_MORPH_TARGETS:
            for i in range(chunk_length // 12):
                base = chunk_payload_start + i * 12
                name_hash = struct.unpack_from('<I', data, base)[0]
                blend = struct.unpack_from('<f', data, base + 8)[0]
                current.morph_targets.append((name_hash, blend))

        elif chunk_id == CHUNK_PLAT_MESH_ENTRIES:
            pos = _align16(chunk_payload_start)
            usable = chunk_payload_start + chunk_length - pos
            for _j in range(usable // 144):
                m, diffuse_id, normal_id, height_id, specular_id, opacity_id = _read_shading_group(data, pos)
                if current.materials and m.effect_id != last_effect_id:
                    stream_index += 1

                def resolve(slot_id):
                    return None if slot_id == diffuse_id else current.texture_hashes[slot_id]

                m.diffuse_hash = current.texture_hashes[diffuse_id]
                m.normal_hash = resolve(normal_id)
                m.specular_hash = resolve(specular_id)
                m.height_hash = resolve(height_id)
                m.opacity_hash = resolve(opacity_id)
                m.vertex_set_index = stream_index
                current.materials.append(m)
                num_vertices_total += m.num_verts
                last_effect_id = m.effect_id
                pos += 144

        elif chunk_id == CHUNK_PLAT_VERTEX_BUFFER:
            pos = _align16(chunk_payload_start)
            vertex_buffers.append(data[pos:chunk_payload_start + chunk_length])

        elif chunk_id == CHUNK_PLAT_INDICES:
            pos = _align16(chunk_payload_start)
            for m in current.materials:
                m.indices = list(struct.unpack_from(f'<{m.num_indices}H', data, pos))
                pos += m.num_indices * 2

        elif chunk_id == CHUNK_PLAT_MESH_ENTRY_NAME:
            if chunk_length > 0:
                end = data.index(b'\x00', chunk_payload_start)
                current.materials[named_material_index].name = \
                    data[chunk_payload_start:end].decode('ascii', errors='replace')
                named_material_index += 1

        # Everything else (light materials, position markers, plat info,
        # UCAP frame weights) is intentionally skipped - matches the C#
        # reader's own "read but discard" handling for that data today.

    finish_current()
    return objects
