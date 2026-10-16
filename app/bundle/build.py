"""Build the Proof Bundle of a finished run (docs/specs/proof-bundle.md).

Inputs are the control plane's own artifacts (validated when they were written) plus what the
GPU job left in the run bucket, which is untrusted: size-capped, parsed when it is JSON,
checked against control-plane digests where they exist, kept as evidence and never used to
decide anything. Hashing, scanning and packing run in the CPU pool (invariant 12); the paper
itself never goes in, only its hash and metadata.
"""

import asyncio
import gzip
import hashlib
import io
import json
import logging
import re
import tarfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from importlib import metadata
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from app.bundle.secrets import find_secrets, redact
from app.bundle.sign import Signer, SigningError, check_signature, signature_document
from app.bundle.store import BundleStore, bundle_key
from app.bundle.verify import MANIFEST, ROOT, SIGNATURE, safe_path
from app.domain.contracts import ARCHIVE_NAME_PATTERN, RUN_ID_PATTERN, ExecManifest
from app.domain.paper import IngestedPaper
from app.execution.config import DIGEST_REF
from app.execution.run_storage import RunStorage
from app.execution.smoke import MAX_WORKSPACE_BYTES
from app.models.prompt_files import PROMPTS_DIR, load_prompt
from app.orchestrator.budget import SpendKind
from app.orchestrator.context import RunContext
from app.storage.artifacts import ArtifactStore
from app.verdict.results import MAX_RESULTS_BYTES

log = logging.getLogger(__name__)

BUNDLE_SCHEMA = "p2p.bundle/v1"
MAX_ATTEMPTS = 2  # the first GPU job and one on-demand retry (EXECUTING)
MAX_JSON_BYTES = MAX_RESULTS_BYTES  # results.json and env.json
MAX_LOG_BYTES = 4 * 1024 * 1024
MAX_LOCK_BYTES = 1024 * 1024
MAX_WORKSPACE_TAR_BYTES = MAX_WORKSPACE_BYTES + 1024 * 1024  # 8 MiB of files + tar headers
TEMPLATE = Path(__file__).resolve().parent / "templates" / "reproduce.sh.j2"
_PLACEHOLDER = re.compile(r"\{\{([a-z_]+)\}\}")


class Origin(StrEnum):
    CONTROL_PLANE = "control_plane"  # written by our code from validated data
    RUN_BUCKET = "run_bucket"  # read back from the per-run bucket: evidence, not authority
    MODEL = "model"  # prose written by a model (the Judge's explanation): display only


class Reason(StrEnum):
    MISSING = "missing"
    TOO_LARGE = "too_large"
    INVALID = "invalid"
    DIGEST_MISMATCH = "digest_mismatch"
    SECRET_PATTERN = "secret_pattern"  # noqa: S105 (a reason code, not a secret)


@dataclass(frozen=True)
class BundleFile:
    path: str
    data: bytes
    origin: Origin
    json: bool = False  # must parse as JSON (again after any redaction)
    sha256: str | None = None  # the digest the control plane expects; a mismatch omits it
    binary: bool = (
        False  # a secret match omits the file (its digest must hold) instead of redacting
    )


@dataclass(frozen=True)
class Omission:
    path: str
    reason: Reason


@dataclass(frozen=True)
class Assembled:
    files: dict[str, bytes]
    manifest: bytes
    digest: bytes  # sha256 of `manifest`, what SIGNATURE signs
    omitted: tuple[str, ...]


@dataclass(frozen=True)
class BundleOutcome:
    key: str
    signed: bool
    omitted: tuple[str, ...]


class BundleBuilder(Protocol):
    async def build(self, ctx: RunContext) -> BundleOutcome: ...


class BundleUnavailable(Exception):
    """The run has nothing to bundle yet (no verdict)."""


def _parses(data: bytes) -> bool:
    try:
        json.loads(data)
    except (ValueError, RecursionError):
        return False
    return True


def dump_json(value: Any) -> bytes:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False).encode() + b"\n"


def assemble(
    files: Sequence[BundleFile],
    omitted: Sequence[Omission],
    meta: Mapping[str, Any],
    known_secrets: Sequence[bytes],
) -> Assembled:
    """Check, scan and list every file; MANIFEST.json last. Pure (CPU pool)."""
    out: dict[str, bytes] = {}
    entries: dict[str, dict[str, Any]] = {}
    omissions = list(omitted)
    redacted: list[str] = []
    for f in files:
        if not safe_path(f.path) or f.path in (MANIFEST, SIGNATURE) or f.path in out:
            raise ValueError(f"invalid bundle path: {f.path!r}")
        data = f.data
        reason: Reason | None = None
        if f.sha256 is not None and hashlib.sha256(data).hexdigest() != f.sha256:
            reason = Reason.DIGEST_MISMATCH
        elif f.json and not _parses(data):
            reason = Reason.INVALID
        elif find_secrets(data, known_secrets):
            data, _ = redact(data, known_secrets)
            if f.binary or (f.json and not _parses(data)):
                reason = Reason.SECRET_PATTERN
            else:
                redacted.append(f.path)
        if reason is not None:
            omissions.append(Omission(f.path, reason))
            continue
        out[f.path] = data
        entries[f.path] = {
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
            "origin": f.origin.value,
        }
    body = {
        **meta,
        "schema": BUNDLE_SCHEMA,
        "files": entries,
        "omitted": [
            {"path": o.path, "reason": o.reason.value}
            for o in sorted(omissions, key=lambda o: o.path)
        ],
        "redacted": sorted(redacted),
    }
    manifest, _ = redact(dump_json(body), known_secrets)  # untrusted metadata (the title)
    paths = tuple(sorted(o.path for o in omissions))
    return Assembled(out, manifest, hashlib.sha256(manifest).digest(), paths)


def pack(files: Mapping[str, bytes]) -> bytes:
    """Deterministic tar.gz under bundle/: sorted, mtime 0, root-owned, 0644 (0755 for .sh).
    Pure (CPU pool)."""
    buffer = io.BytesIO()
    with (
        gzip.GzipFile(filename="", fileobj=buffer, mode="wb", mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar,
    ):
        for path in sorted(files):
            data = files[path]
            info = tarfile.TarInfo(f"{ROOT}/{path}")
            info.size, info.mtime, info.uid, info.gid = len(data), 0, 0, 0
            info.mode = 0o755 if path.endswith(".sh") else 0o644
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def render_reproduce(manifest: ExecManifest, runner_image: str, *, run_id: str) -> bytes:
    """reproduce.sh for one exec manifest. Every value is validated against its pattern: the
    script is generated code that people run."""
    if not re.fullmatch(DIGEST_REF, runner_image):
        raise ValueError("the runner image must be a digest reference")
    if not re.fullmatch(RUN_ID_PATTERN, run_id):
        raise ValueError("invalid run id")
    image = f"{runner_image.rsplit('@', 1)[0]}@{manifest.image_digest}"
    archives = manifest.archives
    sums = [f"{archives.overlay}  overlay.tar.zst"]
    if archives.repo is not None:
        sums.append(f"{archives.repo}  repo.tar.zst")
    for archive in archives.data:
        if not re.fullmatch(ARCHIVE_NAME_PATTERN, archive.name):
            raise ValueError("invalid archive name")
        sums.append(f"{archive.sha256}  data/{archive.name}.tar.zst")
    sums.append(f"{manifest.workspace_sha256}  workspace.tar")
    values = {
        "run_id": run_id,
        "attempt": str(int(manifest.attempt)),
        "runner_image": image,
        "checksums": "\n".join(sums),
    }
    template = TEMPLATE.read_text(encoding="utf-8")
    names = set(_PLACEHOLDER.findall(template))
    if names != set(values):
        raise ValueError(f"template placeholders {sorted(names)} != {sorted(values)}")
    return _PLACEHOLDER.sub(lambda m: values[m.group(1)], template).encode()


def prompt_versions() -> dict[str, dict[str, str]]:
    """Version and sha256 of every prompt file of this build. Pure (CPU pool)."""
    return {
        path.stem: {
            "version": load_prompt(path.stem).version,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in sorted(PROMPTS_DIR.glob("*.md"))
    }


def _version() -> str | None:
    try:
        return metadata.version("paper2proof")
    except metadata.PackageNotFoundError:
        return None


def _utcnow() -> datetime:
    return datetime.now(UTC)


class _Collector:
    """The files of one bundle, with what could not be included and why."""

    def __init__(self) -> None:
        self.files: list[BundleFile] = []
        self.omitted: list[Omission] = []

    def add_json(self, path: str, value: Any) -> None:
        self.files.append(BundleFile(path, dump_json(value), Origin.CONTROL_PLANE, json=True))

    def omit(self, path: str, reason: Reason) -> None:
        self.omitted.append(Omission(path, reason))


class Bundler:
    def __init__(
        self,
        *,
        artifacts: ArtifactStore,
        storage: RunStorage,
        bundles: BundleStore,
        signer: Signer | None,
        runner_image: str,
        known_secrets: Sequence[str],
        public_key_pem: bytes | None = None,
        commit: str | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._artifacts = artifacts
        self._storage = storage
        self._bundles = bundles
        self._signer = signer
        self._runner_image = runner_image
        self._known = tuple(s.encode() for s in known_secrets)
        self._pinned = public_key_pem  # the published key (docs/bundle-public-key.pem)
        self._commit = commit
        self._clock = clock

    async def _artifact(self, key: str) -> Any:
        try:
            return await asyncio.to_thread(self._artifacts.get_json, key)
        except FileNotFoundError:
            return None

    async def _copy(self, out: _Collector, key: str, path: str) -> Any:
        value = await self._artifact(key)
        if value is None:
            out.omit(path, Reason.MISSING)
        else:
            out.add_json(path, value)
        return value

    async def _bucket(
        self,
        ctx: RunContext,
        out: _Collector,
        key: str,
        path: str,
        max_bytes: int,
        **checks: Any,
    ) -> None:
        try:
            data = await self._storage.get_bytes(ctx.run_id, key, max_bytes=max_bytes)
        except ValueError:
            out.omit(path, Reason.TOO_LARGE)
            return
        if data is None:
            out.omit(path, Reason.MISSING)
            return
        out.files.append(BundleFile(path, data, Origin.RUN_BUCKET, **checks))

    async def _paper(self, out: _Collector, prefix: str) -> None:
        raw = await self._artifact(f"{prefix}/ingested.json")
        try:
            paper = IngestedPaper.model_validate(raw)
        except ValidationError:
            out.omit("paper/metadata.json", Reason.MISSING)
            return
        out.add_json(
            "paper/metadata.json",
            {
                "arxiv_id": paper.arxiv_id,
                "sha256": paper.sha256,
                "title": paper.title,
                "source_kind": paper.source_kind,
                "sections": len(paper.sections),
                "tables": len(paper.tables),
            },
        )

    async def _attempts(self, ctx: RunContext, out: _Collector, prefix: str) -> None:
        for n in range(1, MAX_ATTEMPTS + 1):
            manifest = await self._artifact(f"{prefix}/exec_manifest_{n}.json")
            execution = await self._artifact(f"{prefix}/execution_{n}.json")
            if manifest is None and execution is None and n > 1:
                continue  # no such attempt
            if manifest is None:
                out.omit(f"code/exec_manifest_{n}.json", Reason.MISSING)
            else:
                out.add_json(f"code/exec_manifest_{n}.json", manifest)
            if execution is None:
                out.omit(f"results/{n}/execution.json", Reason.MISSING)
            else:
                out.add_json(f"results/{n}/execution.json", execution)
            await self._copy(out, f"{prefix}/analysis_{n}.json", f"results/{n}/analysis.json")
            for leaf, cap, is_json in (
                ("results.json", MAX_JSON_BYTES, True),
                ("stdout.log", MAX_LOG_BYTES, False),
                ("stderr.log", MAX_LOG_BYTES, False),
                ("env.json", MAX_JSON_BYTES, True),
            ):
                await self._bucket(
                    ctx, out, f"results/{n}/{leaf}", f"results/{n}/{leaf}", cap, json=is_json
                )

    async def _judged(self, prefix: str, attempt: int) -> ExecManifest | None:
        """The manifest the judged GPU job received (kept before submission by EXECUTING)."""
        raw = await self._artifact(f"{prefix}/exec_manifest_{attempt}.json")
        if raw is None:
            execution = await self._artifact(f"{prefix}/execution_{attempt}.json")
            raw = execution.get("manifest") if isinstance(execution, dict) else None
        try:
            return ExecManifest.model_validate(raw)
        except ValidationError:
            return None

    async def _receipt(self, ctx: RunContext) -> tuple[dict[str, Any], dict[str, list[str]]]:
        calls = await ctx.store.list_model_calls(ctx.run_id)
        totals: dict[tuple[str, str], dict[str, Any]] = {}
        models: dict[str, set[str]] = {}
        for call in calls:
            models.setdefault(call.role, set()).add(call.model_id)
            row = totals.setdefault(
                (call.role, call.model_id),
                {"role": call.role, "model_id": call.model_id, "calls": 0, "tokens_in": 0,
                 "tokens_out": 0, "cost_usd": 0.0},
            )  # fmt: skip
            row["calls"] += 1
            row["tokens_in"] += call.tokens_in
            row["tokens_out"] += call.tokens_out
            row["cost_usd"] += call.cost_usd or 0.0
        jobs = [
            {"kind": j.kind, "name": j.name, "status": j.status, "gpu_seconds": j.gpu_seconds,
             "cost_usd": j.cost_usd}
            for j in await ctx.store.list_jobs(ctx.run_id)
        ]  # fmt: skip
        ledger = {k.value: await ctx.budget.run_spend(ctx.run_id, k) for k in SpendKind}
        receipt = {
            "currency": "USD",
            "model_calls": [totals[k] for k in sorted(totals)],
            "jobs": jobs,
            "ledger": ledger,
            "total_usd": ledger[SpendKind.INFERENCE_USD.value] + ledger[SpendKind.CLOUD_USD.value],
        }
        return receipt, {role: sorted(ids) for role, ids in sorted(models.items())}

    async def _sign(self, ctx: RunContext, assembled: Assembled) -> bytes | None:
        if self._signer is None:
            return None
        try:
            document = await signature_document(self._signer, assembled.digest)
            pem = self._pinned or (await self._signer.public_key_pem()).encode()
        except SigningError as err:
            log.warning("bundle left unsigned: %s", err, extra={"run_id": ctx.run_id})
            return None
        problem = await ctx.run_cpu(check_signature, document, assembled.manifest, pem)
        if problem is not None:
            log.warning("bundle left unsigned: %s", problem, extra={"run_id": ctx.run_id})
            return None
        return document

    async def build(self, ctx: RunContext) -> BundleOutcome:
        run_id, prefix = ctx.run_id, f"runs/{ctx.run_id}"
        verdict = await self._artifact(f"{prefix}/verdict.json")
        run = await ctx.store.get_run(run_id)
        if not isinstance(verdict, dict) or run is None:
            raise BundleUnavailable("the run has no verdict to bundle")
        attempt = int(verdict["attempt"])
        out = _Collector()
        await self._paper(out, prefix)
        out.add_json(
            "claims.json",
            [
                {**c.model_dump(mode="json"), "selected": selected}
                for c, selected in await ctx.store.get_claims(run_id)
            ],
        )
        await self._copy(out, f"{prefix}/plan.json", "plan.json")
        report = await self._copy(
            out, f"{prefix}/provision_report.json", "env/provision_report.json"
        )
        lock_sha = report.get("lock_sha256") if isinstance(report, dict) else None
        await self._bucket(
            ctx, out, "staging/lock.txt", "env/lock.txt", MAX_LOCK_BYTES, sha256=lock_sha
        )
        smoke = await self._copy(out, f"{prefix}/smoke_result.json", "code/smoke_result.json")
        await self._attempts(ctx, out, prefix)
        judged = await self._judged(prefix, attempt)
        if judged is None:
            out.omit("reproduce.sh", Reason.MISSING)
        workspace_sha = (
            judged.workspace_sha256
            if judged is not None
            else (smoke.get("workspace_sha256") if isinstance(smoke, dict) else None)
        )
        if isinstance(workspace_sha, str):
            await self._bucket(
                ctx, out, "staging/workspace.tar", "code/workspace.tar",
                MAX_WORKSPACE_TAR_BYTES, sha256=workspace_sha, binary=True,
            )  # fmt: skip
        else:
            out.omit("code/workspace.tar", Reason.MISSING)
        if judged is not None:
            script = await asyncio.to_thread(
                render_reproduce, judged, self._runner_image, run_id=run_id
            )
            out.files.append(BundleFile("reproduce.sh", script, Origin.CONTROL_PLANE))
        out.add_json("verdict.json", {k: v for k, v in verdict.items() if k != "explanation_md"})
        explanation = str(verdict.get("explanation_md") or "")
        out.files.append(BundleFile("explanation.md", explanation.encode(), Origin.MODEL))
        receipt, models = await self._receipt(ctx)
        out.add_json("cost_receipt.json", receipt)
        meta = {
            "run_id": run_id,
            "arxiv_id": run.arxiv_id,
            "paper_sha256": run.paper_sha256,
            "built_at": self._clock().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "paper2proof": {"version": await asyncio.to_thread(_version), "commit": self._commit},
            "claim_id": verdict["claim"]["id"],
            "verdict": verdict["verdict"]["kind"],
            "judged_attempt": attempt,
            "config_sha256": verdict.get("config_sha256"),
            "models": models,
            "prompts": await ctx.run_cpu(prompt_versions),
            "images": (
                {"runner": f"{self._runner_image.rsplit('@', 1)[0]}@{judged.image_digest}"}
                if judged is not None
                else {}
            ),
        }
        assembled = await ctx.run_cpu(assemble, out.files, out.omitted, meta, self._known)
        signature = await self._sign(ctx, assembled)
        members = {**assembled.files, MANIFEST: assembled.manifest}
        if signature is not None:
            members[SIGNATURE] = signature
        await self._bundles.put(run_id, await ctx.run_cpu(pack, members))
        return BundleOutcome(bundle_key(run_id), signature is not None, assembled.omitted)
