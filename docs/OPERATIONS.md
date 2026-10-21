# Operations

How the running demo is operated: invites, the public gallery, takedown requests, and the secret
rotation once judging ends. Everything here is a human action on production. Deploying is in
`docs/DEPLOY.md`, the infrastructure in `infra/README.md`.

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

The list is `SECRET_VARS` in `deploy/endpoint.sh`. `deploy/secrets.sh` is the only way values
reach `p2p-secrets`. Without options it loads every key (H-04, refused if the secret already has
a version; `--replace-all` overrides that). `deploy/secrets.sh --only KEY[,KEY...]` writes a new
primary version that replaces only those keys and keeps the others. It reads the S3 secrets from
their SecretStash references and generates new authorized keys itself (expiring
`P2P_KEY_EXPIRES_AT`, by default 2027-01-31: set a later date with at least 30 days of margin if
the demo stays up). Run it from a terminal (a generated `ADMIN_TOKEN` is shown on the terminal
only), never through `tee`. Nothing is redeployed yet, and nothing old is deleted yet (except
the two S3 keys, see below).

- [ ] `NEBIUS_API_KEY` (Token Factory) and `TAVILY_API_KEY`: create a new key in the Token
      Factory console and in the Tavily dashboard, then
      `deploy/secrets.sh --only NEBIUS_API_KEY,TAVILY_API_KEY` (it asks for both, input hidden).
- [ ] `SESSION_KEY` and `ADMIN_TOKEN`: `deploy/secrets.sh --only SESSION_KEY,ADMIN_TOKEN`. The
      session key is generated; paste a new admin token from the password manager (or press
      Enter to have one generated and shown once). Every session cookie becomes invalid at the
      redeploy (users log in again). Keep the old admin token until step 3 is done.
- [ ] `RUNTIME_S3_KEY_ID`, `RUNTIME_S3_SECRET` (S3 access key of sa-api-runtime: Litestream,
      artifacts, bundles): in `infra/terraform/envs/prod-iam`,
      `terraform plan -replace=module.project_iam.nebius_iam_v2_access_key.api_runtime -out=iam.tfplan`,
      review, `terraform apply iam.tfplan`, then `deploy/secrets.sh --only RUNTIME_S3_SECRET`
      (it reads the new key ID from `terraform output -json s3_access_keys` and the secret from
      the SecretStash secret it references). The replace deletes the old key at once: the
      running instance can no longer replicate, which is why it is drained.
- [ ] `JOB_LAUNCHER_S3_KEY_ID`, `JOB_LAUNCHER_S3_SECRET` (S3 access key of sa-job-launcher):
      the same with `nebius_iam_v2_access_key.job_launcher` and
      `deploy/secrets.sh --only JOB_LAUNCHER_S3_SECRET`.
- [ ] `RUNTIME_AUTH_KEY` and `JOB_LAUNCHER_AUTH_KEY` (authorized keys of sa-api-runtime for KMS
      signing, and of sa-job-launcher for jobs, buckets and the gateway):
      `deploy/secrets.sh --only RUNTIME_AUTH_KEY,JOB_LAUNCHER_AUTH_KEY` generates both and
      prints their key IDs (`kid`). Note the old keys' IDs first
      (`nebius iam auth-public-key list --parent-id <project>`).
- [ ] `OBSERVABILITY_STATIC_KEY` (only with Nebius Tracing): issue a new static key
      (`nebius iam static-key issue --name p2p-observability-<date>
      --account-service-account-id <sa> --service observability`, copy `token`), then
      `deploy/secrets.sh --only OBSERVABILITY_STATIC_KEY`.

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
- [ ] The earlier versions of `p2p-secrets`: they still hold the old values (check the
      `nebius mysterybox secret-version` verbs with `--help`), and the SecretStash secrets of the
      replaced S3 keys if they outlived them.
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

How Paper2Proof behaves when a dependency fails (architecture §15), what the user sees, and what
the operator does. Each row has a drill in `tests/reliability/test_failure_modes.py`, run by
`make test` on the fakes; nothing there calls a real service.

| Failure | What the code does | What the user sees | What the operator does |
|---|---|---|---|
| **429 storm on Token Factory** | Up to 4 attempts per call (`config/models.yaml` `provider.max_retries`), waiting what `retry-after` / `x-ratelimit-reset-*` ask (at most 120 s), else exponential backoff with jitter; then one try on the role's fallback (`fallbacks:` in `config/models.yaml`: planner and judge → reader, coder → planner). Every attempt is charged its input estimate. | Nothing, or a slower step. If the fallback is also exhausted: the Judge's prose is replaced by the template explanation (the verdict never depends on it); any other role ends the run `FAILED/MODEL_UNAVAILABLE`. | Watch `p2p_ratelimit_remaining_tokens` (alert under 10% for 5 min). If the storm lasts, pause new runs with the kill switch (below) and retry the failed runs later. |
| **Sandboxes down** | After 3 consecutive Sandboxes errors (`FALLBACK_AFTER`), the smoke executor of the run switches to CPU smoke jobs (`p2p-smoke-*` on `cpu-d3`) for the rest of the run. Without a key or an imported image, runs use jobs from the start; prod runs jobs only until spike S-3 makes `sandboxes` the `smoke_executor` default. | The Coder gets "the smoke executor is unavailable; try again" up to 3 times; those runs count against `fix_iterations` (8). Smoke runs are slower (a job per run). | Nothing urgent. To skip Sandboxes for every run, release with the `smoke_executor` default back at `job` (it is not a deploy setting). Log the outage with `/log-feedback`. |
| **No GPU capacity** (quota used up, "not enough resources": `RESOURCE_EXHAUSTED` at job creation) | EXECUTING retries the same job name on `gpu-l40s-a` every 2 minutes, 3 times, then once on `gpu-l40s-d` / `1gpu-16vcpu-96gb` (kept in `exec_platform_<attempt>.json` so a restart re-attaches there). A refused creation costs nothing. With no capacity anywhere the attempt ends without a job and VERDICT reports `INCONCLUSIVE/CAPACITY`. CPU jobs (provision, fetch, smoke) that hit it fail the run with `CAPACITY`. | `NO_GPU_CAPACITY` warnings in the timeline, then "Inconclusive (capacity)" with the Limitations line "no GPU capacity was available". | Check the GPU quota of `p2p-sandbox` in the console; request more or wait. Re-run the paper later. (No capacity for the on-demand retry after a preemption keeps the first attempt's outcome.) |
| **Tavily down** (or out of credits) | DISCOVERING stops searching at the first Tavily error: no retries, nothing charged. Repository links written by the authors (arXiv comment/abstract, HF summary) and HF datasets still count. | A `DISCOVERY_DEGRADED` warning; the run goes on, possibly `FROM_SCRATCH` without a repository. | Check the Tavily dashboard (credits, key). Nothing to restart. |
| **Hugging Face down** | Same degradation: no HF links or datasets for that run. An outage (unreachable, 429, 5xx, malformed body) is never cached; a 404 is cached for 24 h. | `DISCOVERY_DEGRADED` ("Hugging Face ..."). | Nothing; the next run asks HF again. |
| **Daily cloud cap reached** | The ledger trips the kill switch for the UTC day when committed cloud spend reaches `cloud_usd_per_day`. New runs get `402`; no new job or GPU minute is reserved (a run in flight ends `FAILED/KILL_SWITCH` when it next needs a job); model calls of runs in flight still pass. | "daily budget reached: new runs are paused" (402); the invite quota is not used. The gallery, stats and public runs keep working. | Find the cause in the ledger first. `POST /admin/kill-switch {"on": false}` lifts it for today (or wait for 00:00 UTC). |
| **Control plane restart** (deploy, crash, platform restart) | `litestream restore` at start, then the FSM resumes every non-terminal run from its single RUNNING step; jobs are found again by their deterministic names (get-by-name), so no job is created twice, and the watchdog keeps the first submission's deadline. | A gap in the live log; the timeline continues with a new `step.started` of the same step and attempt. | Run `deploy/dr_drill.sh` after any change to the deploy path (below). |
| **Job stuck** | Our watchdog cancels it at its deadline (`FAILED/TIMEOUT`); the reaper cancels any `p2p-*` job past `watchdog_s + 600` s and alerts. | `FAILED/TIMEOUT`. | Look at the job in the console and at the run's `live.log`. |

### Disaster-recovery drill (`deploy/dr_drill.sh`, H-08)

The drill deletes and recreates the production endpoint with the image it runs now and checks
that the database and an in-flight run survive. It costs one short run (cancelled at the end)
and about 5 minutes of downtime: run it when nobody is using the service, never from an agent.

1. Rehearse without changing anything (read-only lookups; every change is printed). Read the
   secrets without echoing them, so they stay out of the shell history:

   ```bash
   export P2P_CONTROL_PROJECT_ID=project-... SUBNET_CONTROL_ID=vpcsubnet-...
   read -rs ADMIN_TOKEN && export ADMIN_TOKEN
   read -rs P2P_DRILL_INVITE && export P2P_DRILL_INVITE   # an invite code with quota left today
   deploy/dr_drill.sh --dry-run <arxiv-id> deploy/prod.env
   ```

   The dry run prints `n/a (dry run)` for the RTO and the recovery time.
2. Run it for real: `deploy/dr_drill.sh <arxiv-id> deploy/prod.env`. Pick a paper whose run
   lasts well beyond the replacement (about 5 minutes) but is cheap; the drill cancels it once
   it has resumed (`P2P_DRILL_KEEP_RUN=1` keeps it). Optionally set `P2P_EXPECT_COMMIT` to the
   commit you expect in production: the drill stops before any change if `/readyz` reports
   another one. `P2P_DRILL_INVITE`, `DRY_RUN`, `P2P_SKIP_DRAIN` and `P2P_DRAIN_WAIT_S` from your
   shell are never passed on to `deploy/endpoint.sh`. The drill:
   1. reads the endpoint, its image digest and URL, and the commit `/readyz` reports;
   2. logs in with `P2P_DRILL_INVITE` (on stdin) and starts a run, then waits until the run is
      RUNNING with a `step.started`, and notes its last event (id and type) and the step and
      attempt of its last `step.started`;
   3. replaces the endpoint through `deploy/endpoint.sh` in recreate mode (drain, stop with
      Litestream's final sync, delete, create, wait for `/readyz` with the same commit): the
      new container restores the database from the replica in `images/api/start.sh`;
   4. checks the restore: the run is visible to the drill's session and the noted event is
      back with the same id and type;
   5. checks the resume: a `step.started` written after the old instance was told to stop, for
      the step and attempt that were RUNNING when it stopped (the `step.started` right before
      it in event order). A run that finished before the stop fails the drill ("resume not
      exercised"), as does a run that ends, or starts another step or attempt, without it.
      The reference is the stop, not `/readyz`: the app resumes runs in its lifespan, before
      `/readyz` answers, so the resume can come before the drill sees the new instance ready.
      The stop time comes from your clock and the event times from the server's, so keep your
      clock NTP-synced. Only an event after the noted one counts, so a skew can fail the drill
      but never pass it falsely;
   6. cancels the run and prints a summary with the **RTO** (from the start of the replacement
      to `/readyz`) and the **recovery** time (until the drill saw the step started again).
3. Paste the summary into the H-08 issue as `Notes (YYYY-MM-DD): ...`.

**The drill's run.** Runs are private and only their owner can cancel them (there is no admin
cancel), and the owner is the drill's session: logging in again gives a new session that does
not own the run. The session cookie is host-only, so the drill keeps its value in a header
file in a private temporary directory and sends it on every request, also to the recreated
endpoint's new hostname. If the drill fails or is interrupted after it created the run, it
cancels the run before it deletes the session. If that cancel fails, or with
`P2P_DRILL_KEEP_RUN=1`, it keeps the header file, prints its path and the exact command:

```bash
curl -fsS --proto =https -X POST -H @/tmp/tmp.XXXX/cookie.hdr https://<endpoint>/api/runs/<run>/cancel && rm -rf /tmp/tmp.XXXX
```

Run it once the endpoint answers again (with its current URL if it moved); until then the run
keeps spending budget. A failed final cancel makes the drill exit non-zero after the summary.
The header file is a live session: delete its directory when done.

The run creation itself can be ambiguous. Only the app's refusals mean that no run was
created: 400, 401, 403, 402 (kill switch), 413, 429 (invite quota) or the drain's 503 (body
`{"error": "deploy in progress"}`), and the drill says which. A timeout, any other status (a
proxy's 408 or 5xx included), an answer without a run id, or an interrupt while the request
is in flight may hide a created run: the drill keeps the header file the same way and prints
the cancel command with `RUN_ID_HERE` in place of the id. Find the id with
`uv run python -m app.admin_cli --url <endpoint> runs --limit 5` (newest first, the drill's
arXiv ID; the drill prints the command with the URL) or in the server logs. The UI cannot show
it: the run is private to the drill's session.

If the drill fails, the summary is not printed and the message says which check failed. A
failure during the replacement leaves `deploy/endpoint.sh`'s own recovery in place (un-drain
before the old instance is gone; otherwise roll back with the same digest and
`P2P_SKIP_DRAIN=1`, and set `P2P_EXPECT_COMMIT` to the commit that digest was built from, which
`deploy/endpoint.sh` requires). If the restore check fails, stop: do not create runs on an
empty database (it resets the ledger, the daily caps and the kill switch); restore by hand from
the `p2p-state` bucket (`litestream restore -o /tmp/p2p.db` with the replica config) and
compare.

Also part of H-08: turn on the gateway's on-demand mode, and confirm that the daily smoke cron
ran (`.github/workflows/smoke-cron.yml`, last run green).
