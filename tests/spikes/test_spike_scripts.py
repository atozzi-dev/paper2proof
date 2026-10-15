"""Static checks on the day-1 spike kit: no network, no credentials, nothing executed remotely."""

import re
import subprocess
import sys
from pathlib import Path

import pytest

SPIKES = Path(__file__).resolve().parents[2] / "spikes"

# Every spike named in the roadmap (A-02). S1 and S8 are manual console checks (see README).
EXPECTED = [
    "s2_models.py",
    "s3_sandboxes.py",
    "s4_gpu_job.sh",
    "s5_job_gateway.sh",
    "s6_endpoint.sh",
    "s7_oidc.yml",
    "s9_hf_papers.py",
    "s10_sg_scan.sh",
    "s11_buckets.py",
    "s12_pull_time.sh",
    "s13_sse_probe/client.py",
    "s13_sse_probe/server.py",
]
EXECUTABLE = [name for name in EXPECTED if not name.endswith(".yml")]
PYTHON = [name for name in EXPECTED if name.endswith(".py")]
VERDICT_RE = re.compile(r"VERDICT: (\{|\$|PASS|FAIL|PARTIAL)")


def _text(name: str) -> str:
    return (SPIKES / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", EXPECTED)
def test_spike_exists(name: str) -> None:
    assert (SPIKES / name).is_file(), f"missing spikes/{name}"


# The S13 server is the probe *target*; the client and s6_endpoint.sh print its verdicts.
@pytest.mark.parametrize("name", [n for n in EXPECTED if not n.endswith("server.py")])
def test_spike_prints_a_verdict_line(name: str) -> None:
    assert VERDICT_RE.search(_text(name)), f"spikes/{name} never prints 'VERDICT: ...'"


@pytest.mark.parametrize("name", EXECUTABLE)
def test_spike_documents_help(name: str) -> None:
    text = _text(name)
    # argparse generates --help; test_python_spike_help_runs_offline proves it works.
    assert "--help" in text or "argparse.ArgumentParser" in text, f"spikes/{name} has no --help"


@pytest.mark.parametrize("name", [n for n in EXECUTABLE if n.endswith(".sh")])
def test_shell_spike_is_strict(name: str) -> None:
    text = _text(name)
    assert text.startswith("#!/usr/bin/env bash\n")
    assert "set -euo pipefail" in text


@pytest.mark.parametrize("name", PYTHON)
def test_python_spike_help_runs_offline(name: str) -> None:
    # --help must exit 0 before touching the network or reading credentials.
    result = subprocess.run(  # noqa: S603 (fixed argv, local file)
        [sys.executable, str(SPIKES / name), "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": "", "SYSTEMROOT": "C:\\Windows"},
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "usage" in result.stdout.lower()


def test_readme_lists_every_spike_command() -> None:
    readme = _text("README.md")
    for name in EXPECTED:
        if name.endswith("server.py"):
            continue
        assert name in readme, f"spikes/README.md never mentions {name}"


@pytest.mark.parametrize("name", EXPECTED)
def test_spike_never_prints_raw_credentials(name: str) -> None:
    text = _text(name)
    leaks = re.findall(
        r"(?:print|echo)[^\n]*\$\{?(?:NEBIUS_API_KEY|TAVILY_API_KEY|HF_TOKEN|NEBIUS_IAM_TOKEN)\b",
        text,
    )
    leaks += re.findall(
        r"print\([^\n]*\b(?:api_key|NEBIUS_API_KEY|TAVILY_API_KEY|access_token|refresh_token)\b",
        text,
    )
    assert not leaks, f"spikes/{name} prints a credential: {leaks}"


def test_s7_prints_only_allowlisted_exchange_fields() -> None:
    text = _text("s7_oidc.yml")
    assert "json.dumps(payload)" not in text
    assert "SAFE_KEYS" in text


def test_s7_installs_cli_from_a_pinned_checksum_before_any_token() -> None:
    text = _text("s7_oidc.yml")
    assert "| bash" not in text
    assert "sha256sum -c" in text
    assert text.index("sha256sum -c") < text.index("ACTIONS_ID_TOKEN_REQUEST_URL")


def test_readme_s7_teardown_deletes_the_federated_credential() -> None:
    readme = _text("README.md")
    assert "federated-credentials delete" in readme
    assert "throwaway" in readme.lower()


@pytest.fixture
def probe_server() -> object:
    import importlib.util

    path = SPIKES / "s13_sse_probe" / "server.py"
    spec = importlib.util.spec_from_file_location("probe_server", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_probe_server_clamps_and_validates_sse_params(probe_server: object) -> None:
    parse = probe_server.parse_sse_params  # type: ignore[attr-defined]
    assert parse({"hb": ["0"], "duration": ["99999"]}) == (1, 900)
    assert parse({}) == (15, 600)
    assert parse({"hb": ["abc"]}) is None


def test_probe_server_caps_concurrent_streams(probe_server: object) -> None:
    assert probe_server.MAX_STREAMS <= 8  # type: ignore[attr-defined]


def test_spikelib_never_forwards_authorization_on_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib.util
    import urllib.request

    spec = importlib.util.spec_from_file_location("spikelib", SPIKES / "spikelib.py")
    assert spec and spec.loader
    spikelib = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "spikelib", spikelib)  # dataclasses resolve their module
    spec.loader.exec_module(spikelib)
    seen: list[urllib.request.Request] = []

    def fake_urlopen(request: urllib.request.Request, timeout: float) -> object:
        seen.append(request)
        raise OSError("offline test")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    spikelib.http("GET", "https://example.invalid/x", headers={"Authorization": "Bearer t0k3n"})
    request = seen[0]
    assert "Authorization" not in request.headers
    assert request.unredirected_hdrs.get("Authorization") == "Bearer t0k3n"


def test_probe_server_drops_idle_connections(probe_server: object) -> None:
    assert 0 < probe_server.Handler.timeout <= 60  # type: ignore[attr-defined]
