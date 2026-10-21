# Spike results (day 1; run 2026-10-21)

> These verified facts override the specs and the architecture document. Updated after each spike run.

| Spike | Verdict | Decision |
|---|---|---|
| S1 | PASS | Token Factory works from Italy; $25 of credits visible (24.83 left after S2) |
| S2 | FAIL → fixed | vision role → `nvidia/Nemotron-3.5-Super-VL` (no Nano Omni listed); keep `structured_output: tool` and the `chat_template_kwargs` toggle |
| S3 | PASS | Sandboxes enabled: networking off isolates (`lo` only); smoke tests can use Sandboxes |
| S4 | PASS | L40S job ran; provisioning waited ~16 min for capacity with no error |
| S5 | FAIL → rerun | job volumes need the bucket **ID**, not its name: rerun the baseline with the ID; fix the Nebius backend |
| S6 | FAIL → rerun | quota `compute.instance.non-gpu.vcpu` was 0 (raise requested); endpoints count against it |
| S7 | pending | — |
| S8 | PASS | Tavily key works; 4125 credits |
| S9 | PARTIAL | HF Papers has no structured repo field; parse GitHub URLs from the HF summary/arXiv abstract, Tavily as fallback (A-17) |
| S10 | pending | — |
| S11 | PARTIAL | names up to 63 chars, no underscore; create ~4.5 s, get-by-name ~0.2 s; mount needs the bucket ID (as S5) |
| S12 | PASS | pytorch 2.5.1 CUDA image: create → start 3 min 38 s (under 240 s); CUDA available |
| S13 | pending | — |

## S1 — Token Factory account and credits can be activated from Italy

- **Question:** Token Factory account and credits can be activated from Italy?
- **Command:** manual: Token Factory console (credits, Builders Program, API key)
- **Output:** Token Factory credits $24.83 of $25 after S2; API key works (S2).
- **Verdict:** PASS
- **Facts:**
  - The Token Factory account and the promo credits work from Italy; the AI Cloud account has its own $25 card top-up.
  - Only one $25 Token Factory credit is visible on 2026-10-21 (the second Builder Program allocation comes about 30 days later, per its terms).
  - `contree` profiles are per machine: the VPS has none, yet the API key works for Sandboxes (S3).
- **Decision:** none needed. Budget the inference of the eval against ~$25 until more credits arrive.

## S2 — Model IDs, context windows, tool calling, JSON Schema output and reasoning toggle for Super, Ultra, Nano, Omni

- **Question:** Model IDs, context windows, tool calling, JSON Schema output and reasoning toggle for Super, Ultra, Nano, Omni?
- **Command:** `uv run python spikes/s2_models.py`
- **Output (key facts):**
  ```
  28 models listed (incl. nvidia/nemotron-3-super-120b-a12b, nvidia/Nemotron-3-Ultra-550b-a55b,
    nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B, nvidia/Nemotron-3.5-Super-VL, nvidia/Nemotron-3_5-Lightning)
  reader/coder -> nemotron-3-super-120b-a12b; planner/judge -> Nemotron-3-Ultra-550b-a55b;
  utility -> NVIDIA-Nemotron-3-Nano-30B-A3B; FAIL: vision 'nano-omni' hit 0 models
  super: forced tool call OK (909 ms); PARTIAL: json_schema not usable (HTTP 200, content='')
  ultra: forced tool call OK (97 s); json_schema OK
  nano: forced tool call OK (544 ms); json_schema OK
  thinking off only with chat_template_kwargs enable_thinking=false (all three models);
    system /no_think and 'detailed thinking off' are ignored
  ultra latency: 97-122 s with thinking on, 18 s off; ~1k-token prompt 9 s
  rate limits: super/ultra 300 req + 200k tokens per window; nano 100 req + 800k tokens
  VERDICT: FAIL
  ```
- **Verdict:** FAIL (vision role only), fixed in `config/models.yaml`
- **Facts:**
  - Context lengths are not exposed by `/models`.
  - Super does not honour `response_format=json_schema` (empty content); Ultra and Nano do. Forced tool calls work on all three.
  - Reasoning is switched only by `chat_template_kwargs: {enable_thinking: false|true}`; system-prompt flags do nothing.
  - Ultra with thinking on takes up to ~2 minutes for a short prompt.
- **Decision:**
  - `vision` role → `nvidia/Nemotron-3.5-Super-VL` (an NVIDIA open model with image input). Its price is not yet in `config/pricing.yaml`: the ledger charges unpriced calls at the worst-case price until A-30 reconciles prices.
  - Keep `reasoning_toggle: chat_template_kwargs` and `structured_output: tool` for every role (Super needs it).
  - Keep `timeout_s: 300` for planner and judge (Ultra with thinking on).

## S3 — Token Factory Sandboxes access, effective limits, billing, networking disabled works

- **Question:** Token Factory Sandboxes access, effective limits, billing, networking disabled works?
- **Command:** `uv run python spikes/s3_sandboxes.py`
- **Output (key facts):**
  ```
  permissions: import, spawn, spawn_disposable, list, cancel, set_image_tag
  limits: instance_max_timeout 3600 s, instance_max_concurrency 50, max layer 12 GiB,
    images_import_max_concurrency 8
  networking enabled: IFACES eth0 lo, CONNECTED (11.0 s)
  networking disabled: IFACES lo, CONNECT_FAILED (1.5 s)
  VERDICT: PASS
  ```
- **Verdict:** PASS
- **Facts:** beta access is active for the Token Factory project; a disposable instance without networking has only `lo`; no cost is reported per run.
- **Decision:** smoke tests may run in Sandboxes with networking disabled (`SMOKE_EXECUTOR=sandboxes`). Follow-up: `deploy/endpoint.sh` does not accept `SMOKE_EXECUTOR` or `TF_PROJECT_ID` yet, so prod keeps CPU-job smoke tests until that change.

## S4 — GPU quota for L40S in eu-north1; a nvidia-smi job runs

- **Question:** GPU quota for L40S in eu-north1; a nvidia-smi job runs?
- **Command:** `spikes/s4_gpu_job.sh` (WSL)
- **Output (key facts):**
  ```
  nebius CLI 0.12.293; job on gpu-l40s-a / 1gpu-8vcpu-32gb
  logs: Provisioning resources / Waiting before retrying resource provisioning / ... / Starting
  nvidia-smi: NVIDIA L40S, 46068 MiB, driver 580.173.02, CUDA 13.0
  final state COMPLETED, wall 977 s
  VERDICT: PASS
  ```
- **Verdict:** PASS
- **Facts:**
  - The L40S quota (8) is enough to run a job.
  - With no capacity at that moment, the platform **waited and retried provisioning** (about 16 minutes) instead of failing with `RESOURCE_EXHAUSTED`.
- **Decision:** A-32's capacity path (`RESOURCE_EXHAUSTED` on create) still stands for refusals, but a slow provisioning is the common case: the GPU minutes budget and the EXECUTING timeouts must count from the job's `started_at`, not from the create. Check in Billing whether the waiting time was charged.

## S5 — A job in a subnet routed to the gateway: image pull, bucket mount, egress blocked; behaviour with the gateway off

- **Question:** A job in a subnet routed to the gateway: image pull, bucket mount, egress blocked; behaviour with the gateway off?
- **Command:** `spikes/s5_job_gateway.sh --mode baseline` (day 1); `--mode gateway-on` / `--mode gateway-off` after H-04
- **Output:**
  ```
  Error: rpc error: code = InvalidArgument desc = unsupported volume source type "p2p"
  ERROR: job create failed for p2p-spike-s5-baseline-...
  VERDICT: FAIL
  ```
- **Verdict:** FAIL (wrong volume source), to rerun
- **Facts:** for **jobs**, `VolumeMount.source` is a Nebius Storage **bucket ID** or a filesystem ID (SDK docstring: "Use a Nebius Storage bucket ID or Compute filesystem ID"); the CLI reads the part before the first `-` as the source type, so a bucket name such as `p2p-...` fails. Endpoints and Devlabs accept a name or an ID.
- **Decision:** rerun the baseline with `S5_BUCKET=<bucket ID>`. The Nebius backend must resolve each bucket name to its ID before creating a job (A-20 follow-up: today it passes the name).

## S6 — Endpoint update by digest; container disk is ephemeral; Litestream restore works after stop/start

- **Question:** Endpoint update by digest; container disk is ephemeral; Litestream restore works after stop/start?
- **Command:** `KEEP=1 spikes/s6_endpoint.sh` (WSL, parent-id = p2p-control)
- **Output:**
  ```
  Error: rpc error: code = ResourceExhausted desc = Quota limit exceeded ...
    compute.instance.non-gpu.vcpu (limit 0, requested 2)
  VERDICT: FAIL
  ```
- **Verdict:** FAIL (quota), to rerun once the quota is raised
- **Facts:** a Serverless endpoint consumes the tenant's `compute.instance.non-gpu.vcpu` quota, which was 0. Jobs and endpoints run on compute instances of the project (S12 shows a `compute_instance_id`).
- **Decision:** rerun S6 and S13 after the requested quota (16 vCPU, 8 VMs, 10 disks) is approved.

## S7 — Federated credentials with GitHub Actions OIDC

- **Question:** Federated credentials with GitHub Actions OIDC?
- **Command:** `spikes/s7_oidc.yml` copied to `.github/workflows/` on branch `spike/oidc`
- **Output:**
- **Verdict:** pending
- **Facts:**
- **Decision:**

## S8 — Tavily key and credits

- **Question:** Tavily key and credits?
- **Command:** manual: Tavily dashboard + one search call (spikes/README.md)
- **Output:** the search returned a JSON result list; the dashboard shows 4125 credits, 1 used.
- **Verdict:** PASS
- **Facts:** 4125 Tavily credits (Builder Program); one basic search costs 1 credit.
- **Decision:** keep the per-run (20) and per-day (200) Tavily caps in `config/budgets.yaml`.

## S9 — HF Papers API returns the GitHub repo for an arXiv ID

- **Question:** HF Papers API returns the GitHub repo for an arXiv ID?
- **Command:** `uv run python spikes/s9_hf_papers.py` (free, no credentials; run during A-02 on 2026-10-15)
- **Output:**
  ```
  2106.09685: /papers HTTP 200 keys=[ai_keywords, ai_summary, ai_summary_model, authors, discussionId, id, linkedDatasets, linkedModels, linkedSpaces, numTotalDatasets, numTotalModels, numTotalSpaces, publishedAt, summary, title, upvotes]
  2106.09685: /arxiv/repos HTTP 200 keys=[datasets, models, spaces]
  2106.09685: github (abstract text only) -> https://github.com/microsoft/LoRA
  2010.11929: no github URL
  1512.03385: no github URL
  2006.11239: github (abstract text only) -> https://github.com/hojonathanho/diffusion
  PARTIAL: no structured repo field: URLs come from abstract text only -> Tavily fallback
  VERDICT: PARTIAL
  ```
- **Verdict:** PARTIAL
- **Facts:**
  - `GET https://huggingface.co/api/papers/<id>` answers in ~200–300 ms without a token; there is **no** `githubRepo` (or similar) field.
  - `GET /api/arxiv/<id>/repos` returns only HF `models`, `datasets`, `spaces` (no GitHub).
  - GitHub URLs appear only inside the abstract (`summary`), for 2/4 well-known papers (LoRA, DDPM); none for ViT and ResNet.
- **Decision:** A-17 discovery order: (1) regex GitHub URLs from the HF `summary` and the arXiv abstract/comments; (2) Tavily search + extract when nothing is found; (3) Utility ranks candidates. HF `linkedModels`/`linkedDatasets` are still useful for dataset discovery.

## S10 — Default security group still applies to the gateway; rules with priority < 500 win

- **Question:** Default security group still applies to the gateway; rules with priority < 500 win?
- **Command:** `spikes/s10_sg_scan.sh` (WSL, after H-04)
- **Output:**
- **Verdict:** pending
- **Facts:**
- **Decision:**

## S11 — Per-run buckets: name rules, creation latency, quota, mount by name in a job

- **Question:** Per-run buckets: name rules, creation latency, quota, mount by name in a job?
- **Command:** `python3 spikes/s11_buckets.py --count 5 --job-image <python:3.12-alpine digest>` (WSL)
- **Output (key facts):**
  ```
  name 34 chars (production shape): accepted; 63 chars: accepted; 64 chars: rejected; underscore: rejected
  create 4.4-5.5 s, get-by-name 0.1-0.3 s, delete 0.1-0.2 s (x5)
  FAIL: mount job create failed: unsupported volume source type "p2p"
  VERDICT: FAIL
  ```
- **Verdict:** PARTIAL (names and latency PASS; the mount fails for the same reason as S5)
- **Facts:** bucket names up to 63 characters, no underscore; creation takes ~4.5 s and the name is visible to `get-by-name` immediately.
- **Decision:** `p2p-run-<slug>` (34 characters) is valid. A job mounts a bucket by **ID**: the orchestrator keeps the ID it gets when it creates (or finds) the bucket. The mount itself is proven by the S5 rerun.

## S12 — Runner GPU image pull time on a fresh VM

- **Question:** Runner GPU image pull time on a fresh VM?
- **Command:** `spikes/s12_pull_time.sh` (WSL)
- **Output (key facts):**
  ```
  image pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime@sha256:c8268a92...
  created_at 07:53:48.76Z, started_at 07:57:26.50Z, finished_at 07:57:28.56Z
  container output: TORCH 2.5.1+cu124 True
  job defaults: disk NETWORK_SSD 100 GiB, shm 16 GiB, timeout 3600 s
  VERDICT: PASS
  ```
- **Verdict:** PASS
- **Facts:**
  - From create to container start: **3 min 38 s** with a ~3 GB CUDA image on a fresh VM (the script's own "create -> RUNNING 0 s" is wrong: the CLI create waits, so the status timestamps are the source).
  - Default job disk 100 GiB network SSD, `/dev/shm` 16 GiB, timeout 3600 s.
- **Decision:** PASS under the 240 s budget. Measure again with the real runner image (larger) after H-05 (`IMAGE=<registry>/p2p-runner@sha256:...`); if it is much slower, slim the image or cache more in `p2p-cache`.

## S13 — Idle timeout of the managed endpoint URL for long SSE connections

- **Question:** Idle timeout of the managed endpoint URL for long SSE connections?
- **Command:** `uv run python spikes/s13_sse_probe/client.py <S6 url> --minutes 10`
- **Output:**
- **Verdict:** pending
- **Facts:**
- **Decision:**
