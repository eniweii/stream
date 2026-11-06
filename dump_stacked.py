#!/usr/bin/env python3
"""dump_stacked.py - print the stacked (depth) section data of sections.json for the given section IDs.

    python dump_stacked.py sections.json 620 606 614 630

For every ID: whether it is a base section (polygon, bounds, related, depth entries), a depth section
(its base, name, related) and which sections list it as related. For every base with depth entries it
also prints the elevation triangles whose x/y box overlaps the base bounds (name in decimal and hex,
the three corners, z range) and the depth name each one would pick. Paste the output back.
"""
import json
import sys


def hexname(n):
    return "0x%08X" % (int(n) & 0xFFFFFFFF)


def overlaps(tri, bmin, bmax):
    xs = [p[0] for p in tri["points"]]
    ys = [p[1] for p in tri["points"]]
    return not (max(xs) < bmin[0] or min(xs) > bmax[0] or max(ys) < bmin[1] or min(ys) > bmax[1])


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    data = json.load(open(sys.argv[1], encoding="utf-8"))
    ids = [int(a) for a in sys.argv[2:]]
    sections = {s["id"]: s for s in data["sections"]}
    tris = data.get("elevationPolys", [])
    depth_of = {}                                     # depth id -> (base, entry)
    for s in data["sections"]:
        for d in s.get("depth", []):
            depth_of[d["id"]] = (s["id"], d)
    print("sections: %d, with depth: %d, elevation triangles: %d" % (
        len(sections), sum(1 for s in sections.values() if s.get("depth")), len(tris)))
    print("bases with depth: " + ", ".join("%d->%s" % (s["id"], "/".join(str(d["id"]) for d in s["depth"]))
                                           for s in data["sections"] if s.get("depth")))
    for sid in ids:
        print("\n=== %d ===" % sid)
        found = False
        s = sections.get(sid)
        if s:
            found = True
            print("base section: %d points, bounds %s .. %s" % (len(s["points"]), s["boundsMin"], s["boundsMax"]))
            print("  related (%d): %s" % (len(s["related"]), s["related"]))
            for d in s.get("depth", []):
                print("  depth %d name %s (%s) related (%d): %s" % (
                    d["id"], d["name"], hexname(d["name"]), len(d["related"]), d["related"]))
            if s.get("depth"):
                names = {d["name"]: d["id"] for d in s["depth"]}
                hit = [t for t in tris if overlaps(t, s["boundsMin"], s["boundsMax"])]
                print("  elevation triangles overlapping the bounds: %d" % len(hit))
                for t in hit:
                    zs = [p[2] for p in t["points"]]
                    picks = names.get(t["name"])
                    print("    name %s (%s) z %.2f..%.2f -> %s  corners %s" % (
                        t["name"], hexname(t["name"]), min(zs), max(zs),
                        ("depth %d" % picks) if picks else "base",
                        [[round(c, 2) for c in p] for p in t["points"]]))
        if sid in depth_of:
            found = True
            base, d = depth_of[sid]
            print("depth section of base %d, name %s (%s), related (%d): %s" % (
                base, d["name"], hexname(d["name"]), len(d["related"]), d["related"]))
        users = [b for b, sec in sections.items() if sid in sec["related"] and b != sid]
        users += [b for b, (base, d) in depth_of.items() if sid in d["related"] and b != sid]
        if users:
            print("listed as related by: %s" % sorted(set(users)))
        if not found:
            print("not a base section and not a depth section in this file")
    return 0


if __name__ == "__main__":
    sys.exit(main())
