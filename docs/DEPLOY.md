# Deploying the control plane

The control plane is the `p2p-api` image on a Nebius Serverless endpoint in `p2p-control`
(architecture §11.1, ADR-002). The container disk is ephemeral: the SQLite database lives in
the Litestream replica on the `p2p-state` bucket.

## How the container starts

`images/api/start.sh`, under `tini -g`:

1. `litestream restore` of `/var/lib/p2p/p2p.db`. A missing replica is an error, because an empty
   database would reset the ledger, the daily caps and the kill switch. Only the very first
   deploy allows an empty database (`deploy/endpoint.sh --bootstrap`, which sets
   `P2P_DB_BOOTSTRAP=1` on that one create).
2. `alembic upgrade head`. Migrations only go forward, so keep them backward compatible for one
   release (expand, then contract) so that a rollback to the previous image still works.
3. `exec litestream replicate -exec "uvicorn app.main:app ..."`. Litestream replicates every
   write. When uvicorn exits, litestream does a final sync and exits. uvicorn shuts down
   gracefully for at most 10 s.

Required environment:
- **Secrets**, from the Nebius secret named by `P2P_SECRET_NAME`: `NEBIUS_API_KEY`,
  `TAVILY_API_KEY`, `SESSION_KEY`, `ADMIN_TOKEN`, `RUNTIME_S3_KEY_ID`, `RUNTIME_S3_SECRET`,
  `RUNTIME_AUTH_KEY` (the authorized key JSON of sa-api-runtime: KMS signing),
  `JOB_LAUNCHER_AUTH_KEY` (of sa-job-launcher: jobs, buckets, gateway), `JOB_LAUNCHER_S3_KEY_ID`,
  `JOB_LAUNCHER_S3_SECRET`; `OBSERVABILITY_STATIC_KEY` only with Nebius Tracing (see
  "Observability"). The app writes the two authorized keys owner-only to
  `/var/lib/p2p/keys` on the ephemeral container disk, because the Nebius SDK reads a file.
- **Settings**, from `deploy/prod.env`, whose template is `deploy/prod.env.example`. Only its
  keys (including the commented-out optional ones) are accepted. `deploy/endpoint.sh` refuses a
  file without the required ones (the uncommented lines except `S3_REGION` and
  `GATEWAY_INSTANCE_ID`), or with a bad `P2P_GPU_PREEMPTIBLE` or `P2P_FORWARDED_ALLOW_IPS`,
  before it changes anything.

`/readyz` returns 200 only when the database answers, the models are routed, and the process
really runs under litestream. It also reports the `commit` baked into the image at build time
(`--build-arg BUILD_COMMIT`, also the `org.opencontainers.image.revision` label).

`P2P_ENV=prod` refuses to start when a setting or secret above is missing, with
`SMOKE_EXECUTOR=local`, or when model routing fails. With everything in place it runs the Nebius
job backend (sa-job-launcher), the S3 storage (sa-job-launcher for run buckets, sa-api-runtime
for the control bucket), KMS bundle signing and one reaper task. At startup it signs a probe and checks it
under the published key `docs/bundle-public-key.pem`, which must be exported (A-26 PAIR step:
`uv run python -m app.bundle.sign public-key --key-id <id> > docs/bundle-public-key.pem`) and
committed before the image is built; the image ships it.

## Deploying

```bash
deploy/endpoint.sh <registry>/p2p-api@sha256:<digest> deploy/prod.env
```

with `P2P_CONTROL_PROJECT_ID`, `SUBNET_CONTROL_ID`, `P2P_EXPECT_COMMIT` (the commit the image was
built from) and, to replace a running endpoint, `ADMIN_TOKEN` set. The very first deploy adds
`--bootstrap` before the image; it is refused once the endpoint exists.

The script:
1. checks every input before it changes anything;
2. looks the endpoint up by name, and stops on any listing error;
3. prints the current image, which is the rollback target;
4. drains the running instance;
5. by default stops the old endpoint (Litestream's final sync), waits for STOPPED, deletes it,
   waits until the name is free, and creates the new one, so there is a single database
   writer. Use `P2P_ENDPOINT_UPDATE=update` once S-6 shows that an in-place update stops the
   old instance first;
6. waits until `/readyz` reports the new commit. A replace that fails before the old instance
   is gone un-drains it.

**Rollback:** run the same command with the previous digest. If the instance is down, add
`P2P_SKIP_DRAIN=1`.

## Observability

The app writes JSON logs to stdout (one object per line with `run_id` and `trace_id`; uvicorn's
logs too), scrubbed of every configured secret value and key-shaped string; fields named like
prompts, messages or paper text are blanked at any depth. Nebius Logs collects the endpoint's
stdout.

- **Traces** go over OTLP/gRPC to Nebius Tracing: in `deploy/prod.env`,
  `P2P_TRACES_ENDPOINT=https://write.tracing.eu-north1.nebius.cloud:443` and
  `OBSERVABILITY_PROJECT_ID` = the p2p-control project (sent as `iam-container`); in the
  Nebius secret, `OBSERVABILITY_STATIC_KEY`. `deploy/endpoint.sh` maps that secret only when
  `P2P_TRACES_ENDPOINT` is a Nebius Tracing host, so add the key to the secret before the first
  deploy that sets it (a rollback to an older release does not ask for it). The key is a
  static key of a dedicated service account (e.g. `sa-observability-writer`, never
  sa-api-runtime or sa-job-launcher) in a group with at least `editor`, as the Nebius docs
  require: `nebius iam static-key issue --name p2p-observability
  --account-service-account-id <sa> --service observability`, then copy `token`. Check whether
  a narrower role than `editor` is enough. The app sends the key only to
  `write.tracing.<region>.nebius.cloud` over TLS. One trace per run (its ID is derived from
  the run ID), a span per step and per model call. Without the endpoint, tracing is off.
- **Metrics** (`p2p_*`, architecture §14) go over OTLP/gRPC to `P2P_METRICS_ENDPOINT`, never
  with the static key. Nebius Monitoring has no direct OTLP write for application metrics,
  only Prometheus remote-write
  (`https://write.monitoring.<region>.nebius.cloud/projects/<project>/prometheus/api/v1/write`),
  so the endpoint must be an OpenTelemetry Collector with a `prometheusremotewrite` exporter.
  **No collector is deployed yet**: leave `P2P_METRICS_ENDPOINT` unset (metrics stay
  in-process) until one exists in Terraform, reachable only from the control subnet or with
  its own authentication: a public, unauthenticated OTLP receiver would let anyone forge
  `p2p_*` series and trigger or mask alerts. Cost: an always-on cpu-e2 2vcpu-8gb VM is about
  $45/month (config/pricing.yaml).
- **Dashboard and alerts**: import `infra/observability/dashboard.json` into Grafana with the
  Nebius Monitoring data source (`https://read.monitoring.api.nebius.cloud/projects/<project>/
  prometheus`, header `Authorization: Bearer <token>` with a separate read-only static key,
  never the writer's), and the rules of `infra/observability/alerts.yaml` into Grafana
  Alerting (Nebius console alerts cover VMs and buckets only). Both need the metrics above.
  Budget alerts at 50/80/100% are set in Nebius Budgets.

## H-05 checklist (first deploy, also the S-6 facts for RESULTS.md)

1. Does `endpoint update --image <digest>` work in place? Do the old and new revisions overlap?
   If they do, keep `recreate`.
2. After an update, are disk (30Gi), preset, subnet and env unchanged?
3. What is the exact `--env` / `--env-secret` syntax, and how does one secret with several keys
   map to variables? Is ENTRYPOINT/CMD honoured without `--container-command`?
4. Where is the container disk mounted? Is `/var/lib/p2p` writable by uid 10001, and is the
   disk ephemeral across stop/start?
5. Restore round trip: create with `--bootstrap`, write data, redeploy the same digest without
   it (recreate), and check that the data comes back. Also check that restore → migrate →
   replicate → kill → restore keeps both the migration and the later writes.
   5b. Without `P2P_DB_BOOTSTRAP`, `litestream restore` against an empty replica path exits
   non-zero in 0.5.17 (the guard works).
6. SIGTERM: what is the platform's grace period? Do the Litestream logs show the forwarded
   signal and a final sync? Does uvicorn exit in time while an SSE stream is open?
   6b. On recreate, the old instance logs its final Litestream sync before the new instance's
   restore starts (compare the timestamps in both logs).
7. Restart policy when uvicorn or Litestream exits non-zero. Does a slow restore or model
   routing trip a startup probe?
8. The proxy's peer address and forwarding headers, and whether it strips a client-supplied
   `X-Forwarded-For`. Then set `P2P_FORWARDED_ALLOW_IPS` to the proxy CIDR (never `*`), so the
   per-IP limits work. **Gate before the URL goes on Devpost:** without it every client shares the
   proxy's address, so a few judges exhaust the login, API and SSE limits together.
   8b. Log in from a real browser and start a run. The origin check expects `https://<Host>`
   (or `X-Forwarded-Host`) in prod; `/readyz` and the admin CLI send no Origin header, so only a
   browser proves it.
9. Does the URL change after a recreate or a stop/start?
10. Are endpoint names unique, and does `list` paginate? (The script refuses a listing with a
    next page.)
11. The SDK authorized keys from the env secrets: the app writes them to `/var/lib/p2p/keys`,
    and the sa-job-launcher identity can list jobs from inside the endpoint.
12. The real field names in `endpoint get` for the image (`spec.image`?), the URL
    (`status.public_endpoints`) and the state (`status.state`, value `STOPPED`?), so the
    rollback target, the URL and the stop wait can be relied on.
13. With `tini -g`, uvicorn gets SIGTERM from both tini and Litestream: it still shuts down
    gracefully, and Litestream does not SIGKILL its child when its context is cancelled.

## Hybrid deploy (no non-GPU quota)

The design is in `docs/specs/hybrid-deploy.md`. The control plane runs on a host of ours (the
VPS) instead of the Serverless endpoint. Experiments still run in Nebius GPU jobs, and smoke
tests run in Sandboxes. Do these steps in order. Each step that changes the cloud or spends
money is the human's.

**Before anything else:** spike S5 `--mode isolated` must PASS on the exec subnet: the image is
pulled, the bucket is mounted and there is no egress. Its facts go in `docs/spikes/RESULTS.md`.
Without that PASS, exec jobs would have the internet, and the hybrid deploy must not start.

**Cloud (once):**
1. Terraform `envs/prod` with `enable_gateway = false` in `prod.auto.tfvars`, then `envs/prod-iam`
   (H-04 steps 4.5 and 4.6). Keep the plan output and get it reviewed before `apply`.
2. Load the secrets with `deploy/secrets.sh` (H-04 step 4.7).
3. Push `p2p-runner`, `p2p-runner-cpu` and `p2p-provisioner` to the `p2p-sandbox` registry, and
   note their digests. The `p2p-api` image is not used.
4. Import `p2p-runner-cpu@sha256:…` into Sandboxes and record its UUID in
   `config/sandbox_images.yaml`. Without it, every smoke test falls back to a GPU smoke job.

**Host (the VPS):**
1. Docker (rootful: the jobs' uid 10001 must be uid 10001 on the host), the `acl` package,
   `uv`, and `litestream` at the version pinned in `images/api/Dockerfile`.
2. A user `p2p` in the `docker` group. The repository is checked out at the deployed commit. The
   directories `/var/lib/p2p` (the database) and `/var/lib/p2p/jobs` (`P2P_LOCAL_JOBS_DIR`) are
   owned by `p2p`, mode 0700.
3. `docker login` to the sandbox registry with a read-only token, then
   `docker pull <registry>/p2p-provisioner@sha256:…`.
4. `/etc/p2p/p2p.env` (owner `p2p`, mode 0600) holds the prod variables of `deploy/prod.env`,
   plus the values from the table in the spec:
   - `P2P_EXECUTION=hybrid`, `P2P_LOCAL_JOBS_DIR=/var/lib/p2p/jobs`,
     `SMOKE_EXECUTOR=sandboxes`;
   - `P2P_SUBNET_EXEC`, and no `GATEWAY_INSTANCE_ID`;
   - `P2P_FORWARDED_ALLOW_IPS=127.0.0.1/32`;
   - the secrets, copied from `p2p-secrets`.

   The file is never in the repository and never in a backup outside the host (invariant 4).
5. Start as `p2p`, from the checkout:
   `set -a; . /etc/p2p/p2p.env; set +a; P2P_LITESTREAM_CONFIG=images/api/litestream.yml uv run sh images/api/start.sh`.
   Add `P2P_DB_BOOTSTRAP=1` on the very first start only. A systemd unit with
   `EnvironmentFile=/etc/p2p/p2p.env` and `User=p2p` keeps it running.
6. Put a TLS proxy in front, for example Caddy with automatic HTTPS:
   `reverse_proxy 127.0.0.1:8080`. Open only ports 80 and 443 in the firewall. Port 8080 stays
   on localhost.
7. Check `https://<host>/readyz`, then run one canary paper end to end before anything else.
   Its exec job must run on Nebius, and its provision job must appear in `docker ps -a` on the
   host.
