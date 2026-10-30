"""Set A (canaries) and set B (real papers) runners on a fake pipeline, and the HTTP pipeline."""

import json
import runpy
import sys
import warnings
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr

from app.domain.models import (
    Claim,
    ErrorCode,
    ReasonCode,
    RunStatus,
    Verdict,
    VerdictKind,
)
from eval import run_set_a, run_set_b
from eval.canaries import build
from eval.pipeline import (
    INVITE_ENV,
    HttpPipeline,
    InviteRefused,
    PipelineClaim,
    PipelineRun,
    attempt,
    check_api_url,
    invite_from_env,
)
from eval.scoring import match_claims

CANARIES = build.discover()
NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)


def _verdict(kind: VerdictKind) -> Verdict:
    reason = ReasonCode.HIGH_VARIANCE if kind is VerdictKind.INCONCLUSIVE else None
    return Verdict(
        kind=kind,
        reason=reason,
        n_seeds=3,
        mean=0.9,
        std=0.01,
        ci_low=0.88,
        ci_high=0.92,
        tolerance={"w": 0.02, "v": 0.01},
    )


def _plain(claim: Claim, cid: str) -> Claim:
    data = claim.model_dump(exclude={"planted"})
    return Claim.model_validate({**data, "id": cid})


class FakePipeline:
    """Extracts the canary's claims exactly (renumbered p1, p2, ...) and returns `verdict_of`."""

    def __init__(self, verdict_of: Any = None, fail: set[str] | None = None) -> None:
        self.verdict_of = verdict_of or (lambda canary, claim: canary.truth.verdicts[claim.id])
        self.fail = fail or set()
        self.calls: list[tuple[str, str | None]] = []

    async def run(self, arxiv_id: str, *, claim_hint: str | None = None) -> PipelineRun:
        self.calls.append((arxiv_id, claim_hint))
        if arxiv_id in self.fail:
            raise httpx.ConnectError("down")
        canary = next(c for c in CANARIES if c.truth.arxiv_id == arxiv_id)
        claims = [
            PipelineClaim(claim=_plain(c, f"p{i}"), selected=True)
            for i, c in enumerate(reversed(canary.truth.claims), start=1)
        ]
        verdicts = {
            f"p{i}": _verdict(self.verdict_of(canary, c))
            for i, c in enumerate(reversed(canary.truth.claims), start=1)
        }
        return PipelineRun(
            run_id=f"r_{arxiv_id}",
            arxiv_id=arxiv_id,
            status=RunStatus.COMPLETED,
            error_code=None,
            claims=claims,
            verdicts=verdicts,
            cost_usd=0.30,
            duration_s=600.0,
        )


def test_every_canary_truth_matches_itself_one_to_one() -> None:
    for canary in CANARIES:
        claims = list(canary.truth.claims)
        matches = match_claims(claims, claims)
        assert [(m.truth_id, m.predicted_id) for m in matches] == [(c.id, c.id) for c in claims]


async def test_a_perfect_pipeline_scores_full_marks_on_set_a() -> None:
    pipeline = FakePipeline()
    result = await run_set_a.run_set_a(pipeline, CANARIES, now=lambda: NOW)
    claims = sum(len(c.truth.claims) for c in CANARIES)
    assert result.verdicts.claims == claims
    assert result.verdicts.accuracy == 1.0
    assert result.verdicts.false_accusations == 0
    assert result.extraction.f1 == 1.0
    assert [arxiv for arxiv, _ in pipeline.calls] == [c.truth.arxiv_id for c in CANARIES]
    case = result.cases[0]
    assert case.run.cost_usd == 0.30
    assert all(o.predicted_id is not None for o in case.claims)
    # median over runs of cost / verdicts
    per_claim = sorted(0.30 / len(c.truth.claims) for c in CANARIES)
    assert result.cost.median_usd_per_claim == pytest.approx((per_claim[4] + per_claim[5]) / 2)
    assert result.cost.median_duration_s == 600.0
    assert result.cost.total_usd == pytest.approx(0.30 * len(CANARIES))
    assert result.generated_at == NOW


async def test_accusing_true_claims_and_failed_runs_are_counted() -> None:
    def accuse(canary: build.Canary, claim: build.TruthClaim) -> VerdictKind:
        return VerdictKind.NOT_REPRODUCED

    down = CANARIES[0].truth.arxiv_id
    result = await run_set_a.run_set_a(FakePipeline(accuse, fail={down}), CANARIES)
    true_claims = sum(not c.planted for k in CANARIES[1:] for c in k.truth.claims)
    assert result.verdicts.false_accusations == true_claims
    failed = result.cases[0]
    assert failed.run.status is None and failed.run.error == "ConnectError"
    assert all(o.predicted is None for o in failed.claims)
    assert result.verdicts.missing == len(CANARIES[0].truth.claims)


async def test_an_unselected_claim_has_no_verdict() -> None:
    class Unselected(FakePipeline):
        async def run(self, arxiv_id: str, *, claim_hint: str | None = None) -> PipelineRun:
            run = await super().run(arxiv_id, claim_hint=claim_hint)
            first = run.claims[0].claim.id
            return run.model_copy(
                update={"verdicts": {k: v for k, v in run.verdicts.items() if k != first}}
            )

    result = await run_set_a.run_set_a(Unselected(), CANARIES[:1])
    assert result.verdicts.missing == 1


# --- set B -----------------------------------------------------------------------------------


def test_set_b_holds_the_papers_picked_with_the_human() -> None:
    config = run_set_b.load_set_b()
    ids = [p.arxiv_id for p in config.papers]
    assert ids == ["1909.09223", "2001.05016", "2404.19756", "1706.09516", "1503.02531"]
    assert all(p.claim_hint and p.notes for p in config.papers)


def test_set_b_rejects_anything_but_arxiv_ids(tmp_path: Path) -> None:
    bad = tmp_path / "set_b.yaml"
    bad.write_text('papers:\n  - arxiv_id: "https://arxiv.org/abs/2410.01234"\n')
    with pytest.raises(ValueError, match="arxiv_id"):
        run_set_b.load_set_b(bad)


async def test_set_b_reports_inconclusive_rate_reasons_and_failures() -> None:
    kinds = iter([VerdictKind.REPRODUCED, VerdictKind.INCONCLUSIVE, VerdictKind.NOT_REPRODUCED])

    class Real:
        async def run(self, arxiv_id: str, *, claim_hint: str | None = None) -> PipelineRun:
            if arxiv_id == "2410.00003":
                return PipelineRun(
                    run_id="r_3",
                    arxiv_id=arxiv_id,
                    status=RunStatus.FAILED,
                    error_code=ErrorCode.SMOKE_EXHAUSTED,
                    claims=[],
                    verdicts={},
                    cost_usd=0.10,
                    duration_s=100.0,
                )
            verdicts = {"c1": _verdict(next(kinds))}
            if arxiv_id == "2410.00001":
                verdicts["c2"] = _verdict(next(kinds))
            return PipelineRun(
                run_id=f"r_{arxiv_id[-1]}",
                arxiv_id=arxiv_id,
                status=RunStatus.COMPLETED,
                error_code=None,
                claims=[],
                verdicts=verdicts,
                cost_usd=0.40,
                duration_s=900.0,
            )

    config = run_set_b.SetB.model_validate(
        {
            "papers": [
                {"arxiv_id": "2410.00001", "claim_hint": "Table 2"},
                {"arxiv_id": "2410.00002"},
                {"arxiv_id": "2410.00003"},
            ]
        }
    )
    result = await run_set_b.run_set_b(Real(), config)
    assert result.by_kind == {
        VerdictKind.REPRODUCED: 1,
        VerdictKind.INCONCLUSIVE: 1,
        VerdictKind.NOT_REPRODUCED: 1,
    }
    assert result.by_reason == {ReasonCode.HIGH_VARIANCE: 1}
    assert result.inconclusive_rate == pytest.approx(1 / 3)
    assert result.failed_runs == 1
    assert result.cases[2].run.error_code is ErrorCode.SMOKE_EXHAUSTED
    # per-claim cost: 0.40/2, 0.40/1; the failed run has no verdicts and is left out
    assert result.cost.median_usd_per_claim == pytest.approx(0.30)
    assert result.cost.total_usd == pytest.approx(0.90)


# --- the HTTP pipeline (a deployed Paper2Proof API) --------------------------------------------

API = "https://p2p.test"
RUN = "r_01jb7z00000000000000000000"


def _claim_json(cid: str) -> dict[str, Any]:
    return _plain(CANARIES[0].truth.claims[0], cid).model_dump(mode="json")


def _view(status: str, *, awaiting: bool = False, done: bool = False) -> dict[str, Any]:
    steps = []
    if awaiting:
        steps.append(
            {
                "name": "AWAITING_APPROVAL",
                "attempt": 1,
                "status": "running",
                "started_at": NOW.isoformat(),
                "finished_at": None,
                "error_code": None,
            }
        )
    return {
        "id": RUN,
        "arxiv_id": "0000.00001",
        "status": status,
        "error_code": None,
        "created_at": NOW.isoformat(),
        "finished_at": (NOW + timedelta(minutes=12)).isoformat() if done else None,
        "listed": False,
        "approval": None,
        "steps": steps,
        "claims": [{"claim": _claim_json("c1"), "selected": True}] if done else [],
        "verdicts": (
            [{"claim_id": "c1", "verdict": _verdict(VerdictKind.REPRODUCED).model_dump()}]
            if done
            else []
        ),
        "conditions": None,  # the API always sends it (null until there is something to show)
    }


def _events() -> dict[str, Any]:
    def event(i: int, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": i,
            "run_id": RUN,
            "ts": NOW.isoformat(),
            "type": "cost.update",
            "payload": payload,
        }

    return {
        "events": [
            event(1, {"inference_usd": 0.1, "cloud_usd": 0.0, "total_usd": 0.1}),
            event(2, {"inference_usd": 0.2, "cloud_usd": 0.15, "total_usd": 0.35}),
        ]
    }


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@pytest.mark.parametrize("approve", [True, False])
@respx.mock
async def test_http_pipeline_logs_in_starts_follows_and_decides_approval(approve: bool) -> None:
    session = respx.post(f"{API}/api/session").mock(return_value=httpx.Response(204))
    create = respx.post(f"{API}/api/runs").mock(
        return_value=httpx.Response(
            202, json={"run_id": RUN, "events_url": f"/api/runs/{RUN}/events"}
        )
    )
    respx.get(f"{API}/api/runs/{RUN}").mock(
        side_effect=[
            httpx.Response(200, json=_view("RUNNING")),
            httpx.Response(200, json=_view("RUNNING", awaiting=True)),
            httpx.Response(200, json=_view("COMPLETED", done=True)),
        ]
    )
    decision = respx.post(f"{API}/api/runs/{RUN}/{'approve' if approve else 'reject'}").mock(
        return_value=httpx.Response(204)
    )
    respx.get(f"{API}/api/runs/{RUN}/events.json").mock(
        return_value=httpx.Response(200, json=_events())
    )
    sleeps = Sleeps()
    async with httpx.AsyncClient(base_url=API) as http:
        pipeline = HttpPipeline(
            http, SecretStr("invite-not-real"), approve=approve, poll_s=7, sleep=sleeps
        )
        run = await pipeline.run("0000.00001", claim_hint="Table 1")

    assert json.loads(session.calls[0].request.read()) == {"invite_code": "invite-not-real"}
    assert json.loads(create.calls[0].request.read()) == {
        "arxiv_id": "0000.00001",
        "claim_hint": "Table 1",
    }
    assert decision.call_count == 1
    assert sleeps.calls == [7, 7]
    assert run.status is RunStatus.COMPLETED
    assert [c.claim.id for c in run.claims] == ["c1"]
    assert run.verdicts["c1"].kind is VerdictKind.REPRODUCED
    assert run.cost_usd == pytest.approx(0.35)  # the last cost.update
    assert run.duration_s == pytest.approx(720.0)


@respx.mock
async def test_http_pipeline_cancels_a_run_that_takes_too_long() -> None:
    respx.post(f"{API}/api/session").mock(return_value=httpx.Response(204))
    respx.post(f"{API}/api/runs").mock(
        return_value=httpx.Response(202, json={"run_id": RUN, "events_url": "x"})
    )
    respx.get(f"{API}/api/runs/{RUN}").mock(return_value=httpx.Response(200, json=_view("RUNNING")))
    cancel = respx.post(f"{API}/api/runs/{RUN}/cancel").mock(return_value=httpx.Response(202))
    async with httpx.AsyncClient(base_url=API) as http:
        pipeline = HttpPipeline(
            http, SecretStr("x"), approve=False, poll_s=10, timeout_s=25, sleep=Sleeps()
        )
        with pytest.raises(TimeoutError):
            await pipeline.run("2410.01234")
    assert cancel.called


@respx.mock
async def test_http_pipeline_rejects_non_arxiv_input_before_any_request() -> None:
    route = respx.post(f"{API}/api/session")
    async with httpx.AsyncClient(base_url=API) as http:
        pipeline = HttpPipeline(http, SecretStr("x"), approve=False)
        with pytest.raises(ValueError, match="arXiv ID"):
            await pipeline.run("https://arxiv.org/abs/2410.01234")
    assert not route.called


@respx.mock
async def test_http_pipeline_refuses_a_malformed_run_id() -> None:
    respx.post(f"{API}/api/session").mock(return_value=httpx.Response(204))
    respx.post(f"{API}/api/runs").mock(
        return_value=httpx.Response(202, json={"run_id": "../admin", "events_url": "x"})
    )
    async with httpx.AsyncClient(base_url=API) as http:
        pipeline = HttpPipeline(http, SecretStr("x"), approve=False, sleep=Sleeps())
        with pytest.raises(ValueError, match="run_id"):
            await pipeline.run("2410.01234")


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://p2p.example", True),
        ("http://localhost:8000", True),
        ("http://127.0.0.1:8000/", True),
        ("http://p2p.example", False),
        ("ftp://p2p.example", False),
    ],
)
def test_api_url_is_https_or_local(url: str, ok: bool) -> None:
    if ok:
        assert check_api_url(url) == url.rstrip("/")
    else:
        with pytest.raises(ValueError, match="https"):
            check_api_url(url)


def test_the_invite_code_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(INVITE_ENV, raising=False)
    with pytest.raises(ValueError, match=INVITE_ENV):
        invite_from_env()
    monkeypatch.setenv(INVITE_ENV, "invite-not-real")
    assert invite_from_env().get_secret_value() == "invite-not-real"


async def test_a_failed_run_records_only_the_exception_type() -> None:
    class Leaky:
        async def run(self, arxiv_id: str, *, claim_hint: str | None = None) -> PipelineRun:
            raise httpx.HTTPError("invite-not-real rejected")

    info, run = await attempt(Leaky(), "2410.01234")
    assert run is None and info.error == "HTTPError"
    assert "invite" not in info.model_dump_json()


@respx.mock
async def test_http_pipeline_keeps_the_session_against_a_local_dev_backend() -> None:
    """`make dev` serves plain http on localhost, and the session cookie is `__Host-` (Secure):
    httpx would never send it back, so every run was refused with 401 (set A, 2026-10-23)."""
    local = "http://localhost:8000"
    cookie = "__Host-p2p_session=sess-not-real; Path=/; Secure; HttpOnly; SameSite=Strict"
    respx.post(f"{local}/api/session").mock(
        return_value=httpx.Response(204, headers={"set-cookie": cookie})
    )

    def create(request: httpx.Request) -> httpx.Response:
        if "__Host-p2p_session=sess-not-real" not in request.headers.get("cookie", ""):
            return httpx.Response(401, json={"error": "no session"})
        return httpx.Response(202, json={"run_id": RUN, "events_url": f"/api/runs/{RUN}/events"})

    respx.post(f"{local}/api/runs").mock(side_effect=create)
    respx.get(f"{local}/api/runs/{RUN}").mock(
        return_value=httpx.Response(200, json=_view("COMPLETED", done=True))
    )
    respx.get(f"{local}/api/runs/{RUN}/events.json").mock(
        return_value=httpx.Response(200, json=_events())
    )
    async with httpx.AsyncClient(base_url=local) as http:
        pipeline = HttpPipeline(http, SecretStr("invite-not-real"), approve=True, sleep=Sleeps())
        run = await pipeline.run("0000.00001")
    assert run.status is RunStatus.COMPLETED


async def test_an_http_status_failure_records_the_status_code_too() -> None:
    """`HTTPStatusError` alone hid the 401 of set A (2026-10-23); the code is safe to keep."""

    class Refused:
        async def run(self, arxiv_id: str, *, claim_hint: str | None = None) -> PipelineRun:
            request = httpx.Request("POST", "http://localhost:8000/api/runs")
            response = httpx.Response(401, request=request, json={"error": "invite-not-real"})
            raise httpx.HTTPStatusError("refused", request=request, response=response)

    info, run = await attempt(Refused(), "0000.00001")
    assert run is None and info.error == "HTTPStatusError 401"
    assert "invite" not in info.model_dump_json()


# --- a refused invite code stops the whole set ------------------------------------------------
# A wrong P2P_EVAL_INVITE (2026-10-29): every case got 401 from POST /api/session, then 429 from
# the limiter, and the set "finished" in a second with accuracy 0 over the last results file.


@respx.mock
@pytest.mark.parametrize("status", [401, 429])
async def test_a_refused_invite_raises_without_the_code(status: int) -> None:
    respx.post(f"{API}/api/session").mock(
        return_value=httpx.Response(status, json={"detail": "invite-not-real"})
    )
    create = respx.post(f"{API}/api/runs")
    async with httpx.AsyncClient(base_url=API) as http:
        pipeline = HttpPipeline(http, SecretStr("invite-not-real"), approve=False)
        with pytest.raises(InviteRefused) as err:
            await pipeline.run("2410.01234")
        with pytest.raises(InviteRefused):  # attempt never records it as one failed case
            await attempt(pipeline, "2410.01234")
    assert err.value.status_code == status
    assert f"the invite code was refused (HTTP {status}): check {INVITE_ENV}" in str(err.value)
    assert "invite-not-real" not in str(err.value)
    assert not create.called


@respx.mock
@pytest.mark.parametrize(("which", "status"), [("a", 401), ("b", 401), ("a", 429), ("b", 429)])
def test_a_refused_invite_stops_the_set_and_keeps_the_results(
    which: str,
    status: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(INVITE_ENV, "invite-not-real")
    session = respx.post(f"{API}/api/session").mock(return_value=httpx.Response(status))
    create = respx.post(f"{API}/api/runs")
    out = tmp_path / "results.json"
    out.write_text('{"previous": "results"}\n', encoding="utf-8")
    main = run_set_a.main if which == "a" else run_set_b.main
    code = main(["--api-url", API, "--confirm-spend", "--out", str(out)])
    printed = capsys.readouterr()
    assert code != 0
    assert session.call_count == 1  # stopped at once, not once per case
    assert not create.called
    assert f"the invite code was refused (HTTP {status}): check {INVITE_ENV}" in printed.err
    assert "invite-not-real" not in printed.err + printed.out
    assert out.read_text(encoding="utf-8") == '{"previous": "results"}\n'


@respx.mock
async def test_other_session_failures_stay_per_case() -> None:
    session = respx.post(f"{API}/api/session").mock(return_value=httpx.Response(503))
    config = run_set_b.SetB.model_validate(
        {"papers": [{"arxiv_id": "2410.00001"}, {"arxiv_id": "2410.00002"}]}
    )
    async with httpx.AsyncClient(base_url=API) as http:
        pipeline = HttpPipeline(http, SecretStr("x"), approve=False, sleep=Sleeps())
        result = await run_set_b.run_set_b(pipeline, config)
    assert [c.run.error for c in result.cases] == ["HTTPStatusError 503"] * 2
    assert result.failed_runs == 2
    assert session.call_count == 2


# --- the command lines --------------------------------------------------------------------------


def _one_finished_run() -> respx.Route:
    """The deployment answers one session and finished runs; returns the session route."""
    session = respx.post(f"{API}/api/session").mock(return_value=httpx.Response(204))
    respx.post(f"{API}/api/runs").mock(
        return_value=httpx.Response(202, json={"run_id": RUN, "events_url": "x"})
    )
    respx.get(f"{API}/api/runs/{RUN}").mock(
        return_value=httpx.Response(200, json=_view("COMPLETED", done=True))
    )
    respx.get(f"{API}/api/runs/{RUN}/events.json").mock(
        return_value=httpx.Response(200, json=_events())
    )
    return session


@respx.mock
def test_a_finished_set_writes_its_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(INVITE_ENV, "invite-not-real")
    session = _one_finished_run()
    out_a, out_b = tmp_path / "a.json", tmp_path / "b.json"
    set_b = tmp_path / "set_b.yaml"
    set_b.write_text('papers:\n  - arxiv_id: "2410.00001"\n  - arxiv_id: "2410.00002"\n')
    args = ["--api-url", API, "--confirm-spend"]
    names = [CANARIES[0].name, CANARIES[1].name]
    assert run_set_a.main([*args, "--out", str(out_a), "--only", *names]) == 0
    assert run_set_b.main([*args, "--out", str(out_b), "--set", str(set_b)]) == 0
    assert session.call_count == 2  # one session per set, kept for the following cases
    assert len(json.loads(out_a.read_text(encoding="utf-8"))["cases"]) == 2
    assert len(json.loads(out_b.read_text(encoding="utf-8"))["cases"]) == 2
    printed = capsys.readouterr().out
    assert f"wrote {out_a}: accuracy" in printed and f"wrote {out_b}: inconclusive" in printed


@pytest.mark.parametrize("main", [run_set_a.main, run_set_b.main], ids=["a", "b"])
def test_a_set_never_spends_without_confirmation(
    main: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "results.json"
    assert main(["--api-url", API, "--out", str(out)]) == 2
    assert "--confirm-spend" in capsys.readouterr().err
    assert not out.exists()


def test_an_empty_set_b_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    empty = tmp_path / "set_b.yaml"
    empty.write_text("papers: []\n")
    assert run_set_b.main(["--api-url", API, "--confirm-spend", "--set", str(empty)]) == 2
    assert "lists no papers" in capsys.readouterr().err


@pytest.mark.parametrize("module", ["a", "b"])
def test_the_modules_run_as_scripts(module: str, monkeypatch: pytest.MonkeyPatch) -> None:
    name = f"{run_set_a.__name__[:-1]}{module}"
    monkeypatch.setattr(sys, "argv", [name, "--api-url", API])  # no --confirm-spend: exits 2
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # already imported by this test module
        with pytest.raises(SystemExit) as exit_:
            runpy.run_module(name, run_name="__main__")
    assert exit_.value.code == 2
