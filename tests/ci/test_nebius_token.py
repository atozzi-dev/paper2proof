"""The deploy job's Nebius credential step (.github/scripts/nebius_token.py).

The Nebius exchange itself is not exercised here (spike S-7 is pending); these tests pin what
the step does around it: which GitHub token it asks for, that every credential is masked before
anything else is printed, and that nothing secret is left on disk.
"""

import http.client
import io
import json
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.ci import load_script

tok = load_script(".github/scripts/nebius_token.py")

JWT = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJyZXBvIn0.c2lnbmF0dXJl"
ACCESS = "v1.nebius-access-token-value"


class FakeResponse(io.BytesIO):
    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def test_github_jwt_is_requested_for_the_audience_with_the_runner_token() -> None:
    seen: dict[str, Any] = {}

    def opener(request: Any, timeout: float) -> FakeResponse:
        seen["url"] = request.full_url
        seen["auth"] = request.get_header("Authorization")
        seen["timeout"] = timeout
        return FakeResponse(json.dumps({"value": JWT}).encode())

    env = {
        "ACTIONS_ID_TOKEN_REQUEST_URL": "https://token.actions.example/x?api-version=2.0",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "runner-request-token",
    }
    assert tok.request_github_jwt(env, "sts.nebius.com", opener) == JWT
    assert seen["url"] == (
        "https://token.actions.example/x?api-version=2.0&audience=sts.nebius.com"
    )
    assert seen["auth"] == "Bearer runner-request-token"
    assert seen["timeout"] > 0


def test_github_jwt_needs_id_token_write() -> None:
    with pytest.raises(tok.TokenError, match="id-token: write"):
        tok.request_github_jwt({}, "aud", lambda *a, **k: None)


def _run(
    tmp_path: Path,
    env: dict[str, str],
    capsys: pytest.CaptureFixture[str],
    exchange: Any = None,
) -> tuple[int, str, list[Any]]:
    calls: list[Any] = []

    def fake_jwt(env_: dict[str, str], audience: str, opener: Any = None) -> str:
        calls.append(("jwt", audience))
        return JWT

    def fake_exchange(kind: str, path: Path, sa_id: str | None) -> str:
        calls.append((kind, path.read_text(encoding="utf-8"), sa_id, oct(path.stat().st_mode)))
        return ACCESS

    out_file = tmp_path / "nebius.token"
    code = tok.main(
        ["--out", str(out_file)],
        env={"RUNNER_TEMP": str(tmp_path), **env},
        fetch_jwt=fake_jwt,
        exchange=exchange or fake_exchange,
    )
    return code, capsys.readouterr().out, calls


OIDC_ENV = {
    "NEBIUS_AUTH_MODE": "oidc",
    "NEBIUS_CI_SA_ID": "serviceaccount-e00ci",
    "NEBIUS_OIDC_AUDIENCE": "sts.nebius.com",
}


def test_oidc_mode_masks_first_writes_a_private_token_file_and_removes_the_jwt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, calls = _run(tmp_path, OIDC_ENV, capsys)
    assert code == 0
    assert calls[0] == ("jwt", "sts.nebius.com")
    assert calls[1][:3] == ("federated", JWT, "serviceaccount-e00ci")
    lines = out.splitlines()
    assert lines[0] == f"::add-mask::{JWT}"  # masked before anything else is printed
    assert f"::add-mask::{ACCESS}" in lines
    after_mask = lines.index(f"::add-mask::{ACCESS}") + 1
    assert JWT not in "\n".join(lines[1:]) and ACCESS not in "\n".join(lines[after_mask:])
    token_file = tmp_path / "nebius.token"
    assert token_file.read_text(encoding="utf-8") == ACCESS
    if sys.platform != "win32":
        assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
        assert calls[1][3].endswith("600")
    # Only the access token file is left behind.
    assert sorted(p.name for p in tmp_path.iterdir()) == ["nebius.token"]


def test_key_mode_uses_the_environment_secret_and_deletes_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    creds = json.dumps({"subject-credentials": {"kid": "publickey-1", "private-key": "PEM"}})
    env = {"NEBIUS_AUTH_MODE": "key", "NEBIUS_SA_CREDENTIALS": creds}
    code, out, calls = _run(tmp_path, env, capsys)
    assert code == 0
    assert calls[0][:3] == ("credentials-file", creds, None)
    assert out.splitlines()[0] == f"::add-mask::{ACCESS}"
    assert "PEM" not in out
    assert sorted(p.name for p in tmp_path.iterdir()) == ["nebius.token"]


@pytest.mark.parametrize(
    ("env", "missing"),
    [
        ({"NEBIUS_AUTH_MODE": "oidc"}, "NEBIUS_CI_SA_ID"),
        ({**OIDC_ENV, "NEBIUS_OIDC_AUDIENCE": ""}, "NEBIUS_OIDC_AUDIENCE"),
        ({"NEBIUS_AUTH_MODE": "key"}, "NEBIUS_SA_CREDENTIALS"),
        ({"NEBIUS_AUTH_MODE": "password"}, "NEBIUS_AUTH_MODE"),
        ({}, "NEBIUS_AUTH_MODE"),
    ],
)
def test_bad_configuration_fails_without_echoing_values(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], env: dict[str, str], missing: str
) -> None:
    code, out, calls = _run(tmp_path, env, capsys)
    assert code == 2
    assert missing in out
    assert calls == []
    assert not (tmp_path / "nebius.token").exists()


def test_exchange_failure_leaves_no_files_and_prints_no_secret(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def failing(kind: str, path: Path, sa_id: str | None) -> str:
        raise tok.TokenError(f"exchange refused for {path.read_text(encoding='utf-8')[:3]}...")

    code, out, _ = _run(tmp_path, OIDC_ENV, capsys, exchange=failing)
    assert code == 1
    assert "::error::" in out
    assert list(tmp_path.iterdir()) == []


def test_an_empty_token_is_an_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, _, _ = _run(tmp_path, OIDC_ENV, capsys, exchange=lambda *a: "")
    assert code == 1
    assert not (tmp_path / "nebius.token").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_private_files_are_created_0600(tmp_path: Path) -> None:
    path = tmp_path / "f"
    tok.write_private(path, "x")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "extra",
    [{"NEBIUS_SA_CREDENTIALS": "{}"}, {"NEBIUS_SA_CREDENTIALS_SET": "true"}],
)
def test_oidc_mode_refuses_a_leftover_fallback_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], extra: dict[str, str]
) -> None:
    code, out, calls = _run(tmp_path, {**OIDC_ENV, **extra}, capsys)
    assert code == 2
    assert "delete it" in out
    assert calls == []


def test_http_protocol_errors_become_safe_token_errors() -> None:
    def opener(request: Any, timeout: float) -> FakeResponse:
        raise http.client.IncompleteRead(b"partial-secret")

    env = {"ACTIONS_ID_TOKEN_REQUEST_URL": "https://x/y?a=1", "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "t"}
    with pytest.raises(tok.TokenError) as err:
        tok.request_github_jwt(env, "aud", opener)
    assert "partial-secret" not in str(err.value)
    assert "IncompleteRead" in str(err.value)


def test_a_multi_line_token_is_refused_before_it_is_printed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _run(tmp_path, OIDC_ENV, capsys, exchange=lambda *a: "line1\nline2")
    assert code == 1
    assert "line2" not in out
    assert not (tmp_path / "nebius.token").exists()


def test_a_write_error_is_reported_without_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_write(path: Path, text: str) -> None:
        raise PermissionError(13, "denied", str(path))

    monkeypatch.setattr(tok, "write_private", broken_write)
    code, out, _ = _run(tmp_path, OIDC_ENV, capsys)
    assert code == 1
    assert "::error::cannot write a credential file: PermissionError" in out
    assert JWT not in out.split("\n", 1)[1]


def test_oidc_mode_prints_the_subject_and_audience_it_presents(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """On "Incorrect subject id" the operator needs the sub GitHub signed; the SDK error itself
    is never printed (it may echo request fields)."""
    code, out, _ = _run(tmp_path, OIDC_ENV, capsys)
    assert code == 0
    assert "GitHub OIDC token claims: sub=repo aud=None ref=None environment=None" in out
    assert out.index("::add-mask::") < out.index("GitHub OIDC token claims")


def test_unreadable_claims_are_reported_without_the_token() -> None:
    assert tok.describe_claims("not-a-jwt") == "unreadable"
    assert tok.describe_claims("a.!!!.c") == "unreadable"
