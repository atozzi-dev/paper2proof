# Spec — Execution plane contracts

Source: architecture §7, §8, §9 (v0.2). Normative. Anything marked **[S-n]** depends on a day-1 spike: check `docs/spikes/RESULTS.md` before implementing.

## 1. Two-phase model

| | PROVISION | SMOKE | EXECUTE |
|---|---|---|---|
| Who chooses the commands | Deterministic code only | Coder (LLM), constrained | Runner entrypoint (ours) calling the Coder's script |
| Network | Allowlisted egress via the gateway (`provision` subnet) | **None** (Sandboxes `networking.enabled=false`) | Object Storage + Registry only (`exec` subnet) **[S-5]** |
| Where | Serverless Job, CPU | Token Factory Sandboxes (fallback: Serverless Job CPU in `exec`) | Serverless Job, GPU |
| Secrets | None (optional read-only HF token) | None | None |

## 2. Naming (deterministic, idempotent)

| Resource | Name | Notes |
|---|---|---|
| Run ID | `r_` + ULID, lowercase (e.g. `r_01jb7z…`) | Used in the DB, events and bundles |
| Run slug | `run_slug(run_id)` = the 26-char ULID part, lowercase, no `r_` | **Bucket and job names can't contain underscores**: always build them from the slug |
| Per-run bucket | `p2p-run-<slug>` (34 chars) | **[S-11]** confirm length limits and uniqueness scope |
| Provision job | `p2p-prov-<slug>-<attempt>` | |
| Exec job | `p2p-exec-<slug>-<attempt>` | |
| CPU smoke job (fallback) | `p2p-smoke-<slug>-<iteration>` | |

Before creating, call get-by-name. If the resource exists and is not terminal, reuse it. If it's terminal and failed, increment `attempt`.

## 3. Bucket layout (per-run bucket)

```
staging/
  manifest.provision.json      # written by orchestrator before provisioning
  lock.txt                     # uv pip compile output with hashes
  overlay.tar.zst              # venv --system-site-packages, extra packages only
  repo.tar.zst                 # official repo, cloned, NOT installed
  data/<name>.tar.zst          # datasets as archives (never many small files)
  data/smoke.tar.zst           # ≤ 50 MB subset for smoke tests
  workspace/                   # code written by the Coder (synced by orchestrator)
results/<attempt>/
  results.json                 # see schemas/results.schema.json
  live.log                     # flushed every 10 s by the runner
  stdout.log, stderr.log
  env.json                     # pip freeze, nvidia-smi, driver, image digest
  seeds/<seed>/metrics.json    # produced by the experiment script
```

Shared read-only cache bucket `p2p-cache`: `wheels/<sha256>.whl`, `datasets/<sha256>.tar.zst`.

## 4. Package policy (`app/execution/package_policy.py`)

`validate_packages(requested: list[PackageReq], context: PolicyContext) -> PolicyDecision`, where each package is accepted or rejected with a reason code.

Rules, in order:

1. Name matches PEP 508 name regex; specifier optional; no URLs, no `git+`, no local paths, no `--index-url` (`REJECT_SYNTAX`).
2. Not in `config/allowlist.yaml: packages.denylist` (`REJECT_DENYLIST`).
3. Exists on PyPI (JSON API `https://pypi.org/pypi/<name>/json`, cached 24 h) (`REJECT_NOT_FOUND`).
4. **Typosquat check:** if Levenshtein distance ≤ 2 to any name in the file at `packages.popular_list` (top ~500, `config/pypi_top500.txt`), and not an exact match, reject (`REJECT_TYPOSQUAT`).
5. Accept if the package appears in the official repo's requirements (`requirements*.txt`, `pyproject.toml`, `setup.cfg`, `environment.yml`), **or** has ≥ `packages.min_releases` releases **and** its first release is older than `packages.min_age_days` (`REJECT_UNPOPULAR` otherwise).
6. At most `packages.max_per_request` (`config/allowlist.yaml`) per request and `per_run.package_requests` (`config/budgets.yaml`) requests per run (`REJECT_QUOTA`).

The provisioner installs with: `uv pip install --only-binary=:all: --require-hashes -r lock.txt --target <overlay>`. **No sdists, ever.**

## 5. Manifests

`manifest.provision.json` (written by the orchestrator, read by the provisioner):

```json
{
  "schema": "p2p.provision/v1",
  "run_id": "r_01jb7z...",
  "packages": ["timm==1.0.9", "einops"],
  "repo": {"url": "https://github.com/org/repo", "ref": "v1.0"},
  "datasets": [{"kind": "hf", "id": "uoft-cs/cifar10", "split": "train", "max_bytes": 2000000000}],
  "smoke_subset": {"dataset": "uoft-cs/cifar10", "rows": 512},
  "hf_token_secret": null
}
```

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
2. Extract `overlay.tar.zst`, `repo.tar.zst` and `data/*.tar.zst` to local disk under `/work` (not on the mount).
3. For each seed in `manifest.seeds`, run the script with `timeout --signal=TERM --kill-after=30s <remaining_budget>`; write `seeds/<seed>/metrics.json`.
4. Aggregate into `results.json` (`per_seed`, `baseline.per_seed`, `scale`, `wall_clock_s`, `gpu`, `exit_reason`). `higher_is_better` and `baseline.name` are copied from the manifest. Both `per_seed` lists hold `{seed, value}` objects so the control plane can align them by seed; a failed seed has `ok: false` and `value: null`.
5. Flush `live.log` every 10 s, mirroring the last 200 lines of stdout/stderr.
6. Exit codes: `0` completed · `10` experiment error (at least one seed failed) · `20` budget timeout · `30` contract error.

### 7.1 Trusting `results.json`

`results.json` is written by the same job that runs untrusted code, so the control plane treats it as untrusted input:

- `run_id`, `attempt`, `claim_ref`, `claim_type`, `metric`, `higher_is_better`, `scale` and `baseline.name` must equal the stored manifest; any mismatch → `exit_reason` is overridden to `contract_error` (FSM `CONTRACT_ERROR`).
- `per_seed[].seed` values must be unique and a subset of `manifest.seeds`; the same holds for `baseline.per_seed`. `value: null` is allowed only with `ok: false`.
- `Measurements.scale` and `higher_is_better` come from the manifest and the stored `Claim`, never from `results.json`.

## 8. Job specs

| Field | Provision | Exec | Smoke (fallback) |
|---|---|---|---|
| Image | `p2p-provisioner@sha256:…` | `p2p-runner@sha256:…` | `p2p-runner-cpu@sha256:…` |
| Platform / preset | `cpu-d3` / `4vcpu-16gb` | `gpu-l40s-a` / `1gpu-8vcpu-32gb` (fallback `gpu-l40s-d` / `1gpu-16vcpu-96gb`) | `cpu-d3` / `4vcpu-16gb` |
| Disk | 50Gi | 100Gi | 30Gi |
| Timeout (platform) | 1h (minimum allowed) | 1h | 1h |
| Our watchdog | 20 min | `budget_seconds + 300` | 5 min |
| Subnet | `provision` | `exec` | `exec` |
| Volumes | `p2p-run-…:/mnt/run:rw`, `p2p-cache:/mnt/cache:rw` | `p2p-run-…:/mnt/run:rw`, `p2p-cache:/mnt/cache:ro` | same as exec |
| Preemptible | no | yes in dev/eval, **no** for demo recordings (`P2P_GPU_PREEMPTIBLE`) | no |
| Restart policy | `never` | `never` | `never` |

Retries are decided by the orchestrator, never by the platform.

## 9. Interfaces

```python
# app/execution/backend.py
class JobKind(StrEnum): PROVISION = "prov"; EXEC = "exec"; SMOKE = "smoke"

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

class JobBackend(Protocol):
    async def ensure_run_bucket(self, run_id: str) -> str: ...
    async def delete_run_bucket(self, run_id: str) -> None: ...
    async def submit(self, spec: JobSpec) -> JobHandle: ...           # get-by-name first
    async def status(self, handle: JobHandle) -> JobState: ...
    async def cancel(self, handle: JobHandle) -> None: ...
    async def ensure_gateway_up(self) -> None: ...                    # start VM if stopped, wait ready
    async def gateway_idle_check(self, idle_minutes: int = 30) -> None: ...
    async def list_jobs(self, prefix: str) -> list[tuple[JobHandle, JobState, datetime]]: ...  # for the reaper

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

- Cancel `p2p-*` jobs running longer than `watchdog_s + 600`.
- Delete `p2p-run-*` buckets older than 24 h whose run is terminal and whose bundle exists.
- Stop the gateway if idle for 30 min and no run is in PROVISIONING or EXECUTING.
- Emit the metric `p2p_reaper_actions_total{action}`; any job cancellation by the reaper is also an alert.
