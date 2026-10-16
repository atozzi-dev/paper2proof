# Deploying the control plane

The control plane is the `p2p-api` image on a Nebius Serverless endpoint in `p2p-control`
(architecture §11.1, ADR-002). The container disk is ephemeral: the SQLite database lives in
the Litestream replica on the `p2p-state` bucket.

## How the container starts

`images/api/start.sh`, under `tini -g`:

1. `litestream restore` of `/var/lib/p2p/p2p.db`. A missing replica is an error, because an empty
   database would reset the ledger, the daily caps and the kill switch. Only the very first
   deploy sets `P2P_DB_BOOTSTRAP=1`.
2. `alembic upgrade head`. Migrations only go forward, so keep them backward compatible for one
   release (expand, then contract) so that a rollback to the previous image still works.
3. `exec litestream replicate -exec "uvicorn app.main:app ..."`. Litestream replicates every
   write. When uvicorn exits, litestream does a final sync and exits. uvicorn shuts down
   gracefully for at most 10 s.

Required environment:
- **Secrets**, from the Nebius secret named by `P2P_SECRET_NAME`: `NEBIUS_API_KEY`,
  `TAVILY_API_KEY`, `SESSION_KEY`, `ADMIN_TOKEN`, `RUNTIME_S3_KEY_ID`, `RUNTIME_S3_SECRET`.
- **Settings**, from `deploy/prod.env`, whose template is `deploy/prod.env.example`. Only those
  keys are accepted.

`/readyz` returns 200 only when the database answers, the models are routed, and the process
really runs under litestream. It also reports the deployed `commit`.

`P2P_ENV=prod` refuses to start until the Nebius job backend and the S3 storage (A-20) are wired
in, and without `P2P_BUNDLE_KEY_ID`.

## Deploying

```bash
deploy/endpoint.sh <registry>/p2p-api@sha256:<digest> deploy/prod.env
```

with `P2P_CONTROL_PROJECT_ID`, `SUBNET_CONTROL_ID` and, to replace a running endpoint,
`ADMIN_TOKEN` set.

The script:
1. checks every input before it changes anything;
2. looks the endpoint up by name, and stops on any listing error;
3. prints the current image, which is the rollback target;
4. drains the running instance;
5. by default deletes and recreates the endpoint, so there is a single database writer. Use
   `P2P_ENDPOINT_UPDATE=update` once S-6 shows that an in-place update stops the old instance
   first;
6. waits until `/readyz` reports the new commit.

**Rollback:** run the same command with the previous digest. If the instance is down, add
`P2P_SKIP_DRAIN=1`.

## H-05 checklist (first deploy, also the S-6 facts for RESULTS.md)

1. Does `endpoint update --image <digest>` work in place? Do the old and new revisions overlap?
   If they do, keep `recreate`.
2. After an update, are disk (30Gi), preset, subnet and env unchanged?
3. What is the exact `--env` / `--env-secret` syntax, and how does one secret with several keys
   map to variables? Is ENTRYPOINT/CMD honoured without `--container-command`?
4. Where is the container disk mounted? Is `/var/lib/p2p` writable by uid 10001, and is the
   disk ephemeral across stop/start?
5. Restore round trip: deploy with `P2P_DB_BOOTSTRAP=1`, write data, stop/start without the
   flag, and check that the data comes back. Also check that restore → migrate → replicate →
   kill → restore keeps both the migration and the later writes.
6. SIGTERM: what is the platform's grace period? Do the Litestream logs show the forwarded
   signal and a final sync? Does uvicorn exit in time while an SSE stream is open?
7. Restart policy when uvicorn or Litestream exits non-zero. Does a slow restore or model
   routing trip a startup probe?
8. The proxy's peer address and forwarding headers, and whether it strips a client-supplied
   `X-Forwarded-For`. Then set `P2P_FORWARDED_ALLOW_IPS` to the proxy CIDR (never `*`), so the
   per-IP limits work.
9. Does the URL change after a recreate or a stop/start?
10. Are endpoint names unique, and does `list` paginate? (The script refuses a listing with a
    next page.)
11. Precondition: prod startup needs A-20 (the Nebius adapters).
