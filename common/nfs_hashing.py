"""
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
