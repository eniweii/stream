"""Small stdlib reader for the block-style YAML that the Carbon vault export (Attribulator) writes.

No PyYAML needed. It reads exactly the subset the vault files use:
  - a top-level list of maps ("- Key: value" items), or a plain map
  - nested maps by indent, nested lists ("- value" or "- Key: value")
  - scalars: int, float, true/false, null/~, plain strings, 'single' and "double" quoted strings
  - empty flow collections [] and {}
CRLF and LF line endings both work. Tokens that YAML 1.1 would turn into numbers by accident
(hex like 0x9A11381C, digit groups like 4_4_2) stay strings, which is what they are in the vault. Anything outside this subset raises ValueError with the
file line, so a different vault layout fails loudly instead of loading wrong.

    from nfs_vault_yaml import load
    records = load(path)          # list of dicts for gameplay.yml
"""
import re

_INT = re.compile(r"^[-+]?\d+$")
_FLOAT = re.compile(r"^[-+]?(\d+\.\d*|\.\d+|\d+)([eE][-+]?\d+)?$")


def _scalar(text, lineno):
    t = text.strip()
    if t == "":
        return None
    if t[0] == "'":
        if len(t) < 2 or t[-1] != "'":
            raise ValueError("line %d: bad single-quoted scalar %r" % (lineno, t))
        return t[1:-1].replace("''", "'")
    if t[0] == '"':
        if len(t) < 2 or t[-1] != '"':
            raise ValueError("line %d: bad double-quoted scalar %r" % (lineno, t))
        return t[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    if t == "[]":
        return []
    if t == "{}":
        return {}
    if t[0] in "[{&*!|>":
        raise ValueError("line %d: unsupported YAML construct %r" % (lineno, t))
    low = t.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "~"):
        return None
    if _INT.match(t):
        return int(t)
    if _FLOAT.match(t):
        return float(t)
    return t


def _split_key(body, lineno):
    """'Key: value' -> (key, value_text) ; None when the body is a plain scalar."""
    if body[:1] in ("'", '"'):
        return None
    i = body.find(": ")
    if i < 0:
        if body.endswith(":"):
            return body[:-1], ""
        return None
    return body[:i], body[i + 2:]


def _prepare(text):
    lines = []
    for n, raw in enumerate(text.replace("\r\n", "\n").replace("\r", "\n").split("\n"), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw.startswith("---") or raw.startswith("..."):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if raw[indent:indent + 1] == "\t":
            raise ValueError("line %d: tab indent" % n)
        lines.append((indent, raw[indent:].rstrip(), n))
    return lines


def _parse_block(lines, pos, indent):
    """Parse the block whose lines start at lines[pos] with this indent. Returns (value, new_pos)."""
    if pos >= len(lines):
        return None, pos
    body = lines[pos][1]
    if body.startswith("- ") or body == "-":
        return _parse_list(lines, pos, indent)
    return _parse_map(lines, pos, indent)


def _parse_list(lines, pos, indent):
    out = []
    while pos < len(lines):
        ind, body, n = lines[pos]
        if ind < indent:
            break
        if ind > indent:
            raise ValueError("line %d: unexpected indent in list" % n)
        if not (body.startswith("- ") or body == "-"):
            break
        rest = body[2:] if body != "-" else ""
        if rest == "":
            # item value on the following deeper lines
            if pos + 1 < len(lines) and lines[pos + 1][0] > indent:
                val, pos = _parse_block(lines, pos + 1, lines[pos + 1][0])
            else:
                val, pos = None, pos + 1
            out.append(val)
            continue
        kv = _split_key(rest, n)
        if kv is None:
            out.append(_scalar(rest, n))
            pos += 1
            continue
        # "- Key: value" opens a map whose keys sit at indent + 2
        sub_indent = indent + 2
        lines[pos] = (sub_indent, rest, n)
        val, pos = _parse_map(lines, pos, sub_indent)
        out.append(val)
    return out, pos


def _parse_map(lines, pos, indent):
    out = {}
    while pos < len(lines):
        ind, body, n = lines[pos]
        if ind < indent:
            break
        if ind > indent:
            raise ValueError("line %d: unexpected indent in map" % n)
        if body.startswith("- ") or body == "-":
            break
        kv = _split_key(body, n)
        if kv is None:
            raise ValueError("line %d: expected 'key: value', got %r" % (n, body))
        key, vtext = kv
        key = _scalar(key, n) if key[:1] in ("'", '"') else key
        if vtext.strip() == "":
            if pos + 1 < len(lines) and (lines[pos + 1][0] > indent or
                                          (lines[pos + 1][0] == indent and
                                           (lines[pos + 1][1].startswith("- ") or lines[pos + 1][1] == "-"))):
                child_indent = lines[pos + 1][0]
                val, pos = _parse_block(lines, pos + 1, child_indent)
            else:
                val, pos = None, pos + 1
        else:
            val, pos = _scalar(vtext, n), pos + 1
        out[key] = val
    return out, pos


def loads(text):
    lines = _prepare(text)
    if not lines:
        return None
    val, pos = _parse_block(lines, 0, lines[0][0])
    if pos != len(lines):
        raise ValueError("line %d: could not parse the rest of the file" % lines[pos][2])
    return val


def load(path):
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return loads(fh.read())
