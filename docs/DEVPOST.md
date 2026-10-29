# Devpost submission

The text for the Devpost form, kept in the repository so that it stays in sync with the code.
Fields in brackets are filled in at submission.

## Tagline

Do the numbers in this paper hold up? Paper2Proof re-runs an ML paper's claims in an isolated
Nebius sandbox and returns a deterministic verdict with a signed Proof Bundle.

## Inspiration

Most ML papers report numbers that nobody re-runs. Checking one result means finding the code,
rebuilding the environment, writing what is missing, running several seeds and choosing a fair
tolerance: hours of work per claim, so it rarely happens. Coding agents can now do most of that
work. We wanted to see whether they can do it with the rigour a reviewer would accept, and
without letting a model either run untrusted code next to our secrets or decide the outcome.

## What it does

You give Paper2Proof an arXiv ID. It:

1. downloads the paper and extracts its quantitative claims, including numbers in tables and
   figures, each with its source in the paper;
2. finds the official code on Hugging Face Papers, in the abstract or through Tavily search;
3. plans a small experiment within a fixed budget, and asks for approval when the estimate is
   above $0.60;
4. provisions the packages, data and repository in a locked-down container that never runs
   what it downloads;
5. lets a Coder model write the experiment and fix it against smoke runs in a Nebius Token
   Factory sandbox with no network;
6. runs it over at least 3 seeds in a fresh sandbox, also with no network interface and no
   secrets (CPU in the live service; the GPU path is implemented);
7. decides the verdict with a pure function: a 95% confidence interval against a stated
   tolerance band;
8. has a reasoning model explain the verdict, and packs everything into a Proof Bundle signed
   with Nebius KMS that anyone can verify and run again.

Verdicts are *Reproduced*, *Reproduced at reduced scale*, *Inconclusive* (always with a reason)
or *Not reproduced under our conditions*, always with the conditions shown next to them. Each
run has a live timeline, a cost receipt per model role, and a replay mode that shows a full run
in about a minute without spending anything.

## How we built it

- **Models:** six roles mapped to open NVIDIA Nemotron models on Nebius Token Factory.
  Nemotron 3 Super reads the paper and writes the code, Nemotron 3.5 Super VL reads tables and
  figures, Nemotron 3 Nano ranks and repairs, and Nemotron 3 Ultra with reasoning plans the
  experiment and explains the verdict. Roles resolve against `/v1/models` at startup, every
  structured answer is a forced tool call, and the Batch API runs the evaluation at half price.
- **Agent design:** an explicit state machine with one bounded agentic loop (the Coder, at most 8
  fixes). The Coder can read and write workspace files, run smoke tests, search documentation
  and *ask* for packages. Installing packages, creating jobs and handling buckets are done by
  deterministic code after validation.
- **Isolation:** the control plane (FastAPI, React UI and orchestrator in one process) runs on a
  host of ours behind TLS. Generated code runs only in Nebius Token Factory Sandboxes: a fresh
  microVM per smoke run and per experiment, no network interface, no secrets, inputs uploaded
  by content hash and mounted read-only, the job dropped to an unprivileged user, and its result
  files checked as untrusted data. One Nebius bucket per run, images pinned by digest.
- **The designed deploy, kept:** a Serverless endpoint, GPU jobs on an NVIDIA L40S and an egress
  gateway (nftables and Squid, SNI allowlist, fail-closed) are implemented, tested and in
  Terraform. Our tenant never got the CPU quota they need, and no L40S was free when we tested,
  so we moved the execution plane to Sandboxes two days before the deadline, behind the same
  interfaces.
- **State and evidence:** SQLite replicated to Object Storage with Litestream, Proof Bundles
  signed with an asymmetric Nebius KMS key, everything provisioned with Terraform and deployed
  from GitHub Actions through OIDC, with no static cloud key.
- **Cost control:** every model call and every job is checked against per-run and daily budgets
  before it is made, with a kill switch. A run is capped at $1 of inference and 30 minutes of
  experiment; our canary runs cost about $0.03.

## Challenges we ran into

- **Making a verdict trustworthy.** A model deciding "reproduced" is worthless to a reviewer. We
  wrote the verdict as a pure function with property-based tests, and built the rules so that a
  negative verdict needs full scale, at least 3 seeds and a whole confidence interval outside
  the band. A reduced-scale run can support a relative claim, but never refute an absolute one.
- **Running code that a model wrote after reading an arbitrary PDF.** We split execution into a
  provisioning phase where only our code runs with allowlisted network, and an execution phase
  with no network and no secrets, in a separate project.
- **Platform surprises,** found by a day of spikes and fixed in the design: job volumes need
  bucket IDs rather than names, GPU jobs wait for capacity in silence, new tenants have no CPU
  quota (ours never got one), the Sandboxes importer refuses images referenced by digest, and one
  model ignores `json_schema`. Each is in our feedback file.
- **Percent against fraction.** Papers write 94.1, code returns 0.941. The Coder declares the
  unit, the smoke run checks it, and any conversion is printed and recorded in the verdict.

## Accomplishments that we're proud of

- End-to-end runs whose verdicts match the planted truth, including a deliberately falsified
  claim caught as *Not reproduced under our conditions*.
- A Proof Bundle that a stranger can verify with standard tools and re-run with one script.
- No model output is ever executed as a privileged command, and the verdict never depends on a
  model.
- Under four cents of inference for a canary run, with the cost of every role on the receipt.

## What we learned

- The smallest model that does a role well is usually smaller than expected. Ultra pays off for
  planning and explanation, Super is enough for reading and coding, and Nano handles the glue.
- Forced tool calls are the most portable way to get structured output across a model family.
- On a new cloud, a day of measured spikes saves a week of wrong assumptions.

## What's next for Paper2Proof

- More of the evaluation on real papers, and a public leaderboard of reproduced claims.
- Smoke runs in Token Factory Sandboxes, with parallel fix branches.
- Multi-GPU plans for papers that need them, still within an explicit budget.
- An "author mode" where authors can attach their own runs and contest a verdict.

## Built with

Python, FastAPI, Pydantic, SQLAlchemy, SQLite, Litestream, React, Vite, TypeScript,
NVIDIA Nemotron 3 (Nano, Super, Ultra), NVIDIA Nemotron 3.5 Super VL, Nebius Token Factory
(inference, Batch API, Sandboxes), Nebius AI Cloud (Serverless endpoints and jobs, Object
Storage, Container Registry, KMS, MysteryBox, VPC, Compute, IAM), NVIDIA L40S, Tavily,
Hugging Face Hub, arXiv, PyTorch, scikit-learn, Terraform, Docker, Squid, nftables,
GitHub Actions, OpenTelemetry, pytest, Hypothesis, Vitest, Playwright.

## Testing instructions

- **Demo:** <https://146-59-83-160.sslip.io> (no login needed for the gallery and replays).
- **Invite code for live runs:** `[INVITE_CODE]` (3 runs per day).
- **Quick look (1 minute, free):** on the home page, under *Recent verdicts*, press **▶ Replay**
  on a run. It replays the recorded run at 10× speed; nothing is executed.
- **A verdict:** press **Open** on a gallery run to see the claim, the measured interval
  against the tolerance band, the conditions, the explanation, the cost receipt and the Proof
  Bundle.
- **A live run:** enter an arXiv ID such as `[ARXIV_ID]` and the invite code, then press
  **Start the run**. A small paper takes a few minutes; the link keeps working if you leave the
  page.
- **Verify a bundle:** see the README section "For judges: what to try" in
  <https://github.com/atozzi-dev/paper2proof>.

## Links

- Repository: <https://github.com/atozzi-dev/paper2proof> (Apache-2.0)
- Video: [VIDEO_URL]
- Architecture: <https://github.com/atozzi-dev/paper2proof/blob/main/docs/ARCHITECTURE.md>
- Feedback: <https://github.com/atozzi-dev/paper2proof/blob/main/docs/FEEDBACK.md>
