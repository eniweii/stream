#!/usr/bin/env python3
"""
find_flare_hashes.py - Find the flare type -> light_flares collection table in the Carbon exe.

Hyperlib reads flare params through a table of 64 pointers (flare_params_ at VA 0x00A6BF88),
2 slots per flare type (type*2 + layer). The game fills it from light_flares_cg collections.
This tool searches the exe for the collection key hashes (from your light_flares_cg.yml) and
shows where they sit, so the slot order can be read from the exe.

It looks for:
  1. Each of the 32 collection hashes as a little-endian u32 (table of keys, or push/mov imm32 in code)
  2. The class key 0xC7C5806D (light_flares_cg)
  3. Code that references the pointer table address 0x00A6BF88 (the init/use sites)
  4. ASCII names (if the game hashes names at runtime, the exe holds the names)

Needs an UNPACKED Carbon exe (the build hyperlinked targets). A packed exe has no readable tables.
"""

import argparse
import re
import struct
import sys

M = 0xFFFFFFFF
CLASS_KEY = 0xC7C5806D
TABLE_VA = 0x00A6BF88

READABLE = ["headlight_inner", "headlight_outer", "brakelight_inner", "coplightorange",
            "coplightblue_inner", "coplightbrightred", "coplightred_inner", "foglight_inner",
            "lamppost_outer", "reverselight_inner", "sun_flare", "coplightwhite_inner",
            "coplightbrightblue"]
HASHED = [0x4B810536, 0xBF79F943, 0x550B245A, 0x91E89CE5, 0x98E7607C, 0x2A9BE304, 0x0215CA40,
          0x21B44AAE, 0xE61B9443, 0x1A58D63A, 0x238FB6BF, 0x8D397462, 0x85D7B2BA, 0x950BBF97,
          0x1F6E6550, 0xB740451C, 0xD364E8AC, 0x4118B9E2, 0x908A4AD4]
KNOWN_MW = {0xBF79F943: "hand_flare_red", 0xD364E8AC: "brakelight_traffic_inner"}


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


class Pe:
    def __init__(self, data):
        self.sections = []
        self.base = 0
        if data[:2] != b"MZ":
            return
        pe = struct.unpack_from("<I", data, 0x3C)[0]
        if data[pe:pe + 4] != b"PE\0\0":
            return
        nsec = struct.unpack_from("<H", data, pe + 6)[0]
        opt_size = struct.unpack_from("<H", data, pe + 20)[0]
        opt = pe + 24
        self.base = struct.unpack_from("<I", data, opt + 28)[0]
        sec = opt + opt_size
        for i in range(nsec):
            s = sec + i * 40
            name = data[s:s + 8].split(b"\0")[0].decode("ascii", "replace")
            vsize, vaddr, rsize, rptr = struct.unpack_from("<IIII", data, s + 8)
            flags = struct.unpack_from("<I", data, s + 36)[0]
            self.sections.append((name, vaddr, max(vsize, rsize), rptr, rsize, flags))

    def raw_to_va(self, raw):
        for name, vaddr, vsize, rptr, rsize, flags in self.sections:
            if rptr <= raw < rptr + rsize:
                return self.base + vaddr + (raw - rptr), name, flags
        return None, "?", 0

    def va_to_raw(self, va):
        rva = va - self.base
        for name, vaddr, vsize, rptr, rsize, flags in self.sections:
            if vaddr <= rva < vaddr + rsize:
                return rptr + (rva - vaddr)
        return None


def find_all(data, pat):
    out, pos = [], 0
    while True:
        i = data.find(pat, pos)
        if i < 0:
            return out
        out.append(i)
        pos = i + 1


def load_yml(path):
    names = []
    for line in open(path, encoding="utf-8", errors="replace"):
        m = re.match(r"\s*Name:\s*(\S+)", line)
        if m:
            names.append(m.group(1))
    return names


FLARE_TYPES = [
    "car_headlight", "car_brakelight", "car_traffic_brakelight", "car_reverse_light",
    "car_fog_light", "car_cop_light_red", "car_cop_light_blue", "car_cop_light_white",
    "car_cop_headlight_right", "car_cop_headlight_left", "car_cop_light_bright_red",
    "car_cop_light_bright_blue", "car_cop_light_orange", "lamppost", "catseye_orange",
    "catseye_red", "catseye_blue", "blinking_amber", "blinking_red", "blinking_green",
    "hand_flare", "sun_flare", "generic_1", "generic_2", "generic_3", "generic_4",
    "generic_5", "generic_6", "generic_7", "generic_8", "generic_9", "generic_10",
]


def stack_keys(data, start_raw, known):
    """Parse the code that stores keys on the stack: C7 44 24 <b> <imm32> / C7 84 24 <d32> <imm32>."""
    out = []
    pat = re.compile(rb"\xC7\x44\x24(.)(.{4})|\xC7\x84\x24(.{4})(.{4})", re.S)
    for m in pat.finditer(data, start_raw, start_raw + 0x200):
        if m.group(1) is not None:
            off, imm = m.group(1)[0], struct.unpack("<I", m.group(2))[0]
        else:
            off, imm = struct.unpack("<I", m.group(3))[0], struct.unpack("<I", m.group(4))[0]
        if imm in known:
            out.append((off, imm))
    return out


def dump_ptrs(data, pe, keys, params_base, stride):
    raw = pe.va_to_raw(TABLE_VA)
    print(f"\n--- Pointer table at VA 0x{TABLE_VA:08X} (64 slots = 32 types x 2 layers) ---")
    if raw is None or raw + 256 > len(data):
        print("  Not stored in the file (runtime data). It is filled by code: use --disasm.")
        return
    for i in range(64):
        ptr = struct.unpack_from("<I", data, raw + 4 * i)[0]
        idx = (ptr - params_base) // stride if ptr >= params_base and (ptr - params_base) % stride == 0 else None
        key = keys[idx] if idx is not None and idx < len(keys) else None
        t = FLARE_TYPES[i // 2]
        print(f"  slot {i:>2} type {i // 2:>2} {t:<26} layer {i % 2}  ptr 0x{ptr:08X}"
              + (f"  -> key[{idx}] {key}" if key else ""))


def disasm(data, pe, known, va, length):
    try:
        import capstone
    except ImportError:
        print("Install capstone first: pip install capstone")
        return
    raw = pe.va_to_raw(va)
    if raw is None:
        print(f"VA 0x{va:08X} is not in a file-backed section")
        return
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
    hexmap = {f"0x{h:x}": n for h, n in known.items()}
    for ins in md.disasm(data[raw: raw + length], va):
        note = next((f"   ; {n}" for h, n in hexmap.items() if h in ins.op_str.lower()), "")
        print(f"0x{ins.address:08X}  {ins.bytes.hex():<22} {ins.mnemonic} {ins.op_str}{note}")


def main():
    ap = argparse.ArgumentParser(description="Find the flare slot table in the Carbon exe.")
    ap.add_argument("exe")
    ap.add_argument("--yml", help="light_flares_cg.yml (default: built-in list from your export)")
    ap.add_argument("--window", type=int, default=0x60, help="Bytes of context to dump around hits")
    ap.add_argument("--disasm", nargs=2, metavar=("VA", "LEN"), help="Disassemble LEN bytes at VA (needs capstone)")
    ap.add_argument("--ptrs", action="store_true", help="Read the 64-slot pointer table from the exe data")
    ap.add_argument("--params-base", default="0x00A6BA0C", help="VA of the params array (for --ptrs)")
    ap.add_argument("--params-stride", default="0x2C")
    args = ap.parse_args()

    data = open(args.exe, "rb").read()
    pe = Pe(data)
    print(f"Exe size {len(data)} bytes, image base 0x{pe.base:08X}, sections: "
          + ", ".join(f"{s[0]}@0x{pe.base + s[1]:08X}" for s in pe.sections))

    names = load_yml(args.yml) if args.yml else READABLE + [f"0x{h:08X}" for h in HASHED]
    known = {}
    for n in names:
        h = int(n, 16) if n.startswith("0x") else vlt(n)
        known[h] = n if not n.startswith("0x") else KNOWN_MW.get(h, n)
    if args.disasm:
        disasm(data, pe, known, int(args.disasm[0], 16), int(args.disasm[1], 16))
        return
    print(f"Searching for {len(known)} collection hashes (+ class key 0x{CLASS_KEY:08X}).\n")

    hits = []
    for h, n in known.items():
        for off in find_all(data, struct.pack("<I", h)):
            hits.append((off, h, n))
    hits.sort()
    if not hits:
        print("NO hash hits. The exe may be packed/encrypted, or the keys are built at runtime.")
    else:
        print(f"{'RawOff':<10} {'VA':<12} {'Section':<8} {'Hash':<12} Name")
        for off, h, n in hits:
            va, sec, _ = pe.raw_to_va(off)
            print(f"0x{off:08X} {('0x%08X' % va) if va else '-':<12} {sec:<8} 0x{h:08X}   {n}")

    # clusters: hits within 0x80 of each other
    clusters, cur = [], []
    for hit in hits:
        if cur and hit[0] - cur[-1][0] > 0x80:
            clusters.append(cur)
            cur = []
        cur.append(hit)
    if cur:
        clusters.append(cur)
    for cl in clusters:
        if len(cl) < 3:
            continue
        start = (cl[0][0] - args.window) & ~3
        end = cl[-1][0] + args.window
        va0, sec, _ = pe.raw_to_va(cl[0][0])
        print(f"\n=== Cluster of {len(cl)} hits near raw 0x{cl[0][0]:08X} (section {sec}) ===")
        deltas = sorted({b[0] - a[0] for a, b in zip(cl, cl[1:])})
        print("Gaps between hits (bytes):", deltas[:12])
        print("32-bit words (index from region start; hash matches annotated):")
        for off in range(max(start, 0), min(end, len(data) - 3), 4):
            w = struct.unpack_from("<I", data, off)[0]
            va, _, _ = pe.raw_to_va(off)
            tag = known.get(w, "")
            vs = f"0x{va:08X}" if va else "-"
            print(f"  {vs} raw 0x{off:08X}  0x{w:08X}  {tag}")

    order = stack_keys(data, hits[0][0] - 8, known) if hits else []
    if order:
        print("\n--- Key order stored by the init code (stack offset -> collection) ---")
        for n, (off, imm) in enumerate(order):
            print(f"  key[{n:>2}] esp+0x{off:02X}  0x{imm:08X}  {known[imm]}")
    if args.ptrs:
        dump_ptrs(data, pe, [f"0x{i:08X} {known[i]}" for _, i in order],
                  int(args.params_base, 16), int(args.params_stride, 16))

    # class key and table address references
    for label, val in (("class key 0xC7C5806D", CLASS_KEY), (f"table VA 0x{TABLE_VA:08X}", TABLE_VA)):
        refs = find_all(data, struct.pack("<I", val))
        print(f"\n--- References to {label}: {len(refs)} ---")
        for off in refs[:20]:
            va, sec, _ = pe.raw_to_va(off)
            ctx = data[max(off - 12, 0): off + 16]
            print(f"  raw 0x{off:08X} va {('0x%08X' % va) if va else '-'} {sec:<6} {ctx.hex(' ')}")

    # ASCII names
    print("\n--- ASCII name search ---")
    any_str = False
    for n in READABLE:
        for off in find_all(data, n.encode("ascii") + b"\0"):
            any_str = True
            va, sec, _ = pe.raw_to_va(off)
            print(f"  '{n}' at raw 0x{off:08X} va {('0x%08X' % va) if va else '-'} {sec}")
    if not any_str:
        print("  none (names are not stored as plain strings)")


if __name__ == "__main__":
    main()
