# Devpost submission

The text for the Devpost form, kept in the repository so that it stays in sync with the code.
Fields in brackets are filled in at submission.

## Tagline

Do the numbers in this paper hold up? Paper2Proof re-runs an ML paper's claims in an isolated
Nebius Token Factory Sandbox and returns a deterministic verdict with a signed Proof Bundle.

## Inspiration

Most ML papers report numbers that nobody re-runs. Checking one result means finding the code,
rebuilding the environment, writing what is missing, running several seeds and choosing a fair
tolerance: hours of work per claim, so it rarely happens. Coding agents can now do most of that
work. We wanted to see whether they can do it with the rigour a reviewer would accept, and
without letting a model either run untrusted code next to our secrets or decide the outcome.

## What it does

You give Paper2Proof an arXiv ID at <https://paper2proof.com>. It:

1. downloads the paper (LaTeX source when there is one, PDF otherwise) and extracts its
   quantitative claims, including numbers in tables and figures, each with its source in the
   paper;
2. finds the official code from the authors' own links (arXiv, Hugging Face Papers) or, when
   there are none, through Tavily search ranked by a small model;
3. plans a small experiment within a fixed budget, and asks for approval when the estimate is
   above $0.60;
4. provisions the packages, data and repository in a locked-down container that runs only our
   code and never executes what it downloads;
5. lets a Coder model write the experiment and fix it against smoke runs in a Nebius Token
   Factory Sandbox with networking disabled;
6. runs it over at least 3 seeds in a fresh Sandbox, on CPU, also offline and with no secrets;
7. decides the verdict with a pure function: a 95% confidence interval against a stated
   tolerance band;
8. has a reasoning model explain the verdict from facts written by code, and packs everything
   into a Proof Bundle signed with Nebius KMS that anyone can verify.

Verdicts are *Reproduced*, *Reproduced at reduced scale*, *Inconclusive* (always with a reason)
or *Not reproduced under our conditions*, always with the conditions shown next to them. Each
run has a live timeline, a cost receipt per model role, and a replay mode that shows a full run
in about a minute without spending anything. Live runs are invite-only, runs are private by
default, and the public gallery is curated by hand.

## How we built it

- **Models:** six roles mapped to open NVIDIA Nemotron models on Nebius Token Factory.
  Nemotron 3 Super reads the paper and writes the code, Nemotron 3.5 Super VL reads tables and
  figures, Nemotron 3 Nano ranks and repairs, and Nemotron 3 Ultra with reasoning plans the
  experiment and explains the verdict. Roles resolve against `/v1/models` at startup (no model
  ID is hardcoded), and every structured answer is a forced tool call.
- **Agent design:** an explicit state machine with one bounded agentic loop: the Coder, with at
  most 8 smoke runs, told how many it has left. The Coder can read and write workspace files,
  run smoke tests, search documentation and *ask* for packages. Installing packages, creating
  jobs and handling buckets are done by deterministic code after validation.
- **Where code runs:** the control plane (FastAPI, React UI and orchestrator in one process)
  runs on a VPS of ours behind a TLS reverse proxy. Generated code runs only in Nebius Token
  Factory Sandboxes: a fresh microVM per smoke run and per experiment (4 vCPU, about 4 GB),
  networking disabled, no secrets, inputs uploaded by content hash and mounted read-only, the
  job dropped to an unprivileged user, and its result files checked as untrusted data. One
  Nebius bucket per run, the runner image pinned by digest.
- **Safety guards:** paper text, repository files and model output are wrapped as untrusted
  data before every prompt; a package policy decided by code (plain names only, a denylist,
  typosquat checks against the top 500 PyPI names, a minimum history, wheels only and
  hash-locked, no package that publishes only source); deterministic job and bucket names,
  looked up before every creation; a ledger and budget check before every model call and job; a
  stand-in tripwire that stops a script admitting it tests a proxy instead of the paper's
  method or data; and an implausibility check that turns a gap of more than twice the paper's
  value into *Inconclusive* instead of an accusation.
- **The designed deploy, kept:** a Serverless endpoint for the control plane, GPU jobs on an
  NVIDIA L40S and an egress gateway (nftables and Squid, SNI allowlist, fail-closed) are
  implemented, tested and in Terraform, but not used in production. Our tenant was never granted
  the non-GPU vCPU quota that the endpoint and the gateway need, and the isolated GPU job test
  failed with an internal error (with no free L40S outside it), so three days before the
  deadline we moved the experiments to Sandboxes, behind the same job interface.
- **State and evidence:** SQLite replicated to Nebius Object Storage with Litestream, Proof
  Bundles signed with an asymmetric Nebius KMS key, the Nebius infrastructure in Terraform, and
  CI on GitHub Actions (ruff, mypy --strict, pytest with 100% coverage of the verdict engine,
  Vitest, Terraform checks).
- **Cost control:** every model call and every job is checked against per-run and daily budgets
  before it is made, with a kill switch. A run is capped at $1 of inference and 30 minutes of
  experiment.

## Challenges we ran into

- **Making a verdict trustworthy.** A model deciding "reproduced" is worthless to a reviewer. We
  wrote the verdict as a pure function with property-based tests, and built the rules so that a
  negative verdict needs full scale, at least 3 seeds and a whole confidence interval outside
  the band. A reduced-scale run can support a relative claim, but never refute an absolute one.
- **A broken experiment must not accuse a paper.** A measured value several times off the
  paper's is a bug or a unit mismatch far more often than a false claim, so a gap of more than
  twice the paper's value is *Inconclusive* (`IMPLAUSIBLE_GAP`), never "not reproduced".
- **A Coder that quietly changes the question.** When the paper's method or data was hard to
  get offline, the Coder sometimes measured something else and said so only in a comment
  ("as a proxy", a fallback to random data). Its script now cannot finish while it says that,
  and the run's Limitations name the line.
- **Running code that a model wrote after reading an arbitrary PDF.** We split execution into a
  provisioning phase where only our code runs (wheels only, hash-locked, never imported, the
  repository never run) and an execution phase in Sandboxes with networking disabled and no
  secrets.
- **Platform surprises,** found by spikes and fixed in the design: job volumes need bucket IDs
  rather than names, GPU jobs wait for capacity in silence, new tenants have no CPU quota (ours
  never got one), GPU jobs in an isolated subnet ended with a bare internal error, the
  Sandboxes importer refuses images referenced by digest, and one model ignores `json_schema`.
  Each is in our feedback file.
- **Percent against fraction.** Papers write 94.1, code returns 0.941. The Coder declares the
  unit, the smoke run checks it, and any conversion is printed and recorded in the verdict.

## Accomplishments that we're proud of

Latest evaluation round, through the live pipeline:

| Set | Result | Cost |
|---|---|---|
| A: 10 synthetic scikit-learn papers, 16 planted true and false claims | accuracy **1.0** (16/16), **0 false accusations** | about $0.28 for the set |
| B: 5 real papers (Distillation, CatBoost, InterpretML, NAU, KAN) | 3 runs reached a verdict, all *Inconclusive* with a stated reason (CatBoost on real Adult data, KAN, Distillation); 2 failed before one (InterpretML: sandbox disk; NAU: no passing smoke run); **0 false accusations** | about $0.74 for the set |
| C (claim extraction) and the routing ablation | not run for this submission | — |

- Set A accuracy moved over our rounds (0.5 → 0.81 → 0.94 → 0.81 → 0.69 → 0.94 → 1.0 → 0.94 →
  1.0): model variance remains, and we report it. Every miss became a check in code.
- Deliberately falsified claims caught as *Not reproduced under our conditions*, with no true
  claim accused.
- A Proof Bundle that a stranger can verify with standard tools, with a script that re-runs the
  judged attempt.
- No model output is ever executed as a privileged command, and the verdict never depends on a
  model.

## What we learned

- The smallest model that does a role well is usually smaller than expected. Ultra pays off for
  planning and explanation, Super is enough for reading and coding, and Nano handles the glue.
- Forced tool calls are the most portable way to get structured output across a model family.
- Real papers fail on the environment first (data that cannot be fetched offline, packages
  without wheels, too few smoke runs), long before the verdict logic is tested.
- On a new cloud, a day of measured spikes saves a week of wrong assumptions.

## What's next for Paper2Proof

- Run the extraction set and the routing ablation, and grow the real-paper set.
- GPU experiments on Nebius AI Cloud once the quota allows: the path is already implemented.
- Parallel fix branches for the Coder.
- An "author mode" where authors can attach their own runs and contest a verdict.

## Built with

Python, FastAPI, Pydantic, SQLAlchemy, Alembic, SQLite, Litestream, React, Vite, TypeScript,
NVIDIA Nemotron 3 (Nano, Super, Ultra), NVIDIA Nemotron 3.5 Super VL, Nebius Token Factory
(inference, Sandboxes), Nebius AI Cloud (Object Storage, Container Registry, KMS, MysteryBox,
IAM), Tavily, Hugging Face Hub, arXiv, PyTorch, scikit-learn, Terraform, Docker,
GitHub Actions, OpenTelemetry, pytest, Hypothesis, Vitest, Playwright.

## Testing instructions

- **Live service:** <https://paper2proof.com> (no login needed for the gallery and replays).
- **Invite code for live runs:** `[INVITE_CODE]` (3 runs per day; only in this private field,
  never in the public text).
- **Quick look (1 minute, free):** on the home page, under *Recent verdicts*, press **▶ Replay**
  on a run. It replays the recorded run at 10× speed; nothing is executed.
- **A verdict:** press **Open** on a gallery run to see the claim, the measured interval
  against the tolerance band, the conditions, the explanation, the cost receipt and the Proof
  Bundle.
- **A live run:** enter an arXiv ID such as `1706.09516` (CatBoost, about 5 minutes) or `0000.00006`
  (one of our synthetic canary papers, about 2 minutes) and the invite code, then press
  **Start the run**. A small paper takes a few minutes; the link keeps working if you leave the
  page. Experiments run on CPU, so a paper that needs a GPU runs at reduced scale or ends
  *Inconclusive*.
- **Verify a bundle:** see the README section "For judges: what to try" in
  <https://github.com/atozzi-dev/paper2proof>.

## Links

- Live service: <https://paper2proof.com>
- Repository: <https://github.com/atozzi-dev/paper2proof> (Apache-2.0)
- Video: [VIDEO_URL]
- Architecture: <https://github.com/atozzi-dev/paper2proof/blob/main/docs/ARCHITECTURE.md>
- Limitations: <https://github.com/atozzi-dev/paper2proof/blob/main/docs/LIMITATIONS.md>
- Feedback: <https://github.com/atozzi-dev/paper2proof/blob/main/docs/FEEDBACK.md>
