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
VPS) instead of the Serverless endpoint. Smoke tests and experiments run in Token Factory
Sandboxes, on CPU and without network (`P2P_EXEC_BACKEND=sandboxes`, the default). Nebius GPU
jobs are the alternative once their gate passes (`P2P_EXEC_BACKEND=gpu`). Do these steps in order.
Each step that changes the cloud or spends money is the human's.

**Gate for Sandboxes.** Spike S14 (`spikes/s14_sandbox_exec.py --image <imported UUID>`) must
PASS, after the import of cloud step 4 and before the commit to deploy is chosen. Its facts go
in `docs/spikes/RESULTS.md`: uploads, mounted files, `setpriv`, the harvest size, the cancel,
the cold start, and the backend's own job command on the canary (exit 0, and exit 20 with
partial results at the watchdog). When its `NPROC` and `MEM_KB` differ from `4vcpu-16gb`, set
`SANDBOX_PRESET` (`app/execution/sandbox_jobs.py`) in the same commit as the image mapping.

**Gate for GPU jobs (`P2P_EXEC_BACKEND=gpu` only).** Spike S5 `--mode isolated` must PASS on the
exec subnet, and its facts go in `docs/spikes/RESULTS.md`. PASS means:
- the image is pulled and the bucket is mounted;
- the output is complete;
- every internet target times out;
- the job gets no metadata token and has no public IP.

Without that PASS, GPU exec jobs could reach the internet: keep `P2P_EXEC_BACKEND=sandboxes`.
Public names resolving is recorded as a fact and does not block: the VPC resolver answers by
design (architecture §8.3).

A first run before the registry exists can only be PARTIAL, because the image does not come
from the Nebius registry. That is a preliminary result, enough to apply the network. The final
gate is a run with a `PROBE_IMAGE` from the sandbox registry, and it must PASS. No other PARTIAL
is acceptable: not a host that answered, and not the metadata service answering.

**What this host is trusted with.** Read this before using a shared or general-purpose machine.
- **It holds every prod secret.** That includes `sa-job-launcher` (editor on p2p-sandbox, enough
  to attach the gateway allocation to a VM or change `rt-exec`) and `sa-api-runtime`.
- **It parses untrusted input.** The API reads paper PDF/LaTeX, and the local provision and
  fetch containers read git packs, wheels and parquet files, sharing the host's kernel.
- **Docker membership is root.** The API's user is in the `docker` group, which is root on the
  host. A remote-code bug in the API, or a container escape, therefore means root on the host
  plus those keys.

Use a dedicated VPS with nothing else on it. Keep the checkout owned by root (read-only to
`p2p`). Record this accepted risk in the issue notes.

**Cloud (once):**
1. Terraform `envs/prod` with `enable_gateway = false` in `prod.auto.tfvars`, then `envs/prod-iam`
   (H-04 steps 4.5 and 4.6). Get each plan reviewed before `apply`. With `enable_gateway =
   false`, `terraform output -json env` prints `GATEWAY_INSTANCE_ID: null`: leave that variable
   out, never write `null`.
2. Load the secrets with `deploy/secrets.sh` (H-04 step 4.7).
3. Push `p2p-runner-cpu` and `p2p-provisioner` to the `p2p-sandbox` registry, and note their
   digests. `p2p-runner` (the GPU image) is needed with `P2P_EXEC_BACKEND=gpu` only. The
   `p2p-api` image is not used on this host.
4. Import `p2p-runner-cpu@sha256:…` into Sandboxes, on a machine with the repository checked out
   and the nebius CLI configured, from the repository root (not the deployed checkout, which
   must stay clean). The registry password is the access token of a service account that may
   only pull from that registry (the viewer of host step 5), never your own account's token:
   Sandboxes receives it.
   ```
   read -rs NEBIUS_API_KEY && export NEBIUS_API_KEY
   export TF_PROJECT_ID=<Token Factory project>
   P2P_REGISTRY_PASSWORD="$(nebius iam get-access-token --profile <registry-viewer>)" \
     uv run python scripts/sandbox_import.py --username iam --image <registry>/p2p-runner-cpu@sha256:…
   ```
   That machine also needs `zstd` for the S14 gate (`apt install zstd`; on Windows, use WSL).
   The script checks the imported image offline, as the job runs (root, then uid 10001:
   runner files, `setpriv`, `setsid`, `timeout`, a writable `/root`, torch), and writes the
   UUID to `config/sandbox_images.yaml`. Then run spike S14 with `--image <that UUID>` (the
   gate above). Commit the mapping (and `SANDBOX_PRESET` if S14 changed it) on a branch,
   merge it to `develop` and `main`, and deploy that commit (host step 2). With
   `P2P_EXEC_BACKEND=sandboxes` the control plane refuses to start without the mapping for its
   `P2P_RUNNER_CPU_IMAGE`; with `gpu`, every smoke test falls back to a GPU smoke job, billed
   as GPU time.

**Host (the VPS), as root:**
1. Install:
   - Docker, rootful: the jobs' uid 10001 must be uid 10001 on the host.
   - The `acl` package.
   - `uv` (for the build only).
   - Node 20 (for the web build).
   - litestream v0.5.17, with its sha256 checked against the one in `images/api/Dockerfile`:
     `echo "cfb371176d164437ae869f8351cfde49bd1804ae71c61923f75c9cba9c9c006d  litestream-0.5.17-linux-x86_64.tar.gz" | sha256sum -c`.
2. Create user `p2p` in the `docker` group, with no login shell. Then:
   - Clone the repository to `/opt/p2p`, owned by root, at the commit to deploy. Run
     `git status --porcelain`; it must print nothing.
   - Create `/var/lib/p2p` (the database and `P2P_VAR_DIR`) and `/var/lib/p2p/jobs`
     (`P2P_LOCAL_JOBS_DIR`), owned by `p2p`, mode 0700. Put the jobs directory on its own
     filesystem, or on a fixed-size loop device (for example 60 GiB, ext4, which supports ACLs).
     Mount it first, then `chown p2p: /var/lib/p2p/jobs && chmod 0700 /var/lib/p2p/jobs`, because
     the mount hides the directory's own permissions. A huge repository or dataset then fills
     that disk, not the database's: a full database disk loses the ledger and the kill switch.
3. Network for the local jobs:
   - Create it with IPv4 only:
     `docker network create --driver bridge --opt com.docker.network.bridge.name=p2p-jobs p2p-local-jobs`.
     Never add `--ipv6`.
   - Drop anything that is not the internet, with a oneshot unit that runs after docker:
     `/etc/systemd/system/p2p-jobs-firewall.service`. A unit, not `iptables-persistent`, because
     that package removes ufw, which step 8 uses.
     ```
     [Unit]
     After=docker.service
     Requires=docker.service
     Before=p2p.service
     [Service]
     Type=oneshot
     RemainAfterExit=yes
     ExecStart=/usr/sbin/iptables -I DOCKER-USER -i p2p-jobs -d 169.254.0.0/16 -j DROP
     ExecStart=/usr/sbin/iptables -I DOCKER-USER -i p2p-jobs -d 100.64.0.0/10 -j DROP
     ExecStart=/usr/sbin/iptables -I DOCKER-USER -i p2p-jobs -d 10.0.0.0/8 -j DROP
     ExecStart=/usr/sbin/iptables -I DOCKER-USER -i p2p-jobs -d 172.16.0.0/12 -j DROP
     ExecStart=/usr/sbin/iptables -I DOCKER-USER -i p2p-jobs -d 192.168.0.0/16 -j DROP
     ExecStart=/usr/sbin/iptables -I INPUT -i p2p-jobs -j DROP
     [Install]
     WantedBy=multi-user.target
     ```
     The `INPUT` rule keeps the containers away from the host's own services, the API on
     127.0.0.1 included. After a reboot, `iptables -S DOCKER-USER` must list the five drops.
   - If the host's upstream DNS server is a private or link-local address (common on a VPS),
     container lookups are now dropped and provision jobs fail (fail-closed: the canary in step 9
     shows it). Set public resolvers in `/etc/docker/daemon.json` (`"dns": ["1.1.1.1", "9.9.9.9"]`).
   - Never publish docker ports: rootful docker bypasses ufw.
4. Build once, as root in `/opt/p2p` (the checkout, `.venv` and `web/dist` stay root-owned and
   read-only to `p2p`):
   ```
   UV_PYTHON_DOWNLOADS=never uv sync --frozen --no-dev
   cd web && npm ci --ignore-scripts && npm run build
   ```
   Without `web/dist` the API runs, but no UI is served. Repeat this step for every new commit.
5. Images:
   - Log in to the sandbox registry with a viewer-only token (`docker login`).
   - Run `docker pull <registry>/p2p-provisioner@sha256:…`.
   - Run `docker logout`, so the token does not stay in `~p2p/.docker/config.json`.
6. Environment file `/etc/p2p/p2p.env`, owned by root:root, mode 0600. systemd reads it as PID 1,
   and it is the only loader: never `source` it. It holds:
   - the prod variables of `deploy/prod.env`;
   - the hybrid values from the spec's table: `P2P_EXECUTION=hybrid`,
     `P2P_LOCAL_JOBS_DIR=/var/lib/p2p/jobs`, `P2P_LOCAL_JOBS_NETWORK=p2p-local-jobs`, no
     `GATEWAY_INSTANCE_ID`, and either `SMOKE_EXECUTOR=job` with `TF_PROJECT_ID` (Sandboxes, the
     default; `P2P_RUNNER_CPU_IMAGE` must be the digest mapped in `config/sandbox_images.yaml`)
     or `P2P_EXEC_BACKEND=gpu`, `SMOKE_EXECUTOR=sandboxes` and `P2P_SUBNET_EXEC`;
   - `P2P_ENV=prod`, `DB_URL=sqlite+aiosqlite:////var/lib/p2p/p2p.db`, `P2P_VAR_DIR=/var/lib/p2p`,
     `P2P_BUILD_COMMIT=<git rev-parse HEAD>`, `P2P_BIND_HOST=127.0.0.1`,
     `P2P_FORWARDED_ALLOW_IPS=127.0.0.1/32`, `PYTHONDONTWRITEBYTECODE=1`;
   - the secrets, copied from `p2p-secrets`.

   Single-quote every value. The JSON authorized keys stay on one line. Write the file with
   `umask 077` and an editor, never with `echo` in the shell history. Keep it out of the
   repository and out of any backup or provider snapshot of this host (invariant 4).
7. A systemd unit, `/etc/systemd/system/p2p.service`:
   ```
   [Unit]
   After=network-online.target docker.service
   Wants=network-online.target
   [Service]
   User=p2p
   WorkingDirectory=/opt/p2p
   EnvironmentFile=/etc/p2p/p2p.env
   Environment=P2P_LITESTREAM_CONFIG=/opt/p2p/images/api/litestream.yml
   Environment=PATH=/opt/p2p/.venv/bin:/usr/local/bin:/usr/bin:/bin
   ExecStart=/bin/sh images/api/start.sh
   Restart=on-failure
   TimeoutStopSec=30
   ProtectSystem=strict
   ReadWritePaths=/var/lib/p2p
   ProtectHome=yes
   PrivateTmp=yes
   [Install]
   WantedBy=multi-user.target
   ```
   uv is not needed at runtime: `PATH` puts the built virtualenv first.

   **First start only** (an empty database is allowed just this once):
   1. Run `systemctl edit p2p` and add `[Service]` and `Environment=P2P_DB_BOOTSTRAP=1`.
   2. Run `systemctl start p2p`, and wait for `/readyz`.
   3. Remove the drop-in (`systemctl revert p2p`), then run `systemctl restart p2p`.

   Never put `P2P_DB_BOOTSTRAP` in the env file. Only one writer may ever replicate to the
   `p2p-state` path: stop any other control plane, endpoint or dev included, before the first
   start.
8. TLS in front, for example Caddy with automatic HTTPS: `reverse_proxy 127.0.0.1:8080`, using
   the address `127.0.0.1`, not `localhost`. In the firewall, open 80 and 443, plus SSH from
   known addresses only.
9. Checks:
   - `https://<host>/readyz` reports the deployed commit.
   - `curl http://<public IP>:8080/` from another machine fails.
   - `docker network inspect p2p-local-jobs -f '{{index .Options "com.docker.network.bridge.name"}} {{.EnableIPv6}}'`
     prints `p2p-jobs false`. A network made without that option gets a `br-<id>` interface, and
     then no firewall rule matches, silently.
   - A throwaway container on that network reaches the internet but not the host or the
     metadata service: `docker run --rm --network p2p-local-jobs --entrypoint python
     <registry>/p2p-provisioner@sha256:… -c "import socket; [print(h, socket.create_connection((h, p), 3) and 'OPEN') for h, p in (('1.1.1.1', 443),)]"`
     prints `OPEN`. The same with `('169.254.169.254', 80)` and with the host's own address must
     time out.
   - One canary paper runs end to end before anything else. Its provision job must appear in
     `docker ps -a` on the host, and its smoke and exec jobs in
     `/var/lib/p2p/jobs/sandbox-jobs/` (Sandboxes) or in `nebius ai job list` (GPU).
