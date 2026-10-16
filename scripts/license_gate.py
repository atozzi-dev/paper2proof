"""License gate (architecture §13.2): fail if a shipped dependency is GPL or AGPL.

The repository is Apache-2.0 and must stay so. Only what ships is checked: the Python runtime
dependencies (``uv export --no-dev``) and the npm packages that are not dev-only (they end up
in ``web/dist``). LGPL, MPL and dual licenses with a permissive option pass. Packages with no
readable license are listed but do not fail the gate.

    make licenses   # uv export --no-dev ... | python scripts/license_gate.py ...

Uses the standard library only, so it runs without the pip-licenses / license-checker
downloads the roadmap named.
"""

import argparse
import json
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Any, Literal

ROOT = Path(__file__).resolve().parents[1]

# GPL or AGPL in SPDX ids, short names or classifier wording; never LGPL ("Lesser"/"Library").
_COPYLEFT = re.compile(
    r"(?<![A-Za-z])A?GPL(?:v?\d|-|\b)|GNU (?:Affero )?General Public License",
    re.IGNORECASE,
)
_SPDX_OR = re.compile(r"\s+OR\s+")
_SPDX_AND = re.compile(r"\s+AND\s+")
_REQUIREMENT = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*==")
_LICENSE_CLASSIFIER = "License :: "
# A License field longer than this is the full license text: only its title line is read.
_MAX_LICENSE_NAME = 120

Verdict = Literal["ok", "copyleft", "unknown"]


def is_strong_copyleft(text: str) -> bool:
    """True if every alternative of a license (or SPDX expression) is GPL or AGPL."""
    expression = text.strip().strip("()").strip()
    alternatives = _SPDX_OR.split(expression)
    if len(alternatives) > 1:
        return all(is_strong_copyleft(part) for part in alternatives)
    return any(bool(_COPYLEFT.search(part)) for part in _SPDX_AND.split(expression))


@dataclass(frozen=True)
class PythonLicense:
    expression: str  # PEP 639 License-Expression
    license_field: str  # legacy free-text License
    classifiers: tuple[str, ...]  # "License :: ..." trove classifiers


@dataclass
class GateResult:
    violations: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)


def _license_field_name(text: str) -> str:
    """The License field as a name: itself if short, else the title line of the full text."""
    text = text.strip()
    if len(text) > _MAX_LICENSE_NAME:
        text = text.splitlines()[0].strip()
    return text if len(text) <= _MAX_LICENSE_NAME else ""


def _classifier_name(classifier: str) -> str:
    return classifier.rsplit("::", 1)[-1].strip()


def python_verdict(meta: PythonLicense) -> Verdict:
    if meta.expression:
        return "copyleft" if is_strong_copyleft(meta.expression) else "ok"
    names = [_classifier_name(c) for c in meta.classifiers]
    names = [n for n in names if n not in {"OSI Approved", "Other/Proprietary License"}]
    field_text = _license_field_name(meta.license_field)
    if field_text:
        names.append(field_text)
    if not names:
        return "unknown"
    # Several license names mean the user may pick any of them.
    return "copyleft" if all(is_strong_copyleft(n) for n in names) else "ok"


def _describe(meta: PythonLicense) -> str:
    if meta.expression:
        return meta.expression
    names = [_classifier_name(c) for c in meta.classifiers]
    return "; ".join(names) or _license_field_name(meta.license_field)


def requirement_names(exported: str) -> list[str]:
    names = []
    for line in exported.splitlines():
        match = _REQUIREMENT.match(line.strip())
        if match:
            names.append(match.group(1))
    return names


def installed_license(name: str) -> PythonLicense | None:
    try:
        meta = metadata.metadata(name)
    except metadata.PackageNotFoundError:
        return None
    classifiers = tuple(
        c for c in meta.get_all("Classifier") or [] if c.startswith(_LICENSE_CLASSIFIER)
    )
    return PythonLicense(
        meta.get("License-Expression") or "",
        meta.get("License") or "",
        classifiers,
    )


def check_python(
    names: Sequence[str], lookup: Callable[[str], PythonLicense | None] = installed_license
) -> GateResult:
    result = GateResult()
    for name in names:
        meta = lookup(name)
        if meta is None:
            result.unknown.append(f"{name}: not installed")
            continue
        verdict = python_verdict(meta)
        if verdict == "copyleft":
            result.violations.append(f"{name}: {_describe(meta)}")
        elif verdict == "unknown":
            result.unknown.append(f"{name}: no license metadata")
    result.violations.sort()
    result.unknown.sort()
    return result


def _npm_license(entry: dict[str, Any]) -> str:
    value = entry.get("license", "")
    if isinstance(value, dict):  # legacy {"type": "MIT", "url": ...}
        value = value.get("type", "")
    if isinstance(value, list):  # legacy "licenses" arrays
        value = " OR ".join(str(v.get("type", v)) if isinstance(v, dict) else str(v) for v in value)
    return str(value)


def check_npm(lock_text: str) -> GateResult:
    """Packages from an npm v2/v3 lockfile that ship (not dev, not devOptional)."""
    result = GateResult()
    packages: dict[str, dict[str, Any]] = json.loads(lock_text).get("packages", {})
    for path, entry in packages.items():
        if not path.startswith("node_modules/") or entry.get("dev") or entry.get("devOptional"):
            continue
        label = f"{path.removeprefix('node_modules/')}@{entry.get('version', '?')}"
        license_text = _npm_license(entry)
        if not license_text:
            result.unknown.append(f"{label}: no license field")
        elif is_strong_copyleft(license_text):
            result.violations.append(f"{label}: {license_text}")
    result.violations.sort()
    result.unknown.sort()
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--python-requirements",
        required=True,
        help="`uv export --no-dev` output, or - for stdin",
    )
    parser.add_argument("--npm-lock", required=True, type=Path, help="web/package-lock.json")
    args = parser.parse_args(argv)

    if args.python_requirements == "-":
        exported = sys.stdin.read()
    else:
        exported = Path(args.python_requirements).read_text(encoding="utf-8")
    names = requirement_names(exported)
    if not names:
        print("license gate: no Python requirements read (did `uv export` fail?)")
        return 2

    python = check_python(names)
    npm = check_npm(args.npm_lock.read_text(encoding="utf-8"))
    for title, result in (("python", python), ("npm", npm)):
        for line in result.unknown:
            print(f"license gate: {title}: unknown license, check by hand: {line}")
        for line in result.violations:
            print(f"license gate: {title}: GPL/AGPL dependency: {line}")
    if python.violations or npm.violations:
        print("license gate: FAILED (the repository is Apache-2.0; see architecture §13.2)")
        return 1
    print(f"license gate: OK ({len(names)} Python, npm lock {args.npm_lock.name})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
