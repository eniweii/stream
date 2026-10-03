"""
Run this INSIDE Blender (Text Editor, or `blender --background --python
blender_scenery_position_dump.py -- <output.json> [collection_name]`).

Dumps every mesh object's world-space bounding-box center, using the
object's own bound_box corners transformed by matrix_world - this gives
the correct world position regardless of which convention the object is
currently in:
  - identity transform + real position baked into local vertex data
    (unmerged chop/deinstanced content, straight off import)
  - normalized origin + real position in object.location (content that's
    already been through AABB-normalize or mesh-dedup-merger's own origin
    neutralization step)
Both cases produce the same correct world-space center this way, so this
script does not need to know or guess which state any given object is in.

Output is a flat JSON list, one entry per mesh object:
    {"name": "...", "mesh_name": "...", "position": [x, y, z], "origin": [x, y, z]}

`position` is the world-space bbox center (correct for chop/deinstanced
content, where the real placement is baked into local vertex data with an
identity object transform). `origin` is the object's own matrix_world
translation (correct for genuine per-instance content whose transform was
never touched - confirmed empirically to matter: a real match run showed
building-type objects failing to match by a fixed, model-specific 2-10
unit offset between these two measurements - the matcher tries both and
uses whichever is closer, rather than needing to know in advance which
one is right for a given object.

`mesh_name` (the object's .data.name) is included because it's often the
untruncated original geometry name (matches nfs_scenery_dae_scan.py's
`geometry_name` field) and is a useful secondary signal even though it's
not unique for true instances sharing one mesh datablock.

Only intended for a first empirical check of whether DAE-derived positions
actually match current Blender object positions - not yet wired into any
apply/scn_ref-writing step.
"""
import bpy
import json
import os
import sys


def world_bbox_center(obj):
    corners = [obj.matrix_world @ mathutils_vector(c) for c in obj.bound_box]
    xs = [c.x for c in corners]
    ys = [c.y for c in corners]
    zs = [c.z for c in corners]
    return (
        (min(xs) + max(xs)) / 2.0,
        (min(ys) + max(ys)) / 2.0,
        (min(zs) + max(zs)) / 2.0,
    )


def mathutils_vector(c):
    import mathutils
    return mathutils.Vector(c)


def collect(collection_name=None):
    if collection_name:
        coll = bpy.data.collections.get(collection_name)
        if coll is None:
            raise ValueError(f"No collection named {collection_name!r} found")
        objects = [o for o in coll.all_objects if o.type == 'MESH']
    else:
        objects = [o for o in bpy.data.objects if o.type == 'MESH']

    out = []
    for obj in objects:
        bbox_pos = world_bbox_center(obj)
        origin_pos = tuple(obj.matrix_world.translation)
        out.append({
            "name": obj.name,
            "mesh_name": obj.data.name if obj.data else None,
            "position": list(bbox_pos),
            "origin": list(origin_pos),
        })
    return out


class SCENERY_OT_dump_positions(bpy.types.Operator):
    """Dump every mesh object's world-space bbox center to a JSON file,
    for matching against nfs_scenery_dae_scan.py's output"""
    bl_idname = "scenery.dump_positions"
    bl_label = "Dump Scenery Positions to JSON"

    filepath: bpy.props.StringProperty(subtype='FILE_PATH', default="blender_scenery_dump.json")
    collection_name: bpy.props.StringProperty(
        name="Collection (optional)",
        description="Leave blank to dump every mesh object in the whole scene")

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        records = collect(self.collection_name or None)
        with open(self.filepath, 'w') as f:
            json.dump(records, f, indent=2)
        self.report({'INFO'}, f"Wrote {len(records)} object positions to {self.filepath}")
        return {'FINISHED'}


def register():
    bpy.utils.register_class(SCENERY_OT_dump_positions)


def unregister():
    bpy.utils.unregister_class(SCENERY_OT_dump_positions)


if __name__ == '__main__':
    argv = sys.argv
    if '--' in argv:
        # Headless mode: `blender --background file.blend --python this.py -- output.json [collection]`
        argv = argv[argv.index('--') + 1:]
        output_path = argv[0] if len(argv) > 0 else 'blender_scenery_dump.json'
        collection_name = argv[1] if len(argv) > 1 else None
        if os.path.isdir(output_path):
            sys.exit(f"ERROR: output_path {output_path!r} is a folder, not a file. "
                     f"Pass a filename to write to, e.g. "
                     f"{os.path.join(output_path, 'blender_dump.json')!r}")
        records = collect(collection_name)
        with open(output_path, 'w') as f:
            json.dump(records, f, indent=2)
        print(f"Wrote {len(records)} object positions to {output_path}"
              + (f" (collection {collection_name!r})" if collection_name else " (whole scene)"))
    else:
        # Running inside Blender's Text Editor - register the operator and
        # pop up the save-file dialog immediately, rather than requiring
        # the user to dig up this operator in a menu first.
        register()
        bpy.ops.scenery.dump_positions('INVOKE_DEFAULT')
