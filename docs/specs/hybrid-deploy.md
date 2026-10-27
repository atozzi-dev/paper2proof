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

## Settings

| Variable | Hybrid value |
|---|---|
| `P2P_EXECUTION` | `hybrid` (prod only; `nebius` by default) |
| `P2P_LOCAL_JOBS_DIR` | directory of the local run copies and cache, owned by the API process, on its own filesystem |
| `P2P_LOCAL_JOBS_NETWORK` | the docker network of the local jobs, restricted by the host firewall |
| `P2P_BIND_HOST` | `127.0.0.1` (the TLS proxy is on the same host) |
| `P2P_SUBNET_PROVISION` | unused (local jobs) |
| `P2P_SUBNET_EXEC` | the exec subnet of `network_sandbox`, with no gateway |
| `GATEWAY_INSTANCE_ID` | unset |
| `SMOKE_EXECUTOR` | `sandboxes` |

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
