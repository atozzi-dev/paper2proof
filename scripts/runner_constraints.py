"""Runner constraints from the runner lock (A-06). Standard library only: it also runs inside the
image builds.

The provisioner locks a run's extra packages against these constraints and leaves out what the
runner already provides (images/provisioner/provision.py). Public versions only: a local label
such as `+cpu` would not match the PyPI wheels the provisioner resolves.

Usage: python scripts/runner_constraints.py <requirements.lock> [<out>]
"""

import re
import sys
from pathlib import Path

_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([0-9][^\s;\\+]*)(?:\+\S*)?")
HEADER = "# Generated from the runner lock by scripts/runner_constraints.py: do not edit.\n"


def constraints(lock: str) -> str:
    pins = {}
    for line in lock.splitlines():
        match = _PIN.match(line)
        if match:
            name = re.sub(r"[-_.]+", "-", match.group(1)).lower()  # PEP 503 normal form
            pins[name] = match.group(2)
    return HEADER + "".join(f"{name}=={pins[name]}\n" for name in sorted(pins))


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3):
        print(__doc__, file=sys.stderr)
        return 2
    text = constraints(Path(argv[1]).read_text(encoding="utf-8"))
    if len(argv) == 3:
        Path(argv[2]).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
