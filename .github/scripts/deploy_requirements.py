"""Write or check .github/deploy-requirements.txt: the nebius SDK closure from uv.lock, hashed.

The deploy job needs only the nebius SDK (for nebius_token.py). It installs exactly these pinned
wheels with `pip install --require-hashes --only-binary :all: --no-deps`, instead of the whole
project, so nothing else runs (or builds) on the runner that can mint the OIDC token.

    uv run python .github/scripts/deploy_requirements.py > .github/deploy-requirements.txt
    uv run python .github/scripts/deploy_requirements.py --check   # tests/ci runs this
"""

import argparse
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
LOCK = ROOT / "uv.lock"
TARGET = ROOT / ".github" / "deploy-requirements.txt"
ROOT_PACKAGE = "nebius"
HEADER = (
    "# Generated from uv.lock by .github/scripts/deploy_requirements.py: do not edit.\n"
    "# The nebius SDK and its dependencies, for the deploy job only (wheels, hashes).\n"
)


def markers(lock: dict[str, Any], names: set[str]) -> dict[str, str]:
    """Environment marker per package: empty if any edge into it is unconditional."""
    found: dict[str, set[str]] = {}
    for package in lock["package"]:
        if package["name"] not in names:
            continue
        for dep in package.get("dependencies", []):
            found.setdefault(dep["name"], set()).add(dep.get("marker", ""))
    return {
        name: "" if "" in edges else " or ".join(f"({m})" for m in sorted(edges))
        for name, edges in found.items()
    }


def closure(lock: dict[str, Any], root: str = ROOT_PACKAGE) -> list[dict[str, Any]]:
    by_name: dict[str, dict[str, Any]] = {}
    for package in lock["package"]:
        if package["name"] in by_name:
            raise ValueError(f"{package['name']} is locked at two versions; pin one")
        by_name[package["name"]] = package
    seen: dict[str, dict[str, Any]] = {}
    pending = [root]
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen[name] = by_name[name]
        pending.extend(dep["name"] for dep in by_name[name].get("dependencies", []))
    return [seen[name] for name in sorted(seen)]


def render(lock: dict[str, Any]) -> str:
    blocks = []
    packages = closure(lock)
    marker_of = markers(lock, {p["name"] for p in packages})
    for package in packages:
        hashes = [wheel["hash"] for wheel in package.get("wheels", [])]
        if not hashes:
            raise ValueError(f"{package['name']} has no wheel: the deploy job installs wheels only")
        marker = marker_of.get(package["name"], "")
        lines = [f"{package['name']}=={package['version']}" + (f" ; {marker}" if marker else "")]
        lines += [f"    --hash={h}" for h in sorted(hashes)]
        blocks.append(" \\\n".join(lines))
    return HEADER + "\n".join(blocks) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help=f"compare with {TARGET.name}")
    args = parser.parse_args(argv)
    text = render(tomllib.loads(LOCK.read_text(encoding="utf-8")))
    if not args.check:
        sys.stdout.write(text)
        return 0
    if TARGET.read_text(encoding="utf-8") != text:
        print(f"{TARGET.relative_to(ROOT)} is stale: regenerate it from uv.lock (see its header)")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
