"""
Materials Populator.

Standalone batch tool for materials.json.
Scans for textures, matches them to material layers, applies alpha/UV settings,
and handles emissive properties:
- Stages with an emissive texture get emissiveFactor=[1,1,1],
  emissiveIntensityNits=300, and vertColorEmissive=true (multiplying by vertex color).
- Stages with NO emissive texture get emissiveFactor=[0,0,0],
  emissiveIntensityNits=0, and stale emissiveMap/vertColorEmissive fields removed.
"""

import os
import re
import json
import shutil
import traceback
import tkinter as tk
from tkinter import filedialog, scrolledtext, messagebox
from datetime import datetime

FILENAME_RE = re.compile(r'^0x[0-9A-Fa-f]{8}_(?P<basename>.+)_(?P<suffix>REF|AO|[DNMROASIE])(?:\.[^.]+)?$', re.IGNORECASE)
# A file with a hash prefix but no recognised role suffix is the diffuse (e.g. the _ANIM001 frames).
BARE_FILENAME_RE = re.compile(r'^0x[0-9A-Fa-f]{8}_(?P<basename>.+?)(?:\.[^.]+)?$', re.IGNORECASE)
HASH_RE = re.compile(r'0x([0-9A-Fa-f]{8})_')

ROLE_NAMES = {
    'D': 'diffuse', 'N': 'normal', 'M': 'metal', 'R': 'rough', 'O': 'opacity',
    'A': 'ao', 'AO': 'ao', 'S': 'spec', 'REF': 'reflection',
    'I': 'emissive', 'E': 'emissive',
}

ROLE_STAGE_FIELD = {
    'diffuse': 'baseColorMap',
    'normal': 'normalMap',
    'metal': 'metallicMap',
    'rough': 'roughnessMap',
    'opacity': 'opacityMap',
    'ao': 'ambientOcclusionMap',
    'emissive': 'emissiveMap',
}

USE_UV_FIELD_OVERRIDES = {'baseColorMap': 'diffuseMapUseUV'}

# --- animations (Populate animations button) ---
SCROLL_ANIM_FLAG = 4          # animFlags value for a scroll, taken from a stage saved in the Material Editor
SCROLL_SPEED_VALUE = 1.0      # scrollDir carries the per-axis speed (cycles per second), so scrollSpeed stays 1
TIMESTEP_RAW_PER_SECOND = 6552.0   # ESTIMATE: raw timestep 1638 was seen at about 0.25 s per step in game
ANIMATIONS_JSON_NAME = 'material_animations.json'

ALL_MAP_FIELDS = list(ROLE_STAGE_FIELD.values())
FIELD_TO_ROLE = {field: role for role, field in ROLE_STAGE_FIELD.items()}


def apply_emissive_defaults(stage):
    """
    Kicks in when a stage HAS an emissiveMap (both normal and add):
    - Ensures emissiveFactor is active [1, 1, 1].
    - Ensures emissiveIntensityNits is set (default 300).
    - Ensures vertColorEmissive is True so emission is multiplied by vertex color.
    """
    changed = 0

    if stage.get('emissiveFactor') in (None, [0, 0, 0], [0.0, 0.0, 0.0], [0, 0, 0, 0]):
        stage['emissiveFactor'] = [1, 1, 1]
        changed += 1

    if stage.get('emissiveIntensityNits') in (None, 0, 0.0):
        stage['emissiveIntensityNits'] = 300
        changed += 1

    # Normal emissive maps also multiply by vertex color
    if stage.get('vertColorEmissive') is not True:
        stage['vertColorEmissive'] = True
        changed += 1

    return changed


def zero_emissive_fields(target):
    """
    Kicks in when a stage (or material) has NO emissive texture:
    - Removes stale/empty emissiveMap and vertColorEmissive.
    - Sets emissiveFactor to [0, 0, 0].
    - Sets emissiveIntensityNits to 0.
    """
    changed = 0

    if 'emissiveMap' in target and not (target['emissiveMap'] and str(target['emissiveMap']).strip()):
        del target['emissiveMap']
        changed += 1

    if 'vertColorEmissive' in target:
        del target['vertColorEmissive']
        changed += 1

    if target.get('emissiveFactor') != [0, 0, 0]:
        target['emissiveFactor'] = [0, 0, 0]
        changed += 1

    if target.get('emissiveIntensityNits') != 0:
        target['emissiveIntensityNits'] = 0
        changed += 1

    return changed


ADD_ALPHA_VALUES = {
    'alphaRef': 50,
    'alphaTest': False,
    'translucentBlendOp': 'addAlpha',
    'doubleSided': True,
    'translucent': True,
    'translucentRecvShadows': True,
    'translucentZWrite': True,
}


def is_emissive_add_path(path):
    parts = [p.lower() for p in re.split(r'[\\/]+', path or '')]
    return any(a == 'emissive' and b == 'add' for a, b in zip(parts, parts[1:]))


def apply_add_alpha(mat):
    changed = False
    for key, value in ADD_ALPHA_VALUES.items():
        if mat.get(key) != value:
            mat[key] = value
            changed = True
    return changed


def use_uv_field_for(field):
    return USE_UV_FIELD_OVERRIDES.get(field, field + 'UseUV')


def normalize_use_uv(stage, stage_index):
    default_expected = None if stage_index == 0 else 1
    changed = 0
    for field in ALL_MAP_FIELDS:
        uv_field = use_uv_field_for(field)
        expected = default_expected
        if field == 'opacityMap' and is_splat_opacity(stage.get('opacityMap')):
            expected = 0
        if uv_field not in stage or stage[uv_field] != expected:
            stage[uv_field] = expected
            changed += 1
    return changed


def parse_filename(filename):
    """Returns (base name, role, implied). implied is True when no role suffix was found and
    the file is taken as the diffuse."""
    match = FILENAME_RE.match(filename)
    if match:
        return match.group('basename'), ROLE_NAMES[match.group('suffix').upper()], False
    bare = BARE_FILENAME_RE.match(filename)
    if bare:
        return bare.group('basename'), 'diffuse', True
    return None


def derive_base_name(base_color_map_path):
    if not base_color_map_path or not isinstance(base_color_map_path, str):
        return None
    filename = base_color_map_path.replace('\\', '/').rsplit('/', 1)[-1]
    parsed = parse_filename(filename)
    return parsed[0] if parsed else None


def opacity_variant_from_path(full_path):
    parts = [p.upper() for p in re.split(r'[\\/]+', full_path)]
    if 'LERP' in parts:
        return 'lerp'
    if 'CLIP' in parts:
        return 'clip'
    return None


def is_splat_opacity(opacity_map_path):
    return bool(opacity_map_path) and 'splat' in opacity_map_path.lower()


def apply_opacity_variant(mat, full_path):
    variant = opacity_variant_from_path(full_path)
    new_values = {}
    if variant == 'lerp':
        new_values = {
            'alphaRef': 0,
            'alphaTest': False,
            'translucentBlendOp': 'lerpAlpha',
            'doubleSided': True,
            'translucent': True,
            'translucentRecvShadows': True,
            'translucentZWrite': True,
        }
    elif variant == 'clip':
        new_values = {'alphaRef': 150, 'alphaTest': True, 'translucentBlendOp': 'None'}
    changed = False
    for key, value in new_values.items():
        if mat.get(key) != value:
            mat[key] = value
            changed = True
    return variant, changed


def resolve_disk_path(materials_json_path, base_prefix, texture_root):
    normalized_prefix = (base_prefix or '').replace('\\', '/')
    path = (materials_json_path or '').replace('\\', '/')
    if not normalized_prefix or not path.startswith(normalized_prefix):
        return None
    rel_parts = path[len(normalized_prefix):].split('/')
    return os.path.join(texture_root, *rel_parts)


def scan_texture_folder(root, log=print):
    index = {}
    file_count = 0
    for current_root, _dirs, filenames in os.walk(root):
        for filename in filenames:
            file_count += 1
            parsed = parse_filename(filename)
            if not parsed:
                continue
            base_name, role, implied = parsed
            if role not in ROLE_STAGE_FIELD:
                continue
            full_path = os.path.join(current_root, filename)
            roles = index.setdefault(base_name.lower(), {})
            if implied:
                roles.setdefault(role, full_path)   # an explicit _D file wins over a bare one
            else:
                roles[role] = full_path
    log(f"Scanned {file_count} file(s), matched {len(index)} distinct base name(s) under {root}")
    return index


def stage_map_fields(stage):
    return [(k, v) for k, v in stage.items() if k.endswith('Map') and isinstance(v, str) and v.strip()]


def validate_paths(materials_data, base_prefix, texture_root, log=print):
    broken = []
    unchecked_count = 0
    checked_count = 0

    for material_name, mat in materials_data.items():
        stages = mat.get('Stages') or []
        for stage_index, stage in enumerate(stages):
            if not stage:
                continue
            for field, value in stage_map_fields(stage):
                disk_path = resolve_disk_path(value, base_prefix, texture_root)
                if disk_path is None:
                    unchecked_count += 1
                    continue
                checked_count += 1
                if not os.path.isfile(disk_path):
                    broken.append((material_name, stage_index, field, value))

    for material_name, stage_index, field, value in broken:
        log(f"DEAD END: {material_name} [stage {stage_index}] {field} -> {value}")

    log(f"Validated {checked_count} path(s) against disk: {len(broken)} dead end(s), {checked_count - len(broken)} OK.")
    if unchecked_count:
        log(f"{unchecked_count} path(s) fall outside the configured prefix ({base_prefix or '(none set)'}) and were not checked.")

    return broken, unchecked_count, checked_count


def detect_base_prefix(materials_data):
    for mat in materials_data.values():
        for stage in mat.get('Stages', []) or []:
            if not stage:
                continue
            base_color = stage.get('baseColorMap')
            if base_color and derive_base_name(base_color):
                directory = base_color.replace('\\', '/').rsplit('/', 1)[0]
                return directory + '/'
    return ''


def populate_materials(materials_data, texture_index, texture_root, base_prefix, log=print):
    filled_total = 0
    redirected_total = 0
    cleared_total = 0
    useuv_total = 0
    alpha_variant_total = 0
    emissive_defaults_total = 0
    emissive_zeroed_total = 0
    touched_materials = set()

    stages_examined = 0
    no_diffuse_count = 0

    for material_name, mat in materials_data.items():
        stages = mat.get('Stages') or []
        is_multi_layer = sum(1 for s in stages if s) > 1
        base_layer_index = next((i for i, s in enumerate(stages) if s), None)

        for stage_index, stage in enumerate(stages):
            if not stage:
                continue
            stages_examined += 1

            base_name = derive_base_name(stage.get('baseColorMap'))
            if not base_name:
                no_diffuse_count += 1
                continue

            entry = texture_index.get(base_name.lower())
            stage_changed = 0

            for field in ALL_MAP_FIELDS:
                role = FIELD_TO_ROLE[field]
                current_value = stage.get(field)
                match_path = entry.get(role) if entry else None

                if field == 'opacityMap' and match_path and not is_splat_opacity(current_value):
                    candidate = base_prefix + os.path.relpath(match_path, texture_root).replace(os.sep, '/')
                    if current_value != candidate:
                        stage[field] = candidate
                        redirected_total += 1
                        stage_changed += 1
                        log(f"{material_name} [stage {stage_index}] opacityMap: redirected -> {candidate}")
                    continue

                if current_value:
                    disk_path = resolve_disk_path(current_value, base_prefix, texture_root)
                    if disk_path is not None and not os.path.isfile(disk_path):
                        if match_path:
                            rel_path = os.path.relpath(match_path, texture_root).replace(os.sep, '/')
                            stage[field] = base_prefix + rel_path
                            redirected_total += 1
                            stage_changed += 1
                            log(f"{material_name} [stage {stage_index}] {field}: redirected dead end -> {stage[field]}")
                        else:
                            if field == 'emissiveMap':
                                del stage[field]
                            else:
                                stage[field] = ""
                            cleared_total += 1
                            stage_changed += 1
                            log(f"{material_name} [stage {stage_index}] {field}: dead end, no scan replacement found - cleared")
                elif match_path:
                    rel_path = os.path.relpath(match_path, texture_root).replace(os.sep, '/')
                    stage[field] = base_prefix + rel_path
                    filled_total += 1
                    stage_changed += 1

            # Check for emissive/add folder indicator
            has_add_indicator = bool(stage.get('baseColorMap')) and (
                is_emissive_add_path((entry or {}).get('diffuse')) or is_emissive_add_path(stage.get('baseColorMap'))
            )
            if has_add_indicator and stage.get('emissiveMap') != stage['baseColorMap']:
                stage['emissiveMap'] = stage['baseColorMap']
                filled_total += 1
                stage_changed += 1
                log(f"{material_name} [stage {stage_index}] emissiveMap: set to baseColorMap (emissive/add folder).")

            # Remove stale baseColorMap link if no longer in emissive/add and no scan match
            if stage.get('emissiveMap') == stage.get('baseColorMap') and not has_add_indicator:
                del stage['emissiveMap']
                cleared_total += 1
                stage_changed += 1
                log(f"{material_name} [stage {stage_index}] emissiveMap: removed stale baseColorMap link (no longer in emissive/add).")

            # Emissive properties handling
            has_emissive = bool(stage.get('emissiveMap') and str(stage.get('emissiveMap')).strip())
            if has_emissive:
                # Applies emissiveFactor, intensity, and vertColorEmissive=true to all emissives
                emissive_changed = apply_emissive_defaults(stage)
                if emissive_changed:
                    emissive_defaults_total += emissive_changed
                    stage_changed += emissive_changed
                    log(f"{material_name} [stage {stage_index}] emissive defaults applied (vertColorEmissive=true, intensity=300).")
            else:
                # If there is no emissiveMap: zero out factor & intensity, remove stale keys
                zeroed_changes = zero_emissive_fields(stage)
                if zeroed_changes:
                    emissive_zeroed_total += zeroed_changes
                    stage_changed += zeroed_changes
                    log(f"{material_name} [stage {stage_index}] zeroed emission (emissiveFactor=[0,0,0], emissiveIntensityNits=0).")

            # Opacity variants
            final_opacity = stage.get('opacityMap')
            if final_opacity and stage_index == base_layer_index:
                opacity_disk_path = resolve_disk_path(final_opacity, base_prefix, texture_root)
                if opacity_disk_path and os.path.isfile(opacity_disk_path):
                    variant, alpha_changed = apply_opacity_variant(mat, opacity_disk_path)
                    if alpha_changed:
                        alpha_variant_total += 1
                        stage_changed += 1
                        log(f"{material_name} [stage {stage_index}] alpha field(s) set for {variant} opacity variant.")

            if has_add_indicator and apply_add_alpha(mat):
                alpha_variant_total += 1
                stage_changed += 1
                log(f"{material_name} [stage {stage_index}] alpha field(s) set for addAlpha.")

            if is_multi_layer:
                useuv_changed = normalize_use_uv(stage, stage_index)
                useuv_total += useuv_changed
                stage_changed += useuv_changed

            if stage_changed:
                touched_materials.add(material_name)

        # Top-level material cleanup if no stage uses emissive
        mat_has_emissive = any(
            bool(s and s.get('emissiveMap') and str(s.get('emissiveMap')).strip())
            for s in stages
        )
        if not mat_has_emissive:
            mat_zeroed = zero_emissive_fields(mat)
            if mat_zeroed:
                emissive_zeroed_total += mat_zeroed
                touched_materials.add(material_name)
                log(f"{material_name}: zeroed top-level emissive fields.")

    log(f"Examined {stages_examined} stage(s) across {len(materials_data)} material(s): "
        f"{no_diffuse_count} had no baseColorMap-shaped text to derive a name from, "
        f"{filled_total} field(s) filled, {redirected_total} dead end(s) redirected, "
        f"{cleared_total} dead end(s) cleared, {useuv_total} UseUV flag(s) normalized, "
        f"{alpha_variant_total} alpha-variant field set(s) applied, "
        f"{emissive_defaults_total} emissive default(s) filled/updated, "
        f"{emissive_zeroed_total} emissive field(s) zeroed to 0, "
        f"{len(touched_materials)} material(s) touched.")

    total_changes = (filled_total + redirected_total + cleared_total + useuv_total +
                     alpha_variant_total + emissive_defaults_total + emissive_zeroed_total)
    return total_changes, len(touched_materials)


def hash_from_path(path):
    """0xAE651288 from a texture path or file name, or None."""
    if not path or not isinstance(path, str):
        return None
    match = HASH_RE.match(path.replace('\\', '/').rsplit('/', 1)[-1])
    return '0x' + match.group(1).upper() if match else None


def stage_diffuse_hash(stage):
    for field in ('baseColorMap', 'colorMap', 'diffuseMap'):
        found = hash_from_path(stage.get(field))
        if found:
            return found
    return None


def scan_diffuse_by_hash(root, log=print):
    """hash -> disk path of the diffuse file (a bare 0x<hash>_<name> file counts as diffuse)."""
    found = {}
    for current_root, _dirs, filenames in os.walk(root):
        for filename in filenames:
            parsed = parse_filename(filename)
            file_hash = hash_from_path(filename)
            if not parsed or not file_hash or parsed[1] != 'diffuse':
                continue
            if parsed[2]:
                found.setdefault(file_hash, os.path.join(current_root, filename))
            else:
                found[file_hash] = os.path.join(current_root, filename)
    log(f"Found {len(found)} diffuse texture file(s) by hash under {root}")
    return found


def scroll_axes(sc, swap_st, invert):
    """Scroll speeds as (U, V) in cycles per second. The viewer rule: with swap, S drives the
    vertical axis and T the horizontal axis."""
    raw_s = sc.get('rawSpeedS')
    raw_t = sc.get('rawSpeedT')
    speed_s = raw_s / 4096.0 if raw_s is not None else (sc.get('speedS') or 0.0)
    speed_t = raw_t / 4096.0 if raw_t is not None else (sc.get('speedT') or 0.0)
    axes = (speed_t, speed_s) if swap_st else (speed_s, speed_t)
    sign = -1.0 if invert else 1.0
    return sign * axes[0], sign * axes[1]


def is_native_scroll(sc):
    """Smooth scroll with a real speed: BeamNG can do it with animFlags + scrollDir."""
    if not sc or sc.get('type') != 'Smooth' or sc.get('timestepDriven'):
        return False
    return bool(sc.get('rawSpeedS') or sc.get('rawSpeedT') or sc.get('speedS') or sc.get('speedT'))


def clear_scroll_fields(stage):
    changed = False
    if isinstance(stage.get('animFlags'), int) and stage['animFlags'] & SCROLL_ANIM_FLAG:
        stage['animFlags'] &= ~SCROLL_ANIM_FLAG
        if stage['animFlags'] == 0:
            del stage['animFlags']
        changed = True
    for key in ('scrollDir', 'scrollSpeed'):
        if key in stage:
            del stage[key]
            changed = True
    return changed


def lua_entry_for(texture_hash, rec, frame_of, swap_st, invert):
    """The material_animations.json entry for a texture that BeamNG cannot animate natively."""
    entry = {'texture': texture_hash}
    sc = rec.get('scroll')
    fs = rec.get('frameSwap') or (
        {'animation': frame_of[texture_hash][0], 'frame': frame_of[texture_hash][1]}
        if texture_hash in frame_of else None)
    if fs:
        entry.update({'type': 'frame_swap', 'animation': fs['animation'], 'frame': fs['frame']})
        return entry
    if not sc or is_native_scroll(sc):
        return None
    if sc.get('type') == 'OffsetScale':
        off = (sc.get('offsetS', 0.0), sc.get('offsetT', 0.0))
        scale = (sc.get('scaleS', 0.0) or 1.0, sc.get('scaleT', 0.0) or 1.0)
        if swap_st:
            off, scale = off[::-1], scale[::-1]
        entry.update({'type': 'offset_scale', 'offsetU': off[0], 'offsetV': off[1],
                      'scaleU': scale[0], 'scaleV': scale[1]})
        return entry
    raw_step = sc.get('rawTimeStep') or 0
    entry['type'] = 'snap' if sc.get('type') == 'Snap' else 'smooth'
    entry['rawTimeStep'] = raw_step
    entry['stepSecondsEstimate'] = abs(raw_step) / TIMESTEP_RAW_PER_SECOND if raw_step else None
    if sc.get('timestepDriven'):
        # Speed 0 and 0: the game moves it by its timestep, half a UV per step. The jump axis
        # and direction are not decoded yet.
        entry['timestepDriven'] = True
        entry['direction'] = None
    else:
        u, v = scroll_axes(sc, swap_st, invert)
        entry['speedU'], entry['speedV'] = u, v
    return entry


def populate_animations(materials_data, dictionary, diffuse_by_hash, texture_root, base_prefix,
                        swap_st=True, invert=False, log=print):
    """Writes animation data only. Smooth scrolls go on the stage (animFlags + scrollDir).
    Everything else (snap, timestep-driven, offset/scale, frame swap) goes in the returned
    lua data, so a lua middleman can drive it. Returns (changes, lua_data)."""
    textures = dictionary.get('textures') or {}
    animations = dictionary.get('animations') or {}

    frame_of = {}
    for akey, anim in animations.items():
        for i, frame_hash in enumerate(anim.get('frames') or []):
            frame_of.setdefault(frame_hash, (akey, i))

    lua_animations = {}
    for akey, anim in animations.items():
        frames = []
        for frame_hash in anim.get('frames') or []:
            disk = diffuse_by_hash.get(frame_hash.upper().replace('0X', '0x'))
            path = base_prefix + os.path.relpath(disk, texture_root).replace(os.sep, '/') if disk else None
            if not path:
                log(f"  frame {frame_hash} of {anim.get('name') or akey} has no texture file in the folder")
            frames.append({'hash': frame_hash, 'path': path})
        lua_animations[akey] = {'name': anim.get('name'), 'fps': anim.get('fps'),
                                'timeBase': anim.get('timeBase'), 'frames': frames}

    lua_materials = {}
    native_count = cleared_count = lua_count = 0
    touched = set()

    for material_name, mat in materials_data.items():
        for stage_index, stage in enumerate(mat.get('Stages') or []):
            if not stage:
                continue
            texture_hash = stage_diffuse_hash(stage)
            if not texture_hash:
                continue
            if texture_hash not in textures and texture_hash not in frame_of:
                continue    # unknown to the dictionary: leave the stage alone
            rec = dict(textures.get(texture_hash) or {})
            sc = rec.get('scroll')

            if is_native_scroll(sc) and not rec.get('frameSwap'):
                u, v = scroll_axes(sc, swap_st, invert)
                flags = stage.get('animFlags') if isinstance(stage.get('animFlags'), int) else 0
                wanted = {'animFlags': flags | SCROLL_ANIM_FLAG, 'scrollDir': [u, v],
                          'scrollSpeed': SCROLL_SPEED_VALUE}
                if any(stage.get(k) != val for k, val in wanted.items()):
                    stage.update(wanted)
                    native_count += 1
                    touched.add(material_name)
            elif clear_scroll_fields(stage):
                cleared_count += 1
                touched.add(material_name)

            entry = lua_entry_for(texture_hash, rec, frame_of, swap_st, invert)
            if entry:
                entry['stage'] = stage_index
                lua_materials.setdefault(material_name, []).append(entry)
                lua_count += 1

    log(f"Smooth scroll set on {native_count} stage(s); stale scroll fields cleared on {cleared_count} stage(s)")
    log(f"{lua_count} stage(s) in {len(lua_materials)} material(s) go to {ANIMATIONS_JSON_NAME}")
    lua_data = {
        'version': 1,
        'notes': ('speedU/speedV are cycles per second after the S/T swap and invert settings. '
                  'stepSecondsEstimate is an estimate: the timestep decode is not confirmed. '
                  'Timestep-driven entries (speed 0 and 0) have no direction decoded yet.'),
        'animations': lua_animations,
        'materials': lua_materials,
    }
    return native_count + cleared_count, lua_data


class App:
    def __init__(self, root):
        self.root = root
        root.title("Materials Populator")

        self.materials_path_var = tk.StringVar()
        self.texture_folder_var = tk.StringVar()
        self.base_prefix_var = tk.StringVar(value="")
        self.dictionary_path_var = tk.StringVar()
        self.swap_st_var = tk.BooleanVar(value=True)
        self.invert_scroll_var = tk.BooleanVar(value=False)

        row = 0
        tk.Label(root, text="materials.json:").grid(row=row, column=0, sticky="e", padx=4, pady=4)
        tk.Entry(root, textvariable=self.materials_path_var, width=70).grid(row=row, column=1, padx=4)
        tk.Button(root, text="Browse", command=self._pick_materials_json).grid(row=row, column=2, padx=4)

        row += 1
        tk.Label(root, text="Texture folder (scanned recursively):").grid(row=row, column=0, sticky="e", padx=4, pady=4)
        tk.Entry(root, textvariable=self.texture_folder_var, width=70).grid(row=row, column=1, padx=4)
        tk.Button(root, text="Browse", command=self._pick_texture_folder).grid(row=row, column=2, padx=4)

        row += 1
        tk.Label(root, text="Path prefix to write (auto-detected from materials.json):").grid(row=row, column=0, sticky="e", padx=4, pady=4)
        tk.Entry(root, textvariable=self.base_prefix_var, width=70).grid(row=row, column=1, padx=4, sticky="w")

        row += 1
        tk.Label(root, text="Material dictionary (animations only):").grid(row=row, column=0, sticky="e", padx=4, pady=4)
        tk.Entry(root, textvariable=self.dictionary_path_var, width=70).grid(row=row, column=1, padx=4)
        tk.Button(root, text="Browse", command=self._pick_dictionary).grid(row=row, column=2, padx=4)

        row += 1
        option_row = tk.Frame(root)
        option_row.grid(row=row, column=1, sticky="w", padx=4)
        tk.Checkbutton(option_row, text="Swap S/T", variable=self.swap_st_var).pack(side="left")
        tk.Checkbutton(option_row, text="Invert scroll", variable=self.invert_scroll_var).pack(side="left", padx=8)

        row += 1
        button_row = tk.Frame(root)
        button_row.grid(row=row, column=0, columnspan=3, pady=10)
        tk.Button(button_row, text="Populate", command=self.run_populate, bg="#3a6", fg="white", width=20
                  ).pack(side="left", padx=6)
        tk.Button(button_row, text="Populate animations", command=self.run_populate_animations, width=20
                  ).pack(side="left", padx=6)
        tk.Button(button_row, text="Validate Paths Only", command=self.run_validate_only, width=20
                  ).pack(side="left", padx=6)

        row += 1
        self.log_box = scrolledtext.ScrolledText(root, width=110, height=30)
        self.log_box.grid(row=row, column=0, columnspan=3, padx=8, pady=8)

    def _pick_materials_json(self):
        path = filedialog.askopenfilename(filetypes=[("materials.json", "*.json"), ("All files", "*.*")])
        if not path:
            return
        self.materials_path_var.set(path)
        try:
            with open(path, "r", encoding="utf-8") as f:
                materials_data = json.load(f)
        except Exception:
            return
        if not self.base_prefix_var.get():
            self.base_prefix_var.set(detect_base_prefix(materials_data))

    def _pick_dictionary(self):
        path = filedialog.askopenfilename(filetypes=[("Material dictionary", "*.json"), ("All files", "*.*")])
        if path:
            self.dictionary_path_var.set(path)

    def _pick_texture_folder(self):
        path = filedialog.askdirectory()
        if path:
            self.texture_folder_var.set(path)

    def log(self, msg):
        self.log_box.insert(tk.END, msg + "\n")
        self.log_box.see(tk.END)
        self.root.update_idletasks()

    def _load_inputs(self):
        materials_path = self.materials_path_var.get().strip()
        texture_root = self.texture_folder_var.get().strip()
        base_prefix = self.base_prefix_var.get()

        if not materials_path or not os.path.isfile(materials_path):
            messagebox.showerror("Materials Populator", "Pick a valid materials.json file first.")
            return None
        if not texture_root or not os.path.isdir(texture_root):
            messagebox.showerror("Materials Populator", "Pick a valid texture folder first.")
            return None

        self.log_box.delete("1.0", tk.END)

        try:
            with open(materials_path, "r", encoding="utf-8") as f:
                materials_data = json.load(f)
        except Exception as exc:
            messagebox.showerror("Materials Populator", f"Couldn't read materials.json:\n{exc}")
            return None

        self.log(f"Loaded {len(materials_data)} material(s) from {materials_path}")
        return materials_path, texture_root, base_prefix, materials_data

    def _run_safely(self, fn):
        try:
            fn()
        except Exception as exc:
            self.log("")
            self.log("--- ERROR ---")
            self.log(traceback.format_exc())
            messagebox.showerror("Materials Populator", f"Something went wrong:\n{exc}\n\nFull details are in the log.")

    def run_validate_only(self):
        self._run_safely(self._do_validate_only)

    def _do_validate_only(self):
        loaded = self._load_inputs()
        if loaded is None:
            return
        materials_path, texture_root, base_prefix, materials_data = loaded

        broken, unchecked, checked = validate_paths(materials_data, base_prefix, texture_root, log=self.log)
        if broken:
            messagebox.showwarning("Materials Populator", f"{len(broken)} dead end(s) found out of {checked} path(s) checked. See the log for details.")
        else:
            messagebox.showinfo("Materials Populator", f"No dead ends found - all {checked} checked path(s) resolve to a real file.")

    def run_populate(self):
        self._run_safely(self._do_populate)

    def _do_populate(self):
        loaded = self._load_inputs()
        if loaded is None:
            return
        materials_path, texture_root, base_prefix, materials_data = loaded

        texture_index = scan_texture_folder(texture_root, log=self.log)
        if not texture_index:
            self.log("No matching texture files found in that folder (checked recursively).")
            return

        total_changes, touched = populate_materials(materials_data, texture_index, texture_root, base_prefix, log=self.log)

        if total_changes == 0:
            self.log("Nothing to save - see the counts above for exactly why.")
        else:
            timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
            backup_path = f"{materials_path}.{timestamp}.bak"
            self.log(f"Writing backup to {backup_path} ...")
            shutil.copy2(materials_path, backup_path)
            self.log("Backup written.")

            self.log(f"Writing {total_changes} change(s) to {materials_path} ...")
            with open(materials_path, "w", encoding="utf-8") as f:
                json.dump(materials_data, f, indent=4)
            self.log(f"Write complete. {total_changes} change(s) across {touched} material(s).")

        self.log("")
        self.log("--- Validating all paths against disk ---")
        broken, unchecked, checked = validate_paths(materials_data, base_prefix, texture_root, log=self.log)

        summary = f"{total_changes} change(s) across {touched} material(s)." if total_changes else "Nothing changed - check the log for why."
        if broken:
            summary += f"\n\n{len(broken)} dead end(s) found out of {checked} path(s) checked - see the log."
            messagebox.showwarning("Materials Populator", summary)
        else:
            summary += f"\n\nAll {checked} checked path(s) resolve to a real file."
            messagebox.showinfo("Materials Populator", summary)

    def run_populate_animations(self):
        self._run_safely(self._do_populate_animations)

    def _do_populate_animations(self):
        dictionary_path = self.dictionary_path_var.get().strip()
        if not dictionary_path or not os.path.isfile(dictionary_path):
            messagebox.showerror("Materials Populator", "Pick the material dictionary json first.")
            return
        loaded = self._load_inputs()
        if loaded is None:
            return
        materials_path, texture_root, base_prefix, materials_data = loaded

        with open(dictionary_path, "r", encoding="utf-8") as f:
            dictionary = json.load(f)
        self.log(f"Loaded dictionary: {len(dictionary.get('textures') or {})} texture(s), "
                 f"{len(dictionary.get('animations') or {})} animation(s)")

        diffuse_by_hash = scan_diffuse_by_hash(texture_root, log=self.log)
        changes, lua_data = populate_animations(
            materials_data, dictionary, diffuse_by_hash, texture_root, base_prefix,
            swap_st=self.swap_st_var.get(), invert=self.invert_scroll_var.get(), log=self.log)

        if changes:
            timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
            backup_path = f"{materials_path}.{timestamp}.bak"
            self.log(f"Writing backup to {backup_path} ...")
            shutil.copy2(materials_path, backup_path)
            with open(materials_path, "w", encoding="utf-8") as f:
                json.dump(materials_data, f, indent=4)
            self.log(f"Wrote {changes} stage change(s) to {materials_path}")
        else:
            self.log("No stage changes for materials.json.")

        lua_path = os.path.join(os.path.dirname(materials_path), ANIMATIONS_JSON_NAME)
        with open(lua_path, "w", encoding="utf-8") as f:
            json.dump(lua_data, f, indent=2)
        self.log(f"Wrote {lua_path}")


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
