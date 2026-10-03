#!/usr/bin/env python3
"""build_beamng_particles.py - Step 3 of the Carbon -> BeamNG emitter port.

Input : emitters_resolved.json  (written by extract_emitters.py)
Output: <folder of the input>/beamng_out/
    1. managedParticleData.json         All ParticleData objects (for Particle Editor)
    2. managedParticleEmitterData.json  All ParticleEmitterData + NodeData objects
    3. nfsc.datablocks.json             Unified datablocks file
    4. items.level.json                 Every ParticleEmitterNode placed in the world (+150m height)
    5. beamng_build_report.txt          Summary statistics
"""

from collections import Counter
import json
import math
import os
import re
import sys

# ------------------------------------------------------------------ CONFIG
CONFIG = {
    "PREFIX": "nfsc_",
    # Matches your exact directory casing: levels/nfsc/Assets/Emitters/
    "TEX_DIR": "levels/nfsc/Assets/Emitters/",
    "TEX_EXT": ".dds",
    # Explicit node datablock with timeMultiple = 1.0
    "NODE_DATABLOCK": "nfsc_emitterNodeData",
    "DEFINE_NODE_DATABLOCK": True,
    "PARENT_GROUP": "MissionGroup",
    "BLEND_ADDITIVE": "ADDITIVE",
    "BLEND_ALPHA": "NORMAL",
    # Normal (alpha) blended layers come out too dark in daylight, so they
    # are built as additive too. Set False to keep the game blend mode.
    "FORCE_ADDITIVE": True,
    "SIZE_SCALE": 1.0,
    "GRAVITY_SCALE": 1.0 / 9.81,  # Carbon m/s^2 -> BeamNG coefficient
    "DRAG_SCALE": 1.25,  # above 1 = shorter travel, keeps particles from ending too high
    "SPIN_SCALE": 1.0,
    "SPEED_SCALE": 1.0,
    "HEIGHT_OFFSET": 150.0,  # Vertical world offset
    # Carbon rows are the local axes in world space (row 2 = emitter Z).
    # Torque matrices keep the axes in columns, so the 3x3 is transposed.
    # If a tilted emitter points the wrong way in BeamNG, flip this switch.
    "TRANSPOSE_ROTATION": True,
    # Volume emitters: Carbon spawns every particle at its own random point in
    # the VolumeExtent box. BeamNG has no box, so a Lua module
    # (emitter_volume_scatter.lua) moves nodes inside the box every frame.
    # A layer qualifies when its box is larger than its particle size.
    "VOLUME_SCATTER": True,
    "VOLUME_MIN_RATIO": 1.0,  # box side must exceed max particle size * ratio
    "VOLUME_FPS": 60.0,       # one node = one new random point per frame
    "VOLUME_MAX_NODES": 8,    # cap of nodes per placement and layer
    # RenderLinked layers: Carbon draws a ribbon along the flow, so the
    # streak texture stands along the flow. A billboard needs a 90 degree turn.
    "LINKED_SPIN_OFFSET": 90.0,
    # Fire layers (effect name contains FIRE_KEYWORD) are ribbons in the game.
    # BeamNG has no ribbon, so separate quads must overlap to look connected:
    # they use this ejection period (1 = densest, raise to 3..5 if the frame
    # rate drops). All other layers keep the rate-based period.
    "FIRE_KEYWORD": "fire",
    "FIRE_PERIOD_MS": 1,
    # Fire layers with Drag: eject at the speed the particle travels on average
    # over its life in the game (distance / life), not the start speed.
    "FIRE_MEAN_SPEED": True,
    "APPLY_VOLUME_CENTER": True,
    "MIN_PERIOD_MS": 1,     # Hard minimum required by BeamNG
    "MAX_PERIOD_MS": 2047,  # Hard maximum required by BeamNG
}

FILE_MANAGED_PARTICLES = "managedParticleData.json"
FILE_MANAGED_EMITTERS = "managedParticleEmitterData.json"
FILE_UNIFIED_DATABLOCKS = "nfsc.datablocks.json"
FILE_LEVEL_NODES = "items.level.json"


IDENTITY_ROT = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]


def placement_rot(p):
  """Carbon rotation (9 floats, rows = local axes) or identity when missing."""
  rot = p.get("rot")
  if isinstance(rot, list) and len(rot) == 9:
    return [float(v) for v in rot]
  return list(IDENTITY_ROT)


def is_tilted(rot):
  """Local Z (row 2) is not straight up."""
  return abs(rot[6]) > 1e-3 or abs(rot[7]) > 1e-3 or rot[8] < 1 - 1e-3


def rotate_local(rot, v):
  """Local vector -> world offset: v.x*row0 + v.y*row1 + v.z*row2."""
  return tuple(
      v[0] * rot[i] + v[1] * rot[3 + i] + v[2] * rot[6 + i] for i in range(3)
  )


def torque_matrix(rot):
  """9 floats for the ParticleEmitterNode rotationMatrix."""
  if not CONFIG["TRANSPOSE_ROTATION"]:
    return [r6(v) for v in rot]
  return [r6(rot[3 * c + r]) for r in range(3) for c in range(3)]


def scatter_nodes(f):
  """Nodes per placement for a volume layer, or 0 when it is not scattered."""
  C = CONFIG
  if not C["VOLUME_SCATTER"]:
    return 0
  ext = max(vec4(f, "VolumeExtent")[:3])
  size = max(vec4(f, "Size", 1.0))
  if ext <= 0 or ext <= size * C["VOLUME_MIN_RATIO"]:
    return 0
  rate = num(f, "NumParticles") * (1.0 - clamp(num(f, "NumParticlesVariance"), 0.0, 1.0))
  if rate <= 0:
    return 0
  return int(clamp(math.ceil(rate / C["VOLUME_FPS"]), 1, C["VOLUME_MAX_NODES"]))


def fmt_floats(vals):
  return " ".join("%.6f" % float(v) for v in vals)


def map_pos(x, y, z):
  """Carbon world position -> BeamNG world position with vertical +150 offset."""
  return (x, y, z + CONFIG["HEIGHT_OFFSET"])


# ----------------------------------------------------------------- helpers
def clamp(v, lo, hi):
  return max(lo, min(hi, v))


def r6(v):
  return round(float(v), 6)


def num(f, key, default=0.0):
  v = f.get(key, default)
  return (
      float(v)
      if isinstance(v, (int, float)) and not isinstance(v, bool)
      else default
  )


def vec4(f, key, default=0.0):
  v = f.get(key)
  if isinstance(v, list):
    v = [float(x) for x in v] + [default] * 4
    return v[:4]
  return [default] * 4


def mean_abs_uniform(lo, hi):
  """Mean |v| for v uniform in [lo, hi]."""
  if hi < lo:
    lo, hi = hi, lo
  if lo * hi >= 0:
    return (abs(lo) + abs(hi)) / 2.0
  return (lo * lo + hi * hi) / (2.0 * (hi - lo))


def axis_abs_range(lo, hi):
  """Smallest and largest |v| for v uniform in [lo, hi]."""
  if hi < lo:
    lo, hi = hi, lo
  low = 0.0 if lo <= 0.0 <= hi else min(abs(lo), abs(hi))
  return low, max(abs(lo), abs(hi))


def box_speed_range(ranges):
  """Real speed range (min, max) of a per-axis velocity box."""
  lows, highs = zip(*(axis_abs_range(lo, hi) for lo, hi in ranges))
  return (sum(v * v for v in lows) ** 0.5, sum(v * v for v in highs) ** 0.5)


def accel_mean(f):
  """Mean per-axis Accel of a particle. Delta is +/- unless the layer
  uses EliminateUnnecessaryRandomness (then start + 0..delta)."""
  a, d = vec4(f, "AccelStart"), vec4(f, "AccelDelta")
  if f.get("EliminateUnnecessaryRandomness"):
    return [a[i] + d[i] / 2.0 for i in range(3)]
  return [a[i] for i in range(3)]


def effective_gravity(f):
  """Carbon rule: Gravity replaces Accel when it is not 0. Otherwise the
  mean Z Accel becomes an equal and opposite Gravity (up = negative)."""
  g = num(f, "Gravity")
  if g != 0:
    return g
  return -accel_mean(f)[2]


def linear_drag(k, v0, life):
  """Linear BeamNG drag that matches Carbon's quadratic drag.

  Carbon applies dv = -v * k * |v| * dt, BeamNG applies dv = -v * c * dt.
  Solve c so both travel the same distance in `life` seconds from speed v0.
  """
  if k <= 0 or v0 <= 0 or life <= 0:
    return k
  target = math.log(1.0 + k * v0 * life) / k
  lo, hi = 0.0, k * v0  # c = k * v0 is the upper bound (short life limit)
  for _ in range(60):
    c = (lo + hi) / 2.0
    dist = v0 * (1.0 - math.exp(-c * life)) / c if c > 0 else v0 * life
    if dist > target:
      lo = c
    else:
      hi = c
  return (lo + hi) / 2.0


def is_fire_layer(name):
  """True when the effect part of the layer name has the fire keyword.
  The category prefix (emfire_, emwtr_ ...) is ignored."""
  effect = name.split("_", 1)[-1]
  return CONFIG["FIRE_KEYWORD"] in effect.lower()


def cycling(f):
  """Carbon rule: cycling runs only when there is an on time AND an off time."""
  on = num(f, "OnCycle") != 0 or num(f, "OnCycleVariance") != 0
  off = num(f, "OffCycle") != 0 or num(f, "OffCycleVariance") != 0
  return on and off and not f.get("IsOneShot")


def axis_is_none(f):
  """True when the layer has no AxisConstraint (or the value is NONE/0)."""
  v = str(f.get("AxisConstraint", "")).strip().upper()
  return v in ("", "0", "NONE", "CONSTRAIN_PARTICLE_NONE")


def flow_is_vertical(f):
  """True when particles move mostly along Z (nodes have identity rotation)."""
  if num(f, "Speed") != 0:
    return True  # cone velocity runs along the emitter local Z axis
  vs, vd = vec4(f, "VelocityStart"), vec4(f, "VelocityDelta")
  if f.get("EliminateUnnecessaryRandomness"):
    ranges = [(vs[i], vs[i] + vd[i]) for i in range(3)]
  else:
    ranges = [(vs[i] - vd[i], vs[i] + vd[i]) for i in range(3)]
  m3 = [mean_abs_uniform(lo, hi) for lo, hi in ranges]
  return m3[2] > (m3[0] ** 2 + m3[1] ** 2) ** 0.5


def linked_offset_applies(f):
  """RenderLinked + no axis constraint + vertical flow -> 90 degree spin."""
  return bool(f.get("RenderLinked")) and axis_is_none(f) and flow_is_vertical(f)


# ------------------------------------------------------------ layer -> data
def build_layer(layer, stats, warn):
  f = layer["fields"]
  C = CONFIG
  name = layer["name"]
  pname = C["PREFIX"] + name + "_p"
  ename = C["PREFIX"] + name + "_e"
  tex = layer.get("texture") or ""
  tex_path = C["TEX_DIR"] + tex + C["TEX_EXT"] if tex else ""

  additive = tex.upper().endswith("_ADDITIVE")
  stats["blend_" + ("additive" if additive else "alpha")] += 1
  if not additive and C["FORCE_ADDITIVE"]:
    additive = True

  # ---- ParticleData
  colors = [
      [r6(c / 255.0) for c in rgba] for rgba in layer.get("colors_rgba", [])
  ]
  if not colors:
    colors = [[1.0, 1.0, 1.0, 1.0]]
    warn.append("%s: no colors, fallback to white" % name)
  while len(colors) < 4:
    colors.append(list(colors[-1]))

  keys = vec4(f, "KeyPositions")
  if not any(keys):
    keys = [0.0, 1.0, 2.0, 3.0]
  times = [r6(k / 3.0) for k in keys]
  sizes = [r6(s * C["SIZE_SCALE"]) for s in vec4(f, "Size", 1.0)]

  life = min(num(f, "Life", 1.0), 60.0)
  lv = clamp(num(f, "LifeVariance"), 0.0, 1.0)
  life_mid = life * (1.0 - lv / 2.0)
  life_half = life * lv / 2.0

  # Rotation & Angle Conversion
  rot = num(f, "RotationalVelocity") * C["SPIN_SCALE"]
  ang = num(f, "InitialAngleRange")

  # If InitialAngleRange is negative, correct the rotational direction
  if ang < 0:
    rot = -rot
  ang_abs = abs(ang)

  rot_var = clamp(num(f, "RotationVariance"), 0.0, 1.0)
  if f.get("RandomRotationDirection"):
    rot_hi = abs(rot) * (1.0 + rot_var)
    rot_lo = -rot_hi
  else:
    # Preserve rotational sign
    if rot >= 0:
      rot_lo = rot * (1.0 - rot_var)
      rot_hi = rot * (1.0 + rot_var)
    else:
      rot_lo = rot * (1.0 + rot_var)
      rot_hi = rot * (1.0 - rot_var)

  # BeamNG requires spinInitialMin and spinInitialMax to be in [0, 360]
  if linked_offset_applies(f):
    half = min(ang_abs, 360.0) / 2.0
    center = C["LINKED_SPIN_OFFSET"]
    init_min = r6(clamp(center - half, 0.0, 360.0))
    init_max = r6(clamp(center + half, 0.0, 360.0))
    stats["linked_offset"] += 1
  else:
    init_min = 0.0
    init_max = r6(clamp(ang_abs, 0.0, 360.0))
    if f.get("RenderLinked"):
      stats["linked_skipped"] += 1
      warn.append(
          "%s: RenderLinked but axis constraint or non-vertical flow, "
          "no 90 degree offset" % name
      )

  particle = {
      "name": pname,
      "class": "ParticleData",
      "textureName": tex_path,
      "animTexName": tex_path,
      "colors": colors[:4],
      "sizes": sizes,
      "times": times,
      "lifetimeMS": int(round(life_mid * 1000)),
      "lifetimeVarianceMS": int(round(life_half * 1000)),
      "dragCoefficient": r6(num(f, "Drag") * C["DRAG_SCALE"]),
      "gravityCoefficient": r6(effective_gravity(f) * C["GRAVITY_SCALE"]),
      "inheritedVelFactor": r6(clamp(num(f, "MotionInherit"), 0.0, 1.0)),
      "spinSpeed": 1,
      "spinRandomMin": r6(rot_lo),
      "spinRandomMax": r6(rot_hi),
      "spinInitialMin": init_min,
      "spinInitialMax": init_max,
      "useInvAlpha": not additive,
  }

  anim = f.get("TextureAnimation") or {}
  m = re.search(r"(\d+)x(\d+)", str(anim.get("AnimType", "")))
  if m:
    nx, ny = int(m.group(1)), int(m.group(2))
    particle["animateTexture"] = True
    particle["animTexTiling"] = [nx, ny]
    particle["animTexFrames"] = "0-%d" % (nx * ny - 1)
    particle["framesPerSec"] = int(anim.get("FPS", 0) or 0)
    stats["animated"] += 1

  # ---- ParticleEmitterData
  npv = clamp(num(f, "NumParticlesVariance"), 0.0, 1.0)
  rate = num(f, "NumParticles") * (1.0 - npv)
  if rate <= 0:
    warn.append("%s: emission rate is 0, layer skipped" % name)
    return None

  # Volume layers use several nodes; each node emits rate / nodes
  nodes_per = max(1, scatter_nodes(f))
  raw_period = 1000.0 * nodes_per / rate

  # Clamped strictly with minimum of 1 and maximum of 2047
  period_ms = int(
      clamp(round(raw_period), C["MIN_PERIOD_MS"], C["MAX_PERIOD_MS"])
  )

  if is_fire_layer(name):
    period_ms = int(clamp(C["FIRE_PERIOD_MS"], C["MIN_PERIOD_MS"], C["MAX_PERIOD_MS"]))
    raw_period = period_ms
    stats["fire_period"] += 1

  if raw_period < C["MIN_PERIOD_MS"]:
    warn.append("%s: rate %.0f/s clamped to 1000/s" % (name, rate))
  elif raw_period > C["MAX_PERIOD_MS"]:
    warn.append(
        "%s: emission period (%.1f ms) clamped to %d ms"
        % (name, raw_period, C["MAX_PERIOD_MS"])
    )

  if f.get("IsOneShot"):
    warn.append("%s: IsOneShot layer treated as continuous" % name)

  speed = num(f, "Speed")
  spread = num(f, "SpreadAngle")
  if speed != 0:
    sv = clamp(num(f, "SpeedVariance"), 0.0, 1.0)
    vel = abs(speed) * (1.0 - sv / 2.0)
    vvar = abs(speed) * sv / 2.0
    if f.get("SpreadAsDisc"):
      tmin, tmax = 90.0 - spread / 2.0, 90.0 + spread / 2.0
    else:
      tmin, tmax = 0.0, spread / 2.0
    stats["vel_cone"] += 1
  else:
    vs, vd = vec4(f, "VelocityStart"), vec4(f, "VelocityDelta")
    if f.get("EliminateUnnecessaryRandomness"):
      ranges = [(vs[i], vs[i] + vd[i]) for i in range(3)]
    else:
      ranges = [(vs[i] - vd[i], vs[i] + vd[i]) for i in range(3)]
    m3 = [mean_abs_uniform(lo, hi) for lo, hi in ranges]
    horiz = (m3[0] ** 2 + m3[1] ** 2) ** 0.5
    # Real speed range of the box, not a fixed 50 percent spread
    vmin, vmax = box_speed_range(ranges)
    vel = (vmin + vmax) / 2.0
    vvar = (vmax - vmin) / 2.0
    if vel == 0:
      tmin = tmax = 0.0
    elif m3[2] > horiz:
      tmin, tmax = 0.0, 20.0
    else:
      tmin, tmax = 60.0, 120.0
    stats["vel_box"] += 1
    if vel > 0:
      stats["vel_box_moving"] += 1

  # Carbon drag is quadratic (scales with speed), BeamNG drag is linear
  drag = num(f, "Drag")
  if drag > 0 and vel > 0:
    particle["dragCoefficient"] = r6(
        linear_drag(drag, vel * C["SPEED_SCALE"], life_mid) * C["DRAG_SCALE"]
    )
    stats["drag_converted"] += 1
    if is_fire_layer(name) and C["FIRE_MEAN_SPEED"]:
      # Distance the game particle covers in `life_mid`, as a constant speed
      vel = math.log(1.0 + drag * vel * life_mid) / drag / life_mid
      stats["fire_mean_speed"] += 1

  if num(f, "Gravity") == 0 and (
      any(vec4(f, "AccelStart")) or any(vec4(f, "AccelDelta"))
  ):
    am = accel_mean(f)
    if am[2] != 0:
      stats["accel_z_applied"] += 1
    if am[0] != 0 or am[1] != 0:
      stats["accel_xy_ignored"] += 1
      warn.append(
          "%s: horizontal Accel (%.2f, %.2f) not ported, only Z is applied"
          % (name, am[0], am[1])
      )

  emitter = {
      "name": ename,
      "class": "ParticleEmitterData",
      "particles": pname,
      "blendStyle": C["BLEND_ADDITIVE"] if additive else C["BLEND_ALPHA"],
      "ejectionPeriodMS": period_ms,  # Guaranteed >= 1
      "ejectionVelocity": r6(vel * C["SPEED_SCALE"]),
      "velocityVariance": r6(vvar * C["SPEED_SCALE"]),
      "ejectionOffset": 0,
      "thetaMin": r6(clamp(tmin, 0.0, 180.0)),
      "thetaMax": r6(clamp(tmax, 0.0, 180.0)),
      "phiVariance": 360,
      "orientParticles": False,
      "orientOnVelocity": False,
      "sortParticles": not additive,
      "useLighting": False,
  }
  return pname, particle, ename, emitter


# -------------------------------------------------------------------- core
def run(json_path):
  with open(json_path, "r") as fh:
    data = json.load(fh)
  out_dir = os.path.join(
      os.path.dirname(os.path.abspath(json_path)), "beamng_out"
  )
  os.makedirs(out_dir, exist_ok=True)

  stats, warn = Counter(), []
  particles, emitters, layer_info = {}, {}, {}
  linked_info, axis_info, scatter_info = [], [], []
  for lname, layer in data["layers"].items():
    res = build_layer(layer, stats, warn)
    if res is None:
      stats["layers_skipped"] += 1
      continue
    pname, pd, ename, ed = res
    particles[pname] = pd
    emitters[ename] = ed
    layer_info[lname] = ename
    lf = layer["fields"]
    sn = scatter_nodes(lf)
    if sn:
      scatter_info.append(
          "  %s: box %s, %d node(s) per placement"
          % (lname, [round(v, 2) for v in vec4(lf, "VolumeExtent")[:3]], sn)
      )
    if lf.get("RenderLinked"):
      linked_info.append(
          "  %s: %s"
          % (
              lname,
              "90 degree offset"
              if linked_offset_applies(lf)
              else "no offset (axis constraint or non-vertical flow)",
          )
      )
    if not axis_is_none(lf):
      axis_info.append(
          "  %s: %s (texture %s)"
          % (lname, lf.get("AxisConstraint"), layer.get("texture"))
      )

  nodes = []
  C = CONFIG
  for i, p in enumerate(data["placements"]):
    g = data["groups"].get(p["group"]) if p["group"] else None
    if not g:
      stats["placements_skipped"] += 1
      continue
    for j, lname in enumerate(g["layers"]):
      if lname not in layer_info:
        continue
      f = data["layers"][lname]["fields"]

      # Shift world position with +150 height
      x, y, z = map_pos(p["x"], p["y"], p["z"])
      rot = placement_rot(p)
      if is_tilted(rot):
        stats["nodes_tilted"] += 1
      elif rot != IDENTITY_ROT:
        stats["nodes_yawed"] += 1
      if C["APPLY_VOLUME_CENTER"]:
        vc = vec4(f, "VolumeCenter")
        dx, dy, dz = rotate_local(rot, vc)
        x += dx
        y += dy
        z += dz

      node = {
          "name": "%sfx_%04d_%d" % (C["PREFIX"], i, j),
          "class": "ParticleEmitterNode",
          "__parent": C["PARENT_GROUP"],
          "position": [r6(x), r6(y), r6(z)],
          "dataBlock": C["NODE_DATABLOCK"],
          "emitter": layer_info[lname],
          "active": True,
          "rotationMatrix": torque_matrix(rot),
          "sectionID": p["sectionID"],
          "fxGroup": p["group"],
          "fxLayer": lname,
      }
      # Carbon FarClip (meters). The section streamer hides the node beyond it.
      if num(f, "FarClip") > 0:
        node["farClip"] = num(f, "FarClip")
      if cycling(f):
        node["cycleOn"] = num(f, "OnCycle")
        node["cycleOnVar"] = num(f, "OnCycleVariance")
        node["cycleOff"] = num(f, "OffCycle")
        node["cycleOffVar"] = num(f, "OffCycleVariance")
        stats["nodes_cycling"] += 1
      if num(f, "StartDelay") > 0:
        node["cycleDelay"] = num(f, "StartDelay")
        node["cycleDelayRandom"] = bool(f.get("StartDelayRandomVariance"))
      sn = scatter_nodes(f)
      if sn:
        # Dynamic fields are strings. The Lua module reads them.
        node["volExtent"] = fmt_floats(vec4(f, "VolumeExtent")[:3])
        node["volAxes"] = fmt_floats(rot)
        node["volBase"] = fmt_floats([x, y, z])
        for k in range(1, sn):
          twin = dict(node)
          twin["name"] = "%s_v%d" % (node["name"], k)
          nodes.append(twin)
        stats["scatter_nodes"] += sn
      nodes.append(node)

  # Node Datablock Definition (timeMultiple = 1.0)
  node_datablock_def = {
      C["NODE_DATABLOCK"]: {
          "name": C["NODE_DATABLOCK"],
          "class": "ParticleEmitterNodeData",
          "timeMultiple": 1.0,
      }
  }

  # 1. Write managedParticleData.json
  with open(os.path.join(out_dir, FILE_MANAGED_PARTICLES), "w") as fh:
    json.dump(particles, fh, indent=2)

  # 2. Write managedParticleEmitterData.json
  managed_emitters = {}
  managed_emitters.update(node_datablock_def)
  managed_emitters.update(emitters)
  with open(os.path.join(out_dir, FILE_MANAGED_EMITTERS), "w") as fh:
    json.dump(managed_emitters, fh, indent=2)

  # 3. Write unified nfsc.datablocks.json
  blocks = {}
  blocks.update(node_datablock_def)
  blocks.update(particles)
  blocks.update(emitters)
  with open(os.path.join(out_dir, FILE_UNIFIED_DATABLOCKS), "w") as fh:
    json.dump(blocks, fh, indent=2)

  # 4. Write items.level.json (JSONL)
  with open(os.path.join(out_dir, FILE_LEVEL_NODES), "w") as fh:
    for n in nodes:
      fh.write(json.dumps(n, separators=(",", ":")) + "\n")

  # 5. Build Report
  lines = [
      "Generated files in beamng_out/:",
      "  1. %s (%d ParticleData)"
      % (FILE_MANAGED_PARTICLES, len(particles)),
      "  2. %s (%d ParticleEmitterData + 1 NodeData)"
      % (FILE_MANAGED_EMITTERS, len(emitters)),
      "  3. %s (%d Datablocks total)"
      % (FILE_UNIFIED_DATABLOCKS, len(blocks)),
      "  4. %s (%d ParticleEmitterNode instances)"
      % (FILE_LEVEL_NODES, len(nodes)),
      "",
      "Configuration Used:",
      "  Texture Directory:           %s" % C["TEX_DIR"],
      "  Height offset applied:       +%.1f" % C["HEIGHT_OFFSET"],
      "  ejectionPeriodMS Clamp:      [%d, %d]"
      % (C["MIN_PERIOD_MS"], C["MAX_PERIOD_MS"]),
      "  spinInitial Range:           [0.0, 360.0] (positive angles)",
      "",
      "Statistics:",
      "  Placements read:             %d" % len(data["placements"]),
      "  Layers skipped:              %d" % stats["layers_skipped"],
      (
          "  Blend modes:                 alpha=%d, additive=%d"
          % (stats["blend_alpha"], stats["blend_additive"])
      ),
      "  Animated textures:           %d" % stats["animated"],
      (
          "  Velocities:                  cone=%d, box=%d (moving=%d)"
          % (stats["vel_cone"], stats["vel_box"], stats["vel_box_moving"])
      ),
      "  Nodes with on/off cycle:     %d" % stats["nodes_cycling"],
      "  Volume scatter node total:   %d" % stats["scatter_nodes"],
      (
          "  Node rotations:              yaw-only=%d, tilted=%d"
          % (stats["nodes_yawed"], stats["nodes_tilted"])
      ),
      "  Drag converted (quadratic -> linear): %d" % stats["drag_converted"],
      (
          "  Fire layers: period fixed=%d, mean speed=%d"
          % (stats["fire_period"], stats["fire_mean_speed"])
      ),
      (
          "  Accel (Gravity 0 layers):    Z applied=%d, horizontal ignored=%d"
          % (stats["accel_z_applied"], stats["accel_xy_ignored"])
      ),
      (
          "  RenderLinked layers:         offset=%d, skipped=%d"
          % (stats["linked_offset"], stats["linked_skipped"])
      ),
      "",
      "RenderLinked layers:",
  ] + (linked_info or ["  (none)"]) + [
      "",
      "Volume scatter layers (need emitter_volume_scatter.lua):",
  ] + (scatter_info or ["  (none)"]) + [
      "",
      "Layers with AxisConstraint other than NONE:",
  ] + (axis_info or ["  (none)"]) + [""] + ["WARNING: " + w for w in warn]

  report = "\n".join(lines)
  with open(os.path.join(out_dir, "beamng_build_report.txt"), "w") as fh:
    fh.write(report + "\n")
  return report, out_dir


def main():
  path = sys.argv[1] if len(sys.argv) > 1 else None
  tk = None
  if path is None:
    try:
      import tkinter as tk
      from tkinter import filedialog, messagebox

      root = tk.Tk()
      root.withdraw()
      path = filedialog.askopenfilename(
          title="Pick emitters_resolved.json",
          filetypes=[("JSON", "*.json"), ("All files", "*.*")],
      )
      if not path:
        return
    except ImportError:
      path = input("Path to emitters_resolved.json: ").strip().strip('"')

  report, out_dir = run(path)
  print(report)
  print("\nDone! Files written to:", out_dir)
  if tk:
    messagebox.showinfo(
        "Particle build done", (report + "\n\n" + out_dir)[:1800]
    )


if __name__ == "__main__":
  main()