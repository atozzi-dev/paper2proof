"""The bundle in the run: built at the end of VERDICT, served by GET /api/runs/{id}/bundle."""

from pathlib import Path
from typing import Any

from app.bundle.build import BundleOutcome, Bundler
from app.bundle.sign import FakeSigner, SigningError
from app.bundle.store import ArtifactBundleStore
from app.bundle.verify import read_archive, verify_files
from app.domain.models import RunStatus, StepName
from app.models.fake import FakeModelClient
from app.orchestrator.context import RunContext
from app.orchestrator.steps.analyzing import AnalyzingStep
from app.orchestrator.steps.verdict import VerdictStep
from app.storage.artifacts import LocalArtifactStore
from app.storage.store import Store
from tests.bundle.conftest import RUNNER
from tests.steps.conftest import LLM_FIXTURES, RUN
from tests.steps.test_execute_to_verdict import _results, _setup


async def _verdict_with(
    store: Store, tmp_path: Path, signer: FakeSigner | None, bundler: Any = None
) -> tuple[LocalArtifactStore, ArtifactBundleStore, list[Any]]:
    raw = _results([0.95, 0.96, 0.95], [0.90, 0.91, 0.90])
    ctx, artifacts, storage = await _setup(store, tmp_path, raw, StepName.ANALYZING)
    analyzed = await AnalyzingStep(artifacts, storage).run(ctx)
    await store.transition(
        RUN, StepName.ANALYZING, 1, "SUCCEEDED", output_ref=analyzed.output_ref,
        next_step=(StepName.VERDICT, 1),
    )  # fmt: skip
    vctx = RunContext(**{**ctx.__dict__, "step": StepName.VERDICT})
    bundles = ArtifactBundleStore(artifacts)
    if bundler is None:
        bundler = Bundler(
            artifacts=artifacts,
            storage=storage,
            bundles=bundles,
            signer=signer,
            runner_image=RUNNER,
            known_secrets=(),
        )
    step = VerdictStep(
        FakeModelClient(LLM_FIXTURES, case="honest"),
        artifacts,
        bundler=bundler,
        bundle_timeout_s=0.5,
    )
    assert (await step.run(vctx)).next is RunStatus.COMPLETED
    events = [e for e in await store.events_after(RUN, 0) if e.type.value == "warning"]
    return artifacts, bundles, [e.payload for e in events]


async def test_verdict_builds_a_signed_bundle(store: Store, tmp_path: Path) -> None:
    signer = FakeSigner()
    artifacts, bundles, warnings = await _verdict_with(store, tmp_path, signer)
    assert warnings == []
    doc = artifacts.get_json(f"runs/{RUN}/verdict.json")
    assert doc["attempt"] == 1 and doc["claim"]["id"] == "c1"
    assert "### Limitations" in doc["explanation_md"]
    stored = await bundles.get(RUN)
    assert stored is not None
    files = read_archive(stored)
    assert verify_files(files, (await signer.public_key_pem()).encode()) == []
    assert files["explanation.md"].decode() == doc["explanation_md"]


async def test_an_unsigned_bundle_is_a_warning(store: Store, tmp_path: Path) -> None:
    class Down(FakeSigner):
        async def sign_digest(self, digest: bytes) -> bytes:
            raise SigningError("kms down")

    _, bundles, warnings = await _verdict_with(store, tmp_path, Down())
    assert [w.code for w in warnings] == ["BUNDLE_UNSIGNED"]
    assert await bundles.get(RUN) is not None


async def test_a_failed_bundle_never_fails_the_verdict(store: Store, tmp_path: Path) -> None:
    class Broken:
        async def build(self, ctx: RunContext) -> BundleOutcome:
            raise RuntimeError("disk full")

    _, bundles, warnings = await _verdict_with(store, tmp_path, None, bundler=Broken())
    assert [w.code for w in warnings] == ["BUNDLE_FAILED"]
    assert "disk full" not in warnings[0].message  # no internals in events
    assert await bundles.get(RUN) is None
    assert await store.get_verdict(RUN, "c1") is not None


async def test_a_hanging_bundle_times_out(store: Store, tmp_path: Path) -> None:
    import asyncio

    class Hangs:
        async def build(self, ctx: RunContext) -> BundleOutcome:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    _, _, warnings = await _verdict_with(store, tmp_path, None, bundler=Hangs())
    assert [w.code for w in warnings] == ["BUNDLE_FAILED"]
