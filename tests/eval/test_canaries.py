"""Canary papers (eval set A): the script contract, the ground truth and the allowlist.

The experiments are ours (no LLM, no third-party repository), so running them here is allowed;
LLM-written code for a canary still only ever runs through LocalTrustedExecutor.
"""

import gzip
import json
import math
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.domain.models import ClaimType, VerdictKind
from app.execution.local_trusted import (
    LocalTrustedExecutor,
    LocalTrustedRefused,
    PaperRef,
    load_canaries,
)
from app.ingestion.archive import extract_source
from app.ingestion.arxiv import ARXIV_ID
from app.orchestrator.steps.ingesting import _parse
from eval.canaries import build
from eval.canaries.source import CanaryPaperSource

CANARIES = build.discover()
NAMES = [c.name for c in CANARIES]
IMAGE = "cr.eu-north1.nebius.cloud/reg/p2p-runner-cpu@sha256:" + "c" * 64
RUN = "r_01jb7z00000000000000000000"


def test_ten_canaries_with_one_or_two_claims_each() -> None:
    assert len(CANARIES) == 10
    for canary in CANARIES:
        assert 1 <= len(canary.truth.claims) <= 2, canary.name
        assert set(canary.truth.verdicts) == {c.id for c in canary.truth.claims}


def test_half_the_claims_are_planted_and_every_planted_claim_is_not_reproduced() -> None:
    claims = [(c, canary) for canary in CANARIES for c in canary.truth.claims]
    planted = [c for c, _ in claims if c.planted]
    assert len(planted) * 2 == len(claims)
    for claim, canary in claims:
        expected = canary.truth.verdicts[claim.id]
        want = VerdictKind.NOT_REPRODUCED if claim.planted else VerdictKind.REPRODUCED
        assert expected == want, f"{canary.name}/{claim.id}"


def test_arxiv_ids_are_valid_unique_and_never_a_real_paper() -> None:
    ids = [c.truth.arxiv_id for c in CANARIES]
    assert len(set(ids)) == len(ids)
    for arxiv_id in ids:
        assert ARXIV_ID.fullmatch(arxiv_id)
        assert arxiv_id.startswith("0000."), "month 00 does not exist on arXiv"


@dataclass(frozen=True)
class Ran:
    done: subprocess.CompletedProcess[str]
    seconds: float
    cwd: Path


def _script(experiment: Path, cwd: Path, *args: str) -> Ran:
    cwd.mkdir(parents=True)
    started = time.monotonic()
    done = subprocess.run(  # noqa: S603 (our own canary script, fixed argv)
        [sys.executable, "-I", str(experiment), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    return Ran(done, time.monotonic() - started, cwd)


@pytest.fixture(scope="module")
def scripts(tmp_path_factory: pytest.TempPathFactory) -> dict[tuple[str, str], Ran]:
    """Every canary through its CLI: a smoke run and a run missing --seed/--out, concurrently
    (each interpreter spends seconds importing scikit-learn). At most 4 at a time: each one maps
    the BLAS libraries, and more exhaust the commit charge of a small Windows box."""
    root = tmp_path_factory.mktemp("canary-scripts")
    jobs = {
        (c.name, "smoke"): (
            c.experiment,
            root / c.name / "smoke",
            "--seed",
            "0",
            "--out",
            "out",
            "--smoke",
        )
        for c in CANARIES
    } | {
        (c.name, "no-args"): (c.experiment, root / c.name / "no-args", "--smoke") for c in CANARIES
    }
    with ThreadPoolExecutor(max_workers=min(4, os.cpu_count() or 1)) as pool:
        futures = {key: pool.submit(_script, *job) for key, job in jobs.items()}
        return {key: future.result() for key, future in futures.items()}


@pytest.mark.parametrize("name", NAMES)
def test_experiment_follows_the_script_contract(
    name: str, scripts: dict[tuple[str, str], Ran]
) -> None:
    ran = scripts[name, "smoke"]
    assert ran.done.returncode == 0, ran.done.stderr
    assert ran.seconds < 120
    metrics = json.loads((ran.cwd / "out" / "metrics.json").read_text(encoding="utf-8"))
    assert all(isinstance(v, float) and math.isfinite(v) for v in metrics.values())
    for claim in build.get(name).truth.claims:
        assert claim.metric in metrics
        if claim.claim_type == ClaimType.RELATIVE:
            assert f"baseline/{claim.metric}" in metrics
    assert {p.name for p in ran.cwd.iterdir()} == {"out"}, "no writes outside --out"


@pytest.mark.parametrize("name", NAMES)
def test_experiment_requires_seed_and_out(name: str, scripts: dict[tuple[str, str], Ran]) -> None:
    ran = scripts[name, "no-args"]
    assert ran.done.returncode == 2
    assert "--seed" in ran.done.stderr
    assert not (ran.cwd / "out").exists()


@pytest.mark.parametrize("name", NAMES)
def test_truth_matches_decide_on_a_local_rerun(name: str) -> None:
    canary = build.get(name)
    started = time.monotonic()
    measured = build.measure(canary)
    assert (time.monotonic() - started) / len(canary.truth.seeds) < 120, "CPU < 2 min per seed"
    assert {cid: v.kind for cid, v in measured.verdicts.items()} == canary.truth.verdicts


@pytest.mark.parametrize("name", NAMES)
def test_true_claims_report_the_measured_numbers(name: str) -> None:
    canary = build.get(name)
    measured = build.measure(canary)
    for claim in canary.truth.claims:
        if claim.planted:
            continue
        verdict = measured.verdicts[claim.id]
        if claim.claim_type == ClaimType.ABSOLUTE:
            assert claim.reported_value is not None and verdict.mean is not None
            assert claim.reported_value == pytest.approx(verdict.mean, abs=0.0015)
        else:
            assert claim.claimed_delta is not None and verdict.mean is not None
            assert abs(claim.claimed_delta) <= abs(verdict.mean) + 0.0015


@pytest.mark.parametrize("name", NAMES)
def test_paper_states_every_claim(name: str) -> None:
    canary = build.get(name)
    tex = canary.paper.read_text(encoding="utf-8")
    assert "\\documentclass" in tex and "\\begin{table}" in tex
    for claim in canary.truth.claims:
        number = claim.reported_value if claim.reported_value is not None else claim.claimed_delta
        assert number is not None
        assert f"{abs(number):.3f}" in tex, f"{claim.id}: {number} missing from paper.tex"
        if claim.reported_std is not None:
            assert f"{claim.reported_std:.3f}" in tex


def test_source_is_a_deterministic_arxiv_style_eprint() -> None:
    for canary in CANARIES:
        data = build.source_bytes(canary)
        assert data == build.source_bytes(canary)
        assert data[:10] == b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\xff"  # mtime 0, OS unknown
        tex = canary.paper.read_bytes().replace(b"\r\n", b"\n")
        assert gzip.decompress(data) == tex
        source = extract_source(data)
        assert source.kind == "latex" and source.files == {"main.tex": tex}


def test_stored_gzip_handles_large_and_empty_inputs() -> None:
    for payload in (b"", b"x" * 200_000):
        assert gzip.decompress(build.gzip_stored(payload)) == payload


def test_ingesting_hashes_the_same_bytes_and_finds_the_results_table() -> None:
    allowlisted = load_canaries(build.ALLOWLIST)
    for canary in CANARIES:
        paper, pdf = _parse(build.source_bytes(canary), canary.truth.arxiv_id, None)
        assert pdf is None
        assert paper.sha256 == build.source_sha256(canary)
        assert paper.sha256 in allowlisted, f"{canary.name} is not in the allowlist"
        assert paper.tables, f"{canary.name}: no table parsed"


def test_allowlist_binds_each_canary_hash_to_its_arxiv_id() -> None:
    assert load_canaries(build.ALLOWLIST) == {
        build.source_sha256(c): c.truth.arxiv_id for c in CANARIES
    }
    assert build.check() == []


def test_allowlist_entries_without_a_valid_id_or_hash_are_ignored(tmp_path: Path) -> None:
    allowlist = tmp_path / "canary_allowlist.yaml"
    allowlist.write_text(
        "papers:\n"
        f"  - {{arxiv_id: '0000.00001', sha256: {'a' * 64}}}\n"
        f"  - {{arxiv_id: '2106.09685', sha256: {'b' * 64}}}\n"  # a real arXiv ID: never a canary
        "  - {arxiv_id: '0000.00002', sha256: nothex}\n"
        f"  - {'c' * 64}\n",  # the old hash-only form binds nothing
        encoding="utf-8",
    )
    assert load_canaries(allowlist) == {"a" * 64: "0000.00001"}


def test_allowlist_values_must_be_plain_ascii_strings(tmp_path: Path) -> None:
    allowlist = tmp_path / "canary_allowlist.yaml"
    allowlist.write_text(
        "papers:\n"
        f"  - {{arxiv_id: 0000.00001, sha256: {'a' * 64}}}\n"  # unquoted: a float, not an ID
        f"  - {{arxiv_id: '0000.0000{chr(0xFF11)}', sha256: {'b' * 64}}}\n"  # fullwidth 1
        "  - {arxiv_id: '0000.00003', sha256: [1, 2]}\n",
        encoding="utf-8",
    )
    assert load_canaries(allowlist) == {}


@pytest.mark.parametrize("text", ["- a\n", "papers: 5\n", "papers: {a: b}\n"])
def test_a_malformed_allowlist_is_an_error_not_an_empty_list(tmp_path: Path, text: str) -> None:
    allowlist = tmp_path / "canary_allowlist.yaml"
    allowlist.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="canary allowlist"):
        load_canaries(allowlist)


def test_check_reports_a_stale_allowlist(tmp_path: Path) -> None:
    stale = tmp_path / "canary_allowlist.yaml"
    stale.write_text(
        f"papers:\n  - {{arxiv_id: '0000.00001', sha256: {'a' * 64}}}\n", encoding="utf-8"
    )
    problems = build.check(stale)
    assert any("missing" in p for p in problems)
    assert any("unknown" in p for p in problems)


def test_write_allowlist_round_trips(tmp_path: Path) -> None:
    out = tmp_path / "canary_allowlist.yaml"
    build.write_allowlist(out)
    assert out.read_text(encoding="utf-8") == build.ALLOWLIST.read_text(encoding="utf-8")
    assert build.check(out) == []


async def test_local_trusted_executor_accepts_a_canary_and_refuses_anything_else(
    tmp_path: Path,
) -> None:
    canary = CANARIES[0]
    papers = {RUN: PaperRef(arxiv_id=canary.truth.arxiv_id, sha256=build.source_sha256(canary))}

    async def lookup(run_id: str) -> PaperRef | None:
        return papers.get(run_id)

    executor = LocalTrustedExecutor(
        env="dev",
        canaries=load_canaries(build.ALLOWLIST),
        paper=lookup,
        image=IMAGE,
        work_root=tmp_path,
    )
    assert await executor.prepare(RUN, {"train.py": b"print(1)"}) == RUN
    papers[RUN] = PaperRef(arxiv_id=canary.truth.arxiv_id, sha256="b" * 64)
    with pytest.raises(LocalTrustedRefused):
        await executor.prepare(RUN, {"train.py": b"print(1)"})
    other = "9999.99999"  # the canary's own bytes under another ID: refused too
    papers[RUN] = PaperRef(arxiv_id=other, sha256=build.source_sha256(canary))
    executor._approved.clear()
    with pytest.raises(LocalTrustedRefused):
        await executor.prepare(RUN, {"train.py": b"print(1)"})


@pytest.mark.parametrize("env", ["prod", "test"])
async def test_the_allowlist_never_runs_canary_code_outside_dev(tmp_path: Path, env: str) -> None:
    async def lookup(run_id: str) -> PaperRef | None:
        canary = CANARIES[0]
        return PaperRef(arxiv_id=canary.truth.arxiv_id, sha256=build.source_sha256(canary))

    with pytest.raises(LocalTrustedRefused):
        LocalTrustedExecutor(
            env=env,
            canaries=load_canaries(build.ALLOWLIST),
            paper=lookup,
            image=IMAGE,
            work_root=tmp_path,
        )


@pytest.mark.parametrize("arxiv_id", ["2401.00001", "0001.00001", "0000.0001"])
def test_truth_only_accepts_canary_arxiv_ids(arxiv_id: str) -> None:
    raw = CANARIES[0].truth.model_dump(mode="json")
    with pytest.raises(ValidationError):
        build.Truth.model_validate(raw | {"arxiv_id": arxiv_id})


@pytest.mark.parametrize("env", ["prod", "test"])
def test_canary_paper_source_is_dev_only(env: str) -> None:
    with pytest.raises(RuntimeError):
        CanaryPaperSource(env=env)


async def test_canary_paper_source_serves_the_allowlisted_bytes() -> None:
    papers = CanaryPaperSource(env="dev")
    canary = CANARIES[0]
    meta = await papers.metadata(canary.truth.arxiv_id)
    assert meta.arxiv_id == canary.truth.arxiv_id and meta.title
    assert await papers.source(canary.truth.arxiv_id) == build.source_bytes(canary)
    with pytest.raises(KeyError):
        await papers.source("2401.00001")
