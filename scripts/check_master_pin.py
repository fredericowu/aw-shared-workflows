#!/usr/bin/env python3
"""Fail when a workflow references aw-shared-workflows at anything but @master.

Usage: check_master_pin.py PATH [PATH ...]

Each PATH is a workflow file, a `.github/workflows` dir, or a repo checkout
root (searched for `.github/workflows/*.y*ml`; a directory of repo checkouts
is NOT recursed beyond that, pass the repos explicitly or use a shell glob).
Exit 0 = clean, 1 = at least one non-@master reference, 2 = usage/IO error.

Matches `uses:` lines for `fredericowu/aw-shared-workflows` and
`tekflox/aw-shared-workflows`. Comment-only lines and trailing `# ...` text
are ignored. Stdlib only on purpose: it runs on a bare runner or agent box.
"""
import re
import sys
from pathlib import Path

USES = re.compile(
    r"^\s*(?:-\s*)?uses:\s*['\"]?(?P<owner>fredericowu|tekflox)/aw-shared-workflows/"
    r"(?P<path>[^@\s'\"]+)@(?P<ref>[^\s'\"#]+)"
)
ALLOWED_REF = "master"


def scan_text(text: str):
    """Yield (lineno, owner, path, ref) for every non-@master reference."""
    for n, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        m = USES.match(line)
        if m and m["ref"] != ALLOWED_REF:
            yield n, m["owner"], m["path"], m["ref"]


def workflow_files(arg: str):
    p = Path(arg)
    if p.is_file():
        return [p]
    wf = p / ".github" / "workflows" if (p / ".github" / "workflows").is_dir() else p
    return sorted(f for f in wf.glob("*") if f.suffix in (".yml", ".yaml"))


def main(argv) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    bad = 0
    for arg in argv:
        if not Path(arg).exists():
            print(f"error: {arg} does not exist", file=sys.stderr)
            return 2
        for f in workflow_files(arg):
            for n, owner, path, ref in scan_text(f.read_text()):
                bad += 1
                print(f"{f}:{n}: {owner}/aw-shared-workflows/{path}@{ref} "
                      f"-- must be @{ALLOWED_REF}")
    if bad:
        print(f"{bad} non-@{ALLOWED_REF} reference(s)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
