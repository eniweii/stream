"""
Loads a plain wordlist (one candidate string per line, '#' comments
allowed) from the same directory as this file - NOT embedded in the code -
and hashes every line with bin_hash() (bin family) or vlt_hash() (VLT / Attrib key family) to
build a reverse hash->name lookup. Which family a hash belongs to depends on what it keys:
scenery group keys are bin, vault collection keys and collision surface hashes are VLT.

Point this at CiPH3R-88/NFS_Raider_v2.0's hashes_main.txt (or any similarly-
shaped wordlist) by dropping it next to these scripts as hashes_main.txt.
Kept external and optional: nothing in this project requires this file to
be present, it just resolves more names when it is.
"""
# stream bootstrap: make the shared library folders importable
import pathlib as _pl, sys as _sys
_sys.path[:0] = [str(_pl.Path(__file__).resolve().parents[1] / _d) for _d in ['common', 'region', 'scenery', 'solids_materials']]

import os

from nfs_hashing import bin_hash, vlt_hash

_DEFAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hashes_main.txt")

_cache = {}  # path -> {hash: name}
_vlt_cache = {}  # path -> {hash: name}


def load_dictionary(path=_DEFAULT_PATH):
    """Returns {hash: name}. Empty dict if the file isn't present - this is
    an optional companion file, not a hard dependency."""
    if path in _cache:
        return _cache[path]

    table = {}
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                table[bin_hash(line)] = line
    except FileNotFoundError:
        pass

    _cache[path] = table
    return table


def resolve(hash_value, path=_DEFAULT_PATH):
    return load_dictionary(path).get(hash_value)


def load_vlt_dictionary(path=_DEFAULT_PATH):
    """Returns {vlt hash: name} from the same wordlist. Lines with non-ASCII characters are
    skipped (they cannot be vault names). Empty dict if the file isn't present."""
    if path in _vlt_cache:
        return _vlt_cache[path]

    table = {}
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or not line.isascii():
                    continue
                table.setdefault(vlt_hash(line), line)
    except FileNotFoundError:
        pass

    _vlt_cache[path] = table
    return table


def resolve_vlt(hash_value, path=_DEFAULT_PATH):
    return load_vlt_dictionary(path).get(hash_value)
