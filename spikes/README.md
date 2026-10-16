# Day-1 spikes (S1–S13)

Each spike answers one platform question from the architecture (§21) before we build on it.
Every script prints its key facts and ends with `VERDICT: PASS|FAIL|PARTIAL`.
Paste the full output into the task conversation; it goes into `docs/spikes/RESULTS.md`
(Question · Command · Output · Verdict · Facts · Decision).

**Total expected spend: about $1.** All scripts read credentials from the environment and never print them.

## 0. One-time setup

Two shells are used:

| Shell | For | Why |
|---|---|---|
| **Windows** (Git Bash or PowerShell) in the repo, with `uv` | Python spikes that only call HTTP APIs: S2, S3, S9, S13 client | They run under `uv run` with the project's Python |
| **WSL Ubuntu** (or any Linux/macOS) in the same checkout (`/mnt/c/Projects/GIT/paper2proof`) | Everything that drives the `nebius` CLI: S4, S5, S6, S10, S11, S12 | Git Bash rewrites `src:/container/path` arguments into Windows paths and breaks `--inject-file` / `--volume`. The scripts refuse to run there |

```bash
# Both shells: load the environment (fill .env from .env.example first; never commit it)
set -a; source .env; set +a

# WSL only: install and log in to the Nebius CLI (browser login), then point it at a project
curl -sSL https://artifacts.nebius.cloud/cli/install.sh | bash
exec -l "$SHELL"
nebius profile create                                   # interactive
nebius config set parent-id "$NEBIUS_SANDBOX_PROJECT_ID"
sudo apt-get install -y python3 curl                    # if missing
```

Re-check `parent-id` before each CLI spike: S6 uses **p2p-control**, the others **p2p-sandbox**.

## 1. Run order

| # | Spike | Shell | Needs | Cost |
|---|---|---|---|---|
| 1 | S1 account + credits | browser | H-01 | 0 |
| 2 | S8 Tavily key | Windows | `TAVILY_API_KEY` | 1 credit |
| 3 | S2 models | Windows | `NEBIUS_API_KEY` | < $0.10 |
| 4 | S9 HF Papers | Windows | nothing | 0 |
| 5 | S3 Sandboxes | Windows | `NEBIUS_API_KEY`, `TF_PROJECT_ID`, beta access | ~0 |
| 6 | S11 buckets | WSL | CLI, p2p-sandbox | 0 |
| 7 | S4 GPU job | WSL | CLI, L40S quota | ~$0.15 |
| 8 | S5 baseline | WSL | CLI, a test bucket | < $0.01 |
| 9 | S12 pull time | WSL | CLI, L40S quota | ~$0.25 |
| 10 | S6 endpoint (keep it up) | WSL | CLI, p2p-control | ~$0.05 |
| 11 | S13 SSE timeout | Windows | the S6 endpoint URL | 0 |
| 12 | S7 GitHub OIDC | GitHub | repo pushed, SA + federated credential | 0 |
| — | S5 gateway-on/off, S10 | WSL | **after H-04** (infra up) | < $0.05 |

### S1 — Token Factory account and credits from Italy (manual)
Redeem `NEBIUS-DEVPOST-GLOBAL26`, join the Builders Program, create the API key.
**PASS:** the credits show up in the Token Factory billing page and the key works for S2.

### S8 — Tavily key and credits (manual + one call)
```bash
# the key goes through stdin (-H @-), never on the command line
printf 'Authorization: Bearer %s\n' "$TAVILY_API_KEY" | curl -sS https://api.tavily.com/search -H @- \
  -H 'Content-Type: application/json' -d '{"query":"arxiv 2106.09685 github","max_results":1}' | head -c 400; echo
```
**PASS:** a JSON result list; the dashboard shows the remaining credits (write the number down).

### S2 — Models, tool calling, JSON Schema, reasoning
```bash
uv run python spikes/s2_models.py
```
**PASS:** every role in `config/models.yaml` resolves to exactly one model ID, and Super and Ultra
do forced tool calls. **PARTIAL** lines tell us which roles need `structured_output: tool` and how
reasoning is toggled (compare `thinking=` and token counts across the `reasoning[...]` lines).
**FAIL:** `/models` unreachable or a role matches 0 or 2+ models (then we set `exact_id`).

### S9 — HF Papers returns the GitHub repo
```bash
uv run python spikes/s9_hf_papers.py            # or pass arXiv IDs
```
**PASS:** a `github ->` line for at least 2 of the 4 default papers; the printed keys give A-17
its response shape.

### S3 — Sandboxes access, limits, networking disabled
```bash
uv run python spikes/s3_sandboxes.py            # --image <uuid|tag:python:3.12> to choose
```
**PASS:** `whoami` lists permissions and limits; the online run prints `CONNECTED`; the offline
run prints `CONNECT_FAILED` and `IFACES: lo`. **FAIL** with HTTP 401/403 = no beta access yet →
smoke tests fall back to CPU jobs (`JobSmokeExecutor`).

### S11 — Per-run buckets
```bash
nebius config set parent-id "$NEBIUS_SANDBOX_PROJECT_ID"
python3 spikes/s11_buckets.py --count 5 \
  --job-image "python:3.12-alpine@sha256:1b668429b3511ab407d8e00648891631b0b1a4d7e15e3ca70f38ab5b91ad4ab4"
```
The script empties the mount-test bucket before deleting it.
**PASS:** the 34-char `p2p-run-<slug>` name is accepted, 5 buckets are created and deleted with
latencies printed, and the job shows `MOUNT_OK` for a bucket mounted **by name**.
Record whether 63/64-char and underscore names are accepted (bucket names are unique per region).

### S4 — GPU quota and an nvidia-smi job
```bash
spikes/s4_gpu_job.sh                              # SUBNET_ID=... if the project has several subnets
```
**PASS:** final state is a success state and the logs show `NVIDIA-SMI` with an `L40S`.
**FAIL** with a quota/capacity error → open the quota ticket now (fallback `gpu-l40s-d`).

### S5 — Job networking, baseline (day 1)
```bash
nebius storage bucket create --name "p2p-spike-s5-$(date +%s)"   # note the name
S5_BUCKET=<that name> spikes/s5_job_gateway.sh --mode baseline
```
**PASS:** image pulled, `MOUNT_OK`, and open internet reachable (this is the reference without the
gateway). The probe writes `probe-*.txt` files into the bucket: empty it before deleting it
(console, or `aws s3 rm s3://<bucket> --recursive --endpoint-url https://$NEBIUS_STORAGE_HOST`),
then `nebius storage bucket get-by-name --name <bucket>` → `nebius storage bucket delete --id <id>`.

### S12 — GPU image pull time
```bash
spikes/s12_pull_time.sh                           # IMAGE=<registry>/p2p-runner@sha256:... after A-06
```
**PASS:** `create -> RUNNING` under 240 s with a ~3.3 GB image and `torch.cuda.is_available()`
True. **PARTIAL** above 240 s → slimmer runner image, more in `p2p-cache`.

### S6 — Endpoint lifecycle (leave it running for S13)
```bash
nebius config set parent-id "$NEBIUS_CONTROL_PROJECT_ID"
KEEP=1 spikes/s6_endpoint.sh      # kept only if it got as far as serving /boot; prints the delete command
```
**PASS:** endpoint reaches RUNNING, `url:` is printed, the restart behaviour is recorded
(we expect "container disk is EPHEMERAL"), and the in-place `update --image` serves Alpine.
**PARTIAL:** no in-place update → `deploy/endpoint.sh` will delete + create.
If it stops at "no --inject-file", build `spikes/s13_sse_probe/Dockerfile`, push it to the
control registry twice (two digests, e.g. rebuilt with a different label) and re-run with
`IMAGE_A=<registry>/s6-probe@sha256:<first> IMAGE_B=<registry>/s6-probe@sha256:<second>`.

### S13 — SSE idle timeout (10 minutes)
```bash
uv run python spikes/s13_sse_probe/client.py https://<url printed by S6> --minutes 10
nebius ai endpoint delete --id <endpoint id printed by S6>        # WSL, right afterwards: it is public (~$50/month if forgotten)
```
**PASS:** the 15 s stream lasts the full 600 s. Partial lines on 30/60 s tell us the idle timeout.

### S7 — GitHub Actions OIDC → Nebius
Uses a **throwaway service account with no roles** (`whoami` needs none), never `sa-ci-deployer`:
a federated credential outlives the branch, so anyone who re-creates `spike/oidc` later would
get that account's token until the credential is deleted.
```bash
# WSL, parent-id = p2p-control
nebius iam service-account create --name sa-spike-oidc            # note its id -> SPIKE_SA_ID
nebius iam federated-credentials create --parent-id "$NEBIUS_CONTROL_PROJECT_ID" \
  --name gh-spike-oidc --subject-id "$SPIKE_SA_ID" \
  --oidc-provider-issuer-url https://token.actions.githubusercontent.com \
  --federated-subject-id "repo:atozzi-dev/paper2proof:ref:refs/heads/spike/oidc"   # note its id
curl -fsSL https://artifacts.nebius.cloud/cli/install.sh | sha256sum      # -> installer sha256
```
On GitHub set repository **variables** `NEBIUS_SPIKE_SA_ID` and `NEBIUS_CLI_INSTALLER_SHA256`, then:
```bash
git switch -c spike/oidc
mkdir -p .github/workflows && cp spikes/s7_oidc.yml .github/workflows/spike-s7-oidc.yml
git add .github/workflows/spike-s7-oidc.yml && git commit -m "ci: spike S7 OIDC probe"
git push -u origin spike/oidc          # then read the run log on GitHub
```
**PASS:** the last step prints `VERDICT: PASS` (token exchanged and `nebius iam whoami` works).
**FAIL:** fallback = `sa-ci-deployer` access key in a GitHub Environment with required reviewers.
The log shows which exchange endpoint, parameters and audience were accepted (allowlisted fields
only); copy those lines.

Teardown (always, PASS or FAIL):
```bash
git switch main && git push origin --delete spike/oidc && git branch -D spike/oidc
nebius iam federated-credentials delete --id <federated credential id>
nebius iam service-account delete --id "$SPIKE_SA_ID"
```
The real credential for `sa-ci-deployer` (subject pinned to `refs/heads/main`) is created in A-03/H-04.

### After H-04: S5 gateway-on / gateway-off and S10
Mirror the probe image into the sandbox registry (needs `docker login` to the registry):
```bash
docker pull python:3.12-alpine@sha256:1b668429b3511ab407d8e00648891631b0b1a4d7e15e3ca70f38ab5b91ad4ab4
docker tag  python:3.12-alpine@sha256:1b668429b3511ab407d8e00648891631b0b1a4d7e15e3ca70f38ab5b91ad4ab4 "$REGISTRY_SANDBOX/python:3.12-alpine"
docker push "$REGISTRY_SANDBOX/python:3.12-alpine"      # note the pushed digest
export PROBE_IMAGE="$REGISTRY_SANDBOX/python@sha256:<pushed digest>"
S5_BUCKET=<test bucket> spikes/s5_job_gateway.sh --mode gateway-on
S5_BUCKET=<test bucket> spikes/s5_job_gateway.sh --mode gateway-off   # restarts the gateway at the end
GATEWAY_PRIVATE_IP=<allocation ip> spikes/s10_sg_scan.sh
```
**PASS (gateway-on):** pull OK, `MOUNT_OK`, every internet target blocked, storage host reachable.
**PASS (gateway-off):** internet still blocked (fail-closed); the facts say whether pull and mount
still work with the gateway down.
**PASS (S10):** gateway 80/443 reachable, 22/3128/3129/8080 not, no direct egress on other ports.

## Files

| File | Purpose |
|---|---|
| `spikelib.py`, `lib.sh` | Shared helpers (HTTP, verdicts, job create/wait/logs) |
| `net_probe.py` | Injected into S5/S10/S11 jobs: mount, TCP and TLS checks |
| `s13_sse_probe/server.py` | Stdlib probe app deployed by S6 (`/boot`, `/sse`) |
| `s13_sse_probe/Dockerfile` | Fallback image if endpoints lack `--inject-file` |
