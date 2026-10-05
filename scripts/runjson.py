#!/usr/bin/env python3
"""Tiny run.json editor so shell scripts never hand-write JSON.

  runjson.py set FILE key=value [key=value ...]   (ints, floats, true/false typed)
  runjson.py get FILE key                          (prints value, empty if missing)
"""
import json
import os
import re
import sys
import tempfile


NUMBER = re.compile(r"^-?\d+(\.\d+)?([eE][-+]?\d+)?$")


def typed(v, key=""):
    """Booleans and plain decimal numbers are typed; everything else (including
    "infinity", "nan") stays a string. Identifiers (*_rev, *_sha, vm, hostname) always
    stay strings, so an all-digit hash keeps its leading zeros."""
    if key.endswith(("_rev", "_sha")) or key in ("vm", "hostname", "order", "reason"):
        return v
    if v in ("true", "false"):
        return v == "true"
    if NUMBER.match(v):
        return float(v) if any(c in v for c in ".eE") else int(v)
    return v


def load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save(path, data):
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".runjson.")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def main(argv):
    if len(argv) < 3:
        sys.exit(__doc__)
    cmd, path = argv[1], argv[2]
    data = load(path)
    if cmd == "set":
        for kv in argv[3:]:
            k, _, v = kv.partition("=")
            data[k] = typed(v, k)
        save(path, data)
    elif cmd == "get":
        v = data.get(argv[3], "")
        print("" if v is None else v)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
