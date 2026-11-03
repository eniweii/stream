"""Shared chunk walking and layout helpers for the probe tools (chunk_probe_all.py).

Chunk envelope (same as nfs_region_common.walk_chunks): 4-byte ID, 4-byte little endian
payload length, payload. The top bit of the ID marks a container whose payload is more chunks.

Alignment rule (differs from the first chunk_probe.py, which assumed every leaf chunk is
padded): the game pads a leaf payload with 0x11 bytes up to a 16-byte (sometimes larger)
file offset boundary, but some families (the texture pack chunks 0x3331xxxx) are not padded
at all and start with real data. So the pad is whatever run of leading 0x11 bytes ends on a
16-byte boundary; anything else is data.
"""
import math
import re
import struct
from collections import Counter
from functools import reduce

PAD_BYTE = 0x11
MAX_PAD_SCAN = 0x400
MAX_STRIDE = 0x200
ALIGN = 16


def split_payload(mm, payload_start, length):
    """Return (data_start, pad, kind) for a leaf payload.

    kind: "pad11"    leading 0x11 bytes up to an aligned boundary (pad > 0)
          "aligned"  payload already starts on a 16-byte boundary
          "unpadded" payload starts off-boundary and its first bytes are data
    """
    run = 0
    limit = min(length, MAX_PAD_SCAN)
    for b in mm[payload_start:payload_start + limit]:
        if b != PAD_BYTE:
            break
        run += 1
    end = payload_start + run
    pad = max(0, (end - end % ALIGN) - payload_start)
    pad = min(pad, length)
    if pad > 0:
        return payload_start + pad, pad, "pad11"
    if payload_start % ALIGN == 0:
        return payload_start, 0, "aligned"
    return payload_start, 0, "unpadded"


def iter_chunks(mm, start=0, end=None, problems=None):
    """Yield (offset, chunk_id, length, is_container, payload_start, parent_id, depth, parent_offset)
    in file order, descending into containers. A chunk whose length runs past its parent is
    reported in `problems` (a list) and the rest of that level is skipped."""
    if end is None:
        end = len(mm)
    stack = [[start, end, -1, 0, -1]]
    while stack:
        frame = stack[-1]
        pos, level_end, parent, depth, parent_off = frame
        if pos + 8 > level_end:
            if problems is not None and pos < level_end:
                problems.append((pos, "%d trailing bytes (less than a header)" % (level_end - pos), depth))
            stack.pop()
            continue
        cid, length = struct.unpack_from("<II", mm, pos)
        payload_start = pos + 8
        payload_end = payload_start + length
        if payload_end > level_end:
            if problems is not None:
                problems.append((pos, "chunk 0x%08X length %d runs past its parent (%d bytes left)" % (
                    cid, length, level_end - payload_start), depth))
            stack.pop()
            continue
        is_container = bool(cid & 0x80000000)
        yield pos, cid, length, is_container, payload_start, parent, depth, parent_off
        frame[0] = payload_end
        if is_container:
            stack.append([payload_start, payload_end, cid, depth + 1, pos])


def stride_analysis(len_counter):
    """From {data_len: count}: gcd of the sizes, gcd of the size differences and the header
    left over when sizes vary (size = header + n * stride)."""
    sizes = [s for s in len_counter if s > 0]
    out = {"gcd": 0, "diff_gcd": 0, "header": None, "distinct": len(sizes)}
    if not sizes:
        return out
    out["gcd"] = reduce(math.gcd, sizes)
    if len(sizes) > 1:
        s0 = sizes[0]
        dg = reduce(math.gcd, [abs(s - s0) for s in sizes[1:]])
        out["diff_gcd"] = dg
        if dg:
            out["header"] = s0 % dg
    return out


def choose_stride(analysis):
    """Return (stride, how) or (None, reason). Small values are treated as noise."""
    dg = analysis["diff_gcd"]
    if analysis["distinct"] > 1 and dg >= 8 and dg % 4 == 0 and dg <= MAX_STRIDE:
        return dg, "gcd of size differences (header %d bytes)" % (analysis["header"] or 0)
    g = analysis["gcd"]
    if analysis["distinct"] == 1 and 8 <= g <= MAX_STRIDE and g % 4 == 0:
        return g, "single size, taken as one record"
    if analysis["distinct"] > 1 and 8 <= g <= MAX_STRIDE and g % 4 == 0:
        return g, "gcd of sizes"
    return None, "no stride found"


def classify(values):
    """Describe a 4-byte column from raw u32 values."""
    if len(set(values)) == 1:
        return "zero" if values[0] == 0 else "constant"
    for v in values:
        f = struct.unpack("<f", struct.pack("<I", v))[0]
        if v != 0 and (not math.isfinite(f) or abs(f) > 1e7 or abs(f) < 1e-9):
            break
    else:
        return "float?"
    if max(values) < 0x10000:
        return "small int"
    return "u32/hash"


def float_text(u):
    """Float reading of a u32, or '-' when it does not look like a float."""
    if u == 0:
        return "0"
    f = struct.unpack("<f", struct.pack("<I", u))[0]
    if math.isfinite(f) and 1e-6 <= abs(f) <= 1e7:
        return "%.5g" % f
    return "-"


def ascii_strings(buf, min_len=4, limit=6):
    return [m.group().decode("ascii") for m in re.finditer(rb"[\x20-\x7e]{%d,}" % min_len, buf)][:limit]


def hexdump(buf, base):
    lines = []
    for i in range(0, len(buf), 16):
        row = buf[i:i + 16]
        lines.append("0x%08X  %-47s  %s" % (
            base + i, " ".join("%02X" % b for b in row),
            "".join(chr(b) if 32 <= b < 127 else "." for b in row)))
    return "\n".join(lines)
