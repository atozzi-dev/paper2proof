# Spec — Proof Bundle

Source: architecture §10.3–§10.4 (v0.2). Normative. Code: `app/bundle/`.

The Proof Bundle is the downloadable evidence of one run: what was claimed, what ran, what
came out, the deterministic verdict and what it cost. Anyone holding the public key
(`docs/bundle-public-key.pem`) can check that nothing changed after the verdict.

## 1. Archive

- One gzip-compressed tar per run, `proof-bundle-<run_id>.tar.gz`, stored in `p2p-bundles` at
  `bundles/<run_id>/proof-bundle.tar.gz` and served by `GET /api/runs/{id}/bundle`.
- gzip (not zstd as in architecture §10.4): verifiers need nothing outside the Python standard
  library and `cryptography`, or plain `tar` + `openssl`.
- Deterministic: members sorted by path, regular files only, mode `0644` (`0755` for
  `reproduce.sh`), uid/gid `0`, mtime `0`, gzip header mtime `0`. Every member is under the top directory `bundle/`.

## 2. Layout

```
bundle/
├── MANIFEST.json                    # sha256 + size of every other file, versions (§3)
├── SIGNATURE                        # KMS signature of sha256(MANIFEST.json) (§4); absent if unsigned
├── paper/metadata.json              # arXiv ID, title, sha256 and kind of the source: never the PDF/LaTeX
├── claims.json                      # every extracted claim (+ `selected`), with its source reference
├── plan.json                        # the Planner's plan as approved
├── env/provision_report.json        # validated provision report (archive digests, repo commit)
├── env/lock.txt                     # full install lock with hashes (checked against lock_sha256)
├── code/workspace.tar               # the exact workspace the GPU job ran (checked against workspace_sha256)
├── code/smoke_result.json           # entrypoint, args, smoke metrics of the last passing smoke run
├── code/exec_manifest_<attempt>.json  # the manifest each GPU job received
├── results/<attempt>/results.json   # runner output (untrusted, as read back)
├── results/<attempt>/{stdout.log, stderr.log, env.json}
├── results/<attempt>/execution.json # how the job ended (control plane)
├── results/<attempt>/analysis.json  # measurements built from results.json (control plane)
├── verdict.json                     # the verdict, the claim, the tolerance config and its sha256
├── explanation.md                   # the explanation shown in the UI (Limitations written by code)
├── cost_receipt.json                # tokens and cost per role, jobs, ledger totals for the run
└── reproduce.sh                     # re-runs the judged attempt locally with docker --gpus all (§6)
```

Files that could not be included are listed in `MANIFEST.json` under `omitted` with a reason;
the bundle is still built.

## 3. `MANIFEST.json`

```json
{
  "schema": "p2p.bundle/v1",
  "run_id": "r_01jb7z...",
  "arxiv_id": "2106.09685",
  "paper_sha256": "<hex>",
  "built_at": "2026-10-25T10:00:00Z",
  "paper2proof": {"version": "0.1.0", "commit": "<git sha or null>"},
  "claim_id": "c1",
  "verdict": "REPRODUCED",
  "judged_attempt": 1,
  "config_sha256": "<sha256 of config/verdict.yaml used by decide()>",
  "models": {"planner": ["<model id>"], "judge": ["<model id>"]},
  "prompts": {"judge_explain": {"version": "1", "sha256": "<hex of the prompt file>"}},
  "images": {"runner": "<registry>/p2p-runner@sha256:<hex>"},
  "files": {"claims.json": {"sha256": "<hex>", "bytes": 1234, "origin": "control_plane"}},
  "omitted": [{"path": "results/1/stderr.log", "reason": "too_large"}],
  "redacted": ["results/1/stdout.log"]
}
```

- `models` comes from the run's recorded model calls (what was actually used); `prompts` lists
  every prompt file of the Paper2Proof build that made the bundle (with `paper2proof.commit`).
- `origin` is `control_plane` (written by our code from validated data), `run_bucket` (read
  back from the per-run bucket, written next to untrusted code: evidence, not authority) or
  `model` (`explanation.md`: the Judge's prose plus the code-written Limitations; display only).
- Omission reasons: `missing`, `too_large`, `invalid` (not the expected format),
  `digest_mismatch`, `secret_pattern`.
- `MANIFEST.json` itself is never listed in `files`; `SIGNATURE` is never listed either.

## 4. `SIGNATURE`

```json
{
  "schema": "p2p.signature/v1",
  "algorithm": "ecdsa_nist_p256_sha_256",
  "key_id": "<Nebius KMS asymmetric key ID>",
  "manifest_sha256": "<hex>",
  "signature": "<base64 DER ECDSA signature>"
}
```

The signature is Nebius KMS `AsymmetricCryptoService.SignHash` over the 32-byte
sha256 of the exact `MANIFEST.json` bytes, with the asymmetric key `p2p-bundle-signing`
(`ecdsa_nist_p256_sha_256`). The control plane checks every signature against the published key
(`docs/bundle-public-key.pem`, or the KMS public key when none is configured) before storing it;
the KMS responses must name the requested key. If KMS is unavailable the bundle is stored without `SIGNATURE` (cut list
item 2) and the run gets a `BUNDLE_UNSIGNED` warning.

KMS tool (human-run, uses the `nebius` CLI profile's credentials):
`uv run python -m app.bundle.sign public-key --key-id <id> > docs/bundle-public-key.pem` and
`uv run python -m app.bundle.sign sign <bundle> --run-id <id> --key-id <id> -o <signed bundle>`.
It signs only an unsigned bundle of that run whose files match its `MANIFEST.json`, checks the
signature against `docs/bundle-public-key.pem` and asks for confirmation: sign only bundles
fetched from `p2p-bundles`, never one received from someone else.

Manual check: `jq -r .signature bundle/SIGNATURE | base64 -d > sig.der && openssl dgst -sha256
-verify docs/bundle-public-key.pem -signature sig.der bundle/MANIFEST.json`, then
`sha256sum` every file against `MANIFEST.json`.

## 5. Verification

`uv run python -m app.bundle.verify <bundle.tar.gz | extracted dir> [--public-key PEM]
[--allow-unsigned]` (default key: `docs/bundle-public-key.pem`). Exit `0` when valid, `1`
with one `FAIL <path>: <problem>` line per problem, `2` on usage errors. It checks:

1. the archive is safe to read (regular files under `bundle/`, no `..`, no links, at most 512
   members and 256 MiB in total) and is read in memory, never extracted;
2. every file in `files` exists with the same size and sha256; no file is missing or extra;
3. `SIGNATURE` names `ecdsa_nist_p256_sha_256`, its `manifest_sha256` matches, and the
   signature verifies against the public key. A bundle without `SIGNATURE` fails unless
   `--allow-unsigned`.

## 6. `reproduce.sh`

`./reproduce.sh <staging dir>` re-runs the judged attempt with the runner image by digest:

- `<staging dir>` holds `overlay.tar.zst`, `repo.tar.zst` (when the run used the repository)
  and `data/<name>.tar.zst`. They are not in the bundle (size); within 24 h of the run they are
  in `p2p-run-<slug>/staging/`. The script checks each one against the digest in the exec
  manifest and stops on a mismatch.
- It runs code written by a model and code from the paper's repository: it warns, asks for
  confirmation (`P2P_REPRODUCE_YES=1` skips it) and must only run on an isolated, disposable
  machine, never a developer or control-plane host (invariant 1).
- It copies `code/workspace.tar` next to them, mounts the directory at `/mnt/run/staging`
  (read-only), `results/` at `/mnt/run/results`, the exec manifest at `/etc/p2p/manifest.json`
  (read-only) and a tmpfs at `/mnt/cache`, and runs `docker run --rm --gpus all --network none
  --cap-drop ALL --security-opt no-new-privileges --pids-limit 4096 <runner>@sha256:…`.
- Results land in `<staging dir>/results/<attempt>/results.json`.

## 7. Content rules

- Never the paper PDF or LaTeX: only its sha256 and metadata.
- No secrets (invariant 4). Every file is scanned before packing for key-shaped strings
  (`tvly-…`, `Bearer <token>`, private key blocks, `hf_…`, `AKIA…`, `sk-…`, GitHub, Slack and
  Google keys, JWTs) and for the control plane's own secret values (as is and JSON-escaped). Matches in text files are replaced with `[REDACTED]`
  (the path goes in `redacted`); a match inside `code/workspace.tar` omits it
  (`secret_pattern`), since its digest must stay the one the job ran.
- Everything read from the run bucket is size-capped (`results.json` and `env.json` 256 KiB,
  logs 4 MiB, `lock.txt` 1 MiB, `workspace.tar` 9 MiB), must parse when it is JSON, and is
  checked against a control-plane digest when one exists (`workspace_sha256`, `lock_sha256`).
- Hashing, scanning, packing and signature checks run in the CPU pool (invariant 12).
- Images are recorded by digest only (invariant 8).
