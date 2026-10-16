# Feedback — Nebius Token Factory, Nebius AI Cloud, NVIDIA

Collected during development. Consolidated into the Devpost feedback field at submission (task A-33).

## Token Factory (inference API)

## Token Factory Sandboxes

### 2026-10-15 — Billing model of Sandboxes is not documented
- **Context:** A-02, writing spike S3 (Sandboxes access, limits, networking disabled).
- **What happened:** The limits/retention page lists concurrency and output limits but does not say whether Sandboxes usage is charged against Token Factory credits, nor any price; the operation `cost` field is described as diagnostics only.
- **Expected:** A pricing line (per CPU-second or per operation) and whether hackathon credits cover it. https://docs.tokenfactory.nebius.com/sandboxes/operate/limits-retention-and-usage.md
- **Impact:** We cannot put Sandboxes in the per-run cost model before measuring it; spike S3 has to record the `cost` field and reconcile with billing later.
- **Suggestion:** Add a short "Pricing and billing" section, including how credits apply during the beta.
- **Severity:** minor

## Serverless AI (Endpoints, Jobs)

### 2026-10-15 — `ai job get`, job states and `ai endpoint update` are undocumented
- **Context:** A-02, writing spikes S4/S6/S12 (job lifecycle polling, endpoint update by digest).
- **What happened:** The jobs page documents create/list/logs/cancel/delete but not how to get one job (by id or name) nor the list of job states; the endpoints page has no command to change the image of an existing endpoint.
- **Expected:** `get` / `get-by-name` for jobs and endpoints, the state machine (PENDING → … → COMPLETED/FAILED), and an `update --image` example. https://docs.nebius.com/serverless/jobs/manage.md, https://docs.nebius.com/serverless/endpoints/manage.md
- **Impact:** Our scripts have to probe `--help` and try several command forms; idempotent "get-by-name then create" deploys (a documented best practice elsewhere in Nebius) can't be written from the docs alone.
- **Suggestion:** Add get/get-by-name and update examples plus a states table to both pages, and a JSON output example showing `status.state`.
- **Severity:** major

## Networking, IAM, Storage, KMS

### 2026-10-15 — No guide for GitHub Actions OIDC → Nebius access token (federated credentials)
- **Context:** A-02, spike S7 (keyless CI deploys with `sa-ci-deployer`).
- **What happened:** `nebius iam federated-credentials create` is documented (issuer URL, subject id, federated subject from `sub`), but nothing explains how a workload then exchanges its external JWT for a Nebius access token: endpoint, grant type, `subject_token_type`, how the service account is selected. The WIF pages only cover the outbound direction (Nebius token → external OIDC token via sts.nebius.com).
- **Expected:** A "GitHub Actions" how-to with the exact token-exchange request (or a CLI/action that performs it), as other clouds provide. https://docs.nebius.com/cli/reference/iam/federated-credentials/create
- **Impact:** The S7 spike has to try several endpoints and parameter shapes; if none works we fall back to a long-lived access key in GitHub Environments.
- **Suggestion:** Publish an end-to-end GitHub Actions example (federated credential + workflow snippet), ideally an official `nebius/login` action.
- **Severity:** major

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

## Nemotron models (Nano, Super, Ultra, Nano Omni)

## NemoClaw / OpenShell

## Tavily
