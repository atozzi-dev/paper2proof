# Operations

How the running demo is operated: invites, the public gallery, takedown requests, and the secret
rotation once judging ends. Everything here is a human action on production (CLAUDE.md,
"Human-only actions"). Deploying is in `docs/DEPLOY.md`, the infrastructure in `infra/README.md`.

## Admin helper

`app/admin_cli.py` calls the `/admin/*` routes of the running endpoint. They are authenticated
with `X-Admin-Token` and are not in the public OpenAPI schema (`docs/specs/api-and-events.md`).

```bash
export P2P_API_URL=https://<endpoint-host>       # https only (plain http only for localhost)
read -rs ADMIN_TOKEN && export ADMIN_TOKEN        # never on the command line or in history
uv run python -m app.admin_cli invite list
```

Without `ADMIN_TOKEN` the helper prompts for it. There is no `--token` option on purpose: a
token in argv ends up in the shell history and the process list. Unset it when done
(`unset ADMIN_TOKEN`).

| Command | What it does |
|---|---|
| `invite create [--quota N] [--expires ISO]` | New invite code, printed **once** (only its sha256 is stored). Default quota: 3 runs per day |
| `invite list` | Every invite by id (first 12 hex characters of the code's sha256), quota, use today, expiry, active |
| `invite revoke --id <id>` / `invite revoke --code-stdin` | The invite expires now. With `--code-stdin` the code is read from stdin and hashed locally |
| `runs [--listed \| --unlisted] [--limit N]` | Recent runs with their verdict kinds, newest first (curation candidates) |
| `gallery add <run_id> [--reviewed]` | List a finished run in the public gallery |
| `gallery remove <run_id>` | Take a run down from the gallery (always succeeds) |
| `drain on\|off`, `kill-switch on\|off` | Stop accepting new runs (deploy) / pause new runs for the day (budget) |

A refused request exits with status 1 and prints the HTTP status and the server's message.

## Invites

- Live runs need an invite code (`POST /api/session`), which sets a signed, `HttpOnly`,
  `Secure`, `SameSite=Strict` session cookie for 7 days. The public gallery needs nothing.
- Each invite has a quota of runs per UTC day, counted when a run is created
  (`POST /api/runs`). **Default: 3 per day.** Raise it only for a known person
  (`--quota`, at most 100).
- Codes are shown once and never stored: only `sha256(code)` is. Lost codes cannot be
  recovered; create a new one and revoke the old one by its id.
- Give every invite an expiry. For the judges: one code in the Devpost testing instructions,
  `invite create --expires 2026-12-16T00:00:00Z` (the day after judging ends). For
  `smoke-cron`: a separate code with `--quota 1` and the same expiry, stored only as the
  `P2P_SMOKE_INVITE` secret of the GitHub environment `smoke`.
- One invite per person or group, so one leaked code can be revoked alone.
- **Revoking** (`invite revoke`) makes the invite expire now: no new session and no new run,
  for existing sessions too. Runs already started continue; cancel one with the owner's
  session or turn the kill switch on.
- **Abuse** (many runs, odd papers, a code posted publicly): revoke the invite first, then
  check spend (`p2p_*` metrics, Nebius Budgets). The kill switch (`kill-switch on`) pauses
  every new run for the rest of the UTC day; the gallery keeps working.

## Gallery curation

Runs are **private by default**: only the session that created a run can see it, its events and
its bundle; everyone else gets 404. Nothing enters the public gallery automatically
(architecture §16.1). The admin lists runs one by one:

1. `uv run python -m app.admin_cli runs --unlisted` shows finished candidates and their verdicts.
2. Open the run with the owner's session or locally, and check it: the claim, the conditions
   next to every verdict (scale, seeds, hardware, deviations from the paper), the code that
   ran, the Limitations section of the bundle, and the explanation's wording.
3. `gallery add <run_id>` lists it. Only finished runs can be listed (409 otherwise).
   Canary papers, `REPRODUCED`, `REPRODUCED_AT_REDUCED_SCALE` and `INCONCLUSIVE` runs can be
   listed after this check.
4. A run with a `NOT_REPRODUCED` verdict is listed only with `--reviewed`, after a careful
   manual review: is the setup faithful enough to the paper that the conditions make the
   result meaningful? When in doubt, do not list it. A negative verdict on a real paper,
   shared publicly, can hurt real people.

Listing also stores the gallery card's summary (the verdict kinds). `gallery remove` takes a
run down at once: the run, its events and its bundle become 404 to the public.

### Copy rules

Everywhere the project speaks in public (UI, bundle, README, video, Devpost, posts):

- `NOT_REPRODUCED` is always **"Not reproduced under our conditions"**, with the conditions
  next to it. Never "fails to reproduce", "wrong", "false", "debunked" or a similar word about
  a paper or its authors.
- `REPRODUCED_AT_REDUCED_SCALE` is "Reproduced at reduced scale", with the scale factor.
- `INCONCLUSIVE` is "Inconclusive", with the human-readable reason.
- The verdict is the deterministic engine's (`app/verdict/engine.py`); the model only writes
  the explanation. Say so when a verdict is discussed.
- No paper PDF or source is redistributed: bundles hold hashes and metadata only.

## Takedown

For paper authors (README, "For paper authors") and anyone named in a run:

1. **Unlist first, discuss later.** On any request about a listed run, run
   `gallery remove <run_id>` right away, without asking for a justification. It is
   immediate: the run is private again (404 to everyone but the session that created it).
2. Reply on the issue that the run is no longer public.
3. If the author reports an error in our setup (wrong claim, metric, dataset, scale or
   hyperparameters), record it in a GitHub issue with the arXiv ID and the run ID. Fix what is
   ours; a corrected run is listed again only after the curation steps above, and only if the
   author agrees.
4. **Full removal on request:** delete the run's objects in `p2p-bundles` (the
   `P2P_CONTROL_BUCKET`): `bundles/<run_id>/` (the Proof Bundle) and `runs/<run_id>/` (the
   run's artifacts). The bucket is versioned: delete the noncurrent versions too, or they
   expire after 30 days by lifecycle. The per-run bucket `p2p-run-<slug>` is already deleted by
   the reaper once the bundle is stored. The database keeps the private run record (arXiv ID,
   hashes, verdict) and the Litestream replica keeps it for its 7-day retention; no tool
   deletes rows yet, so record the request in the issue and say what was kept.
5. Never re-list a removed run.

## Rotate after judging

Judging ends on 2026-12-15. Afterwards every credential that existed during judging is replaced
or deleted, in one maintenance window. Values are generated and loaded in a shell, never
written to a file, a ticket or a chat. The order matters: **load every new value, redeploy
once, verify, and only then delete the old credentials.**

### 1. Before

`uv run python -m app.admin_cli kill-switch on`, then `drain on`. Keep the current
`ADMIN_TOKEN` at hand: the redeploy in step 3 drains the running instance with it, because that
instance still runs with the old secret version.

### 2. New values for the endpoint (Nebius secret `p2p-secrets`)

The list is `SECRET_VARS` in `deploy/endpoint.sh`. `deploy/secrets.sh` (prepared for H-04) is
the only way values reach `p2p-secrets`. Nothing is redeployed yet, and nothing old is deleted
yet (except the two S3 keys, see below).

- [ ] `NEBIUS_API_KEY` (Token Factory): create a new API key in the Token Factory console and
      load it.
- [ ] `TAVILY_API_KEY`: create a new key in the Tavily dashboard and load it.
- [ ] `SESSION_KEY`: `openssl rand -hex 32`, load it. Every session cookie becomes invalid at
      the redeploy (users log in again).
- [ ] `ADMIN_TOKEN`: `openssl rand -hex 32` (different from `SESSION_KEY`), load it. Keep the old
      one until step 3 is done.
- [ ] `RUNTIME_S3_KEY_ID`, `RUNTIME_S3_SECRET` (S3 access key of sa-api-runtime: Litestream,
      artifacts, bundles): in `infra/terraform/envs/prod-iam`,
      `terraform plan -replace=module.project_iam.nebius_iam_v2_access_key.api_runtime -out=iam.tfplan`,
      review, `terraform apply iam.tfplan`. Read the new key ID from
      `terraform output -json api_runtime_s3_key` and the secret from the SecretStash secret it
      references (MYSTERY_BOX delivery), and load both. The replace deletes the old key at once:
      the running instance can no longer replicate, which is why it is drained.
- [ ] `JOB_LAUNCHER_S3_KEY_ID`, `JOB_LAUNCHER_S3_SECRET` (S3 access key of sa-job-launcher):
      the same with `nebius_iam_v2_access_key.job_launcher` and
      `terraform output -json job_launcher_s3_key`.
- [ ] `JOB_LAUNCHER_AUTH_KEY` (authorized key of sa-job-launcher: jobs, buckets, gateway):
      generate a new key pair with the CLI (`nebius iam auth-public-key generate
      --service-account-id <sa-job-launcher> ...`, check the flags with `--help`) and load the
      credentials JSON. Note the old key's ID (`nebius iam auth-public-key list`).
- [ ] `RUNTIME_AUTH_KEY` (authorized key of sa-api-runtime: KMS bundle signing): the same for
      sa-api-runtime.
- [ ] `OBSERVABILITY_STATIC_KEY` (only with Nebius Tracing): issue a new static key
      (`nebius iam static-key issue --name p2p-observability-<date>
      --account-service-account-id <sa> --service observability`, copy `token`) and load it.

The **KMS signing key** (`p2p-bundle-signing`) is not rotated: published bundles verify against
`docs/bundle-public-key.pem`. Rotate it only if it may be compromised, and then publish the new
public key and say which bundles the old one signed.

### 3. Redeploy and verify

- [ ] Redeploy (`deploy/endpoint.sh` or the deploy workflow) with the **old** `ADMIN_TOKEN` for
      the drain (if the drain fails, `P2P_SKIP_DRAIN=1`: the instance is already drained).
- [ ] `/readyz` is 200 (models routed with the new key, replication with the new S3 key); the
      start-up signing probe passed with the new `RUNTIME_AUTH_KEY`.
- [ ] The old `ADMIN_TOKEN` gets 401 and the new one works (`invite list`).
- [ ] If the demo stays up: a canary run creates its jobs and bucket (new launcher keys), then
      `kill-switch off` with the new token (the kill switch is stored and stays on for the day;
      the new instance starts undrained). Otherwise stop the endpoint.

### 4. Delete the old credentials

- [ ] The old Token Factory API key (console) and the old Tavily key (dashboard).
- [ ] The old authorized public keys of sa-job-launcher and sa-api-runtime
      (`nebius iam auth-public-key delete --id <old>`).
- [ ] The old observability static key, and Grafana's read-only static key: issue a new one
      for Grafana's data source, then revoke the old one.

### 5. Secrets of GitHub Actions

- [ ] `P2P_ADMIN_TOKEN` (environment `prod`): `gh secret set P2P_ADMIN_TOKEN --env prod` with
      the new `ADMIN_TOKEN` (it reads the value from stdin).
- [ ] `P2P_SMOKE_INVITE` (environment `smoke`): `smoke-cron` stops after 2026-12-15. Revoke
      the invite (`invite revoke --code-stdin`) and delete the secret
      (`gh secret delete P2P_SMOKE_INVITE --env smoke`); create a new one only if the demo stays
      up.
- [ ] `TFSTATE_ACCESS_KEY_ID`, `TFSTATE_SECRET_ACCESS_KEY` (S3 key of sa-tfstate, for the
      Terraform state): create a new access key as in `infra/README.md` (bootstrap step 2, with
      `--expires-at`), set both secrets with `gh secret set ... --env prod`, run a plan, then
      delete the old key (`nebius iam v2 access-key delete --id <old>`).
- [ ] `NEBIUS_SA_CREDENTIALS` (only in `NEBIUS_AUTH_MODE=key`, the fallback before OIDC): delete
      the secret (`gh secret delete NEBIUS_SA_CREDENTIALS --env prod`) and the old authorized key
      of sa-ci-deployer. With OIDC there is no static CI key to rotate.

### 6. Everything else

- [ ] Revoke every invite handed out for judging (`invite list`, then `invite revoke --id`).
- [ ] Developers' credentials: delete the Token Factory and Tavily keys used for
      `make test-live`, `make eval` and the spikes; delete the dev authorized keys behind
      `JOB_LAUNCHER_AUTH_KEY_FILE` / `RUNTIME_AUTH_KEY_FILE` (in Nebius, then the files); empty
      the secret values in every local `.env`.
- [ ] Humans' own sessions: `unset` any `NEBIUS_IAM_TOKEN`, `AWS_*` and `ADMIN_TOKEN` in open
      shells, and revoke personal Nebius CLI profiles that are no longer needed.
- [ ] Record what was rotated, when and by whom in a GitHub issue comment (no values).

## Reliability runbook

To be written with A-32 (failure drills, `deploy/dr_drill.sh`).
