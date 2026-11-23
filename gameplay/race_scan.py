#!/usr/bin/env python3
"""
race_scan.py - Carbon race events from the vault export (gameplay yml) to plain TSV/JSON middle files.

Reads <vault>/exported/main/gameplay/*/gameplay.yml. The ten race_bin_* folders hold the race roots
(178 of them); each event's checkpoints, start grid, finish line and shortcuts live in the folder
named like the race (race_bin_muscle/2_1_3 -> gameplay/2_1_3/gameplay.yml). Records inherit fields
from their ParentName chain (gpcore templates), so every field is resolved up the chain.

Usage:
    python gameplay/race_scan.py <vault folder>
    python gameplay/race_scan.py <vault folder> --out E:/somewhere
<vault folder> is the folder that contains exported/ (or exported/main, or its gameplay folder).
Default output: outputs/race_scan/

Output:
  races.tsv          one row per race root: event id, type, region, length, time limit, traffic, counts
  race_objects.tsv   ordered route objects per race: start, checkpoint, finish, shortcut, corner, draft
                     with position, rotation (degrees), resolved dimensions, time bonus and the
                     resolved trigger flare (ParticleEffect, FlareSpacing and where each came from)
  race_barriers.tsv  barrier selectors per race: Barriers (signed number) and BarriersExtra (string with
                     a leading * ) as group name, BIN hash, flip
  barrier_groups.tsv every distinct barrier group name with its BIN hash, flip uses and races
  race_scan_report.txt  counts and every warning

Settled readings (checked on the real vault):
  - Positions are X, Y, Z with Z up; Rotation is degrees measured from +X towards +Y (a gate's forward
    is (cos, sin)). Checked against the route order of all races: the checkpoint-to-checkpoint direction
    agrees with this heading far more than with any other axis choice.
  - Barriers: a negative number means flip, name BARRIER_SPLINE_<abs>. BarriersExtra: a leading * means flip.
  - A flare comes from the resolved ParticleEffect (empty = none); FlareSpacing counts only when an effect
    exists. Most triggers inherit spacing 5 from the template instead of writing it.
Not read here: NIS, stategraph handlers, opponents, drift section triggers.
"""
# stream bootstrap: make the shared library folders importable
import pathlib as _pl, sys as _sys
_sys.path[:0] = [str(_pl.Path(__file__).resolve().parents[1] / _d) for _d in ['common', 'region', 'scenery', 'solids_materials']]

import argparse
import csv
import json
import math
import os
from collections import Counter, defaultdict

from nfs_hashing import bin_hash
from nfs_outputs import out_dir
from nfs_vault_yaml import load

ROUTE_ARRAYS = [("Checkpoint", "checkpoint"), ("Shortcuts", "shortcut"), ("Corners", "corner"), ("Drafts", "draft")]
MAX_PARENT_DEPTH = 40
RACE_BIN_PREFIX = "race_bin_"


def find_gameplay_dir(path):
    base = _pl.Path(path)
    for sub in ["", "gameplay", "main/gameplay", "exported/main/gameplay"]:
        d = base / sub if sub else base
        if (d / "gpcore" / "gameplay.yml").is_file():
            return d
    raise SystemExit("No gameplay/gpcore/gameplay.yml under %s (give the folder that contains exported/)" % path)


class Vault:
    def __init__(self, gameplay_dir):
        self.records = []          # (folder, record)
        self.by_name = {}
        self.folders = set()
        for yml in sorted(gameplay_dir.glob("*/gameplay.yml")):
            folder = yml.parent.name
            self.folders.add(folder)
            data = load(str(yml))
            for rec in data or []:
                self.records.append((folder, rec))
                name = rec.get("Name")
                if name is not None:
                    self.by_name.setdefault(name, rec)

    def resolve(self, rec, key):
        """(value, owner record name) for the first record up the parent chain that holds the key."""
        depth = 0
        while rec is not None and depth < MAX_PARENT_DEPTH:
            data = rec.get("Data") or {}
            if key in data:
                return data[key], rec.get("Name") or "(template)"
            rec = self.by_name.get(rec.get("ParentName"))
            depth += 1
        return None, None

    def ref(self, ref):
        if not isinstance(ref, dict):
            return None
        return self.by_name.get(ref.get("CollectionKey"))


def vec(d):
    if not isinstance(d, dict):
        return (None, None, None)
    return (d.get("X"), d.get("Y"), d.get("Z"))


def fmt(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, float):
        return repr(round(v, 6))
    return str(v)


def parse_barrier(value, source):
    """-> (group_name, flip) for a Barriers number or a BarriersExtra string."""
    if source == "Barriers":
        n = int(value)
        return "BARRIER_SPLINE_%d" % abs(n), n < 0
    s = str(value)
    flip = s.startswith("*")
    return s.lstrip("*"), flip


def write_tsv(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, delimiter="\t", lineterminator="\n")
        w.writerow(header)
        for r in rows:
            w.writerow([fmt(c) for c in r])


def scan(vault, out, log):
    warn = []
    roots = [(f, r) for f, r in vault.records
             if f.startswith(RACE_BIN_PREFIX) and "EventID" in (r.get("Data") or {})]
    roots.sort(key=lambda fr: fr[1]["Name"])
    race_rows, obj_rows, bar_rows = [], [], []
    group_info = defaultdict(lambda: {"flips": 0, "plain": 0, "races": set()})
    type_counter = Counter()
    flare_counter = Counter()

    for folder, root in roots:
        d = root["Data"]
        rname = root["Name"]
        rtype = root.get("ParentName")
        type_counter[rtype] += 1
        counts = {}

        # --- ordered route objects
        order = 0

        def add_obj(role, ref, idx):
            nonlocal order
            rec = vault.ref(ref)
            if rec is None:
                warn.append("%s: %s #%d refers to %r, not in the vault" % (rname, role, idx, (ref or {}).get("CollectionKey")))
                return
            rd = rec.get("Data") or {}
            pos = vec(rd.get("Position"))
            rot = rd.get("Rotation")
            if pos[0] is None:
                warn.append("%s: %s has no Position" % (rec["Name"], role))
            dims_v, _ = vault.resolve(rec, "Dimensions")
            dim = vec(dims_v)
            bonus, _ = vault.resolve(rec, "TimeBonus")
            effect, effect_from = vault.resolve(rec, "ParticleEffect")
            spacing, spacing_from = vault.resolve(rec, "FlareSpacing")
            has_effect = bool(effect)
            if has_effect:
                src = "explicit" if "ParticleEffect" in rd else "inherited"
                sp_src = "explicit" if "FlareSpacing" in rd else ("inherited" if spacing is not None else "none")
                flare_counter[(effect, src, sp_src)] += 1
            fwd = (None, None)
            if rot is not None:
                a = math.radians(rot)
                fwd = (round(math.cos(a), 6), round(math.sin(a), 6))
            order += 1
            obj_rows.append([rname, order, role, rec["Name"].split("/")[-1], rec["Name"], rec.get("ParentName"),
                             pos[0], pos[1], pos[2], rot, fwd[0], fwd[1], dim[0], dim[1], dim[2], bonus,
                             effect if has_effect else "", spacing if has_effect else "",
                             ("explicit" if "ParticleEffect" in rd else "inherited") if has_effect else "",
                             ("explicit" if "FlareSpacing" in rd else ("inherited" if spacing is not None else "none")) if has_effect else ""])
            counts[role] = counts.get(role, 0) + 1

        if d.get("racestart"):
            add_obj("start", d["racestart"], 0)
        for key, role in ROUTE_ARRAYS:
            arr = d.get(key)
            if not arr:
                continue
            items = arr.get("Data", [])
            if arr.get("Capacity") != len(items):
                warn.append("%s: %s Capacity %s but %d entries" % (rname, key, arr.get("Capacity"), len(items)))
            for i, ref in enumerate(items):
                add_obj(role, ref, i)
        if d.get("racefinish"):
            add_obj("finish", d["racefinish"], 0)

        # --- barriers
        nb = ne = 0
        for src in ("Barriers", "BarriersExtra"):
            arr = d.get(src)
            if not arr:
                continue
            for i, item in enumerate(arr.get("Data", [])):
                raw = item.get("Value") if isinstance(item, dict) else item
                name, flip = parse_barrier(raw, src)
                h = bin_hash(name)
                bar_rows.append([rname, i, src, raw, name, "0x%08X" % h, flip])
                gi = group_info[name]
                gi["flips" if flip else "plain"] += 1
                gi["races"].add(rname)
                if src == "Barriers":
                    nb += 1
                else:
                    ne += 1

        race_rows.append([rname, d.get("EventID"), rtype, folder, d.get("Region"), d.get("RaceLength"),
                          d.get("TimeLimit"), d.get("TrafficLevel"), d.get("CopDensity"), d.get("CopsInRace"),
                          d.get("TrackRecord"), d.get("BossRace"), d.get("gameplayvault"),
                          d.get("NumLaps"), d.get("CheckpointsVisible"), d.get("RollingStart"),
                          d.get("InitialPlayerSpeed"), d.get("DoCountdown"),
                          counts.get("checkpoint", 0), counts.get("shortcut", 0), counts.get("corner", 0),
                          counts.get("draft", 0), nb, ne,
                          counts.get("start", 0), counts.get("finish", 0),
                          len((d.get("Opponents") or {}).get("Data", []))])

    out.mkdir(parents=True, exist_ok=True)
    write_tsv(out / "races.tsv",
              ["race", "event_id", "type", "bin", "region", "race_length", "time_limit", "traffic_level",
               "cop_density", "cops_in_race", "track_record", "boss_race", "gameplay_vault",
               "num_laps", "checkpoints_visible", "rolling_start", "initial_player_speed", "do_countdown",
               "n_checkpoints", "n_shortcuts", "n_corners", "n_drafts", "n_barriers", "n_barriers_extra",
               "has_start", "has_finish", "n_opponents"], race_rows)
    write_tsv(out / "race_objects.tsv",
              ["race", "order", "role", "name", "collection", "parent_class", "x", "y", "z", "rotation_deg",
               "fwd_x", "fwd_y", "dim_x", "dim_y", "dim_z", "time_bonus", "particle_effect", "flare_spacing",
               "effect_source", "spacing_source"], obj_rows)
    write_tsv(out / "race_barriers.tsv",
              ["race", "index", "source", "raw", "group_name", "group_hash", "flip"], bar_rows)
    write_tsv(out / "barrier_groups.tsv",
              ["group_name", "group_hash", "plain_uses", "flip_uses", "n_races", "races"],
              [[n, "0x%08X" % bin_hash(n), g["plain"], g["flips"], len(g["races"]), ",".join(sorted(g["races"]))]
               for n, g in sorted(group_info.items())])

    # --- report
    lines = ["race_scan report", "================", "gameplay folders: %d, records: %d" % (len(vault.folders), len(vault.records)),
             "race roots: %d" % len(roots), "by type: " + ", ".join("%s %d" % kv for kv in type_counter.most_common()),
             "route objects: %d" % len(obj_rows), "barrier rows: %d (%d distinct groups)" % (len(bar_rows), len(group_info)),
             "", "resolved trigger flares (effect, effect source, spacing source): count"]
    for k, v in sorted(flare_counter.items(), key=lambda kv: -kv[1]):
        lines.append("  %s: %d" % (", ".join(k), v))
    outside = [r["Name"] for f, r in vault.records
               if not f.startswith(RACE_BIN_PREFIX) and "EventID" in (r.get("Data") or {}) and r.get("Name")]
    lines += ["", "records with an EventID outside the race_bin folders (not scanned): " + ", ".join(outside)]
    no_pos = sum(1 for r in obj_rows if r[6] in (None, ""))
    lines += ["", "objects without Position: %d" % no_pos, "warnings: %d" % len(warn)]
    lines += ["  " + w for w in warn[:200]]
    if len(warn) > 200:
        lines.append("  ... %d more" % (len(warn) - 200))
    (out / "race_scan_report.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    log("\n".join(lines[:14]))
    return out


def main():
    ap = argparse.ArgumentParser(description="Carbon race events from the vault gameplay yml to TSV middle files.")
    ap.add_argument("vault", help="folder that contains exported/ (or exported/main or the gameplay folder)")
    ap.add_argument("--out", help="output folder (default outputs/race_scan/)")
    args = ap.parse_args()
    gp = find_gameplay_dir(args.vault)
    vault = Vault(gp)
    out = _pl.Path(args.out) if args.out else out_dir("race_scan")
    scan(vault, out, print)
    print("written to %s" % out)


if __name__ == "__main__":
    main()
