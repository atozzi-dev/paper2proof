# Operations

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
   another one. `DRY_RUN`, `P2P_SKIP_DRAIN` and `P2P_DRAIN_WAIT_S` from your shell are never
   passed on to `deploy/endpoint.sh`. The drill:
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
      exercised"), as does a run that ends, or starts another step or attempt, without it;
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
