#!/usr/bin/env python3
"""Fail when a workflow references aw-shared-workflows at anything but @master.

Usage: check_master_pin.py PATH [PATH ...]

Each PATH is a workflow file, a `.github/workflows` dir, or a repo checkout
root (searched for `.github/workflows/*.y*ml`; a directory of repo checkouts
is NOT recursed beyond that, pass the repos explicitly or use a shell glob).
Exit 0 = clean, 1 = at least one non-@master reference, 2 = usage/IO error.

Matches `uses:` lines for `fredericowu/aw-shared-workflows` and
`tekflox/aw-shared-workflows`. Comment-only lines and trailing `# ...` text
are ignored. : it runs on a bare runner or agent box.
"""
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    print("error: PyYAML is required (pip install pyyaml)", file=sys.stderr)
    sys.exit(2)

USES = re.compile(
    r"^(?P<owner>fredericowu|tekflox)/aw-shared-workflows/(?P<path>[^@\s]+)@(?P<ref>\S+)$"
)
ALLOWED_REF = "master"


def _uses_values(doc):
    """Every `uses:` value under jobs.<id>.uses and jobs.<id>.steps[].uses."""
    jobs = doc.get("jobs") if isinstance(doc, dict) else None
    for job in (jobs.values() if isinstance(jobs, dict) else []):
        if not isinstance(job, dict):
            continue
        yield job.get("uses")
        for step in job.get("steps") or []:
            if isinstance(step, dict):
                yield step.get("uses")


def scan_text(text: str):
    """Yield (owner, path, ref) for every non-@master reference.

    Parses the YAML rather than matching lines, so quoted keys, folded
    scalars and trailing comments are handled by the parser and `uses:` text
    inside a `run:` script is not mistaken for a reference.
    """
    for value in _uses_values(yaml.safe_load(text)):
        if isinstance(value, str):
            m = USES.match(value.strip())
            if m and m["ref"] != ALLOWED_REF:
                yield m["owner"], m["path"], m["ref"]


def workflow_files(arg: str):
    p = Path(arg)
    if p.is_file():
        return [p]
    wf = p / ".github" / "workflows" if (p / ".github" / "workflows").is_dir() else p
    return sorted(f for f in wf.glob("*") if f.suffix in (".yml", ".yaml") and f.is_file())


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
            try:
                hits = list(scan_text(f.read_text()))
            except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
                print(f"error: {f}: {e}", file=sys.stderr)
                return 2
            for owner, path, ref in hits:
                bad += 1
                print(f"{f}: {owner}/aw-shared-workflows/{path}@{ref} "
                      f"-- must be @{ALLOWED_REF}")
    if bad:
        print(f"{bad} non-@{ALLOWED_REF} reference(s)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
