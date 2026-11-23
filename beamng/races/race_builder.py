#!/usr/bin/env python3
"""
race_builder.py - Carbon races (from gameplay/race_scan.py) to BeamNG mission folders.

For each selected race it writes, under <out>/gameplay/missions/<LEVEL>/<missionType>/<id>/ :
    info.json          mission definition (format taken from the West Coast USA aiRace/timeTrial missions)
    race.race.json     race path: pathnodes, segments, start positions, classification
    carbon_source.json where every node came from (Carbon collection names, source rotation, dimensions)
    race_groups.json   the scenery groups the race switches (only when it has any): enable (name + flip) and
                       disable lists, read by the level host's barrierSets component when the mission starts
It reads only the race_scan files (races.tsv, race_objects.tsv, race_barriers.tsv); it never reads game files.

Usage:
    python beamng/races/race_builder.py                         (uses outputs/race_scan/)
    python beamng/races/race_builder.py --scan E:/scan --race race_bin_muscle/2_1_3
    python beamng/races/race_builder.py --event-id mu.1.3 --out E:/levels_out
Default output: outputs/race_builder/. Copy its gameplay/ folder into the BeamNG user folder or level mod.

What a race becomes (route races only; drift, pursuit, challenge and drag are not built here):
    circuit                  closed loop (lap line = the finish line), laps = NumLaps
    p2p, canyon, canyonsprint, tollboothrace, speedtraprace   start -> checkpoints -> finish
    mission type: aiRace when the race has opponents and is circuit/p2p/canyon/canyonsprint, else timeTrial
Shortcuts, corners, drafts, time bonuses, flares and barriers are NOT in the path yet (they stay in the
middle files for the next steps); the number of skipped shortcuts is reported.

Conversions (settled; change the constants below if a test says otherwise):
    position  : (x, y, z + HEIGHT_OFFSET), identity axes, same as the particle and flare exporters
    heading   : Rotation is degrees from +X towards +Y; node normal = (cos, sin, 0)
    quaternion: yaw about Z = heading + an offset that depends on the Carbon race type (START_YAW_OFFSET_BY_TYPE):
                +90 for circuit, p2p, canyon, canyonsprint; 0 for tollbooth and speedtrap. Tested in game on
                2_1_3, 2_3_3 (0 right), 2_3_2, 3_3_2 (+90 right).
    radius    : pathnode radius = gate width (Carbon Dimensions Y) * RADIUS_FACTOR. Carbon gates are boxes
                (typically 40 to 80 wide, 40 tall); a pathnode only has a radius, so this is a first guess to test.
"""
# stream bootstrap: make the shared library folders importable
import pathlib as _pl, sys as _sys
_sys.path[:0] = [str(_pl.Path(__file__).resolve().parents[2] / _d) for _d in ['common', 'region', 'scenery', 'solids_materials']]

import argparse
import csv
import json
import math
import re
import time

from nfs_outputs import out_dir

# ---- settings -----------------------------------------------------------------------------------
LEVEL = "nfsc"
HEIGHT_OFFSET = 150.0          # added to every Z, as in the particle and flare exporters
# Tested in game (start trigger preview and spawn): circuit, p2p, canyon and canyonsprint races need heading + 90,
# tollbooth and speedtrap races use the heading as is. Keyed on the Carbon race type; if the races of those types
# that are built as timeTrial (no opponents, e.g. 1_2_2, 8_3_1, 1_3_1) turn out to follow the mission type
# instead, the table has to be keyed by mission type (pass mtype instead of rtype to the lookup in build_path).
START_YAW_OFFSET_BY_TYPE = {"circuit": 90.0, "p2p": 90.0, "canyon": 90.0, "canyonsprint": 90.0,
                            "tollboothrace": 0.0, "speedtraprace": 0.0}
START_YAW_OFFSET_DEFAULT = 0.0
RADIUS_FACTOR = 0.5            # pathnode radius = Dimensions.Y * this
MIN_RADIUS = 4.0
SIDE_PADDING = [1, 3]          # copied from the West Coast USA pathnodes
RACER_GAP = 9.0                # meters between racer start rows
RACER_SIDE = 3.5               # lateral offset of the racer start slots
MAX_RACERS = 5
START_TRIGGER_RADIUS = 3

ROUTE_TYPES = {"circuit", "p2p", "canyon", "canyonsprint", "tollboothrace", "speedtraprace"}
AIRACE_TYPES = {"circuit", "p2p", "canyon", "canyonsprint"}
CLOSED_TYPES = {"circuit"}


def num(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def read_tsv(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


def groups_of(rows):
    """race_barriers.tsv rows of one race -> race_groups.json content (None when the race has no groups)."""
    enable, disable, seen = [], [], set()
    for r in rows:
        name = r["group_name"]
        if r["source"] == "DisableSelectionSet":
            if ("d", name) not in seen:
                seen.add(("d", name))
                disable.append(name)
        elif ("e", name) not in seen:
            seen.add(("e", name))
            enable.append({"name": name, "flip": r["flip"] == "1"})
    if not enable and not disable:
        return None
    return {"version": 1, "gameMode": "racing", "enable": enable, "disable": disable}


def mission_id(race):
    """race_bin_muscle/2_1_3 -> muscle_2_1_3"""
    b, n = race.split("/", 1)
    b = b[len("race_bin_"):] if b.startswith("race_bin_") else b
    return re.sub(r"[^A-Za-z0-9_\-]", "_", "%s_%s" % (b, n))


def pos3(o):
    return [num(o["x"]), num(o["y"]), num(o["z"]) + HEIGHT_OFFSET]


def normal_of(o):
    deg = num(o["rotation_deg"], 0.0)
    a = math.radians(deg)
    return [round(math.cos(a), 9), round(math.sin(a), 9), 0.0]


def quat_for_heading(deg, offset=0.0):
    y = math.radians(deg + offset)
    return [0.0, 0.0, round(math.sin(y / 2), 9), round(math.cos(y / 2), 9)]


def offset_pos(p, heading_deg, back, side):
    a = math.radians(heading_deg)
    fx, fy = math.cos(a), math.sin(a)
    sx, sy = -fy, fx            # left of travel
    return [p[0] - fx * back + sx * side, p[1] - fy * back + sy * side, p[2]]


class Ids:
    def __init__(self, start=2):
        self.n = start - 1

    def next(self):
        self.n += 1
        return self.n


def build_path(race, rtype, objs, laps, opponents):
    """-> (race.race.json dict, carbon_source dict, skipped info). objs are race_objects rows in order."""
    start = next(o for o in objs if o["role"] == "start")
    finish = next((o for o in objs if o["role"] == "finish"), None)
    cps = [o for o in objs if o["role"] == "checkpoint"]
    skipped = {"shortcut": sum(1 for o in objs if o["role"] == "shortcut"),
               "corner": sum(1 for o in objs if o["role"] == "corner"),
               "draft": sum(1 for o in objs if o["role"] == "draft")}
    closed = rtype in CLOSED_TYPES
    ids = Ids(2)
    off = START_YAW_OFFSET_BY_TYPE.get(rtype, START_YAW_OFFSET_DEFAULT)

    # --- start positions first, so their ids come first like in the BeamNG samples
    s_head = num(start["rotation_deg"], 0.0)
    s_pos = pos3(start)
    start_positions = []
    if opponents > 0 and rtype in AIRACE_TYPES:
        start_positions.append({"name": "Player Start", "oldId": ids.next(), "pos": s_pos, "rot": quat_for_heading(s_head, off)})
        for i in range(1, min(opponents, MAX_RACERS) + 1):
            row = (i + 1) // 2
            side = RACER_SIDE if i % 2 else -RACER_SIDE
            start_positions.append({"name": "Racer Start %d" % i, "oldId": ids.next(),
                                    "pos": offset_pos(s_pos, s_head, RACER_GAP * row, side), "rot": quat_for_heading(s_head, off)})
        default_start = start_positions[0]["oldId"]
        rev = roll = rollrev = -1
    else:
        fwd = {"group": "start", "name": "Start Position fwd", "oldId": ids.next(), "pos": s_pos, "rot": quat_for_heading(s_head, off)}
        rev = {"group": "start", "name": "Start Position rev", "oldId": ids.next(), "pos": s_pos, "rot": quat_for_heading(s_head + 180, off)}
        rol = {"group": "start", "name": "Start Position fwd roll", "oldId": ids.next(), "pos": s_pos, "rot": quat_for_heading(s_head, off)}
        rolr = {"group": "start", "name": "Start Position rev roll", "oldId": ids.next(), "pos": s_pos, "rot": quat_for_heading(s_head + 180, off)}
        start_positions += [fwd, rev, rol, rolr]
        default_start, rev, roll, rollrev = fwd["oldId"], rev["oldId"], rol["oldId"], rolr["oldId"]

    # --- route objects in driving order
    if closed:
        lap_obj = finish or start
        route = [("Lap line", lap_obj)] + [("Checkpoint %02d" % (i + 1), c) for i, c in enumerate(cps)]
    else:
        route = [("Start", start)] + [("Checkpoint %02d" % (i + 1), c) for i, c in enumerate(cps)]
        if finish is not None:
            route.append(("Finish", finish))

    nodes, source_nodes = [], []
    for label, o in route:
        nid = ids.next()
        width = num(o["dim_y"], 0.0) or 0.0
        radius = max(MIN_RADIUS, round(width * RADIUS_FACTOR, 6))
        node = {"customFields": {"names": {}, "tags": {}, "types": {}, "values": {}},
                "mode": "manual", "name": label, "navRadiusScale": 1, "normal": normal_of(o), "oldId": nid,
                "pos": pos3(o), "radius": radius, "recovery": -1, "reverseRecovery": -1,
                "sidePadding": list(SIDE_PADDING), "visible": True}
        nodes.append(node)
        source_nodes.append({"oldId": nid, "name": label, "carbon": o["collection"], "class": o["parent_class"],
                             "rotation_deg": num(o["rotation_deg"]), "dimensions": [num(o["dim_x"]), num(o["dim_y"]), num(o["dim_z"])],
                             "time_bonus": num(o["time_bonus"]), "particle_effect": o["particle_effect"] or None,
                             "flare_spacing": num(o["flare_spacing"])})

    # recovery positions: one forward and one reverse per node (BeamNG uses them for resets)
    for node in nodes:
        h = math.degrees(math.atan2(node["normal"][1], node["normal"][0]))
        f = {"group": "recovery", "name": "%s Recovery Forward" % node["name"], "oldId": ids.next(), "pos": node["pos"], "rot": quat_for_heading(h, off)}
        r = {"group": "recovery", "name": "%s Recovery Reverse" % node["name"], "oldId": ids.next(), "pos": node["pos"], "rot": quat_for_heading(h + 180, off)}
        start_positions += [f, r]
        node["recovery"], node["reverseRecovery"] = f["oldId"], r["oldId"]

    segments = []
    pairs = list(zip(nodes[:-1], nodes[1:]))
    if closed and len(nodes) > 1:
        pairs.append((nodes[-1], nodes[0]))
    for a, b in pairs:
        segments.append({"capsules": {}, "from": a["oldId"], "mode": "waypoint", "name": "Segment %d" % len(segments),
                         "oldId": ids.next(), "to": b["oldId"]})

    path = {"authors": "Carbon export", "classification": {"allowRollingStart": False, "branching": False,
                                                             "closed": closed, "reversible": False},
            "date": int(time.time()), "defaultLaps": max(1, laps if closed else 1),
            "defaultStartPosition": default_start, "description": "Carbon %s" % race, "difficulty": 50,
            "endNode": -1 if closed or finish is None else nodes[-1]["oldId"],
            "forwardPrefabs": {}, "hideMission": False, "name": race, "pacenotes": {},
            "pathnodes": nodes, "prefabs": {}, "reversePrefabs": {},
            "reverseStartPosition": rev, "rollingReverseStartPosition": rollrev, "rollingStartPosition": roll,
            "segments": segments, "simplifyAiPath": False, "startNode": nodes[0]["oldId"], "startPositions": start_positions}
    source = {"race": race, "type": rtype, "closed": closed, "nodes": source_nodes,
              "start_grid": {"carbon": start["collection"], "rotation_deg": s_head},
              "notes": "z includes HEIGHT_OFFSET %s; shortcuts/corners/drafts not in the path" % HEIGHT_OFFSET}
    return path, source, skipped


def build_info(race, rtype, mtype, mid, race_row, path, laps, opponents):
    sp = next(s for s in path["startPositions"] if s["oldId"] == path["defaultStartPosition"])
    record = num(race_row.get("track_record"))
    length = num(race_row.get("race_length"), 0.0)
    time_limit = num(race_row.get("time_limit"))
    gold = record if record and record < 5000 else max(60.0, length / 40.0)
    base = "/gameplay/missions/%s/%s/%s/" % (LEVEL, mtype, mid)
    info = {"additionalAttributes": {}, "author": "Carbon export", "customAdditionalAttributes": {},
            "date": int(time.time()), "description": "Carbon %s (%s)" % (race_row.get("event_id") or race, rtype),
            "devMission": False, "grouping": {"id": "", "label": ""},
            "layers": [{"dir": base, "fixed": True, "isMissionFolderDir": True, "isMissionTypeDir": False},
                       {"dir": "/gameplay/missionTypes/%s/" % mtype, "fixed": True, "isMissionFolderDir": False, "isMissionTypeDir": True}],
            "missionType": mtype, "name": "%s %s" % (race_row.get("event_id") or "", race.split("/")[-1]),
            "retryBehaviour": "infiniteRetries",
            "setupModules": {"environment": {"enabled": False}, "traffic": {"enabled": False},
                             "vehicles": {"enabled": True, "includePlayerVehicle": True, "playerVehicleDiffs": {},
                                          "prioritizePlayerVehicle": True, "vehicles": {}}},
            "startCondition": {"type": "automatic"},
            "startTrigger": {"level": LEVEL, "pos": sp["pos"], "radius": START_TRIGGER_RADIUS, "rot": sp["rot"], "type": "coordinates"},
            "careerSetup": {"showInCareer": False, "showInFreeroam": True, "skill": "(none)", "defaultStarKeys": [],
                            "starOutroTexts": {}, "starRewards": {}, "starsActive": {}},
            "visibleCondition": {"type": "always"}}
    closed = path["classification"]["closed"]
    if mtype == "timeTrial":
        info["missionTypeData"] = {"allowFlip": True, "allowRecover": True, "allowRollingStart": False,
                                   "canChangeLapAmount": closed, "closed": closed, "defaultLaps": path["defaultLaps"],
                                   "flipLimit": -1, "recoverLimit": -1, "flipPenalty": 5, "recoverPenalty": 5,
                                   "goldTime": round(gold), "silverTime": round(gold * 1.15), "bronzeTime": round(gold * 1.35),
                                   "goldTimePenalty": 0, "silverTimePenalty": 10, "bronzeTimePenalty": 20, "justFinishPenalty": 30,
                                   "mapPreviewMode": "navgraph", "reversible": False, "startScreenText": ""}
        info["missionTypeData"]["goldTimeTotal"] = info["missionTypeData"]["goldTime"] * (path["defaultLaps"] if closed else 1)
        info["missionTypeData"]["silverTimeTotal"] = info["missionTypeData"]["silverTime"] * (path["defaultLaps"] if closed else 1)
        info["missionTypeData"]["bronzeTimeTotal"] = info["missionTypeData"]["bronzeTime"] * (path["defaultLaps"] if closed else 1)
    else:
        racers = min(opponents, MAX_RACERS)
        info["missionTypeData"] = {"allowFlip": True, "allowRecover": True, "camPathActive": False, "damageFailActive": False,
                                   "damageLimit": 10000, "flipLimit": -1, "goalCollisionsLimit": 1, "goalCollisionsTarget": 5,
                                   "goalLaps": path["defaultLaps"], "goalRecoveries": 0, "goalTime": round(gold * 1.35),
                                   "lapCount": path["defaultLaps"], "mapPreviewMode": "navgraph",
                                   "maxRacerCount": racers, "minRacerCount": 1, "playerStartIndex": 0, "prefabActive": False,
                                   "raceFile": base + "race.race.json", "racerAggression": 1.0, "racerAwareness": True,
                                   "racerCount": max(1, racers), "racerRandomnessScale": 1, "racerRough": False,
                                   "racerRubberbandMode": False, "racerSkill": 1, "recoverLimit": -1, "shuffleGroup": True,
                                   "smartStartPlace": False, "startText": "", "stoppedLimit": 0, "useGroundmarkers": False,
                                   "vehicleGroupActive": False, "vehicleGroupByClassActive": False, "vehiclePerformanceOffset": 5}
    if mtype == "timeTrial":
        info["missionTypeData"]["raceFile"] = base + "race.race.json"
    return info


def main():
    ap = argparse.ArgumentParser(description="Carbon races (race_scan files) to BeamNG mission folders.")
    ap.add_argument("--scan", help="race_scan output folder (default outputs/race_scan/)")
    ap.add_argument("--out", help="output folder (default outputs/race_builder/)")
    ap.add_argument("--race", action="append", help="race collection, e.g. race_bin_muscle/2_1_3 (repeatable)")
    ap.add_argument("--event-id", action="append", help="event id, e.g. mu.1.3 (repeatable)")
    args = ap.parse_args()

    scan = _pl.Path(args.scan) if args.scan else out_dir("race_scan")
    out = _pl.Path(args.out) if args.out else out_dir("race_builder")
    races = read_tsv(scan / "races.tsv")
    by_race = {}
    for o in read_tsv(scan / "race_objects.tsv"):
        by_race.setdefault(o["race"], []).append(o)

    groups_by_race = {}
    if (scan / "race_barriers.tsv").is_file():
        for b in read_tsv(scan / "race_barriers.tsv"):
            groups_by_race.setdefault(b["race"], []).append(b)
    want_race = set(args.race or [])
    want_event = set(args.event_id or [])
    index, report = [], []
    for r in races:
        rn = r["race"]
        if want_race and rn not in want_race:
            continue
        if want_event and r["event_id"] not in want_event:
            continue
        rtype = r["type"]
        if rtype not in ROUTE_TYPES:
            report.append("skip %s: type %s is not a route race here" % (rn, rtype))
            continue
        objs = by_race.get(rn, [])
        if not any(o["role"] == "start" for o in objs) or not any(o["role"] == "checkpoint" for o in objs):
            report.append("skip %s: no start or no checkpoints" % rn)
            continue
        if rtype not in CLOSED_TYPES and not any(o["role"] == "finish" for o in objs):
            report.append("skip %s: no finish line" % rn)
            continue
        opponents = int(num(r.get("n_opponents"), 0) or 0)
        laps = int(num(r.get("num_laps"), 1) or 1)
        mtype = "aiRace" if (opponents > 0 and rtype in AIRACE_TYPES) else "timeTrial"
        mid = mission_id(rn)
        path, source, skipped = build_path(rn, rtype, objs, laps, opponents)
        info = build_info(rn, rtype, mtype, mid, r, path, laps, opponents)
        d = out / "gameplay" / "missions" / LEVEL / mtype / mid
        d.mkdir(parents=True, exist_ok=True)
        files = [("info.json", info), ("race.race.json", path), ("carbon_source.json", source)]
        groups = groups_of(groups_by_race.get(rn, []))
        if groups:
            files.append(("race_groups.json", groups))
        for name, data in files:
            (d / name).write_text(json.dumps(data, indent=1), encoding="utf-8")
        index.append([rn, r["event_id"], rtype, mtype, mid, len(path["pathnodes"]), path["classification"]["closed"],
                      path["defaultLaps"], skipped["shortcut"], skipped["corner"], skipped["draft"],
                      (len(groups["enable"]), len(groups["disable"])) if groups else (0, 0)])
    with open(out / "missions_index.tsv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, delimiter="\t", lineterminator="\n")
        w.writerow(["race", "event_id", "type", "mission_type", "mission_id", "nodes", "closed", "laps",
                    "shortcuts_not_built", "corners_not_built", "drafts_not_built", "groups_enable_disable"])
        w.writerows([r[:-1] + ["%d/%d" % r[-1]] for r in index])
    (out / "race_builder_report.txt").write_text("built %d missions\n%s\n" % (len(index), "\n".join(report)), encoding="utf-8")
    print("built %d missions, %d skipped; written to %s" % (len(index), len(report), out))


if __name__ == "__main__":
    main()
