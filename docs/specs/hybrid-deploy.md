# Hybrid deploy: no non-GPU compute on Nebius

**Why.** The tenant's `compute.instance.non-gpu.vcpu` quota is 0 in every region, and the raise
was not granted (checked 2026-10-27). That blocks every CPU instance on Nebius AI Cloud: the
Serverless endpoint of the control plane (S6), the egress gateway VM (A-04), and the CPU jobs
(provision, fetch, smoke fallback). GPU jobs are not affected: the L40S quota is 8 (S4, S12).
Token Factory (models, Sandboxes), Object Storage and KMS work as before.

`P2P_EXECUTION=hybrid` (prod only) keeps every invariant with what is left. The default,
`P2P_EXECUTION=nebius`, is the architecture as designed and needs the quota.

## Topology

| Piece | Designed (`nebius`) | Hybrid |
|---|---|---|
| Control plane (API, UI, orchestrator, SQLite) | Serverless endpoint (CPU) | a host of ours (the VPS): the checkout at a pinned commit under systemd, `images/api/start.sh` with litestream, bound to 127.0.0.1 behind a TLS proxy (not a container: the local jobs hand host paths to dockerd) |
| Models | Token Factory | Token Factory |
| Provision and fetch jobs (our provisioner image: PyPI wheels, repo clone, HF parquet) | Nebius CPU jobs, egress through the gateway | local docker on the same host, then uploaded to the run bucket |
| Smoke tests of the Coder's script | Sandboxes, falling back to CPU smoke jobs | smoke jobs in **Sandboxes** (`P2P_EXEC_BACKEND=sandboxes`, the default); with `gpu`, Sandboxes falling back to **GPU** smoke jobs |
| Experiment (exec job) | Nebius GPU job, exec subnet | a job in **Sandboxes**, on CPU (the default); with `gpu`, a Nebius GPU job in the exec subnet |
| Per-run buckets, cache, Proof Bundles, signing | Object Storage, KMS | the same |

**Invariant 1.** Code written by a model, or taken from a paper's repository, still runs only in
Sandboxes and in Nebius GPU jobs. The host runs the provisioner image, which is our code:
- it installs hash-locked wheels into a directory and never imports them;
- it shallow-clones the repository and never runs it;
- it reads parquet files to cut the smoke subset.

Those untrusted bytes are handled inside the local container's isolation (`DevDockerBackend`):
uid 10001, no capabilities, no new privileges, read-only root, capped memory and processes, a
host-side watchdog. Only the run's directory is mounted, plus the cache for the provision job.
Unlike the designed deploy, that isolation is a container on the control-plane host, sharing
its kernel, not a separate VM: a container escape there reaches the host and every prod key
(see `docs/DEPLOY.md` §Hybrid, "What this host is trusted with").

Without a gateway the provision and fetch containers reach the internet through their own docker
network (`P2P_LOCAL_JOBS_NETWORK`). The host's firewall drops link-local addresses (the metadata
service), private ranges and the host itself from that network. Public internet stays open, so
the egress allowlist is not enforced for them. This is a deviation from §8 of the execution
contracts. The deviation is limited by the package policy, the hash lock, wheels only, and
parquet only for datasets.

**Exec isolation without a gateway.** The exec subnet routes `0.0.0.0/0` to the gateway's
private allocation (`infra/terraform/modules/network_sandbox`). With no gateway VM holding it,
that route has no next hop, so a GPU job has no internet (fail-closed by design). Image pull
and bucket mount must still work there. Spike S5 `--mode isolated` checks exactly this, and
hybrid exec jobs are allowed only once it passes. That fact is recorded in
`docs/spikes/RESULTS.md`.

## `HybridJobBackend` (`app/execution/hybrid.py`)

A `JobBackend` that routes by job kind:
- `PROVISION`, `FETCH`: a `DevDockerBackend` limited to those two kinds. It accepts any paper,
  since these jobs never run model or paper code, and runs the provisioner image by its
  registry digest.
- `EXEC`, `SMOKE`: the `NebiusJobBackend`. `SMOKE` uses the exec platform and preset
  (`ExecutionConfig.smoke_platform`). It is priced at that rate and counts GPU minutes, in the
  step's reservation and in the reaper, so the budget check stays true (invariant 7).

The run bucket exists twice: the Nebius bucket (`p2p-run-<slug>`, what the GPU jobs mount and
what `RunStorage` reads), and a local directory with the same name that the local jobs mount.
When a local job reaches `SUCCEEDED`, the backend uploads every regular file of the local run
directory to the Nebius bucket, under the same keys (`staging/...` and the reports), before it
reports `SUCCEEDED`. `status` keeps answering `RUNNING` until the upload is done, and `FAILED`
if it cannot be done. The upload follows these rules:
- **Links:** links, FIFOs and devices are never followed or sent (the fetch job wrote those
  bytes from untrusted sources).
- **Bounds:** at most 10 000 files and 30 GiB per run. Over that, the job fails.
- **Walk:** the copy is walked through directory fds with `O_NOFOLLOW` at every component. The
  file opened must be the inode and size the scan saw, and at most that many bytes are read.
- **Idempotency:** each object carries its sha256, and an object with the same sha256 is skipped.
  A resumed step or a poll after success uploads nothing twice, and a same-size rewrite is sent
  again. Large files go as multipart uploads.
- **Failed jobs:** the copy of a failed local job is uploaded once, best effort, so its error
  report reaches the run.
- **Identity:** the upload uses the job-launcher S3 key (sa-job-launcher), which reaches only
  the per-run buckets.

`ensure_run_bucket` creates both copies. `delete_run_bucket` deletes both. `list_jobs` and
`list_run_buckets` merge local and Nebius results, so the reaper sees everything.
`ensure_gateway_up` does nothing: there is no gateway, and exec isolation comes from the route.

## Experiments in Sandboxes (`P2P_EXEC_BACKEND=sandboxes`)

Spike S5 `--mode isolated` never reached a verdict. On 2026-10-27 GPU jobs in the exec subnet
ended `ERROR` with a bare internal error, and in the default subnet there was no L40S capacity
(`docs/FEEDBACK.md`). On-demand GPUs are also not a promise a live demo can rely on. So the
hybrid deploy runs the smoke and exec jobs in **Token Factory Sandboxes** by default, and keeps
the GPU path for when the gate passes (`P2P_EXEC_BACKEND=gpu`).

**What changes for a run.** The experiment runs on CPU, in a disposable Sandboxes microVM with
no network interface (spike S3), at most 1 hour per instance. The runner, the manifest, the
results contract, the verdict and the bundle are the same. The image is `p2p-runner-cpu`: the
same entrypoint and package versions as `p2p-runner`, with PyTorch CPU wheels, imported once
into Sandboxes (`config/sandbox_images.yaml`, digest → image UUID). The judged hardware the
bundle records is the Sandboxes platform (`sandbox`), never a GPU.

**`SandboxesJobBackend` (`app/execution/sandbox_jobs.py`)** is a `JobBackend` for `SMOKE` and
`EXEC` specs only, so `ExecutingStep`, `JobSmokeExecutor`, the ledger, the budget and the reaper
work unchanged. For one spec it:
1. Accepts only the configured `p2p-runner-cpu` digest (invariant 8) and spawns the image UUID
   imported for it. Any other image, or a kind other than `SMOKE`/`EXEC`, is refused.
2. **Mounts the run bucket by copy.** The inputs the injected manifest names (overlay, repo,
   data archives, workspace) are uploaded to the Sandboxes files API and mounted read-only,
   owned by root (`0444`: uid 10001 reads them, never changes them), at the very paths the job
   would see under `/mnt/run`. Uploads are content-addressed: a file whose sha256 the
   namespace already has is not sent again, and a stream is sent with its length. The staging
   archives are read from the host's local run copy (`P2P_LOCAL_JOBS_DIR`, written by the local
   provision and fetch jobs, opened without following a link, at most 12 GiB each), the
   workspace from `RunStorage`. Injected files (`/etc/p2p/*.json`) are uploaded the same way.
3. **Runs the job as the job would.** The instance starts as root with networking disabled. It
   creates the output directory owned by uid 10001, then runs the image's entrypoint as uid
   10001 through `setpriv --no-new-privs`, in its own session with no stdin. `timeout` sends
   SIGTERM 60 s before the job's watchdog (SIGKILL 30 s later), so the runner still writes
   partial results and the harvest ends before the control plane may cancel the instance. The
   runner's own output goes to a root-only file (`/root/p2p`, or `mktemp -d` when `/root` is not
   writable), never to the instance's stdout; its tail goes to stderr. When it exits, every
   process of uid 10001 is killed. Exit 40/41: the instance could not hold the log or the
   output directory.
4. **Harvests the outputs.** `/opt/p2p/harvest.py` (in the image) runs as uid 10001 and prints,
   between two lines carrying a random per-submission marker, a base64 tar of the allowed
   output files only: `results.json`, `env.json`, `live.log`, `stdout.log`, `stderr.log` of the
   exec job (logs tail-capped), or `result.json` of a smoke job. Files are opened with
   `O_NOFOLLOW` and must be regular. The backend parses the last such block (everything after
   the kill is ours) with the same rules (names, sizes, regular members only, parsed in the CPU
   pool) and writes the files to the run bucket under the keys the job would have written,
   plus `instance.log` (the stderr tail) for a failed job. Everything there is untrusted, as
   before: only exit codes are logged.
5. **Maps the states.** Sandboxes `PENDING`/`ASSIGNED` → `PENDING`, `EXECUTING` → `RUNNING`,
   `SUCCESS` → `SUCCEEDED` with exit code 0, else `FAILED` (the runner's exit codes), `FAILED` →
   `FAILED`, `CANCELLED` → `CANCELLED` (its partial results harvested if it got that far). An
   instance is reported terminal only after its outputs were written; a bucket that cannot
   be written is `JobStatusUnavailable`, asked again at the next poll, like HTTP errors. A 429
   at spawn is `CapacityError`.
6. **Names are deterministic (invariant 9).** Sandboxes has no names, so the backend keeps an
   index on the host, one file per job name (`P2P_LOCAL_JOBS_DIR/sandbox-jobs/<name>.json`),
   written before the spawn (intent) and after it (operation id). `submit` is get-by-name
   first. The intent is dropped only when the spawn certainly did not happen (no connection,
   or a 4xx). After a timeout, a 5xx or an unreadable reply it stays, and the next `submit`
   resolves it by looking up recent operations (every page) whose `metadata.env.P2P_JOB_NAME`
   is the name, before a new instance is spawned; a lookup that fails is an outage, never "not
   found". `list_jobs` reads the index and adopts what an intent finds, so the reaper sees and
   cancels every Sandboxes job; an intent never spawned is forgotten after 10 minutes. Index
   files are 0600, fsynced, in a 0700 directory. `delete_run_bucket` also drops the run's
   entries.

**Smoke tests.** With the Sandboxes job backend, smoke tests are smoke jobs
(`SMOKE_EXECUTOR=job`): the same `JobSmokeExecutor`, which mounts the overlay, the repository
and the smoke data. `SandboxesSmokeExecutor` (workspace only) stays for the designed deploy.

**Budget and prices (invariants 5 and 7).** Sandboxes reports no price per run. Until billing
says otherwise (to reconcile with the invoice), `config/pricing.yaml` prices the `sandbox`
platform like `cpu-d3` as an estimate, and
experiment minutes count against the GPU-minute caps, as they would on a GPU. The plan's cost
estimate uses the configured exec platform.

**Planning.** The Planner is told the experiment runs on CPU, in at most 30 minutes, without
network, with the packages the provision job installs. A plan that needs a GPU is still
planned; the run then reports `REPRODUCED_AT_REDUCED_SCALE` or `INCONCLUSIVE` honestly.

**Invariant 1** holds: model and paper code runs only inside Sandboxes. The host only uploads
bytes and parses the harvested tar as untrusted data.

## Settings

| Variable | Hybrid value |
|---|---|
| `P2P_EXECUTION` | `hybrid` (prod only; `nebius` by default) |
| `P2P_EXEC_BACKEND` | `sandboxes` (the default); `gpu` once spike S5 `--mode isolated` passes |
| `P2P_LOCAL_JOBS_DIR` | directory of the local run copies and cache, owned by the API process, on its own filesystem |
| `P2P_LOCAL_JOBS_NETWORK` | the docker network of the local jobs, restricted by the host firewall |
| `P2P_BIND_HOST` | `127.0.0.1` (the TLS proxy is on the same host) |
| `P2P_SUBNET_PROVISION` | unused (local jobs) |
| `P2P_SUBNET_EXEC` | with `gpu`: the exec subnet of `network_sandbox`, with no gateway; unused with `sandboxes` |
| `P2P_RUNNER_IMAGE` | with `gpu`: the GPU runner; with `sandboxes` the exec job runs `P2P_RUNNER_CPU_IMAGE` |
| `GATEWAY_INSTANCE_ID` | unset |
| `SMOKE_EXECUTOR` | `job` with `P2P_EXEC_BACKEND=sandboxes` (smoke jobs in Sandboxes); `sandboxes` with `gpu` |
| `NEBIUS_API_KEY`, `TF_PROJECT_ID` | required with `P2P_EXEC_BACKEND=sandboxes` (Sandboxes API) |

Everything else is as in prod: the S3 keys, the SDK keys, `P2P_BUNDLE_KEY_ID` and the image
digests (invariant 8). The provisioner image must also be present in the host's docker, pulled
by digest.

**Not checked by code yet** (follow-ups):
- that `P2P_SUBNET_EXEC` really is the routed exec subnet with no public pool (the S5 gate and
  the checklist cover it);
- a smoke allowance in the plan's cost estimate;
- an alert when the gateway allocation or `rt-exec` change.

## Infrastructure

Terraform `envs/prod` is applied with `enable_gateway = false`: network, storage, registries and
secrets, and no gateway VM. The control-plane host is outside Terraform. Its checklist is in
`docs/DEPLOY.md` §Hybrid: the trust it carries, its own docker network and firewall rules, the
systemd unit, the TLS proxy, secrets from the secret store, and Litestream to `p2p-state`.
