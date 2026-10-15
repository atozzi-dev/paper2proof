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

## License

[Apache-2.0](LICENSE)
