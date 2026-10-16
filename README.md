# Paper2Proof

Paper2Proof takes an arXiv ID, extracts the paper's quantitative claims, re-runs them in an isolated experiment on Nebius GPUs and returns a **deterministic verdict** together with a signed, re-runnable **Proof Bundle**.

Built for the Nebius × NVIDIA Global AI Hackathon (*Coding and Agentic Engineering* track).

> Work in progress. Demo link, judge instructions, architecture and evaluation results will be added before submission.

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
uses the CPU runner, since there is no GPU locally. On a Linux docker host the bucket and cache
directories must be writable by uid 10001; Docker Desktop's bind mounts already are.

## License

[Apache-2.0](LICENSE)
