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
| Control plane (API, UI, orchestrator, SQLite) | Serverless endpoint (CPU) | a host of ours (the VPS), in the `p2p-api` container behind a TLS proxy |
| Models | Token Factory | Token Factory |
| Provision and fetch jobs (our provisioner image: PyPI wheels, repo clone, HF parquet) | Nebius CPU jobs, egress through the gateway | local docker on the same host, then uploaded to the run bucket |
| Smoke tests of the Coder's script | Sandboxes, falling back to CPU smoke jobs | Sandboxes, falling back to **GPU** smoke jobs |
| Experiment (exec job) | Nebius GPU job, exec subnet | the same |
| Per-run buckets, cache, Proof Bundles, signing | Object Storage, KMS | the same |

**Invariant 1.** Code written by a model, or taken from a paper's repository, still runs only in
Sandboxes and in Nebius GPU jobs. The host runs the provisioner image, which is our code:
- it installs hash-locked wheels into a directory and never imports them;
- it shallow-clones the repository and never runs it;
- it reads parquet files to cut the smoke subset.

Those untrusted bytes are handled inside the local container's isolation (`DevDockerBackend`):
uid 10001, no capabilities, no new privileges, read-only root, capped memory and processes, a
host-side watchdog. Only the run's directory is mounted, plus the cache for the provision job.
Without a gateway the provision and fetch containers reach the internet through docker's
default network. This is the dev behaviour, and it is a deviation from §8 of the execution
contracts: the egress allowlist is not enforced for them. The deviation is limited by the
package policy, the hash lock, wheels only, and parquet only for datasets.

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
  (`HYBRID_SMOKE_PLATFORM`), priced as such, so the budget check stays true (invariant 7).

The run bucket exists twice: the Nebius bucket (`p2p-run-<slug>`, what the GPU jobs mount and
what `RunStorage` reads), and a local directory with the same name that the local jobs mount.
When a local job reaches `SUCCEEDED`, the backend uploads every regular file of the local run
directory to the Nebius bucket, under the same keys (`staging/...` and the reports), before it
reports `SUCCEEDED`. `status` keeps answering `RUNNING` until the upload is done, and `FAILED`
if it cannot be done. The upload follows these rules:
- **Links:** links, FIFOs and devices are never followed or sent (the fetch job wrote those
  bytes from untrusted sources).
- **Bounds:** at most 10 000 files and 30 GiB per run. Over that, the job fails.
- **Idempotency:** a key that already exists with the same size is skipped, so a resumed step
  or a poll after success uploads nothing twice. Large files go as multipart uploads.
- **Identity:** the upload uses the job-launcher S3 key (sa-job-launcher), which reaches only
  the per-run buckets.

`ensure_run_bucket` creates both copies. `delete_run_bucket` deletes both. `list_jobs` and
`list_run_buckets` merge local and Nebius results, so the reaper sees everything.
`ensure_gateway_up` does nothing: there is no gateway, and exec isolation comes from the route.

## Settings

| Variable | Hybrid value |
|---|---|
| `P2P_EXECUTION` | `hybrid` (prod only; `nebius` by default) |
| `P2P_LOCAL_JOBS_DIR` | directory of the local run copies and cache, owned by the API process |
| `P2P_SUBNET_PROVISION` | unused (local jobs) |
| `P2P_SUBNET_EXEC` | the exec subnet of `network_sandbox`, with no gateway |
| `GATEWAY_INSTANCE_ID` | unset |
| `SMOKE_EXECUTOR` | `sandboxes` |

Everything else is as in prod: the S3 keys, the SDK keys, `P2P_BUNDLE_KEY_ID` and the image
digests (invariant 8). The provisioner image must also be present in the host's docker, pulled
by digest.

## Infrastructure

Terraform `envs/prod` is applied with `enable_gateway = false`: network, storage, registries and
secrets, and no gateway VM. The control-plane host is outside Terraform. Its checklist is in
`docs/DEPLOY.md` §Hybrid: TLS proxy, the api container, secrets from the secret store, Litestream
to `p2p-state`.
