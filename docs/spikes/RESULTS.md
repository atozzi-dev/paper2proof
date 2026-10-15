# Spike results (day 1)

> These verified facts override the specs and the architecture document. Updated after each spike run.

| Spike | Verdict | Decision |
|---|---|---|
| S1 | pending | — |
| S2 | pending | — |
| S3 | pending | — |
| S4 | pending | — |
| S5 | pending | — |
| S6 | pending | — |
| S7 | pending | — |
| S8 | pending | — |
| S9 | PARTIAL | HF Papers has no structured repo field; parse GitHub URLs from the HF summary/arXiv abstract, Tavily as fallback (A-17) |
| S10 | pending | — |
| S11 | pending | — |
| S12 | pending | — |
| S13 | pending | — |

## S1 — Token Factory account and credits can be activated from Italy

- **Question:** Token Factory account and credits can be activated from Italy?
- **Command:** manual: Token Factory console (credits, Builders Program, API key)
- **Output:**
- **Verdict:** pending
- **Facts:**
- **Decision:**

## S2 — Model IDs, context windows, tool calling, JSON Schema output and reasoning toggle for Super, Ultra, Nano, Omni

- **Question:** Model IDs, context windows, tool calling, JSON Schema output and reasoning toggle for Super, Ultra, Nano, Omni?
- **Command:** `uv run python spikes/s2_models.py`
- **Output:**
- **Verdict:** pending
- **Facts:**
- **Decision:**

## S3 — Token Factory Sandboxes access, effective limits, billing, networking disabled works

- **Question:** Token Factory Sandboxes access, effective limits, billing, networking disabled works?
- **Command:** `uv run python spikes/s3_sandboxes.py`
- **Output:**
- **Verdict:** pending
- **Facts:**
- **Decision:**

## S4 — GPU quota for L40S in eu-north1; a nvidia-smi job runs

- **Question:** GPU quota for L40S in eu-north1; a nvidia-smi job runs?
- **Command:** `spikes/s4_gpu_job.sh` (WSL)
- **Output:**
- **Verdict:** pending
- **Facts:**
- **Decision:**

## S5 — A job in a subnet routed to the gateway: image pull, bucket mount, egress blocked; behaviour with the gateway off

- **Question:** A job in a subnet routed to the gateway: image pull, bucket mount, egress blocked; behaviour with the gateway off?
- **Command:** `spikes/s5_job_gateway.sh --mode baseline` (day 1); `--mode gateway-on` / `--mode gateway-off` after H-04
- **Output:**
- **Verdict:** pending
- **Facts:**
- **Decision:**

## S6 — Endpoint update by digest; container disk is ephemeral; Litestream restore works after stop/start

- **Question:** Endpoint update by digest; container disk is ephemeral; Litestream restore works after stop/start?
- **Command:** `KEEP=1 spikes/s6_endpoint.sh` (WSL, parent-id = p2p-control)
- **Output:**
- **Verdict:** pending
- **Facts:**
- **Decision:**

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
- **Output:**
- **Verdict:** pending
- **Facts:**
- **Decision:**

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
- **Output:**
- **Verdict:** pending
- **Facts:**
- **Decision:**

## S12 — Runner GPU image pull time on a fresh VM

- **Question:** Runner GPU image pull time on a fresh VM?
- **Command:** `spikes/s12_pull_time.sh` (WSL)
- **Output:**
- **Verdict:** pending
- **Facts:**
- **Decision:**

## S13 — Idle timeout of the managed endpoint URL for long SSE connections

- **Question:** Idle timeout of the managed endpoint URL for long SSE connections?
- **Command:** `uv run python spikes/s13_sse_probe/client.py <S6 url> --minutes 10`
- **Output:**
- **Verdict:** pending
- **Facts:**
- **Decision:**
