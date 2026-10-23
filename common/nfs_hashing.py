"""
Two hash families live here: `bin_hash` (Black Box `hashing::bin`) and `vlt_hash` (the
Attrib/VLT key hash, see below). Both are 32 bit and not interchangeable.

The `bin` string hash from hyperlinked's Common/hashing.hpp (Carbon's own
decompiled source) - confirmed, not guessed, since scenery::group's key
comparison in scenery.cpp does `key == hashing::bin_const("SCENERY_GROUP_DOOR")`
directly. A classic times-33 hash, seeded at 0xFFFFFFFF.

NOT the same hash family as the emitter/effect name table (emittergroup_hash.h)
- that one's values don't match this algorithm's output for the same names,
so it's a different hash used by a different subsystem (Attrib::Key, not
hashing::bin). Don't mix the two up or assume one table's names resolve
against the other's keys.
"""

BIN_HASH_INITIAL = 0xFFFFFFFF


def bin_hash(string, prefix=BIN_HASH_INITIAL):
    if not string:
        return prefix
    for ch in string:
        prefix = (prefix * 0x21 + ord(ch)) & 0xFFFFFFFF
    return prefix


VLT_HASH_INITIAL = 0xABCDEF00
_M = 0xFFFFFFFF


def _vlt_mix(a, b, c):
    a = (a - b - c) & _M; a ^= c >> 13
    b = (b - c - a) & _M; b ^= (a << 8) & _M
    c = (c - a - b) & _M; c ^= b >> 13
    a = (a - b - c) & _M; a ^= c >> 12
    b = (b - c - a) & _M; b ^= (a << 16) & _M
    c = (c - a - b) & _M; c ^= b >> 5
    a = (a - b - c) & _M; a ^= c >> 3
    b = (b - c - a) & _M; b ^= (a << 10) & _M
    c = (c - a - b) & _M; c ^= b >> 15
    return a, b, c


def vlt_hash(string, init=VLT_HASH_INITIAL):
    """The VLT / Attrib key hash (Bob Jenkins' lookup2, seed 0xABCDEF00): the keys of vault
    collections and of Attrib references, e.g. the surface hashes of collision articles and
    the group keys of scenery. Same function as flares/flare_report.py `vlt` and
    common/nfs_hash_lookup_gui.py `vlt_hash` (checked equal; 15 of the 32 carbon flare keys
    resolve from hashes_main.txt with it). Case sensitive, ASCII names only."""
    key = string.encode('latin-1', errors='ignore')
    a = b = 0x9E3779B9
    c = init
    length = len(key)
    o = 0
    while length - o >= 12:
        a = (a + int.from_bytes(key[o:o + 4], 'little')) & _M
        b = (b + int.from_bytes(key[o + 4:o + 8], 'little')) & _M
        c = (c + int.from_bytes(key[o + 8:o + 12], 'little')) & _M
        a, b, c = _vlt_mix(a, b, c)
        o += 12
    c = (c + length) & _M
    rest = key[o:]
    n = len(rest)
    if n >= 11: c = (c + (rest[10] << 24)) & _M
    if n >= 10: c = (c + (rest[9] << 16)) & _M
    if n >= 9: c = (c + (rest[8] << 8)) & _M
    if n >= 8: b = (b + (rest[7] << 24)) & _M
    if n >= 7: b = (b + (rest[6] << 16)) & _M
    if n >= 6: b = (b + (rest[5] << 8)) & _M
    if n >= 5: b = (b + rest[4]) & _M
    if n >= 4: a = (a + (rest[3] << 24)) & _M
    if n >= 3: a = (a + (rest[2] << 16)) & _M
    if n >= 2: a = (a + (rest[1] << 8)) & _M
    if n >= 1: a = (a + rest[0]) & _M
    a, b, c = _vlt_mix(a, b, c)
    return c
