from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app.domain.models import PackageReq
from app.execution.package_policy import (
    PackagePolicy,
    PolicyContext,
    PyPIClient,
    Reason,
    levenshtein,
    load_policy_config,
    parse_repo_requirements,
    typosquat_threshold,
)
from app.storage.store import Store

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 20, tzinfo=UTC)


def _pypi(name: str, *, releases: int = 20, first_days_ago: int = 2000) -> httpx.Response:
    first = NOW - timedelta(days=first_days_ago)
    data: dict[str, Any] = {
        "info": {"name": name},
        "releases": {
            f"1.{i}": [{"upload_time_iso_8601": (first + timedelta(days=i)).isoformat()}]
            for i in range(releases)
        },
    }
    return httpx.Response(200, json=data)


@pytest.fixture
def policy(store: Store) -> PackagePolicy:
    return PackagePolicy(
        load_policy_config(ROOT / "config"),
        PyPIClient(store),
        now=lambda: NOW,
    )


async def _decide(policy: PackagePolicy, *names: str, **ctx: Any) -> list[Reason]:
    reqs = [PackageReq(name=n) for n in names]
    decision = await policy.validate(reqs, PolicyContext(**ctx))
    return [d.reason for d in decision.decisions]


def test_levenshtein() -> None:
    assert levenshtein("requests", "reqeusts") == 1  # an adjacent swap is one edit (OSA)
    assert levenshtein("tqdm", "tqmd") == 1
    assert levenshtein("ab", "ba") == 1
    assert levenshtein("numpy", "numpy") == 0
    assert levenshtein("torch", "torchvision") == 6


@pytest.mark.parametrize(
    "raw",
    [
        "git+https://github.com/a/b",
        "https://evil.example/pkg.whl",
        "../local/pkg",
        "pkg @ https://x/y.whl",
        "--index-url",
        "pkg; os_name=='nt'",
        "pkg[extra]",
        "",
    ],
)
async def test_syntax_rejects_urls_paths_and_options(policy: PackagePolicy, raw: str) -> None:
    decision = await policy.validate(
        [PackageReq(name=raw or "x", specifier=None)] if raw else [], PolicyContext()
    )
    if raw:
        assert decision.decisions[0].reason is Reason.REJECT_SYNTAX


async def test_bad_specifiers_are_rejected(policy: PackagePolicy) -> None:
    decision = await policy.validate(
        [PackageReq(name="einops", specifier="@ https://x")], PolicyContext()
    )
    assert decision.decisions[0].reason is Reason.REJECT_SYNTAX


@respx.mock
async def test_typosquat_is_rejected(policy: PackagePolicy) -> None:
    respx.get("https://pypi.org/pypi/reqeusts/json").mock(return_value=_pypi("reqeusts"))
    assert await _decide(policy, "reqeusts") == [Reason.REJECT_TYPOSQUAT]


def test_typosquat_threshold_scales_with_length() -> None:
    assert typosquat_threshold("mlx", "jax") == 1  # 2-4 letters: one edit only
    assert typosquat_threshold("torh", "torch") == 1  # the shorter name decides
    assert typosquat_threshold("reqeusts", "requests") == 2
    assert typosquat_threshold("numpyy", "numpy") == 2


@respx.mock
@pytest.mark.parametrize("name", ["mlx", "pot", "ogb", "tfds"])
async def test_short_real_names_are_not_typosquats(policy: PackagePolicy, name: str) -> None:
    """Two edits from jax/six/py/dgl/tqdm, but real packages: a fixed distance of 2 is noise."""
    respx.get(f"https://pypi.org/pypi/{name}/json").mock(return_value=_pypi(name))
    assert await _decide(policy, name) == [Reason.ACCEPT]


@respx.mock
@pytest.mark.parametrize("name", ["jaxx", "torh", "nmpy", "tqmd", "pfet", "ntlk"])
async def test_one_edit_from_a_short_popular_name_is_a_typosquat(
    policy: PackagePolicy, name: str
) -> None:
    respx.get(f"https://pypi.org/pypi/{name}/json").mock(return_value=_pypi(name))
    assert await _decide(policy, name) == [Reason.REJECT_TYPOSQUAT]


@respx.mock
async def test_popular_and_established_packages_pass(policy: PackagePolicy) -> None:
    respx.get("https://pypi.org/pypi/einops/json").mock(return_value=_pypi("einops"))
    respx.get("https://pypi.org/pypi/requests/json").mock(return_value=_pypi("requests"))
    assert await _decide(policy, "einops", "requests") == [Reason.ACCEPT, Reason.ACCEPT]


async def test_denylisted_tools_are_rejected(policy: PackagePolicy) -> None:
    assert await _decide(policy, "pip", "UV", "SetupTools") == [Reason.REJECT_DENYLIST] * 2 + [
        Reason.REJECT_DENYLIST
    ]


@respx.mock
async def test_unknown_packages_are_rejected(policy: PackagePolicy) -> None:
    respx.get("https://pypi.org/pypi/no-such-pkg-xyz/json").mock(return_value=httpx.Response(404))
    assert await _decide(policy, "no-such-pkg-xyz") == [Reason.REJECT_NOT_FOUND]


@respx.mock
async def test_young_packages_need_the_repo_requirements(policy: PackagePolicy) -> None:
    respx.get("https://pypi.org/pypi/brandnewlib/json").mock(
        return_value=_pypi("brandnewlib", releases=2, first_days_ago=10)
    )
    assert await _decide(policy, "brandnewlib") == [Reason.REJECT_UNPOPULAR]
    assert await _decide(policy, "brandnewlib", repo_requirements={"brandnewlib"}) == [
        Reason.ACCEPT
    ]


@respx.mock
async def test_quota_per_request_and_per_run(policy: PackagePolicy) -> None:
    for i in range(6):
        respx.get(f"https://pypi.org/pypi/lib{i}xyzq/json").mock(return_value=_pypi(f"lib{i}"))
    reasons = await _decide(policy, *(f"lib{i}xyzq" for i in range(6)))
    assert reasons[:5] == [Reason.ACCEPT] * 5
    assert reasons[5] is Reason.REJECT_QUOTA
    assert await _decide(policy, "lib0xyzq", requests_so_far=2) == [Reason.REJECT_QUOTA]


@respx.mock
async def test_pypi_metadata_is_cached(policy: PackagePolicy) -> None:
    route = respx.get("https://pypi.org/pypi/einops/json").mock(return_value=_pypi("einops"))
    await _decide(policy, "einops")
    await _decide(policy, "einops")
    assert route.call_count == 1


def test_repo_requirements_are_parsed_from_common_files() -> None:
    files = {
        "requirements.txt": "torch>=2.0\nEinops==0.8  # comment\n-r other.txt\ngit+https://x/y\n",
        "requirements-dev.txt": "pytest\n",
        "pyproject.toml": '[project]\ndependencies = ["timm>=1.0", "Pillow"]\n',
        "setup.cfg": "[options]\ninstall_requires =\n    scikit_learn>=1.3\n    tqdm\n",
        "environment.yml": "dependencies:\n  - python=3.10\n  - pip:\n    - lightning==2.2\n",
        "README.md": "pip install fakepkg",
    }
    assert parse_repo_requirements(files) == {
        "torch", "einops", "pytest", "timm", "pillow", "scikit-learn", "tqdm", "lightning",
    }  # fmt: skip


def test_hostile_or_odd_repo_files_contribute_nothing() -> None:
    bomb = "a: &a [x, x, x]\nb: &b [*a, *a, *a]\ndependencies: [*b]\n"
    odd = "dependencies:\n  - pip: [1, {x: y}, null, einops]\n  - 7\n  - pip: 5\n"
    assert parse_repo_requirements({"environment.yml": bomb}) == set()  # aliases are refused
    assert parse_repo_requirements({"environment.yml": odd}) == {"einops"}
    assert parse_repo_requirements({"environment.yml": "dependencies: 5\n"}) == set()
    assert parse_repo_requirements({"environment.yaml": "- just\n- a list\n"}) == set()
    assert (
        parse_repo_requirements({"pyproject.toml": '[project]\ndependencies = "torch"\n'}) == set()
    )
    assert parse_repo_requirements({"pyproject.toml": "tool = 5\nproject = 3\n"}) == set()


@respx.mock
async def test_specifiers_allow_plain_spaces_only_and_are_normalized(
    policy: PackagePolicy,
) -> None:
    respx.get("https://pypi.org/pypi/einops/json").mock(return_value=_pypi("einops"))
    for bad in ["==0.8\n", ">=0.7,\t<1", "==0.8\r", "\n==0.8"]:
        decision = await policy.validate(
            [PackageReq(name="einops", specifier=bad)], PolicyContext()
        )
        assert decision.decisions[0].reason is Reason.REJECT_SYNTAX, bad
    ok = await policy.validate(
        [PackageReq(name="einops", specifier=" >= 0.7 , < 1 ")], PolicyContext()
    )
    assert ok.accepted[0].specifier == ">=0.7,<1"


def test_deeply_nested_repo_files_contribute_nothing() -> None:
    deep_yaml = "dependencies: " + "[" * 5000 + "]" * 5000 + "\n"
    deep_toml = "x = " + "[" * 5000 + "]" * 5000 + "\n"
    assert parse_repo_requirements({"environment.yml": deep_yaml}) == set()
    assert parse_repo_requirements({"pyproject.toml": deep_toml}) == set()
