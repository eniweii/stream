#!/usr/bin/env python3
"""dump_fx_trigger_matrices.py - read the undecoded 0x30 block of every
WorldFXTrigger record (chunk 0x0003BC00) and test if it is a rotation matrix.

Record layout (same as ExportFxTriggersCommand.cs, 0x50 bytes):
  0x00 u32 groupKey | 0x04 pad | 0x08 u32 sectionRef | 0x0C pad
  0x10 12 x float32 (the block under test = matrix rows 0..2)
  0x40 float32 x, y, z, w (position = matrix row 3, w seen as 1.0)

Usage:
  python dump_fx_trigger_matrices.py STREAML5RA.BUN [out.tsv] [--examples N]

Output:
  - out.tsv (default: <input>.fxmatrices.tsv): one line per record, 16 floats
  - a short summary on screen. Paste the summary into the chat.
The file is read with seek only, so large files stay cheap.
"""

import math
import struct
import sys
from collections import Counter

CHUNK_ID = 0x0003BC00
CONTAINER_FLAG = 0x80000000
RECORD_SIZE = 0x50
EPS = 1e-3


def scan(f, end, out):
  """Walk the chunk envelope (u32 type, u32 length, payload)."""
  while f.tell() + 8 <= end:
    off = f.tell()
    hdr = f.read(8)
    if len(hdr) < 8:
      return
    ctype, length = struct.unpack("<II", hdr)
    start = f.tell()
    stop = start + length
    if stop > end:
      print("Stop at 0x%X: chunk length %d runs past container" % (off, length))
      return
    if ctype == CHUNK_ID:
      parse_chunk(f, off, start, length, out)
      f.seek(stop)
    elif ctype & CONTAINER_FLAG:
      scan(f, stop, out)
      f.seek(stop)
    else:
      f.seek(stop)


def parse_chunk(f, off, start, length, out):
  f.seek(start)
  if length < 0x18:
    return
  endian, version, legacy, sec, count, sec_ref = struct.unpack(
      "<iiiiii", f.read(0x18)
  )
  if endian != 0x11111111:
    print("Chunk 0x%X: EndianSwapped=0x%08X (expected 0x11111111)" % (off, endian & 0xFFFFFFFF))
  if count < 0 or count > 10000:
    print("Chunk 0x%X: implausible record_count=%d, skipped" % (off, count))
    return
  for i in range(count):
    raw = f.read(RECORD_SIZE)
    if len(raw) < RECORD_SIZE:
      print("Chunk 0x%X: ran out of data at record %d" % (off, i))
      return
    key = struct.unpack_from("<I", raw, 0)[0]
    rec_ref = struct.unpack_from("<I", raw, 8)[0]
    m = struct.unpack_from("<16f", raw, 0x10)
    out.append(
        {
            "chunk": off,
            "section": sec,
            "sec_ref": sec_ref,
            "rec_ref": rec_ref,
            "index": i,
            "key": key,
            "m": m,
        }
    )


def dot(a, b):
  return sum(x * y for x, y in zip(a, b))


def classify(m):
  """Return (kind, info) for the 3 rotation rows of the matrix."""
  r = [m[0:3], m[4:7], m[8:11]]
  pad = [m[3], m[7], m[11]]
  lens = [math.sqrt(dot(v, v)) for v in r]
  ident = [(1, 0, 0), (0, 1, 0), (0, 0, 1)]
  if all(abs(r[i][j] - ident[i][j]) < EPS for i in range(3) for j in range(3)):
    kind = "identity"
  elif all(abs(l - 1.0) < EPS for l in lens) and all(
      abs(dot(r[i], r[j])) < EPS for i in range(3) for j in range(i + 1, 3)
  ):
    kind = "rotation"
  elif all(l < EPS for l in lens):
    kind = "zero"
  else:
    kind = "other"
  return kind, lens, pad


def main():
  args = [a for a in sys.argv[1:] if not a.startswith("--")]
  examples = 15
  if "--examples" in sys.argv:
    examples = int(sys.argv[sys.argv.index("--examples") + 1])
    args = [a for a in args if a != str(examples)]
  if not args:
    print(__doc__)
    return
  path = args[0]
  out_path = args[1] if len(args) > 1 else path + ".fxmatrices.tsv"

  recs = []
  with open(path, "rb") as f:
    f.seek(0, 2)
    end = f.tell()
    f.seek(0)
    scan(f, end, recs)

  kinds = Counter()
  pads_nonzero = 0
  by_key = {}
  z_up = 0
  shown = []
  with open(out_path, "w") as fh:
    fh.write(
        "chunk\tsection\tsecRef\trecRef\tidx\tgroupKey\t"
        + "\t".join("m%d" % i for i in range(16))
        + "\n"
    )
    for r in recs:
      kind, lens, pad = classify(r["m"])
      kinds[kind] += 1
      if any(abs(p) > EPS for p in pad):
        pads_nonzero += 1
      by_key.setdefault(r["key"], Counter())[kind] += 1
      zrow = r["m"][8:11]
      if abs(zrow[0]) < EPS and abs(zrow[1]) < EPS and zrow[2] > 1 - EPS:
        z_up += 1
      if kind != "identity" and len(shown) < examples:
        shown.append((r, kind))
      fh.write(
          "0x%X\t%d\t%d\t%d\t%d\t0x%08X\t%s\n"
          % (
              r["chunk"],
              r["section"],
              r["sec_ref"],
              r["rec_ref"],
              r["index"],
              r["key"],
              "\t".join("%.6g" % v for v in r["m"]),
          )
      )

  print("File: %s" % path)
  print("Records read: %d   TSV: %s" % (len(recs), out_path))
  print("Rotation block kinds: %s" % dict(kinds))
  print("Records with non-zero 4th column (rows 0-2): %d" % pads_nonzero)
  print("Records whose row 2 (local Z, cone axis) is exactly +Z: %d" % z_up)
  print("")
  print("Per groupKey (kind counts), non-identity groups first:")
  rows = sorted(
      by_key.items(),
      key=lambda kv: (-sum(v for k, v in kv[1].items() if k != "identity"), kv[0]),
  )
  for key, c in rows[:40]:
    print("  0x%08X: %s" % (key, dict(c)))
  if len(rows) > 40:
    print("  ... %d more groups in the TSV" % (len(rows) - 40))
  print("")
  print("First %d non-identity records (rows 0..2, then position):" % len(shown))
  for r, kind in shown:
    m = r["m"]
    print(
        "  sec %d rec %d key 0x%08X [%s]" % (r["section"], r["index"], r["key"], kind)
    )
    for i in range(3):
      print("    row%d: %8.4f %8.4f %8.4f | pad %.4g" % (i, m[4 * i], m[4 * i + 1], m[4 * i + 2], m[4 * i + 3]))
    print("    pos:  %10.3f %10.3f %10.3f | w %.4g" % (m[12], m[13], m[14], m[15]))


if __name__ == "__main__":
  main()
