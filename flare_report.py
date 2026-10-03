#!/usr/bin/env python3
"""
flare_report.py - Join the scenery flare TSV (flare_scenery_scan.py --tsv) with the vault
light_flares_cg.yml and print what each flare type looks like and how it behaves.

Type -> vault entry mapping is read from the Carbon exe (NFSC.exe):
  - init code stores 32 collection keys in a fixed order (KEYS below) and copies them to
    params[0..31] at 0xA6BA00, 0x2C bytes each
  - the 64-slot pointer table at 0xA6BF88 (type*2 + layer) points into that array
Result: params index = type for types 14..31, and the table below for types 0..13.
Behavior (blink timers, conditions) comes from hyperlinked's flare renderer.
"""

import argparse
import csv
import re
import sys
from collections import Counter, defaultdict

M = 0xFFFFFFFF

# Collection keys in the order the exe init code stores them (key[0]..key[31]).
KEYS = [0x356F692A, 0xA95AB5AD, 0xB60DAC69, 0xD364E8AC, 0x30EA58B4, 0x35CE359C, 0xF51BF17F, 0xDDE4B816,
        0x44C51B58, 0x93007A08, 0x0299E454, 0x47CB2AD7, 0xB740451C, 0x4FF2B81C, 0x85D7B2BA, 0x1A58D63A,
        0x91E89CE5, 0x950BBF97, 0xE61B9443, 0x98E7607C, 0xBF79F943, 0x55EAA808, 0x8D397462, 0x21B44AAE,
        0x0215CA40, 0x2A9BE304, 0x4B810536, 0x4118B9E2, 0x238FB6BF, 0x550B245A, 0x1F6E6550, 0x908A4AD4]

# type -> params indexes per layer (None = null slot in the exe table)
TYPE_PARAMS = {0: (0, 1), 1: (2, None), 2: (3, None), 3: (4, None), 4: (5, None), 5: (6, None),
               6: (7, None), 7: (8, None), 8: (0, 1), 9: (0, 1), 10: (9, None), 11: (10, None),
               12: (11, None), 13: (12, None)}
for _t in range(14, 32):
    TYPE_PARAMS[_t] = (_t, None)

TEXTURES = ["HEADLIGHTFLAREINNER", "HEADLIGHTFLAREOUTER", "HEADLIGHTGLOW", "HEADLIGHTCATSEYE", "LAMPPOSTFLARE"]
# blink: type -> (timer name, half period in seconds); full cycle = 2 x half period
BLINK = {17: ("amber", 0.4), 18: ("red", 0.5), 19: ("green", 0.45), 29: ("green", 0.45)}
CONDITION = {15: "only while racing (not at a checkpoint, not drifting) or in a pursuit",
             23: "game flag at 0x00B42F04 (meaning not known)"}
UNI_DIRECTIONAL = {17, 18, 19, 29}
FIELDS = ["MinSize", "MaxSize", "MinScale", "Maxscale", "Power", "ZBias"]


def vlt(s, init=0xABCDEF00):
    k = s.encode("latin-1")
    a = b = 0x9E3779B9
    c = init
    L, o = len(k), 0

    def mix(a, b, c):
        a = (a - b - c) & M; a ^= c >> 13
        b = (b - c - a) & M; b ^= (a << 8) & M
        c = (c - a - b) & M; c ^= b >> 13
        a = (a - b - c) & M; a ^= c >> 12
        b = (b - c - a) & M; b ^= (a << 16) & M
        c = (c - a - b) & M; c ^= b >> 5
        a = (a - b - c) & M; a ^= c >> 3
        b = (b - c - a) & M; b ^= (a << 10) & M
        c = (c - a - b) & M; c ^= b >> 15
        return a, b, c

    while L - o >= 12:
        a = (a + int.from_bytes(k[o:o + 4], "little")) & M
        b = (b + int.from_bytes(k[o + 4:o + 8], "little")) & M
        c = (c + int.from_bytes(k[o + 8:o + 12], "little")) & M
        a, b, c = mix(a, b, c)
        o += 12
    c = (c + len(k)) & M
    r = k[o:]
    n = len(r)
    if n >= 11: c = (c + (r[10] << 24)) & M
    if n >= 10: c = (c + (r[9] << 16)) & M
    if n >= 9: c = (c + (r[8] << 8)) & M
    if n >= 8: b = (b + (r[7] << 24)) & M
    if n >= 7: b = (b + (r[6] << 16)) & M
    if n >= 6: b = (b + (r[5] << 8)) & M
    if n >= 5: b = (b + r[4]) & M
    if n >= 4: a = (a + (r[3] << 24)) & M
    if n >= 3: a = (a + (r[2] << 16)) & M
    if n >= 2: a = (a + (r[1] << 8)) & M
    if n >= 1: a = (a + r[0]) & M
    a, b, c = mix(a, b, c)
    return c


def load_yml(path):
    """Minimal parser for the Attribulator light_flares_cg.yml. Returns {hash: entry}."""
    entries, cur, in_colour = {}, None, False
    for line in open(path, encoding="utf-8", errors="replace"):
        s = line.strip()
        if line.startswith("- ParentName"):
            cur, in_colour = {"colour": {}}, False
            continue
        if cur is None or not s:
            continue
        if s.startswith("Name:"):
            name = s.split(":", 1)[1].strip()
            cur["name"] = name
            h = int(name, 16) if name.lower().startswith("0x") else vlt(name)
            entries[h] = cur
        elif s == "colour:":
            in_colour = True
        elif in_colour and re.match(r"^[XYZW]:", s):
            cur["colour"][s[0]] = float(s.split(":", 1)[1])
        elif ":" in s:
            k, v = s.split(":", 1)
            if v.strip():
                in_colour = False
                try:
                    cur[k] = float(v)
                except ValueError:
                    pass
    return entries


def describe(entry, idx):
    c = entry["colour"]
    r, g, b, a = (c.get(k, 0.0) for k in "XYZW")
    hexc = "#%02X%02X%02X" % tuple(round(max(0, min(1, v)) * 255) for v in (r, g, b))
    tex = int(entry.get("flare_texture", 0))
    tname = TEXTURES[tex] if 0 <= tex < len(TEXTURES) else f"?{tex}"
    return (f"params[{idx}] {entry['name']}  rgba=({r:.3f},{g:.3f},{b:.3f},{a:.3f}) {hexc} alpha {a:.2f}  "
            f"tex {tex}={tname}  size {entry['MinSize']:g}..{entry['MaxSize']:g}  "
            f"scale {entry['MinScale']:g}..{entry['Maxscale']:g}  power {entry['Power']:g}  zbias {entry['ZBias']:g}")


def main():
    ap = argparse.ArgumentParser(description="Join flare TSV with the vault yml.")
    ap.add_argument("tsv", help="TSV from flare_scenery_scan.py --tsv")
    ap.add_argument("yml", help="light_flares_cg.yml")
    ap.add_argument("--enriched", help="Write the joined rows to this TSV")
    ap.add_argument("--lamppost-outer", action="store_true",
                    help="Use params[13] (lamppost_outer) as lamppost layer 1 (not in the exe table; unverified)")
    args = ap.parse_args()

    vault = load_yml(args.yml)
    missing = [i for i, h in enumerate(KEYS) if h not in vault]
    print(f"Vault entries read: {len(vault)}. Key slots without a yml entry: {missing or 'none'}")

    rows = list(csv.DictReader(open(args.tsv, encoding="utf-8"), delimiter="\t"))
    print(f"Flares in TSV: {len(rows)}\n")
    if not rows:
        return
    by_type = Counter(int(r["type"]) for r in rows)
    by_section = Counter(int(r["section"]) for r in rows)

    tp = dict(TYPE_PARAMS)
    if args.lamppost_outer:
        tp[13] = (12, 13)

    print("=== Flare types in the scenery ===")
    for t, n in sorted(by_type.items()):
        name = next(r["type_name"] for r in rows if int(r["type"]) == t)
        print(f"\nType {t} {name}: {n} flares")
        layers = tp.get(t)
        if not layers:
            print("  no vault mapping for this type (type outside 0..31)")
            continue
        for li, idx in enumerate(layers):
            if idx is None:
                continue
            entry = vault.get(KEYS[idx])
            print(f"  layer {li}: " + (describe(entry, idx) if entry else f"params[{idx}] key 0x{KEYS[idx]:08X} not in yml"))
        if t == 13:
            print("  note: the exe table has no layer 1 for lamppost; params[13] lamppost_outer is not referenced by it")
        dirs = "uni-directional (fades with view angle)" if t in UNI_DIRECTIONAL else "n-directional (same from every side)"
        print(f"  direction: {dirs}")
        if t in BLINK:
            tn, half = BLINK[t]
            print(f"  blinking: {tn} timer, {half:g} s on / {half:g} s off (cycle {2 * half:g} s), all flares of this timer blink together")
        if t in CONDITION:
            print(f"  condition: {CONDITION[t]}")
        zero = sum(1 for r in rows if int(r["type"]) == t and r["tint"] == "00000000")
        print(f"  tint all zero (vault colour used): {zero} of {n}")

    print("\n=== Flares per section (top 15) ===")
    for sec, n in by_section.most_common(15):
        print(f"  section {sec:<6} {n}")
    print(f"  ({len(by_section)} sections in total)")

    if args.enriched:
        cols = list(rows[0].keys())
        extra = []
        for li in (0, 1):
            extra += [f"L{li}_{k}" for k in ("params", "r", "g", "b", "a", "texture")] + [f"L{li}_{f}" for f in FIELDS]
        extra += ["blink_cycle_s", "blink_timer", "condition", "directional"]
        with open(args.enriched, "w", encoding="utf-8") as fh:
            fh.write("\t".join(cols + extra) + "\n")
            for r in rows:
                t = int(r["type"])
                vals = []
                layers = tp.get(t, (None, None))
                for li in (0, 1):
                    idx = layers[li] if li < len(layers) else None
                    e = vault.get(KEYS[idx]) if idx is not None else None
                    if e:
                        c = e["colour"]
                        tex = int(e.get("flare_texture", 0))
                        vals += [e["name"], *(f"{c.get(k, 0):.5f}" for k in "XYZW"),
                                 TEXTURES[tex] if 0 <= tex < len(TEXTURES) else str(tex),
                                 *(f"{e[f]:g}" for f in FIELDS)]
                    else:
                        vals += [""] * (6 + len(FIELDS))
                bl = BLINK.get(t)
                vals += [f"{2 * bl[1]:g}" if bl else "", bl[0] if bl else "", CONDITION.get(t, ""),
                         "uni" if t in UNI_DIRECTIONAL else "n"]
                fh.write("\t".join([r[c] for c in cols] + vals) + "\n")
        print(f"\nWrote {len(rows)} joined rows to {args.enriched}")


if __name__ == "__main__":
    main()
