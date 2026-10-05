#!/usr/bin/env python3
"""Print the VmFlags of a process's mapping whose path ends with SUFFIX.

  vmflags.py PID SUFFIX        e.g. vmflags.py 1234 member/snap/db

Exit 0 and print the flags (space separated) if found; exit 2 if no such mapping.
Used for the variant guard: bbolt's MADV_RANDOM shows up as the `rr` flag
(VM_RAND_READ) on the db mapping, so control must have `rr` and treatment must not.
"""
import re
import sys

HEADER = re.compile(r"^[0-9a-f]+-[0-9a-f]+\s")


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    pid, suffix = sys.argv[1], sys.argv[2]
    in_target = False
    with open(f"/proc/{pid}/smaps") as f:
        for line in f:
            if HEADER.match(line):
                parts = line.split(None, 5)
                path = parts[5].strip() if len(parts) == 6 else ""
                in_target = path.endswith(suffix)
            elif in_target and line.startswith("VmFlags:"):
                print(line.split(":", 1)[1].strip())
                return
    sys.exit(2)


if __name__ == "__main__":
    main()
