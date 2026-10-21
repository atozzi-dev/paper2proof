# Infrastructure

Terraform for the Nebius projects of Paper2Proof (architecture §8, §9, §10.3, §12), region
`eu-north1`, provider `nebius/nebius` pinned to `0.6.64` (checksums in `.terraform.lock.hcl`),
Terraform `>= 1.11` (write-only arguments and ephemeral variables keep secrets out of the state).

```
infra/cloud-init/              egress gateway: cloud-init, nftables ruleset, squid.conf renderer, scripts
infra/terraform/
├── .checkov.yaml              checkov config (run from the repo root)
├── checkov/                   custom checkov policies CKV_P2P_* (project invariants)
├── envs/
│   ├── prod/                  everything an `editor` can apply: CI applies this root
│   └── prod-iam/              service accounts, groups, permits, OIDC, access keys, signing key: human only
└── modules/
    ├── project_iam/           service accounts, groups, access permits, GitHub OIDC, S3 access keys
    ├── network_control/       net-control + subnet-control
    ├── network_sandbox/       net-sandbox: provision, exec, gateway subnets; rt-provision, rt-exec; gateway allocation
    ├── egress_gateway/        gateway VM (nftables + Squid SNI allowlist per subnet), sg-egress-gw
    ├── storage/               p2p-state, p2p-bundles, p2p-cache (+ lifecycle, versioning)
    ├── registry/              one Container Registry per project
    ├── secrets/               SecretStash container p2p-secrets (no versions, ever)
    ├── kms/                   asymmetric signing key p2p-bundle-signing
    └── postgres/              Managed PostgreSQL, off (enable_managed_pg = false)
```

Two roots because Nebius needs `admin` to manage groups, access permits, access keys and bucket
policies, while `sa-ci-deployer` is only `editor`. `envs/prod` contains nothing that needs `admin`,
so CI can plan and apply it; `envs/prod-iam` is applied by a human, after `envs/prod` (it looks up
the buckets and the secret by name), and also owns the signing key in `p2p-signing` (CI only views
that project). Both keep their state in `p2p-tfstate` under different keys.

Not managed here: the projects themselves (H-01, plus `p2p-signing` at H-04), the state bucket
`p2p-tfstate` (bootstrap), the Serverless endpoint (`deploy/endpoint.sh`), Serverless jobs and
per-run buckets `p2p-run-<slug>` (orchestrator, at runtime), the gateway VM's running state (the
orchestrator starts and stops it on demand; Terraform ignores `stopped`), secret values
(`deploy/secrets.sh`) and the job launcher's authorized key (CLI, never in the state).

## Checks

```bash
make tf-check
```

1. `terraform fmt -check -recursive`
2. for `envs/prod` and `envs/prod-iam`: `terraform init -backend=false -lockfile=readonly` +
   `terraform validate` (no credentials, no backend)
3. `tflint --recursive` with the bundled Terraform ruleset, preset `all` (`.tflint.hcl`)
4. `checkov` (terraform + secrets frameworks) with the custom policies in `infra/terraform/checkov/`

Tools: `terraform` and `tflint` on `PATH` (override with `make tf-check TERRAFORM=... TFLINT=...`);
checkov runs through `uv tool run`, pinned to 3.3.22. checkov has no built-in Nebius checks, so the
invariants are custom policies:

| ID | Rule |
|---|---|
| CKV_P2P_1 | S3 access keys use `secret_delivery_mode = "MYSTERY_BOX"` (secret never in the state) |
| CKV_P2P_2 | SecretStash secrets have no `secret_version` / `sensitive` payload |
| CKV_P2P_3 | No access permit grants `admin` or `storage.bucket-policy-editor` |
| CKV_P2P_4 | No bucket policy grants anonymous access |
| CKV_P2P_5 | Managed PostgreSQL has no public access |
| CKV_P2P_6 | A subnet with a custom route table has no public IP pool (no bypass of the gateway) |

The egress gateway's configuration is tested by `tests/infra/test_gateway_config.py`: the host-side
tests run in `make test`; the container tests check the rendered files with the real tools on
Ubuntu 24.04 (`squid -k parse`, `nft -c -f`, `cloud-init schema`, the exec pinning script):

```bash
uv run pytest -m docker tests/infra
```

Skip list (`skip-check` in `.checkov.yaml`): empty; any future skip carries its reason there.
`tests/infra/test_no_secret_versions.py` (part of `make test`) also checks: no secret version,
payload or access-key secret read in `infra/`; every access key uses `MYSTERY_BOX`; no tfvars or
state file in git; no permit on the tenant; no role for `sa-api-runtime` on `p2p-control`; the OIDC
subject pins repository, environment and branch.

## Design notes

- **Least privilege by project.** Nebius general roles are granted per project (or per bucket,
  registry, secret or IAM object), so isolation comes from project boundaries:
  - `sa-job-launcher` is in `p2p-job-launcher`, a group created inside `p2p-sandbox`: a project
    group can only ever hold permits in its own project. `editor` there.
  - `sa-api-runtime` has **no** role on `p2p-control`: `storage.object-editor` on the `p2p-state` and
    `p2p-bundles` buckets, `mysterybox.payload-viewer` on `p2p-secrets` only, and `editor` on
    `p2p-signing`, a project that holds nothing but the signing key (KMS signing needs `editor`, and
    KMS keys accept no resource-level permit). Its Litestream key therefore cannot read
    `p2p-tfstate`, push images or touch the endpoint.
  - `sa-ci-deployer` is in a tenant group with `editor` on `p2p-control` and `p2p-sandbox` and
    `viewer` on `p2p-signing`. It cannot change IAM or the signing key: those are human applies.
- **GitHub OIDC.** The federated credential accepts only
  `sub = repo:<github_repo>:environment:prod:ref:refs/heads/main`. GitHub's default `sub` for a job
  that uses an environment has no ref, so the repository's OIDC subject template must include it
  (H-04 step 0), and the `prod` environment restricts deployment branches to `main` with a required
  reviewer.
- **Secrets never in the state.** Access keys are created with `secret_delivery_mode = "MYSTERY_BOX"`:
  the secret half goes to a SecretStash secret and the state keeps only the key ID and
  `secret_reference_id` (sensitive outputs). The PostgreSQL password is a write-only argument fed by
  an ephemeral variable (`TF_VAR_pg_password`). Credentials for the provider (`NEBIUS_IAM_TOKEN`) and
  the backend (`AWS_*`) come only from environment variables.
- **Sandbox network, fail-closed.** `provision` and `exec` route `0.0.0.0/0` to the private
  allocation `gateway-private` (fixed IP `cidrhost(gateway_cidr, 10)`, `prevent_destroy`) and have no
  public IP pool. The egress gateway VM carries that allocation; while it is stopped or not yet
  created the next hop is unassigned, which the VPC docs describe as a black hole. The `gateway`
  subnet keeps the default route table (a custom one would loop).
- **Egress gateway (§8.2, A-04).** VM `p2p-egress-gw` (`cpu-e2` `2vcpu-8gb`, Ubuntu 24.04
  `ubuntu24.04-driverless`, 20 GiB) in the `gateway` subnet with `gateway-private` and a dynamic
  public IP on `eth0`. No service account, no user, no SSH key; SSH is disabled at first boot.
  - *Squid* (`squid-openssl`) intercepts tcp/443 on 3129 (`ssl_bump peek` at step 1, `splice` when
    the SNI is on the source subnet's list, `terminate all` otherwise; nothing is decrypted, the
    `dummy.pem` key is generated on the VM) and tcp/80 on 3128 (`dstdomain` on the Host header).
    `host_verify_strict off` for CDN-backed hosts. `squid.conf` is rendered at first boot by
    `infra/cloud-init/render_squid.py` from the shipped copy of `config/allowlist.yaml`, the same
    renderer the tests use; `${NEBIUS_STORAGE_HOST}` and `${NEBIUS_REGISTRY_HOST}` come from
    `envs/prod` (`storage.<region>.nebius.cloud`, the sandbox registry's FQDN).
  - *nftables* redirects tcp/443 and tcp/80 from `provision` and `exec` to Squid; anything else from
    those subnets that is not addressed to the gateway is logged (`p2p-egress-deny`) and dropped.
    The gateway **never forwards**: `net.ipv4.ip_forward=0` (the redirect delivers locally, so no
    forwarding is needed) and the forward chain's policy is drop. If nftables fails to load,
    nothing is redirected and the kernel drops every job packet addressed to the internet. There is
    no MASQUERADE or SNAT rule.
  - *Exec destination pinning.* `exec` may only reach the IPv4 addresses its allowlisted hosts
    resolve to: `p2p-exec-pin.timer` resolves `/etc/p2p/exec-hosts.txt` every minute into the set
    `exec_dst4`, which is empty at boot. This closes the SNI-spoofing gap of
    `host_verify_strict off` for the subnet that runs untrusted code.
  - *Security group `sg-egress-gw`.* Ingress tcp 80/443 and ICMP from the `provision` and `exec`
    CIDRs (priority 100, 110), egress tcp 80/443 (100), deny-all both ways (200): below the default
    group's allow-all at 500, so the denies win. nftables enforces the same policy on the VM.
  - *Logs.* Squid logs every connection (client, original destination, SNI, result) to syslog,
    nftables logs denials; journald is persistent (1 GiB) and the label
    `nebius.o11y.systemd-logs-collection.enabled=true` lets the Nebius monitoring agent forward it
    to Observability Logs.
  - *Lifecycle.* The VM is created **stopped** (`create_stopped = true`): its first start, by a
    human at H-04 or by the orchestrator, runs cloud-init. cloud-init runs once, so any change to
    `infra/cloud-init/` or to the allowlist replaces the VM (`replace_triggered_by` on the hash of
    the user data), and the replacement has a **new instance ID**: the orchestrator must find it
    by name (`GATEWAY_NAME` = `p2p-egress-gw` in p2p-sandbox; `GATEWAY_INSTANCE_ID` is only valid
    until the next replacement). The dynamic public IP is released on stop and a new one is
    assigned on start.
  - *Readiness.* RUNNING comes before Squid is up, and the control plane cannot reach net-sandbox
    to probe it. Squid starts only after nftables and the first refresh of `exec_dst4`; setup
    prints `p2p-gateway-setup: egress gateway ready` (or `FAILED ...`) on the serial console.
    Contract for the orchestrator: wait about 60 s after RUNNING, and the provisioner and runner
    retry their first egress for about 120 s.
  - *Recovery.* A failed first boot (for example an apt mirror outage after five retries) leaves
    the gateway fail-closed and dead. Replace it:
    `terraform apply -replace=module.egress_gateway.nebius_compute_v1_instance.gateway`.
- **Lifecycle (§10.3).** `p2p-state`: versioning, noncurrent versions expire after 7 days.
  `p2p-bundles`: versioning, objects expire on 2027-01-31, noncurrent versions 30 days later.
  `p2p-cache`: objects expire 30 days after upload (Nebius expiration counts from upload; "days since
  last access" exists only for storage-class transitions). Versioned buckets drop orphan delete
  markers; all abort incomplete multipart uploads after 1 day. `p2p-state`, `p2p-bundles` and the
  signing key have `prevent_destroy`.

## Residual risks (declared, see architecture §8.3)

| Risk | Why it remains | Mitigation / follow-up |
|---|---|---|
| Whoever holds `sa-job-launcher` credentials can rewrite sandbox routes, subnets or the gateway allocation | The orchestrator needs project `editor` in `p2p-sandbox` to create jobs and buckets; there is no finer role for that | Jobs never receive its credentials (invariant 4; check in S-5 that a job has no SA token). Follow-up: scheduled `terraform plan -detailed-exitcode` drift check on `envs/prod` and audit-log alerts on route, subnet and allocation changes |
| `provision` and `exec` share `net-sandbox`, so they can reach each other by private IP | Subnets of one network have local routes; the default security group allows all | PROVISION runs only deterministic code (wheels only, no build scripts), so nothing listens there; the S-10 port scan covers it. Fallback: a third network for `provision` |
| `p2p-tfstate` lives in `p2p-control` | Created by hand at bootstrap | Only `sa-ci-deployer`, `sa-tfstate` and humans have roles that reach it; the state holds no secret values |
| CI (`editor` on `p2p-control`) can write the `prod-iam` state and recreate the buckets or secret it looks up by name, steering the next admin apply | Both states share `p2p-tfstate`; a human-only bucket needs a project where no service account has a role | Every `envs/prod-iam` apply goes through `terraform plan -out` and a review of each destroy, replace and permit target before `apply`. Follow-up: move the `prod-iam` state to a bucket in a human-only project |
| `editor` on `p2p-signing` lets the runtime create other resources there (cost abuse) | KMS signing needs `editor` at project scope | Set compute and GPU quotas of `p2p-signing` to zero and add a budget alert; settle in A-26 whether `viewer` is enough (assumption #10) |
| `ak-api-runtime`, `ak-job-launcher` and the `sa-tfstate` key do not expire automatically | The provider has no `expires_at` for access keys; the bootstrap key uses the CLI default delivery (readable again with `access-key get`) | Rotate or delete all three at the end of judging (dated H-task); the bootstrap key has a 120-day `--expires-at` |
| CI has no credentials for the S3 state backend yet | An OIDC token is not an S3 key | A-05: deliver the `sa-tfstate` key secret to SecretStash and let CI read it after the OIDC exchange, or use a key in the protected `prod` environment |
| A `provision` job can reach any IP on 443/80 by sending an allowlisted SNI or Host header | With `host_verify_strict off` Squid connects to the original destination when the name does not match it; strict checking breaks CDN-backed hosts (HF, PyPI) | PROVISION runs only deterministic code (wheels only, no build scripts). `exec` is pinned to the resolved addresses of its hosts. Re-evaluate `host_verify_strict on` with the S-5 gateway logs |
| `exec` can talk both ways (upload, C2) with any bucket or registry on the allowlisted Object Storage and Container Registry endpoints, and could domain-front through them | The endpoints are shared by every Nebius customer; the gateway splices TLS and sees only the SNI | Jobs hold no credentials (invariant 4), but code from a paper could carry its own keys. Follow-up tied to S-5: if image pull and bucket mount happen outside the job network, empty `egress.exec`; otherwise consider bucket-scoped access (signed URLs only) |
| DNS from jobs reaches the VPC resolver directly | `169.254.0.0/16` is not evaluated by security groups and is not routed through the gateway | Declared in architecture §8.3 (DNS tunnelling); nothing secret is in a job |
| Connection attempts dropped by `sg-egress-gw` (other ports, UDP) never reach the VM, so the gateway cannot log them | Security groups have no logging here | nftables and Squid log everything that reaches the VM (`p2p-egress-deny`, one Squid line per connection) |
| The gateway's own NTP traffic is blocked unless the platform serves time over `169.254.0.0/16` | `sg-egress-gw` allows egress tcp 80/443 only | Squid only splices (no certificate checks), so drift does not weaken the policy; check `timedatectl` at H-04 |

## Assumptions to verify at H-04

Spike results in `docs/spikes/RESULTS.md` override this table. Each one fails loudly at apply time
or in the named spike rather than silently weakening isolation.

| # | Assumption | Depends on | If wrong |
|---|---|---|---|
| 1 | A new network's private pool covers `10.1.0.0/24`, `10.1.1.0/24`, `10.1.2.0/27` (eu-north1 default range 10.0.0.0/13) | S-5, H-04 | Set `*_cidr` in tfvars to ranges inside the pool `nebius vpc network get` shows |
| 2 | Serverless jobs start in a subnet without public IP pools and pull images / mount buckets through the gateway route | S-5 | Allowlist Registry and Object Storage on the gateway (A-04); never re-enable public pools on routed subnets |
| 3 | With the gateway stopped or the allocation unassigned, a job in `exec` has no egress at all (fail-closed) | S-5 `--mode gateway-off` | Stop: isolation is broken; run no untrusted code until fixed |
| 4 | The allocation accepts a bare IP (`10.1.2.10`) without showing drift on the next plan | H-04 (`plan -detailed-exitcode`) | Write the form the API returns (e.g. `/32`) |
| 5 | Bucket names `p2p-state`, `p2p-bundles`, `p2p-cache` are free (names are unique per region) | S-11 | Override `*_bucket_name` in both tfvars files |
| 6 | Federated credentials: `parent_id` = the SA's project; exact match on `sub`, which GitHub signs as `repo:<owner>@<owner id>/<name>@<repo id>:...` (S-7, verified 2026-10-21 for the default template; the custom template's form is confirmed by the first CI deploy). GitHub token audience `sts.nebius.com` | S-7 | Set `github_oidc_subject` to the sub the deploy log prints (`GitHub OIDC token claims: sub=...`); `enable_github_oidc = false` and a deploy key in the `prod` GitHub environment as the last resort |
| 7 | Access keys with `MYSTERY_BOX` delivery work for service accounts | H-04 | Create the keys with the CLI and remove them from Terraform (`deploy/secrets.sh` would then need to read the secret from the CLI instead of the SecretStash reference) |
| 8 | A SecretStash secret can be created without a version | H-04 | Create it with the CLI and `terraform import` it |
| 9 | Expiration `date` format `2027-01-31T00:00:00Z` is accepted | H-04 | Adjust `bundles_expire_on` |
| 10 | Bucket permits, the secret permit and `editor` on `p2p-signing` are all the runtime needs | A-26, S-6 | Grant the missing role on the specific resource, never project-wide on `p2p-control` |
| 11 | `--env-secret` injection into the endpoint does not need `mysterybox.payload-viewer` for the deployer | S-6 | Prefer a separate secret for the endpoint's env over giving CI read access to `p2p-secrets` |
| 12 | `editor` can read buckets, the secret container and (as `viewer`) the KMS key without drift in CI plans | A-05 | Move the offending attribute to `envs/prod-iam` |
| 13 | PostgreSQL platform/preset/disk defaults (`cpu-e2`, `2vcpu-8gb`, `network_ssd`) | only if enabled | Adjust the module variables |
| 14 | The `nebius_mysterybox_v1_secret` data source returns no version or payload into the `prod-iam` state | H-04 (`terraform show -json \| grep -c payload` is 0) | Pass the secret ID as a variable instead of the data source |
| 15 | A Serverless job gets no IAM token of `sa-job-launcher` (metadata or env) | S-5 | Stop: isolation is broken; jobs must run with no identity |
| 16 | The VPC delivers packets routed to `gateway-private` to the gateway's NIC with their original destination IP, so the nftables redirect sees them | S-5 `--mode gateway-on` | The transparent proxy does not work: fall back to an explicit proxy (`HTTPS_PROXY`) for provision and no egress for exec |
| 17 | Image pull and bucket mount of Serverless jobs need only `${NEBIUS_STORAGE_HOST}` and `${NEBIUS_REGISTRY_HOST}`, as concrete hosts (pinnable), on tcp/443 | S-5 | Add the hosts S-5's gateway logs show to `config/allowlist.yaml` (exec entries must stay concrete names); if the platform pulls outside the VM network, empty the exec list |
| 18 | The real Hugging Face / PyPI / GitHub CDN hosts are covered by the provision list | S-5 (gateway logs) | Add the hosts Squid denied (its log in Observability Logs, assumption 23; also `release-assets.githubusercontent.com` for GitHub release downloads) to `config/allowlist.yaml`; the VM is replaced on apply |
| 19 | The addresses the gateway resolves for the exec hosts are the ones jobs get (stable regional endpoints) | S-5 | Intermittent exec failures with `p2p-egress-deny` in the gateway log: refresh more often, or drop pinning and accept the SNI-only policy for exec |
| 20 | Rules of `sg-egress-gw` at priority 100–200 win over the default group's allow-all at 500, and stateful allows let replies through the deny-all rules | S-10 port scan | If the default group still lets other ports through, nftables (input policy drop, no forwarding) is the only enforcement on the gateway; record it in the notes |
| 21 | Security groups do not filter DHCP and the metadata service of the gateway | S-10, H-04 (the VM keeps its address after a lease renewal) | Add an ingress allow for the DHCP server's address on udp/68 |
| 22 | Nebius accepts at least 32 KiB of cloud-init user data (the module checks this) | H-04 | Compress the shipped files (`gz+b64`) |
| 23 | The Nebius monitoring agent ships journald to Observability Logs from a VM with **no** service account | H-04 (query the logs with `nebius` CLI / Grafana) | Attach a dedicated `sa-egress-gw-logs` with only a log-writer role (jobs cannot reach the gateway's metadata: 169.254.0.0/16 is not routed through it), or keep logs on a separate disk; until then the logs are only on the boot disk, which is replaced with the VM, and nobody can log in to read them |
| 24 | The `provision` and `exec` subnets have no IPv6 address or route (nothing bypasses the IPv4 default route) | S-10 | Disable IPv6 on the subnets or route it to the gateway too |
| 25 | `ubuntu24.04-driverless` has `python3`, netplan/networkd and the Nebius monitoring agent (which ships logs over tcp/443 only), and `squid-openssl`, `nftables`, `python3-yaml` install over tcp/80/443 | H-04 (serial console output of the first boot) | Switch the image family, add the missing package, or open the agent's port in `sg-egress-gw` |
| 26 | `terraform plan` is empty right after apply, with the VM stopped through the API and after a restart (new dynamic public IP; `cloud_init_user_data` returned unchanged) | H-04 (`plan -detailed-exitcode`) | Add the drifting attribute to `ignore_changes`; a diff on `boot_disk` or the NIC would replace the VM |
| 27 | A new image published in `ubuntu24.04-driverless` does not show up as a boot disk replacement; the image's minimum disk size is at most 20 GiB and the family is not deprecated before 2026-12-15 | H-04, then a plan after the next image release | Pin `source_image_id` instead of the family; raise `disk_size_gibibytes` |

## Bootstrap and apply (H-04, human only)

Run from WSL with the `nebius` CLI logged in as a tenant admin and `gh` logged in as the repository
owner. Nothing below writes a secret to a file; keys live only in the shell environment.

```bash
# 0. GitHub: OIDC subject must carry the ref; prod environment = main only + required reviewer.
gh api -X PUT repos/atozzi-dev/paper2proof/actions/oidc/customization/sub \
  -F use_default=false -f 'include_claim_keys[]=repo' -f 'include_claim_keys[]=context' \
  -f 'include_claim_keys[]=ref'
#    Settings > Environments > prod: required reviewers, deployment branches = main only.

export TENANT_ID=tenant-...                  # nebius iam tenant list
export NEBIUS_CONTROL_PROJECT_ID=project-...
export NEBIUS_SANDBOX_PROJECT_ID=project-...

# 1. Signing project (only the KMS key), in the console or with the CLI (check flags with --help):
nebius iam project create --parent-id "$TENANT_ID" --name p2p-signing
#    Set its compute and GPU quotas to zero (it only holds a KMS key).
#    Also delete the default network of p2p-sandbox if no job uses it (defence in depth).

# 2. State bucket and a state-only service account (outside Terraform: chicken and egg).
nebius storage bucket create --parent-id "$NEBIUS_CONTROL_PROJECT_ID" \
  --name p2p-tfstate --versioning-policy enabled
TFSTATE_BUCKET_ID=$(nebius storage bucket get-by-name --parent-id "$NEBIUS_CONTROL_PROJECT_ID" \
  --name p2p-tfstate --format jsonpath='{.metadata.id}')
SA_TFSTATE_ID=$(nebius iam service-account create --parent-id "$NEBIUS_CONTROL_PROJECT_ID" \
  --name sa-tfstate --format jsonpath='{.metadata.id}')
GRP_TFSTATE_ID=$(nebius iam group create --parent-id "$NEBIUS_CONTROL_PROJECT_ID" \
  --name p2p-tfstate --format jsonpath='{.metadata.id}')
nebius iam access-permit create --parent-id "$GRP_TFSTATE_ID" \
  --resource-id "$TFSTATE_BUCKET_ID" --role storage.object-editor
nebius iam group-membership create --parent-id "$GRP_TFSTATE_ID" --member-id "$SA_TFSTATE_ID"
TFSTATE_KEY_ID=$(nebius iam v2 access-key create --parent-id "$NEBIUS_CONTROL_PROJECT_ID" \
  --account-service-account-id "$SA_TFSTATE_ID" --description 'Terraform state backend' \
  --expires-at "$(date -u -d '+120 days' +%Y-%m-%dT%H:%M:%SZ)" \
  --format jsonpath='{.metadata.id}')
export AWS_ACCESS_KEY_ID=$(nebius iam v2 access-key get --id "$TFSTATE_KEY_ID" --format json | jq -r '.status.aws_access_key_id')
export AWS_SECRET_ACCESS_KEY=$(nebius iam v2 access-key get --id "$TFSTATE_KEY_ID" --format json | jq -r '.status.secret')

# 3. Provider credentials: a short-lived user token, environment only.
export NEBIUS_IAM_TOKEN=$(nebius iam get-access-token)

# 4. Resources an editor can manage (CI applies this root afterwards).
cd infra/terraform/envs/prod
cp prod.auto.tfvars.example prod.auto.tfvars          # project IDs only (gitignored)
terraform init -lockfile=readonly
terraform apply

# 5. Identity and access (human only, after step 4: it looks up buckets and the secret by name).
cd ../prod-iam
cp prod-iam.auto.tfvars.example prod-iam.auto.tfvars  # tenant and project IDs only (gitignored)
terraform init -lockfile=readonly
terraform plan -out=iam.tfplan                        # review every destroy/replace and permit target
terraform apply iam.tfplan && rm iam.tfplan

# 6. Secret values (H-04 step 3): AWS_* still exported, both envs initialized, CLI logged in as
#    a tenant admin. It asks for the Token Factory and Tavily keys (hidden), generates
#    SESSION_KEY and two authorized keys (sa-api-runtime, sa-job-launcher; they expire
#    2027-01-31), reads the S3 secrets, and writes one new version of p2p-secrets. Keep the
#    ADMIN_TOKEN for `gh secret set P2P_ADMIN_TOKEN --env prod`. Never pipe it through tee.
../../../../deploy/secrets.sh
terraform plan -detailed-exitcode                     # in prod-iam and in prod: exit 0 = no changes

# 7. Non-secret outputs for .env and the GitHub repository variables.
terraform -chdir=../prod output -json env
terraform -chdir=../prod output -json network_sandbox

unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY NEBIUS_IAM_TOKEN
```

The sensitive output `s3_access_keys` of `envs/prod-iam` holds key IDs and SecretStash references
only; read it with `terraform output -json s3_access_keys` inside `deploy/secrets.sh`, never into a
file.
