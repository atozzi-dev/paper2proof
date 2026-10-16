"""License gate (A-05, architecture §13.2): the shipped dependencies stay Apache-2.0 compatible."""

import json
from pathlib import Path

import pytest

from tests.ci import load_script

gate = load_script("scripts/license_gate.py")


@pytest.mark.parametrize(
    "text",
    [
        "GPL-3.0-only",
        "GPL-2.0-or-later",
        "AGPL-3.0",
        "AGPL-3.0-or-later",
        "GPLv3",
        "GNU General Public License v3 (GPLv3)",
        "GNU Affero General Public License v3",
        "GPL-2.0 OR GPL-3.0",
        "Apache-2.0 AND GPL-3.0-only",
        "(MIT AND AGPL-3.0)",
    ],
)
def test_strong_copyleft_is_detected(text: str) -> None:
    assert gate.is_strong_copyleft(text)


@pytest.mark.parametrize(
    "text",
    [
        "MIT",
        "Apache-2.0",
        "BSD-3-Clause",
        "LGPL-2.1-or-later",
        "LGPLv3+",
        "GNU Lesser General Public License v2 or later (LGPLv2+)",
        "GNU Library or Lesser General Public License (LGPL)",
        "MIT OR GPL-2.0",
        "(Apache-2.0 OR BSD-3-Clause)",
        "Apache-2.0 OR GPL-3.0-or-later",
        "MPL-2.0",
        "",
    ],
)
def test_permissive_weak_copyleft_and_dual_licenses_pass(text: str) -> None:
    assert not gate.is_strong_copyleft(text)


def test_requirement_names_come_from_uv_export() -> None:
    exported = (
        "aiohttp==3.14.5\n"
        "cffi==2.1.1 ; platform_python_implementation != 'PyPy'\n"
        "# comment\n"
        "\n"
        "sqlalchemy==2.1.4\n"
    )
    assert gate.requirement_names(exported) == ["aiohttp", "cffi", "sqlalchemy"]


def _meta(expression: str = "", license_field: str = "", classifiers: tuple[str, ...] = ()):
    return gate.PythonLicense(expression, license_field, classifiers)


def test_license_expression_is_authoritative() -> None:
    meta = _meta(
        "MIT", classifiers=("License :: OSI Approved :: GNU General Public License (GPL)",)
    )
    assert gate.python_verdict(meta) == "ok"


def test_only_copyleft_classifiers_fail() -> None:
    gpl = "License :: OSI Approved :: GNU General Public License v3 (GPLv3)"
    assert gate.python_verdict(_meta(classifiers=(gpl,))) == "copyleft"
    mit = "License :: OSI Approved :: MIT License"
    # Several license classifiers mean the user may pick any of them.
    assert gate.python_verdict(_meta(classifiers=(gpl, mit))) == "ok"


def test_license_field_is_read_by_name_or_by_the_title_of_the_full_text() -> None:
    assert gate.python_verdict(_meta(license_field="GPL-3.0")) == "copyleft"
    body = "\n" + "Permission is hereby granted ... " * 20
    assert gate.python_verdict(_meta(license_field="The MIT License (MIT)" + body)) == "ok"
    gpl_text = "GNU GENERAL PUBLIC LICENSE\nVersion 3, 29 June 2007" + body
    assert gate.python_verdict(_meta(license_field=gpl_text)) == "copyleft"
    lgpl_text = "GNU LESSER GENERAL PUBLIC LICENSE\nVersion 3, 29 June 2007" + body
    assert gate.python_verdict(_meta(license_field=lgpl_text)) == "ok"
    untitled = "Copyright (c) Someone. " + "Permission is hereby granted ... " * 20
    assert gate.python_verdict(_meta(license_field=untitled)) == "unknown"
    assert gate.python_verdict(_meta()) == "unknown"


def test_python_gate_fails_on_copyleft_and_reports_unknowns() -> None:
    licenses = {
        "good": _meta("Apache-2.0"),
        "bad": _meta(
            classifiers=("License :: OSI Approved :: GNU Affero General Public License v3",)
        ),
        "mystery": _meta(),
    }
    result = gate.check_python(["good", "bad", "mystery", "absent"], licenses.get)
    assert result.violations == ["bad: GNU Affero General Public License v3"]
    assert result.unknown == ["absent: not installed", "mystery: no license metadata"]


def _lock(packages: dict[str, dict[str, object]]) -> str:
    return json.dumps({"lockfileVersion": 3, "packages": {"": {"name": "web"}, **packages}})


def test_npm_gate_checks_only_shipped_packages() -> None:
    lock = _lock(
        {
            "node_modules/react": {"version": "19.2.8", "license": "MIT"},
            "node_modules/bad-runtime": {"version": "1.0.0", "license": "GPL-3.0"},
            "node_modules/bad-tool": {"version": "1.0.0", "license": "GPL-3.0", "dev": True},
            "node_modules/opt-tool": {
                "version": "1.0.0",
                "license": "AGPL-3.0",
                "devOptional": True,
            },
            "node_modules/nameless": {"version": "1.0.0"},
            "node_modules/a/node_modules/nested": {"version": "2.0.0", "license": "AGPL-3.0"},
        }
    )
    result = gate.check_npm(lock)
    assert result.violations == [
        "a/node_modules/nested@2.0.0: AGPL-3.0",
        "bad-runtime@1.0.0: GPL-3.0",
    ]
    assert result.unknown == ["nameless@1.0.0: no license field"]


def test_npm_legacy_license_objects_are_read() -> None:
    lock = _lock({"node_modules/old": {"version": "0.1.0", "license": {"type": "GPL-2.0"}}})
    assert gate.check_npm(lock).violations == ["old@0.1.0: GPL-2.0"]


def test_main_fails_on_an_empty_requirement_list(tmp_path: Path) -> None:
    # `uv export | license_gate.py` must not pass vacuously when the export fails.
    lock = tmp_path / "package-lock.json"
    lock.write_text(_lock({}), encoding="utf-8")
    reqs = tmp_path / "reqs.txt"
    reqs.write_text("", encoding="utf-8")
    assert gate.main(["--python-requirements", str(reqs), "--npm-lock", str(lock)]) == 2


def test_main_passes_on_this_repository(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    reqs = tmp_path / "reqs.txt"
    reqs.write_text("pyyaml==6.0.3\npydantic==2.14.1\n", encoding="utf-8")
    lock = gate.ROOT / "web" / "package-lock.json"
    assert gate.main(["--python-requirements", str(reqs), "--npm-lock", str(lock)]) == 0
    assert "license gate: OK" in capsys.readouterr().out
