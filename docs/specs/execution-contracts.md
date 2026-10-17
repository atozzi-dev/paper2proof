# Spec — Execution plane contracts

Source: architecture §7, §8, §9 (v0.2). Normative. Anything marked **[S-n]** depends on a day-1 spike: check `docs/spikes/RESULTS.md` before implementing.

## 1. Two-phase model

| | PROVISION | SMOKE | EXECUTE |
|---|---|---|---|
| Who chooses the commands | Deterministic code only | Coder (LLM), constrained | Runner entrypoint (ours) calling the Coder's script |
| Network | Allowlisted egress via the gateway (`provision` subnet) | **None** (Sandboxes `networking.enabled=false`) | Object Storage + Registry only (`exec` subnet) **[S-5]** |
| Where | Serverless Jobs, CPU: a provision job, then a fetch job (§5) | Token Factory Sandboxes (fallback: Serverless Job CPU in `exec`) | Serverless Job, GPU |
| Secrets | None (optional read-only HF token) | None | None |

## 2. Naming (deterministic, idempotent)

| Resource | Name | Notes |
|---|---|---|
| Run ID | `r_` + ULID, lowercase (e.g. `r_01jb7z…`) | Used in the DB, events and bundles |
| Run slug | `run_slug(run_id)` = the 26-char ULID part, lowercase, no `r_` | **Bucket and job names can't contain underscores**: always build them from the slug |
| Per-run bucket | `p2p-run-<slug>` (34 chars) | **[S-11]** confirm length limits and uniqueness scope |
| Provision job | `p2p-prov-<slug>-<attempt>` | Packages only: the one job that writes `p2p-cache` |
| Fetch job | `p2p-fetch-<slug>-<attempt>` | Repository and datasets; never mounts `p2p-cache` |
| Exec job | `p2p-exec-<slug>-<attempt>` | |
| CPU smoke job (fallback) | `p2p-smoke-<slug>-<iteration>` | |

Before creating, call get-by-name. If the resource exists and is not terminal, reuse it. If it's terminal and failed, increment `attempt`.

## 3. Bucket layout (per-run bucket)

```
staging/
  lock.txt                     # uv pip compile output with hashes (full lock, for the bundle)
  overlay.tar.zst              # `uv pip install --target` dir: only packages the runner lacks
  repo.tar.zst                 # official repo, cloned, NOT installed
  repo_files.json              # bounded text snapshot of the clone for the Coder's read_file
  data/<name>.tar.zst          # datasets as archives (never many small files)
  data/smoke.tar.zst           # ≤ 50 MB subset for smoke tests
  packages_report.json         # lock and overlay digests (provision job)
  fetch_report.json            # repository and data digests and metadata (fetch job)
  provision_error.json         # the provisioner's own error message on failure (either job)
  workspace.tar                # the Coder's workspace at `finish` (plain tar, the bytes of the
                               # last passing smoke run): sha256 in the exec manifest
smoke/<iteration>/
  workspace.tar                # the Coder's workspace, sha256 in the injected smoke manifest
  result.json                  # exit code, output tails, metrics, the manifest's nonce
results/<attempt>/
  results.json                 # see schemas/results.schema.json
  live.log                     # flushed every 10 s by the runner
  stdout.log, stderr.log
  env.json                     # pip freeze, nvidia-smi, driver, image digest
  seeds/<seed>/metrics.json    # produced by the experiment script
```

Shared cache bucket `p2p-cache` (read-write only for the provision job, never mounted by the
fetch job, read-only for smoke and exec): `overlays/<key>.tar.zst` with key = sha256(install lock
+ cache salt + constraints sha256). A hit is served only when the control plane trusts the digest
(`overlay_digests` table, first writer wins, sent in the manifest as `trusted_overlays`); the
bucket itself is never the authority.

A digest becomes trusted only from the report of a provision job, whose inputs are the package
list and PyPI wheels (installed, never built or run). The repository clone and the parquet
parsing for the smoke subset run later, in the fetch job, which has no cache mount: a dataset or
repository that gets code execution there reaches its own run bucket only, never the cache or the
trust table. The control plane reads `packages_report.json` and records the digest **before** it
submits the fetch job, and no other job of the run is running while the provision job does
(leftover smoke, fetch and exec jobs are stopped first), so nothing untrusted can write that
report. The provision job publishes to the cache from its local archive, never from the run
bucket. Old digests, recorded while one job did both, are dropped (migration 0006).

This holds as long as jobs reach buckets only through their mounts: all jobs share one platform
identity, so a container that could obtain object-storage credentials (to check in S-5/S-11)
could write `p2p-cache` directly. The trust table would still hold (hits are served only for
digests from provision-job reports, checked on local disk), but the cache could be corrupted
(rebuilds, not poisoning). Per-kind job identities are the follow-up.

Every file in this bucket can be written by an earlier smoke job (it mounts the bucket
read-write until S-11 shows a read-only/prefix mount works), so the control plane reads it as
untrusted input and the jobs check digests that travel in their injected manifests.

## 4. Package policy (`app/execution/package_policy.py`)

`validate_packages(requested: list[PackageReq], context: PolicyContext) -> PolicyDecision`, where each package is accepted or rejected with a reason code.

Rules, in order:

1. Name matches PEP 508 name regex; specifier optional; no URLs, no `git+`, no local paths, no `--index-url` (`REJECT_SYNTAX`).
2. Not in `config/allowlist.yaml: packages.denylist` (`REJECT_DENYLIST`).
3. Exists on PyPI (JSON API `https://pypi.org/pypi/<name>/json`, cached 24 h) (`REJECT_NOT_FOUND`).
4. **Typosquat check:** if the edit distance (optimal string alignment: Levenshtein where an adjacent swap is one edit) to any name in the file at `packages.popular_list` (top ~500, `config/pypi_top500.txt`) is at most the threshold, and not an exact match, reject (`REJECT_TYPOSQUAT`). The threshold scales with length: 1 when the shorter of the two names has fewer than 5 characters, 2 otherwise (a fixed 2 flags most 2–4 letter names, e.g. `mlx` against `jax`). Packages the official repository declares are exempt (rule 5).
5. Accept if the package appears in the official repo's requirements (`requirements*.txt`, `pyproject.toml`, `setup.cfg`, `environment.yml`), **or** has ≥ `packages.min_releases` releases **and** its first release is older than `packages.min_age_days` (`REJECT_UNPOPULAR` otherwise).
6. At most `packages.max_per_request` (`config/allowlist.yaml`) per request and `per_run.package_requests` (`config/budgets.yaml`) requests per run (`REJECT_QUOTA`).

The provisioner installs with: `uv pip install --only-binary=:all: --require-hashes -r lock.txt --target <overlay>`. **No sdists, ever.**

## 5. Manifests

PROVISIONING runs two jobs of the provisioner image, one after the other. Each gets its manifest
injected at `/etc/p2p/provision.json` (read-only, ≤ 64 KiB), never written to the bucket, with a
`phase` that selects what it does; a field of the other phase is rejected.

The provision job (`phase: "packages"`, mounts the run bucket and `p2p-cache` read-write) locks,
installs and packs the overlay and writes `lock.txt`, `overlay.tar.zst` and
`packages_report.json`:

```json
{
  "schema": "p2p.provision/v2",
  "phase": "packages",
  "run_id": "r_01jb7z...",
  "packages": ["timm==1.0.9", "einops"],
  "cache_salt": "sha256:<runner>+sha256:<provisioner>+py3.12+x86_64-manylinux_2_28",
  "policy": {"denylist": ["pip", "..."], "popular": ["numpy", "..."]},
  "trusted_overlays": {"<cache key>": "<sha256>"},
  "deadline_unix": 1792000000.0
}
```

`packages_report.json`: `{schema: "p2p.packages-report/v1", run_id, lock_sha256,
overlay_cache_key, overlay_cache_hit, overlay_sha256}`.

The fetch job (`phase: "fetch"`, mounts the run bucket only; it refuses to start when a cache
directory is present) clones the repository and downloads the datasets, and writes
`repo.tar.zst`, `repo_files.json`, `data/*.tar.zst` and `fetch_report.json`:

```json
{
  "schema": "p2p.provision/v2",
  "phase": "fetch",
  "run_id": "r_01jb7z...",
  "repo": {"url": "https://github.com/org/repo", "ref": null},
  "datasets": [{"kind": "hf", "id": "uoft-cs/cifar10", "split": "train", "max_bytes": 2000000000}],
  "smoke_subset": {"dataset": "uoft-cs/cifar10", "rows": 512},
  "hf_token_secret": null,
  "deadline_unix": 1792000000.0
}
```

`fetch_report.json`: `{schema: "p2p.fetch-report/v1", run_id, repo, repo_commit,
requirement_files, repo_files_sha256, repo_sha256, datasets, data_archives, smoke}`.

The control plane validates both reports (strict shape, whitelisted fields) and stores their
union as the `p2p.provision-report/v1` artifact that later steps and the bundle read
(`runs/<run_id>/provision_report.json`, the same fields as before the split). The repository and
the datasets don't change between attempts: an attempt after a `request_packages` call runs a
new provision job and reuses the stored fetch report (the runners check every archive digest).

Both jobs share the 20-minute provisioning budget: `deadline_unix` is 2 minutes before the
watchdog, counted from the provision job's submission (after the gateway is up), and the fetch
job's watchdog is what is left of those 20 minutes; the provisioner exits with its error file at
its deadline. The two jobs mean two cold starts (VM, image pull, mounts) in the same 20 minutes,
not measured yet (S-4/S-12): a fetch job is submitted only with at least 5 minutes left before
the deadline, otherwise the step times out without starting it. `ref: null` means the default
branch. Every resolved dependency (not only the
requested names) passes the denylist and the same typosquat check (§4 rule 4), except packages the runner image
already provides.

The smoke manifest (`/etc/p2p/smoke.json`, CPU smoke jobs): `{schema: "p2p.smoke/v1", run_id,
iteration, argv, timeout_s (≤ 240), workspace_sha256, nonce, archives}`. `archives` (also in the exec
manifest) lists the sha256 of the overlay, repo and data archives the runner may extract, taken from
the validated provision report; the runners hash the bytes they read and discard anything that
doesn't match, and never glob `staging/data`. `argv` is
`python <script> --seed 0 --out out --smoke [args]`; args may never set or abbreviate
`--seed`, `--out` or `--smoke` (also `-s`, `-o`, clusters and `@file`), here and in the exec
manifest. A result counts only when the job SUCCEEDED and `result.json` echoes the nonce.

The exec manifest is in `schemas/manifest.schema.json` (injected with `--inject-file`, ≤ 64 KiB).

## 6. Experiment script contract (produced by the Coder)

```
python <entrypoint> --seed <int> --out <dir> [--smoke] [--config <path>]
```

- Writes `<dir>/metrics.json`: `{"<metric>": <float>, "baseline/<metric>": <float>?}`. Values must be finite.
- `--smoke` uses `data/smoke` and finishes in ≤ 120 s on CPU.
- No network. No writes outside `<dir>` and `/tmp`.
- Exit code 0 on success.

`finish(entrypoint, args, expected_outputs)` validates: the entrypoint file exists in the workspace, the last smoke run passed with `--smoke`, and `metrics.json` contains the claim's metric (and its baseline for RELATIVE claims).

## 7. Runner entrypoint (`images/runner/entrypoint.py`)

1. Read `/etc/p2p/manifest.json`; validate against the schema; exit 30 on failure.
2. Extract the workspace archive (`paths.workspace`, checked against `workspace_sha256`), `overlay.tar.zst`, `repo.tar.zst` and exactly the `archives.data` entries to local disk under `/work` (not on the mount), hashing the bytes read; nothing else from the bucket is executed or imported.
3. For each seed in `manifest.seeds`, run the script from the extracted workspace in its own process group within the remaining budget (SIGTERM to the group at the deadline, SIGKILL 30 s later); `seeds/<seed>/` is emptied first, then the script writes `seeds/<seed>/metrics.json`. Until the last seed, `results.json` reports `budget_timeout` (a job stopped from outside never looks completed).
4. Aggregate into `results.json` (`per_seed`, `baseline.per_seed`, `scale`, `wall_clock_s`, `gpu`, `exit_reason`). `higher_is_better` and `baseline.name` are copied from the manifest. Both `per_seed` lists hold `{seed, value}` objects so the control plane can align them by seed; a failed seed has `ok: false` and `value: null`.
5. Flush `live.log` every 10 s, mirroring the last 200 lines of stdout/stderr.
6. Exit codes: `0` completed · `10` experiment error (at least one seed failed) · `20` budget timeout · `30` contract error.

### 7.1 Trusting `results.json`

`results.json` is written by the same job that runs untrusted code, so the control plane treats it as untrusted input:

- `run_id`, `attempt`, `claim_ref`, `claim_type`, `metric`, `higher_is_better`, `scale` and `baseline.name` must equal the stored manifest; any mismatch → `exit_reason` is overridden to `contract_error` (FSM `CONTRACT_ERROR`).
- `per_seed[].seed` values must be unique and a subset of `manifest.seeds`; the same holds for `baseline.per_seed`. `value: null` is allowed only with `ok: false`.
- `Measurements.scale` and `higher_is_better` come from the manifest and the stored `Claim`, never from `results.json`.

## 8. Job specs

| Field | Provision | Fetch | Exec | Smoke (fallback) |
|---|---|---|---|---|
| Image | `p2p-provisioner@sha256:…` | `p2p-provisioner@sha256:…` | `p2p-runner@sha256:…` | `p2p-runner-cpu@sha256:…` |
| Platform / preset | `cpu-d3` / `4vcpu-16gb` | `cpu-d3` / `4vcpu-16gb` | `gpu-l40s-a` / `1gpu-8vcpu-32gb` (fallback `gpu-l40s-d` / `1gpu-16vcpu-96gb`) | `cpu-d3` / `4vcpu-16gb` |
| Disk | 50Gi | 50Gi | 100Gi | 30Gi |
| Timeout (platform) | 1h (minimum allowed) | 1h | 1h | 1h |
| Our watchdog | 20 min (provisioner deadline 18 min) | what is left of the provision job's 20 min (same deadline) | `budget_seconds + 300` | `timeout_s + 180` s (setup margin, check with S-12) |
| Subnet | `provision` | `provision` | `exec` | `exec` |
| Volumes | `p2p-run-…:/mnt/run:rw`, `p2p-cache:/mnt/cache:rw` | `p2p-run-…:/mnt/run:rw` only | `p2p-run-…:/mnt/run:rw`, `p2p-cache:/mnt/cache:ro` | same as exec |
| Preemptible | no | no | yes in dev/eval, **no** for demo recordings (`P2P_GPU_PREEMPTIBLE`) | no |
| Restart policy | `never` | `never` | `never` | `never` |

Retries are decided by the orchestrator, never by the platform.

**No GPU capacity** (architecture §15). `submit()` raises `CapacityError` when the platform refuses
to create the job for lack of capacity (gRPC `RESOURCE_EXHAUSTED` on create, or a create operation
that ends with it: GPU quota used up, "not enough resources"); no job exists afterwards and the
refused creation costs nothing (the reservation is released at 0). EXECUTING then retries the same
attempt (same job name) on `gpu-l40s-a` after 2 minutes, up to 3 times, then once on
`gpu-l40s-d` / `1gpu-16vcpu-96gb` (same preemptibility, priced from `config/pricing.yaml`). Each
retry emits a `NO_GPU_CAPACITY` warning. The fallback platform is kept in
`runs/<run_id>/exec_platform_<attempt>.json` before that submission, so a resumed step re-attaches
to the job where it was created. When neither platform has capacity, the attempt ends without a
job: `execution_<attempt>.json` has `results_key: null`, `exit_reason: "capacity"` and VERDICT
reports `INCONCLUSIVE/CAPACITY` (an on-demand retry after a preemption that finds no capacity keeps
the first attempt's outcome instead). A run cancelled during the 2-minute waits stops there.
`CapacityError` from any other job (provision, fetch, smoke) fails the run with `CAPACITY`.

## 9. Interfaces

```python
# app/execution/backend.py
class JobKind(StrEnum): PROVISION = "prov"; FETCH = "fetch"; EXEC = "exec"; SMOKE = "smoke"

class JobSpec(BaseModel):
    name: str
    kind: JobKind
    image_digest_ref: str            # registry/path@sha256:...
    platform: str
    preset: str
    disk_gib: int
    subnet_id: str
    volumes: list[VolumeMount]
    inject_files: dict[str, bytes]   # container path -> content (each ≤ 64 KiB)
    env: dict[str, str]              # never secrets
    preemptible: bool
    watchdog_s: int

class JobState(StrEnum): PENDING; STARTING; RUNNING; SUCCEEDED; FAILED; CANCELLED; UNKNOWN

class JobHandle(BaseModel): name: str; nebius_id: str

class CapacityError(Exception): ...  # submit(): no capacity, the job was not created

class JobBackend(Protocol):
    async def ensure_run_bucket(self, run_id: str) -> str: ...
    async def delete_run_bucket(self, run_id: str) -> None: ...
    async def submit(self, spec: JobSpec) -> JobHandle: ...           # get-by-name first; CapacityError
    async def status(self, handle: JobHandle) -> JobState: ...
    async def cancel(self, handle: JobHandle) -> None: ...
    async def ensure_gateway_up(self) -> None: ...                    # start VM if stopped, wait ready
    async def gateway_idle_check(self, idle_minutes: int = 30) -> bool: ...           # True when it stopped the VM
    async def list_jobs(self, prefix: str) -> list[tuple[JobHandle, JobState, datetime]]: ...  # for the reaper
    async def list_run_buckets(self) -> list[tuple[str, datetime]]: ...  # p2p-run-* (name, created_at), for the reaper

# app/execution/smoke.py
class SmokeResult(BaseModel):
    exit_code: int; stdout: str; stderr: str; duration_s: float
    metrics: dict[str, float] | None

class SmokeExecutor(Protocol):
    async def prepare(self, run_id: str, files: dict[str, bytes]) -> str: ...   # returns env/checkpoint id
    async def run(self, env_id: str, argv: list[str], files: dict[str, bytes], timeout_s: int) -> SmokeResult: ...
    async def run_branches(self, env_id: str, variants: list[dict[str, bytes]], argv: list[str], timeout_s: int) -> list[SmokeResult]: ...
```

Implementations: `NebiusJobBackend`, `FakeJobBackend`; `SandboxesSmokeExecutor`, `JobSmokeExecutor`, `LocalTrustedExecutor` (dev + canary allowlist only), `FakeSmokeExecutor`.

## 10. Sandboxes flow **[S-3]**

1. Once per image version: import `p2p-runner-cpu@sha256:…` from the registry; store the image UUID in `config/sandbox_images.yaml`.
2. `prepare`: upload overlay, workspace and smoke data; run `/opt/p2p/extract.sh` **with save** and networking disabled; keep the returned image UUID as the checkpoint.
3. `run`: start from the checkpoint, inject the current workspace files, networking disabled, discard changes; capture stdout, stderr and `metrics.json` (cat it at the end of the command).
4. `run_branches`: same as `run`, concurrently, one per variant (max 2).

Base URL: `https://api.tokenfactory.nebius.com/sandboxes`, headers `Authorization: Bearer <NEBIUS_API_KEY>` and `Project: <TF_PROJECT_ID>`. Check the effective limits with `GET /v1/whoami` at startup and log them.

## 11. Reaper (every 5 min)

- Cancel `p2p-*` jobs running longer than `watchdog_s + 600`. The deadline is the `jobs` row's
  (first submission + `watchdog_s`); a job with no row (submitted before a crash, never recorded)
  gets the fallback watchdog of 45 min (architecture §7). A job whose run is terminal or unknown
  has nobody watching it and is cancelled at once (it still mounts the run bucket read-write).
- Delete `p2p-run-*` buckets older than 24 h whose run is terminal and whose bundle exists
  (`bundles/<run_id>/proof-bundle.tar.gz`), and only when none of the run's jobs is still running.
  No bundle, no deletion: the bucket is kept and the action is counted.
- Ledger gaps: a `jobs` row that was never settled (the control plane died while watching it) and
  whose run is terminal is charged once its job is terminal, from its first submission to now
  (capped at the 1 h platform timeout) at the kind's on-demand rate, then marked settled. At
  worst this counts the watched part twice, never zero.
- Stop the gateway if idle for 30 min and no run is in PROVISIONING, SMOKE_TESTING or EXECUTING
  (SMOKE_TESTING too: a fallback smoke job runs in the `exec` subnet routed to the gateway).
  "Idle" = no non-terminal `p2p-*` job and no `ensure_gateway_up` for `idle_minutes`.
- Emit the metric `p2p_reaper_actions_total{action}`; any job cancellation by the reaper is also an alert.
