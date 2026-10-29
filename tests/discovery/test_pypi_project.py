"""The authors' package on PyPI: planned only when the PyPI project's own metadata links back
to the official repository (a project that merely shares the repository's name could be
anybody's: supply-chain risk). Any doubt adds nothing."""

from collections.abc import Callable

import httpx
import pytest
import respx

from app.discovery.github import GITHUB_API
from app.discovery.pypi_project import (
    MAX_BODY_BYTES,
    authors_package,
    project_links,
    repo_of,
)
from app.execution.package_policy import PYPI
from app.ingestion.arxiv import USER_AGENT

REPO = "https://github.com/catboost/catboost"
MOVED = "https://github.com/microsoft/interpret"  # what the paper links; now interpretml's


def _info(**info: object) -> dict[str, object]:
    blank: dict[str, object] = {"project_urls": None, "home_page": None, "download_url": None}
    return {"info": {**blank, **info}, "releases": {"1.0": []}}


def _pypi(**info: object) -> httpx.Response:
    return httpx.Response(200, json=_info(**info))


@pytest.mark.parametrize(
    "link",
    [
        "https://github.com/catboost/catboost",
        "https://GitHub.com/CatBoost/CatBoost/",
        "https://github.com/catboost/catboost.git",
        "https://github.com/catboost/catboost.git/",
        "https://github.com/catboost/catboost/tree/master/catboost/python-package",
        "https://github.com/catboost/catboost/issues",
        "https://github.com/catboost/catboost#readme",
        "http://www.github.com/catboost/catboost",
        "  https://github.com/catboost/catboost  ",
    ],
)
def test_links_into_the_repository_name_it(link: str) -> None:
    assert repo_of(link) == "catboost/catboost"


@pytest.mark.parametrize(
    "link",
    [
        "https://catboost.ai",
        "https://github.com/catboost",
        "https://gitlab.com/catboost/catboost",
        "https://github.com.example.org/catboost/catboost",
        "https://example.org/github.com/catboost/catboost",
        "https://user@github.com/catboost/catboost",
        "https://github.com:8443/catboost/catboost",
        "ftp://github.com/catboost/catboost",
        "https://github.com/../catboost",
        "https://github.com/catboost/.git",
        "https://github.com/catboost/catboost\nhttps://x",
        "",
        None,
        3,
    ],
)
def test_anything_else_names_no_repository(link: object) -> None:
    assert repo_of(link) is None


def test_the_links_are_the_project_urls_the_home_page_and_the_download_url() -> None:
    body = httpx.Response(
        200,
        json=_info(
            project_urls={"Source": "https://github.com/a/b", "Docs": "https://b.dev", "x": 1},
            home_page="https://b.dev/home",
            download_url="https://github.com/a/b/releases",
            description="https://github.com/a/b in a README does not count",
        ),
    ).content
    assert project_links(body) == [
        "https://github.com/a/b",
        "https://b.dev",
        "https://b.dev/home",
        "https://github.com/a/b/releases",
    ]


@pytest.mark.parametrize(
    "body", [b"not json", b"[1, 2]", b'{"info": "x"}', b'{"info": {"project_urls": [1]}}']
)
def test_odd_metadata_has_no_links(body: bytes) -> None:
    assert project_links(body) == []


async def test_a_project_that_links_the_repository_is_the_authors() -> None:
    with respx.mock(assert_all_called=False) as router:
        route = router.get(f"{PYPI}/catboost/json").mock(
            return_value=_pypi(project_urls={"GitHub": "https://github.com/catboost/catboost"})
        )
        github = router.get(url__startswith=GITHUB_API)
        assert await authors_package(REPO, planned=set()) == "catboost"
    request = route.calls.last.request
    assert request.headers["user-agent"] == USER_AGENT and "authorization" not in request.headers
    assert not github.called  # the repository was not moved: GitHub is never asked


@respx.mock
async def test_a_moved_repository_is_matched_under_its_new_name() -> None:
    """InterpretML: the paper links microsoft/interpret, PyPI links interpretml/interpret, and
    GitHub's own redirect says they are the same repository."""
    respx.get(f"{PYPI}/interpret/json").mock(
        return_value=_pypi(home_page="https://github.com/interpretml/interpret")
    )
    respx.get(f"{GITHUB_API}/repos/microsoft/interpret").mock(
        return_value=httpx.Response(301, headers={"location": f"{GITHUB_API}/repositories/1"})
    )
    respx.get(f"{GITHUB_API}/repositories/1").mock(
        return_value=httpx.Response(200, json={"full_name": "interpretml/interpret"})
    )
    assert await authors_package(MOVED, planned=set()) == "interpret"


@respx.mock
async def test_a_project_that_merely_shares_the_name_is_not_the_authors() -> None:
    respx.get(f"{PYPI}/catboost/json").mock(
        return_value=_pypi(
            project_urls={"Source": "https://github.com/someone/catboost"},
            home_page="https://github.com/catboost/catboost-extra",
        )
    )
    respx.get(f"{GITHUB_API}/repos/catboost/catboost").mock(
        return_value=httpx.Response(200, json={"full_name": "catboost/catboost"})
    )
    assert await authors_package(REPO, planned=set()) is None


@pytest.mark.parametrize(
    "github",
    [
        httpx.Response(500),
        httpx.Response(200, json={"full_name": 3}),
        httpx.Response(200, json=["catboost"]),
        httpx.Response(200, content=b"not json"),
        httpx.ConnectError("down"),
    ],
)
@respx.mock
async def test_a_mismatch_github_cannot_explain_adds_nothing(
    github: httpx.Response | Exception,
) -> None:
    respx.get(f"{PYPI}/catboost/json").mock(
        return_value=_pypi(home_page="https://github.com/someone/catboost")
    )
    route = respx.get(f"{GITHUB_API}/repos/catboost/catboost")
    if isinstance(github, Exception):
        route.mock(side_effect=github)
    else:
        route.mock(return_value=github)
    assert await authors_package(REPO, planned=set()) is None


@respx.mock
async def test_the_normalized_name_is_tried_when_the_repository_name_is_not_on_pypi() -> None:
    respx.get(f"{PYPI}/Py_KAN/json").mock(return_value=httpx.Response(404))
    respx.get(f"{PYPI}/py-kan/json").mock(
        return_value=_pypi(project_urls={"Source": "https://github.com/someone/Py_KAN"})
    )
    assert await authors_package("https://github.com/someone/Py_KAN", planned=set()) == "py-kan"


async def test_pypi_answering_for_one_spelling_answers_for_all() -> None:
    """pykan (KAN): its PyPI metadata declares no links at all, so it is never planned."""
    with respx.mock(assert_all_called=False) as router:
        first = router.get(f"{PYPI}/Py_KAN/json").mock(return_value=_pypi())
        second = router.get(f"{PYPI}/py-kan/json")
        github = router.get(url__startswith=GITHUB_API)
        assert await authors_package("https://github.com/someone/Py_KAN", planned=set()) is None
    assert first.called and not second.called
    assert not github.called  # no link into any repository: nothing a move could explain


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(404),
        httpx.Response(301, headers={"location": "https://evil.example/pypi/catboost/json"}),
        httpx.Response(200, content=b"x" * (MAX_BODY_BYTES + 1)),
        httpx.ConnectError("down"),
        httpx.ReadTimeout("slow"),
    ],
)
@respx.mock
async def test_any_doubt_about_pypi_adds_nothing(answer: httpx.Response | Exception) -> None:
    route = respx.get(f"{PYPI}/catboost/json")
    if isinstance(answer, Exception):
        route.mock(side_effect=answer)
    else:
        route.mock(return_value=answer)
    assert await authors_package(REPO, planned=set()) is None


async def test_a_package_already_planned_is_not_looked_up() -> None:
    with respx.mock(assert_all_called=False) as router:
        route = router.get(url__startswith=PYPI)
        assert await authors_package(REPO, planned={"catboost"}) is None
        assert not route.called


@pytest.mark.parametrize(
    "repo",
    ["https://gitlab.com/a/b", "https://github.com/a/..", "https://github.com/a/-x-"],
)
async def test_a_repository_without_a_valid_package_name_is_not_looked_up(repo: str) -> None:
    with respx.mock(assert_all_called=False) as router:
        route = router.get(url__startswith=PYPI)
        assert await authors_package(repo, planned=set()) is None
        assert not route.called


async def test_the_metadata_is_parsed_through_the_cpu_pool() -> None:
    seen: list[Callable[[bytes], list[str]]] = []

    async def run_cpu(fn: Callable[[bytes], list[str]], body: bytes) -> list[str]:
        seen.append(fn)
        return fn(body)

    async with httpx.AsyncClient() as http:
        with respx.mock as router:
            router.get(f"{PYPI}/catboost/json").mock(
                return_value=_pypi(download_url="https://github.com/catboost/catboost")
            )
            name = await authors_package(REPO, planned=set(), http=http, run_cpu=run_cpu)
        assert not http.is_closed  # a client the caller passed is the caller's to close
    assert name == "catboost" and seen == [project_links]
