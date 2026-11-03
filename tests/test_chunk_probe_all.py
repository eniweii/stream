"""Synthetic tests for nfs_chunk_walk.py and chunk_probe_all.py (no game files needed)."""
import struct
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "common"))

from nfs_chunk_walk import choose_stride, iter_chunks, split_payload, stride_analysis  # noqa: E402
from chunk_probe_all import scan, example_indices  # noqa: E402


def header(cid, length):
    return struct.pack("<II", cid, length)


def padded_leaf(buf, cid, data):
    """Leaf chunk whose payload is 0x11-padded to a 16-byte file offset, like the game's."""
    start = len(buf) + 8
    pad = (-start) % 16
    payload = b"\x11" * pad + data
    return header(cid, len(payload)) + payload


def raw_leaf(cid, data):
    return header(cid, len(data)) + data


def container(cid, children):
    body = b"".join(children)
    return header(cid | 0x80000000, len(body)) + body


def build():
    buf = bytearray()
    # container with two padded 136-byte records (like the world_anim rtnode)
    inner = bytearray()
    for i in range(2):
        inner += padded_leaf(bytearray(b"\0" * (len(buf) + 8 + len(inner))), 0x37250, struct.pack("<34I", *range(i, i + 34)))
    buf += header(0x80034100, len(inner)) + inner
    # unpadded leaf starting off a 16-byte boundary with real data (texture pack style)
    while (len(buf) + 8) % 16 == 0:
        buf += raw_leaf(0x1234, b"\x01\x02\x03\x04")
    buf += raw_leaf(0x33312002, b"\x4E\xB5\xC6\xFE" + b"\x00" * 20)
    # filler chunks
    buf += padded_leaf(buf, 0, b"")
    buf += raw_leaf(0, b"\x11" * 12)
    buf += raw_leaf(0, b"\x00" * 8)
    buf += raw_leaf(0, b"\x11\x00\x11\x00")
    return bytes(buf)


class WalkTests(unittest.TestCase):
    def test_pad_kinds(self):
        data = struct.pack("<4I", 1, 2, 3, 4)
        buf = bytearray(b"\0" * 4)
        chunk = padded_leaf(buf, 0x10, data)
        mm = bytes(buf) + chunk
        data_start, pad, kind = split_payload(mm, 4 + 8, len(chunk) - 8)
        self.assertEqual(kind, "pad11")
        self.assertEqual(data_start % 16, 0)
        self.assertEqual(mm[data_start:data_start + 16], data)

    def test_unpadded_data_is_kept(self):
        mm = b"\0" * 12 + b"\x4E\xB5\xC6\xFE" + b"\0" * 20
        data_start, pad, kind = split_payload(mm, 12, 24)
        self.assertEqual((data_start, pad, kind), (12, 0, "unpadded"))

    def test_data_beginning_with_pad_like_bytes_is_not_overskipped(self):
        # payload starts at 12; real data begins with 0x11 bytes beyond the boundary
        mm = b"\0" * 12 + b"\x11" * 4 + b"\x11\x11\x11\x11" + b"\0" * 8
        data_start, pad, kind = split_payload(mm, 12, 16)
        self.assertEqual(data_start, 16)
        self.assertEqual(pad, 4)

    def test_truncated_chunk_is_reported(self):
        good = raw_leaf(0x10, b"\0" * 8)
        bad = header(0x11, 1000) + b"\0" * 4
        problems = []
        ids = [c[1] for c in iter_chunks(good + bad, problems=problems)]
        self.assertEqual(ids, [0x10])
        self.assertEqual(len(problems), 1)

    def test_container_parent_and_depth(self):
        data = container(0x100, [raw_leaf(0x1, b"\0" * 4), container(0x200, [raw_leaf(0x2, b"\0" * 4)])])
        rows = {c[1]: c for c in iter_chunks(data)}
        self.assertEqual(rows[0x1][5:7], (0x80000100, 1))
        self.assertEqual(rows[0x2][5:7], (0x80000200, 2))


class StrideTests(unittest.TestCase):
    def test_single_size_is_one_record(self):
        an = stride_analysis({136: 95})
        self.assertEqual(choose_stride(an)[0], 136)

    def test_varying_sizes_use_difference_gcd(self):
        an = stride_analysis({72: 3, 144: 2, 9576: 1})
        self.assertEqual(choose_stride(an)[0], 72)
        self.assertEqual(an["header"], 0)

    def test_header_plus_records(self):
        an = stride_analysis({16 + 88: 5, 16 + 88 * 3: 2})
        stride, _ = choose_stride(an)
        self.assertEqual((stride, an["header"]), (176, 104 % 176))

    def test_noise_gcd_is_rejected(self):
        an = stride_analysis({100: 1, 1004: 1, 40000: 1})
        self.assertIsNone(choose_stride(an)[0])


class ScanTests(unittest.TestCase):
    def setUp(self):
        import mmap
        import tempfile
        self.tmp = tempfile.NamedTemporaryFile(delete=False)
        self.tmp.write(build())
        self.tmp.close()
        self.fh = open(self.tmp.name, "rb")
        self.mm = mmap.mmap(self.fh.fileno(), 0, access=mmap.ACCESS_READ)

    def tearDown(self):
        self.mm.close()
        self.fh.close()
        Path(self.tmp.name).unlink()

    def test_scan(self):
        stats, problems, top_end, depth, mixed = scan(self.mm)
        self.assertEqual(problems, [])
        self.assertEqual(top_end, len(self.mm))
        rt = stats[0x37250]
        self.assertEqual(rt.count, 2)
        self.assertEqual(rt.parents, {0x80034100: 2})
        self.assertEqual(set(rt.data_hist), {136})
        self.assertEqual(stats[0x80034100].patterns[(0x37250, 0x37250)], 1)
        self.assertEqual(stats[0x33312002].pads["unpadded"], 1)
        filler = stats[0].filler
        self.assertEqual((filler["all 0x11"], filler["all zero"], filler["mixed"]), (2, 1, 1))
        self.assertEqual(len(mixed), 1)

    def test_examples_unique_and_bounded(self):
        stats, *_ = scan(self.mm)
        idx = example_indices(stats[0x37250], 3)
        self.assertEqual(len(idx), len(set(idx)))
        self.assertLessEqual(len(idx), 3)


if __name__ == "__main__":
    unittest.main()
