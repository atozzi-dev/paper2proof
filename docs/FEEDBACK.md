# Feedback — Nebius Token Factory, Nebius AI Cloud, NVIDIA

Collected while we built Paper2Proof, from 2026-10-15 to the submission. Every entry below says
what we were doing, what happened, what we expected, what it cost us and what we suggest. The
summary comes first, ranked by impact.

## Summary

**What blocked or slowed us most**

1. **New tenants start with 0 non-GPU vCPUs** (blocker). No CPU job, no VM and no Serverless
   endpoint can start until a quota request is approved, while GPU jobs work. Our request was never
   answered: after six days the quota was still 0 in every region, and we redesigned the deploy
   three days before the deadline (control plane on our own host, only GPU work on Nebius). A small
   default CPU quota, or a timely answer to small requests, would fix it.
2. **GitHub Actions OIDC to Nebius is undocumented** (major). The token exchange, the accepted
   audience (`sts.nebius.com`, no scheme) and the subject form with numeric IDs had to be found
   by trial, over about two hours and several CI runs. An official example or a `nebius/login`
   action would save every team this time.
3. **A job volume needs a bucket ID, and a name fails** with `unsupported volume source type`
   (major). Endpoints accept names. This was a production bug that only a live test found.
4. **Job lifecycle commands are undocumented** (major): `get` / `get-by-name`, the list of job
   states and `endpoint update` are missing from the Serverless pages, so idempotent deploys
   could not be written from the docs alone.
5. **No OTLP metrics ingest and no alerts on custom metrics** (major). Traces have a direct OTLP
   endpoint, but metrics need an extra collector, and alerts cover only VMs and buckets.

**Smaller gaps:** GPU jobs wait for capacity with no state detail; Sandboxes pricing is not
documented, its files API answers a large upload with a bare 502, and an instance that runs
out of disk (no size documented) says so only in the operation's `error` field; the model catalog and `/v1/models` disagree, and `/v1/models` has no context
length; Super ignores `response_format: json_schema`, and the thinking switch works only through
`chat_template_kwargs`; the Batch API wire format is not shown in full; the Python SDK needs an
undeclared package on Windows; tracing ingest is gRPC-only.

**What worked well:** Token Factory's OpenAI-compatible API with forced tool calls on every
Nemotron model; Sandboxes with networking disabled (truly isolated, 1.5 s start); the Python
SDK's message types and service clients, which made the cloud adapter easy to test; Tavily,
which worked on the first call.

## Token Factory (inference API)

### 2026-10-17 — Batch API wire format and batch pricing not pinned down for us yet
- **Context:** A-30, building `app/models/batch.py` for the extraction eval (set C) and the routing ablation. Offline only: no paid call made yet.
- **What happened:** Our model-routing spec only says "Files API upload, create a batch, poll, download output and error files, map by `custom_id`". To write the client we had to assume the OpenAI Batch format: input lines `{custom_id, method, url, body}` with `url = /v1/chat/completions`, `completion_window = "24h"`, output lines `{id, custom_id, response: {status_code, request_id, body}, error}`, a separate error file, and per-line `usage`. Not yet verified against Token Factory: whether `response` wraps the completion in `status_code`/`body`, whether failed lines go to the error file or inline, which `completion_window` values are accepted, whether `chat_template_kwargs` (reasoning on/off) is honoured in batch bodies, and the batch discount (`batch_discount: 0.5` in `config/pricing.yaml` is indicative).
- **Expected:** One page with a complete input line, a complete output line, a complete error line, the accepted `completion_window` values, the batch limits and the batch price per model.
- **Impact:** The parser accepts both shapes (`response.body` and a bare completion, inline or top-level `error`) and the budget is charged conservatively (missing lines at their input estimate, a timed-out batch in full). The first paid run must confirm the format before the results are trusted.
- **Suggestion:** Publish example JSONL for input, output and error files, plus batch pricing and limits, on the Batch API page.
- **Severity:** minor

## Token Factory Sandboxes

### 2026-10-30 — An instance runs out of disk with no limit documented, and only the operation says so
- **Context:** eval set B, InterpretML (1909.09223): smoke runs of an experiment with
  `interpret`, `lightgbm` and `xgboost` installed, the authors' repository and a parquet
  dataset mounted as input files (A-30).
- **What happened:** every instance failed about 5 s after it started, with no exit code and
  no output. Only `GET /v1/operations/<uuid>` showed why: `status: FAILED`,
  `error: RemoteCallError('OSError', '28', 'No space left on device (os error 28)')`. The
  instance limits list `instance_max_layer_bytes` (12 GiB) but no disk or tmpfs size, and the
  4 vCPU / ~3.8 GiB shape has no larger variant we could find.
- **Expected:** the writable disk size of an instance documented next to its CPU and memory,
  a way to ask for more, and the failure reason in the operation's `result` (or a distinct
  status) so a client can tell "the sandbox could not hold the inputs" from "the program
  failed".
- **Impact:** the Coder spent its 8 smoke runs on a failure it could not fix, and the run
  ended `SMOKE_EXHAUSTED`; finding the cause took a manual operation lookup.
- **Suggestion:** document the per-instance disk, offer larger shapes, and report ENOSPC as a
  structured error.
- **Severity:** major

### 2026-10-29 — The Sandboxes image import fails on an image referenced by digest
- **Context:** importing `p2p-runner-cpu` from our Nebius Container Registry into Sandboxes (H-03, plan B of the hybrid deploy).
- **What happened:** `POST /v1/images/import` with `docker://cr.eu-north1.nebius.cloud/<registry>/p2p-runner-cpu@sha256:<digest>` ended `FAILED` with `HTTP 404 Not Found ... NAME_UNKNOWN ... Entity Folder not found for registry`. The same credentials could read that digest with `docker manifest inspect`, and the import of the same image by tag worked. The image is an OCI index (docker's default build attestation). The operation's `error` is the only diagnostic, and the import script's 300-character excerpt hid the cause.
- **Expected:** an import by digest, the only reference that pins an image, or a clear "references by digest are not supported" error.
- **Impact:** about an hour. We now import by tag, after checking that the tag resolves to the digest, and record the UUID under the digest.
- **Suggestion:** resolve `@sha256:` references (indexes included) in the importer, and document the supported reference forms in the import API.
- **Severity:** major

### 2026-10-29 — The files API answers 502 to a large upload, with no hint whether it was stored
- **Context:** A-30, production eval on Sandboxes: a smoke run whose package overlay held CatBoost, LightGBM and XGBoost (hundreds of MB), sent with `POST /v1/files` and a Content-Length.
- **What happened:** the upload answered HTTP 502 with an HTML gateway page. Nothing says whether the file was stored, whether a size limit applies, or whether to retry.
- **Expected:** a documented maximum upload size, a 413 with that size when it is exceeded, and a JSON error body; or resumable/multipart uploads for large archives.
- **Impact:** the run failed with an internal error until we added retries with a lookup by sha256 between them; about an hour of diagnosis.
- **Suggestion:** document the upload size limit and the retry semantics of `/v1/files`, and answer 413 rather than 502 above it.
- **Severity:** major

### 2026-10-29 — A wrong `Project` header gives HTTP 200 with every permission false, then 403
- **Context:** the same import, after a shell restart, with a placeholder in `TF_PROJECT_ID`.
- **What happened:** `GET /v1/whoami` answered 200 with all permissions `false`, and the import answered 403, with no hint that the project ID was unknown.
- **Expected:** 404 or 400 "unknown project" from `whoami`.
- **Impact:** minor, a few minutes.
- **Suggestion:** reject an unknown `Project` header explicitly.
- **Severity:** minor

### 2026-10-27 — Sandboxes can run a whole exec job offline (praise)
- **Context:** spike S14: a disposable instance with networking disabled, inputs uploaded and mounted, the job command as uid 10001, the outputs returned on stdout.
- **What happened:** uploads with deduplication, root-owned read-only mounts, 8 MiB of stdout, cancellation in 0.2 s, and the env echoed in the operation metadata all worked as documented. Start-up takes seconds.
- **Impact:** it replaced GPU jobs for our live runs in a day (no CPU quota, no GPU capacity).
- **Severity:** praise

### 2026-10-15 — Billing model of Sandboxes is not documented
- **Context:** A-02, writing spike S3 (Sandboxes access, limits, networking disabled).
- **What happened:** The limits/retention page lists concurrency and output limits but does not say whether Sandboxes usage is charged against Token Factory credits, nor any price; the operation `cost` field is described as diagnostics only.
- **Expected:** A pricing line (per CPU-second or per operation) and whether hackathon credits cover it. https://docs.tokenfactory.nebius.com/sandboxes/operate/limits-retention-and-usage.md
- **Impact:** We cannot put Sandboxes in the per-run cost model before measuring it; spike S3 has to record the `cost` field and reconcile with billing later.
- **Suggestion:** Add a short "Pricing and billing" section, including how credits apply during the beta.
- **Severity:** minor

### 2026-10-21 — Sandboxes with networking disabled are isolated and fast (praise)
- **Context:** spike S3 (H-03), smoke tests of agent-written code.
- **What happened:** after activation, a disposable run with networking disabled had only `lo` and could not connect out, and started in 1.5 s (11 s with networking on). `whoami` lists permissions and limits clearly.
- **Expected:** this.
- **Impact:** smoke tests can move from CPU jobs to Sandboxes.
- **Suggestion:** none; document the per-run cost.
- **Severity:** praise

## Serverless AI (Endpoints, Jobs)

### 2026-10-15 — `ai job get`, job states and `ai endpoint update` are undocumented
- **Context:** A-02, writing spikes S4/S6/S12 (job lifecycle polling, endpoint update by digest).
- **What happened:** The jobs page documents create/list/logs/cancel/delete but not how to get one job (by id or name) nor the list of job states; the endpoints page has no command to change the image of an existing endpoint.
- **Expected:** `get` / `get-by-name` for jobs and endpoints, the state machine (PENDING → … → COMPLETED/FAILED), and an `update --image` example. https://docs.nebius.com/serverless/jobs/manage.md, https://docs.nebius.com/serverless/endpoints/manage.md
- **Impact:** Our scripts have to probe `--help` and try several command forms; idempotent "get-by-name then create" deploys (a documented best practice elsewhere in Nebius) can't be written from the docs alone.
- **Suggestion:** Add get/get-by-name and update examples plus a states table to both pages, and a JSON output example showing `status.state`.
- **Severity:** major

### 2026-10-21 — A job volume needs a bucket ID; a name fails as "unsupported volume source type"
- **Context:** spikes S5 and S11 (H-03), per-run bucket mounted in a Serverless job.
- **What happened:** `nebius ai job create --volume p2p-spike-s5-...:/mnt/run:rw` failed with `InvalidArgument: unsupported volume source type "p2p"`: the CLI reads the text before the first `-` as a source type. The CLI reference does not define SOURCE; only the SDK docstring says jobs take "a Nebius Storage bucket ID or Compute filesystem ID". Endpoints and Devlabs accept a name or an ID.
- **Expected:** the same rule for jobs as for endpoints (name or ID), or at least a documented SOURCE and an error such as "use the bucket ID".
- **Impact:** a production bug found only by the spike (the backend passed names), plus a backend fix that looks every bucket up first.
- **Suggestion:** accept bucket names for jobs, and document `--volume` with examples.
- **Severity:** major

### 2026-10-21 — New tenants have 0 non-GPU vCPUs, so no Serverless job or endpoint can start
- **Context:** spikes S5 and S6 (H-03).
- **What happened:** even a CPU-only job and a 2-vCPU endpoint failed with `ResourceExhausted: compute.instance.non-gpu.vcpu (limit 0, requested 2)`, while GPU jobs on L40S worked within the GPU quota. The increase request has been "waiting for approval" for hours.
- **Expected:** a small default CPU quota (a few vCPUs), or a hint in the Serverless quickstart that this quota must be raised first.
- **Impact:** endpoint, network and mount spikes blocked for a day during a 2-week hackathon.
- **Suggestion:** grant a default CPU quota with the account, or auto-approve small increases.
- **Severity:** blocker

### 2026-10-27 — The non-GPU vCPU quota stayed at 0 in every region for the whole hackathon
- **Context:** H-01/H-04, deploying the control plane; the request was filed on 2026-10-21 and followed up through Devpost support.
- **What happened:** after six days the quota `compute.instance.non-gpu.vcpu` was still 0, and it was 0 in every region we checked in the console. No answer or ETA came back. So no Serverless endpoint, no CPU job and no VM (our egress gateway) could ever start, while the L40S GPU quota (8) worked from day one.
- **Expected:** a small default CPU quota with a new tenant, or an answer to the increase request with a time frame, within a hackathon's two weeks.
- **Impact:** we redesigned the deploy three days before the deadline: the control plane runs on a VPS of ours, CPU work runs in local docker or Token Factory Sandboxes, and only the GPU experiments run on Nebius AI Cloud (docs/specs/hybrid-deploy.md). That cost about two days of engineering and review, and adds a host we must secure ourselves.
- **Suggestion:** give hackathon tenants a few non-GPU vCPUs by default (the GPU quota already shows the intent), or let a request below a small threshold be approved automatically.
- **Severity:** blocker

### 2026-10-21 — A GPU job waits ~16 minutes for capacity without any signal
- **Context:** spike S4 (H-03), one L40S job.
- **What happened:** the logs showed "Waiting before retrying resource provisioning" and the job started after ~16 minutes of wall time; the job state stayed in provisioning with no reason or estimate.
- **Expected:** a state detail ("waiting for capacity") so that an orchestrator can choose another platform or report the wait.
- **Impact:** our timeouts and GPU budgets must count from `started_at`, and the capacity fallback cannot trigger on a slow start.
- **Suggestion:** expose the provisioning reason in `status.state_details`.
- **Severity:** minor

### 2026-10-27 — A GPU job in a custom subnet fails with a bare "internal error" and no reason
- **Context:** spike S5 `--mode isolated` (H-03): `gpu-l40s-a` / `1gpu-8vcpu-32gb`, `--subnet-id` of our exec subnet, whose route table sends `0.0.0.0/0` to an allocation no VM holds (no gateway, by design).
- **What happened:** three runs. The CLI printed `Job ID: aijob-…`, then `Error: operation failed [id=opvmapp-…, code=13]: Operation failed with internal error`. `nebius ai job get` showed only `{"state": "ERROR"}`, and `nebius ai job logs` had nothing. The same job in the default subnet failed differently, with a clear `NotEnoughResources … VM schedule timeout` (code 8).
- **Expected:** either the job starts in a subnet with no egress (the point of the setup), or the error names the cause (unsupported route, missing public pool, capacity).
- **Impact:** we could not tell a subnet problem from a capacity problem, three days before the deadline, so our isolated-execution gate stayed open and we planned a fallback.
- **Suggestion:** propagate the compute operation's error into the job operation and `status`, as the code-8 path already does, and document which subnet setups jobs support.
- **Severity:** major

### 2026-10-27 — No L40S capacity for a 1-GPU job, and no way to see it before trying
- **Context:** the same spike, default subnet, `gpu-l40s-a` / `1gpu-8vcpu-32gb`.
- **What happened:** after the scheduling wait, `NotEnoughResources: … VM schedule timeout, most likely due to insufficient hardware resources`.
- **Expected:** a way to check capacity (or reserve it) before creating the job.
- **Impact:** GPU experiments become best effort near the deadline; our orchestrator already maps this to the `CAPACITY` error code and reports the verdict as inconclusive for capacity.
- **Suggestion:** a capacity hint per platform and preset in the API or console.
- **Severity:** major

## Networking, IAM, Storage, KMS

### 2026-10-15 — No guide for GitHub Actions OIDC → Nebius access token (federated credentials)
- **Context:** A-02, spike S7 (keyless CI deploys with `sa-ci-deployer`).
- **What happened:** `nebius iam federated-credentials create` is documented (issuer URL, subject id, federated subject from `sub`), but nothing explains how a workload then exchanges its external JWT for a Nebius access token: endpoint, grant type, `subject_token_type`, how the service account is selected. The WIF pages only cover the outbound direction (Nebius token → external OIDC token via sts.nebius.com).
- **Expected:** A "GitHub Actions" how-to with the exact token-exchange request (or a CLI/action that performs it), as other clouds provide. https://docs.nebius.com/cli/reference/iam/federated-credentials/create
- **Impact:** The S7 spike has to try several endpoints and parameter shapes; if none works we fall back to a long-lived access key in GitHub Environments. Resolved on 2026-10-21 by trial (see the two entries below): SDK flow, audience `sts.nebius.com`, subject with numeric IDs.
- **Suggestion:** Publish an end-to-end GitHub Actions example (federated credential + workflow snippet), ideally an official `nebius/login` action.
- **Severity:** major

### 2026-10-21 — Federated credentials: the expected JWT audience is not documented
- **Context:** spike S7 (H-03), keyless CI deploys with GitHub Actions OIDC.
- **What happened:** the exchange (SDK `FederatedCredentialsBearer`) failed with `INVALID_ARGUMENT: JWT audience is missing or unsupported: '[https://github.com/atozzi-dev]'` (GitHub's default), then with `token-service.iam.new.nebiuscloud.net` and `https://sts.nebius.com`. Only `sts.nebius.com` (no scheme) works, found by trying candidates in a workflow. The federated credential resource has no audience field, and the error does not say which value is expected.
- **Expected:** the accepted audience in the federated-credentials docs and in the error message. https://docs.nebius.com/cli/reference/iam/federated-credentials/create
- **Impact:** about two hours and four CI runs.
- **Suggestion:** document `aud = sts.nebius.com` with a GitHub Actions example, and print the expected audience in the error.
- **Severity:** major

### 2026-10-21 — GitHub OIDC subjects carry numeric IDs; the HTTP exchange endpoint answers a Cloudflare 1010
- **Context:** spike S7 (H-03).
- **What happened:** GitHub's `sub` for this repository is `repo:atozzi-dev@261738626/paper2proof@1420235945:ref:refs/heads/...`, not the name-only form that federated-credential examples use, so the first credential never matched (`Incorrect subject id: ...`, which at least prints the value). `https://auth.eu.nebius.com/oauth2/token/exchange` answered GitHub-hosted runners with HTTP 403 "error code: 1010" (Cloudflare), so the documented HTTP exchange cannot be used from CI; the SDK's gRPC token service works.
- **Expected:** an example subject in the ID form, and one supported exchange path for CI.
- **Impact:** two extra CI runs.
- **Suggestion:** show both subject forms in the docs, and say that CI should use the SDK/CLI flow rather than the HTTP gateway.
- **Severity:** minor

## Observability (Tracing, Monitoring, Logs)

### 2026-10-16 — No direct OTLP write for application metrics, and alerts only for VMs and buckets
- **Context:** A-28, exporting the control plane's own metrics (`p2p_runs_total`, tokens per model, cost) from a Serverless endpoint.
- **What happened:** Tracing has a direct OTLP writer (`write.tracing.<region>.nebius.cloud:443`), but metrics ingestion is documented only as Prometheus remote-write from a Prometheus or Vector instance (`write.monitoring.<region>.nebius.cloud/projects/<project>/prometheus/api/v1/write`). Console alerts cover Compute VMs and Object Storage buckets only, with no rules on custom metrics or logs and no import from a file or Terraform.
- **Expected:** an OTLP metrics endpoint next to the tracing one (https://docs.nebius.com/observability/metrics/ingest), and alert rules on any metric in the project (https://docs.nebius.com/observability/alerts).
- **Impact:** an extra OpenTelemetry Collector to run just to forward metrics; alerts and the dashboard have to live in a separate Grafana.
- **Suggestion:** accept OTLP metrics with the same static key and `iam-container` header as tracing, and allow PromQL alert rules (importable as Prometheus rule files) on custom metrics.
- **Severity:** major

### 2026-10-16 — Tracing direct ingest is gRPC-only with app-specific env vars
- **Context:** A-28, configuring the OpenTelemetry SDK exporter for Nebius Tracing.
- **What happened:** the direct-ingest page documents OTLP/gRPC only (`authorization: Bearer <static key>`, `iam-container: <project>`), with examples reading `NEBIUS_REGION_ID`/`NEBIUS_PROJECT_ID`/`NEBIUS_STATIC_TOKEN`; OTLP/HTTP and the standard `OTEL_EXPORTER_OTLP_*` variables are not mentioned, nor token lifetime.
- **Expected:** both OTLP transports and a copy-paste `OTEL_EXPORTER_OTLP_ENDPOINT`/`OTEL_EXPORTER_OTLP_HEADERS` setup (https://docs.nebius.com/observability/traces/direct-ingest).
- **Impact:** about 30 minutes reading pages to confirm the transport and headers; we pulled in the gRPC exporter.
- **Suggestion:** add OTLP/HTTP (`/v1/traces`) and a standard-env-var example, and state the static key's lifetime.
- **Severity:** minor

## Terraform provider, CLI, SDK

### 2026-10-16 — Python SDK can't be constructed on Windows without an undeclared package
- **Context:** A-20, unit tests of the Serverless Jobs / Object Storage / Compute adapter built on `nebius` 0.6.24, on a Windows dev box.
- **What happened:** `nebius.sdk.SDK(credentials_file_name=...)` raises at construction time, before any request: `RuntimeError: Install 'certifi-win32' to access Windows certificates` (`nebius/base/tls_certificates.py:46`). `certifi-win32` is not a declared dependency, not even as a Windows-only marker.
- **Expected:** constructing the SDK works on every supported OS (falling back to `certifi`), or the dependency is declared with a `sys_platform == "win32"` marker and the requirement is in the SDK README.
- **Impact:** ~10 min; the wiring test patches the `SDK` class (prod runs on Linux).
- **Suggestion:** fall back to `certifi.where()` on Windows, or declare `certifi-win32; sys_platform == "win32"`.
- **Severity:** minor

### 2026-10-16 — SDK message types and service clients are easy to test against (praise)
- **Context:** A-20, mocking the SDK without network access.
- **What happened:** real message types (`Job`, `JobSpec`, `Bucket`, `Instance`, `ListJobsResponse`) build directly in tests; service clients (`JobServiceClient`, `BucketServiceClient`, `InstanceServiceClient`) satisfy small `typing.Protocol` seams under `mypy --strict`; `RequestError(RequestStatusExtended(code=NOT_FOUND, ...))` is easy to raise in fakes.
- **Expected:** —
- **Impact:** the adapter is fully unit-tested with fake clients that speak the real types.
- **Suggestion:** document this testing pattern (fake client + real messages) in the SDK README.
- **Severity:** praise

### 2026-10-27 — `nebius ai job create` creates the job, then fails on a lost operation stream
- **Context:** spike S5 `--mode isolated` (H-03) on `gpu-l40s-a` / `1gpu-8vcpu-32gb`, nebius CLI from the VPS.
- **What happened:** the CLI printed `Job ID: aijob-…`, then exited non-zero with `Error: can't get operation: rpc error: code = Internal desc = server closed the stream without sending trailers`. The job had been created, but any caller that trusts the exit code believes it was not, and a GPU job can then keep running (and billing) until its timeout.
- **Expected:** `create` either retries the operation poll, or exits with a distinct code that says "created, state unknown" next to the resource ID.
- **Impact:** a lost spike run (about 15 minutes of capacity wait). Our scripts already look the job up by its deterministic name and delete it on failure, and the orchestrator is get-by-name first for the same reason.
- **Suggestion:** retry `Internal` / stream-reset errors on the operation poll inside the CLI and SDK, and document that create may succeed even when the operation wait fails.
- **Severity:** major

## Nemotron models (Nano, Super, Ultra, Nano Omni)

### 2026-10-21 — Super ignores `response_format=json_schema`; thinking is switched only by `chat_template_kwargs`
- **Context:** spike S2 (H-03), structured extraction of claims.
- **What happened:** `nvidia/nemotron-3-super-120b-a12b` returned HTTP 200 with empty content for `response_format: json_schema`, while Ultra and Nano honour it; forced tool calls work on all three. Reasoning ignores the system-prompt switches (`/no_think`, "detailed thinking off") and turns off only with `chat_template_kwargs: {enable_thinking: false}`.
- **Expected:** consistent structured-output support across the Nemotron family, and the thinking switch in the model card.
- **Impact:** we route every role through tool calls instead.
- **Suggestion:** document per model which structured-output modes and reasoning switches are supported.
- **Severity:** minor

### 2026-10-21 — The model catalog and `/models` disagree
- **Context:** spike S2 (H-03), choosing the vision model.
- **What happened:** `GET /v1/models` lists `nvidia/Nemotron-3.5-Super-VL` and it answers, but the public catalog does not show it; Nemotron-3-Nano-Omni (our planned vision model) is listed as "Dedicated" only. Context windows are shown in the catalog but not in `/models`.
- **Expected:** one source of truth, with context length and modality in `/models`.
- **Impact:** a failed role resolution at the first run and a manual check of the catalog.
- **Suggestion:** expose `context_length` and modalities in `/models`, and keep the catalog in sync.
- **Severity:** minor

## Tavily

### 2026-10-21 — Key, credits and search worked on the first call (praise)
- **Context:** spike S8 (H-03), repository discovery and documentation search for the Coder.
- **What happened:** one basic search returned a JSON result list; the dashboard showed 4,125
  credits and the cost of each call (1 credit for a basic search).
- **Expected:** this.
- **Impact:** none; per-run and per-day credit caps were easy to set from the documented costs.
- **Suggestion:** none.
- **Severity:** praise
