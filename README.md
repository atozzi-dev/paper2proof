# Paper2Proof

Paper2Proof takes an arXiv ID, extracts the paper's quantitative claims, re-runs them in an isolated experiment on Nebius GPUs and returns a **deterministic verdict** together with a signed, re-runnable **Proof Bundle**.

Built for the Nebius × NVIDIA Global AI Hackathon (*Coding and Agentic Engineering* track).

> Work in progress. Demo link, judge instructions, architecture and evaluation results will be added before submission.

What a verdict means, where a run can be wrong and what is not supported:
[docs/LIMITATIONS.md](docs/LIMITATIONS.md).

## For paper authors

If one of your papers was run through Paper2Proof, here is what that means and what you can do.

- **A verdict is about our run, not about your paper.** The verdict comes from a deterministic
  rule over the numbers our experiment measured (a language model only writes the explanation).
  The experiment often runs at a smaller scale, with fewer seeds, other hardware or code we
  wrote ourselves. That is why a negative result always reads **"Not reproduced under our
  conditions"**, with those conditions listed next to it, and every Proof Bundle has a
  Limitations section written by code.
- **Runs are private by default.** Only the person who started a run can see it. Nothing
  reaches the public gallery automatically: we list runs one by one, and a negative verdict on
  a real paper only after a manual review.
- **We do not redistribute your paper.** Bundles hold its hash and metadata only.
- **Report an error:** open an issue at
  <https://github.com/atozzi-dev/paper2proof/issues> with the arXiv ID and the run link, and
  tell us what we got wrong (the claim, the metric, the dataset, the setup). We fix our side
  and say so on the issue.
- **Removal:** ask for a run to be taken down in an issue. We remove it from the public gallery
  right away, without asking why, and on request also delete its Proof Bundle and stored
  artifacts. The procedure is in
  [docs/OPERATIONS.md](docs/OPERATIONS.md#takedown).

## Development

Requirements: [uv](https://docs.astral.sh/uv/), Python 3.12, Node.js 24, GNU make, Docker.

```bash
cp .env.example .env   # fill in what you have; never commit .env
make setup
make lint
make test
```

`make help` lists every target.

### Dev end-to-end with local jobs

`make dev` runs the control plane with local artifacts and a fake job backend, so a run never gets
past provisioning. To run the real job images on your machine instead:

```bash
make images
make dev-images-env >> .env   # P2P_JOB_BACKEND=docker and the local images by id
make dev
```

Only papers whose sha256 is in `config/canary_allowlist.yaml` get a job: the jobs of any other
run are refused. Jobs run as uid 10001 with no capabilities, a read-only root, capped memory
and logs, a kill at their watchdog, and no network. The exception is the provisioner, which
runs our code on docker's default network: unlike the cloud gateway, its egress is not
allowlisted. Each run's bucket is a directory under `var/jobs/buckets/`. Executing
uses the CPU runner, since there is no GPU locally. On a Linux docker host the jobs' uid 10001
is a host uid too, so the backend shares `var/jobs/{buckets,cache,jobs}` with it through ACLs
(`setfacl`, from the `acl` package) and makes `var/jobs` private to you. Rootless docker and
userns-remap are not supported. Docker Desktop needs none of this.

## License

[Apache-2.0](LICENSE)
