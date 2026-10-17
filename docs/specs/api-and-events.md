# Spec — HTTP API and SSE events

Source: architecture §9.3, §11.1, §11.3, §16.1 (v0.2). Normative.

## Auth model

- **Public:** health endpoints, gallery, gallery runs and their bundles, stats.
- **Session:** `POST /api/session {invite_code}` checks `sha256(code)` against `invites.code_hash` and sets cookie `p2p_session` (itsdangerous-signed, `HttpOnly; Secure; SameSite=Strict; Max-Age=7d`). A session owns the runs it creates.
- **Admin:** header `X-Admin-Token`, compared in constant time with `ADMIN_TOKEN`.
- The endpoint's native bearer auth stays **off**: browsers can't send it.

## Endpoints

| Method | Path | Auth | Request | Response |
|---|---|---|---|---|
| GET | `/healthz` | — | — | `200 {"ok":true}` |
| GET | `/readyz` | — | — | `200` when the DB is open, Litestream is replicating and model roles are resolved; else `503` with the failing checks |
| POST | `/api/session` | — | `{"invite_code": str}` | `204` + cookie · `401` |
| POST | `/api/runs` | session | `{"arxiv_id": str, "claim_hint": str \| null}` | `202 {"run_id", "events_url"}` · `400` bad ID · `402` kill switch active · `429` quota · `503` draining |
| GET | `/api/runs/{id}` | gallery → public, else owner | — | `RunView` |
| GET | `/api/runs/{id}/events` | same | headers `Last-Event-ID`; query `after`, `replay=1`, `speed` (default 10) | SSE stream |
| GET | `/api/runs/{id}/events.json` | same | query `after` | `{"events": [...]}` (polling fallback) |
| POST | `/api/runs/{id}/approve` | owner | — | `204` · `409` if not in AWAITING_APPROVAL |
| POST | `/api/runs/{id}/reject` | owner | — | `204` |
| POST | `/api/runs/{id}/cancel` | owner | — | `202` |
| GET | `/api/runs/{id}/bundle` | same as run | — | `302` to a 5-minute presigned URL (`200` with the archive when the store can't presign: local store in dev) · `404` until built (`docs/specs/proof-bundle.md`) |
| GET | `/api/gallery` | — | — | `[RunCard]` |
| GET | `/api/stats` | — | — | eval numbers, aggregate spend, runs by verdict |
| POST | `/admin/drain` | admin | `{"on": bool}` | `204` |
| POST | `/admin/kill-switch` | admin | `{"on": bool}` | `204` |
| GET | `/admin/runs` | admin | query `listed` (optional bool), `limit` (1–200, default 50) | `[AdminRun]`, newest first: `{id, arxiv_id, status, listed, created_at, verdicts: [VerdictKind]}` (curation candidates) |
| POST | `/admin/gallery/{id}` | admin | `{"listed": bool, "reviewed": bool = false}` | `204` · `404` · `409` when listing a run that is not terminal, or one with a `NOT_REPRODUCED` verdict without `"reviewed": true` (architecture §16.1). Listing stores `verdict_summary` (the verdict kinds) for the gallery card; unlisting always succeeds (takedown) |
| POST | `/admin/invites` | admin | `{"quota_per_day": int = 3, "expires_at": iso \| null}` | `201 {"code"}` (shown once: only its sha256 is stored) |
| GET | `/admin/invites` | admin | — | `[InviteView]`: `{id, quota_per_day, used_today, used_on, expires_at, active}`; `id` is the first 12 hex characters of the code's sha256, never the code |
| POST | `/admin/invites/revoke` | admin | `{"id": hex}` (12–64 characters: a prefix of the code's sha256) | `204` (the invite expires now: no new session, no new run) · `404` · `409` if the prefix matches several invites |

Admin routes are not part of the public OpenAPI schema (`/api/openapi.json`). `python -m app.admin_cli` calls them with the admin token read from `ADMIN_TOKEN` or a prompt, never from the command line (docs/OPERATIONS.md).

`arxiv_id` must match `^\d{4}\.\d{4,5}(v\d+)?$`.

## Rate limits

- Per IP: 60 req/min on `/api/*`; 5 req/min on `POST /api/session`.
- Per invite: `quota_per_day` live runs (default 3), counted at `POST /api/runs`.
- Max 2 concurrent SSE streams per session; 50 total. Past the limit → `429`, and the client falls back to polling.

## SSE

- Each event: `id: <monotonic int>`, `event: <type>`, `data: <json>`.
- Heartbeat comment `: hb` every 15 s.
- On reconnect with `Last-Event-ID`, replay every event with a greater id, then go live.
- `replay=1`: re-emit the stored events with the original gaps divided by `speed`, capped at 2 s each.

### Event types and payloads

| type | payload |
|---|---|
| `run.status` | `{status, error_code?}` |
| `step.started` | `{step, attempt}` |
| `step.finished` | `{step, attempt, ok, error_code?, duration_ms}` |
| `claim.extracted` | `{claim: Claim}` |
| `plan.ready` | `{plan: PlanView, est_cost_usd, needs_approval}` |
| `approval.required` | `{expires_at}` |
| `model.call` | `{role, model_id, tokens_in, tokens_out, latency_ms, cost_usd}` |
| `sandbox.output` | `{iteration, branch?, stream, lines: [str] (≤ 50)}` |
| `job.status` | `{kind, name, state}` |
| `job.log` | `{kind, lines: [str] (≤ 50)}` |
| `egress.blocked` | `{host, subnet}` (from gateway logs, best effort) |
| `verdict.ready` | `{claim_id, verdict: Verdict, explanation_md}` |
| `cost.update` | `{inference_usd, cloud_usd, total_usd}` |
| `warning` | `{code, message}`, non-fatal. Codes: `DISCOVERY_DEGRADED` (Tavily or HF unavailable), `ABSOLUTE_AT_REDUCED_SCALE` (plan tests an ABSOLUTE claim at reduced scale), `PACKAGE_REJECTED` (a planned package failed the policy), `PACKAGES_REQUESTED` (the Coder asked for packages: back to PROVISIONING), `REPO_SNAPSHOT_UNAVAILABLE` (the Coder works without repository files), `JOB_PREEMPTED` (the GPU job was preempted: retried once on demand, or not when the budget does not allow it), `NO_GPU_CAPACITY` (the platform had no GPU capacity for the job: retried in 2 minutes, then on the fallback platform, then INCONCLUSIVE/CAPACITY), `BUNDLE_UNSIGNED` (the Proof Bundle was stored without a signature: KMS unavailable), `BUNDLE_FAILED` (no Proof Bundle could be built; the verdict stands). New codes are added here first |
| `error` | `{code, message}` (no stack traces) |

All payloads are pydantic models in `app/domain/events.py`. `schemas/events.schema.json` is generated from them (`uv run python scripts/export_schemas.py`); a contract test fails if it is stale. The frontend imports TypeScript types generated from the OpenAPI schema (`npm run gen:types`).

## Security headers (all responses)

`Content-Security-Policy: default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'` · `Strict-Transport-Security: max-age=31536000` · `X-Content-Type-Options: nosniff` · `Referrer-Policy: no-referrer`.

Markdown coming from models or papers is rendered in the UI with a sanitizer (no raw HTML).

## Copy rules (UI)

- `NOT_REPRODUCED` → "Not reproduced under our conditions" + a conditions list (scale, seeds, hardware, deviations from the paper).
- `REPRODUCED_AT_REDUCED_SCALE` → "Reproduced at reduced scale" + the scale factor.
- `INCONCLUSIVE` → "Inconclusive" + the human-readable reason.
