"""The admin CLI helper (docs/OPERATIONS.md): it calls /admin/* with the token from the
environment or a prompt, never from the command line."""

import hashlib
import io
import json
from collections.abc import Callable

import httpx
import pytest

from app import admin_cli

URL = "https://p2p.example"
TOKEN = "t" * 64


class Recorder:
    def __init__(self, status: int = 200, body: object = None) -> None:
        self.requests: list[httpx.Request] = []
        self.status = status
        self.body = body

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status == 204:
            return httpx.Response(204)
        return httpx.Response(self.status, json=self.body)

    def only(self) -> httpx.Request:
        assert len(self.requests) == 1
        return self.requests[0]


def _run(
    argv: list[str],
    recorder: Recorder,
    *,
    env: dict[str, str] | None = None,
    stdin: str = "",
    prompt: Callable[[str], str] | None = None,
) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()

    def no_prompt(label: str) -> str:
        raise AssertionError("the token was in the environment")

    code = admin_cli.main(
        argv,
        environ={"ADMIN_TOKEN": TOKEN, "P2P_API_URL": URL} if env is None else env,
        transport=httpx.MockTransport(recorder),
        stdin=io.StringIO(stdin),
        stdout=out,
        stderr=err,
        prompt=prompt or no_prompt,
    )
    return code, out.getvalue(), err.getvalue()


def test_invite_create_uses_the_default_quota_and_prints_the_code_once() -> None:
    recorder = Recorder(201, {"code": "new-code"})
    code, out, _ = _run(["invite", "create"], recorder)
    request = recorder.only()
    assert code == 0 and out.strip() == "new-code"
    assert (request.method, str(request.url)) == ("POST", f"{URL}/admin/invites")
    assert request.headers["x-admin-token"] == TOKEN
    assert json.loads(request.content) == {"quota_per_day": 3, "expires_at": None}


def test_invite_create_with_quota_and_expiry() -> None:
    recorder = Recorder(201, {"code": "c"})
    argv = ["invite", "create", "--quota", "5", "--expires", "2026-12-15T00:00:00Z"]
    assert _run(argv, recorder)[0] == 0
    body = json.loads(recorder.only().content)
    assert body == {"quota_per_day": 5, "expires_at": "2026-12-15T00:00:00Z"}


def test_the_token_is_prompted_for_when_not_in_the_environment() -> None:
    recorder = Recorder(200, [])
    asked: list[str] = []

    def prompt(label: str) -> str:
        asked.append(label)
        return TOKEN

    code, _, _ = _run(["invite", "list"], recorder, env={"P2P_API_URL": URL}, prompt=prompt)
    assert code == 0 and asked and recorder.only().headers["x-admin-token"] == TOKEN


def test_the_token_is_never_taken_from_the_command_line() -> None:
    with pytest.raises(SystemExit) as exit_info:
        _run(["--token", TOKEN, "invite", "list"], Recorder())
    assert exit_info.value.code == 2


@pytest.mark.parametrize("url", ["http://p2p.example", "ftp://x", "https://user:pw@p2p.example"])
def test_only_https_urls_are_accepted(url: str) -> None:
    recorder = Recorder(200, [])
    code, _, err = _run(["--url", url, "invite", "list"], recorder)
    assert code == 2 and "https" in err and not recorder.requests


def test_plain_http_is_allowed_for_a_local_dev_server() -> None:
    recorder = Recorder(200, [])
    assert _run(["--url", "http://localhost:8000", "invite", "list"], recorder)[0] == 0
    assert str(recorder.only().url) == "http://localhost:8000/admin/invites"


def test_revoke_by_code_hashes_it_locally() -> None:
    recorder = Recorder(204)
    code, _, _ = _run(["invite", "revoke", "--code-stdin"], recorder, stdin="the-code\n")
    request = recorder.only()
    assert code == 0 and str(request.url) == f"{URL}/admin/invites/revoke"
    assert json.loads(request.content) == {"id": hashlib.sha256(b"the-code").hexdigest()}
    assert b"the-code" not in request.content


def test_revoke_by_id() -> None:
    recorder = Recorder(204)
    assert _run(["invite", "revoke", "--id", "ab" * 6], recorder)[0] == 0
    assert json.loads(recorder.only().content) == {"id": "ab" * 6}


def test_gallery_add_and_remove() -> None:
    run_id = "r_" + "a" * 26
    recorder = Recorder(204)
    assert _run(["gallery", "add", run_id, "--reviewed"], recorder)[0] == 0
    assert _run(["gallery", "remove", run_id], recorder)[0] == 0
    first, second = recorder.requests
    assert str(first.url) == f"{URL}/admin/gallery/{run_id}"
    assert json.loads(first.content) == {"listed": True, "reviewed": True}
    assert json.loads(second.content) == {"listed": False, "reviewed": False}


def test_a_refused_request_exits_non_zero_with_the_server_message() -> None:
    recorder = Recorder(409, {"error": "a NOT_REPRODUCED verdict is listed only after a review"})
    code, _, err = _run(["gallery", "add", "r_" + "a" * 26], recorder)
    assert code == 1 and "409" in err and "review" in err


def test_runs_listing_passes_the_filters() -> None:
    recorder = Recorder(200, [{"id": "r_x"}])
    code, out, _ = _run(["runs", "--unlisted", "--limit", "10"], recorder)
    request = recorder.only()
    assert code == 0 and json.loads(out) == [{"id": "r_x"}]
    assert request.url.path == "/admin/runs"
    assert dict(request.url.params) == {"listed": "false", "limit": "10"}
