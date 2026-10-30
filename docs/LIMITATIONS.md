# Limitations and known failure cases

Paper2Proof re-runs one experiment under stated conditions and reports what it measured. A
verdict is a statement about those conditions, never about the paper's honesty. This page lists
what the system cannot do, where it can be wrong, and how each case shows up in a run.

## What a verdict means

- **"Not reproduced under our conditions"** is only possible at full scale. It needs the whole
  confidence interval to fall outside the tolerance band: the larger of 2% of the claimed
  value, the claim's own reported std (×2) and a per-metric absolute tolerance
  (`config/verdict.yaml`). A run with fewer than 3 usable seeds is always **Inconclusive**.
- **A measured value several times off the paper's** (more than twice, either way, same
  sign) is **Inconclusive** (`IMPLAUSIBLE_GAP`) instead of "not reproduced". A wrong claim is
  typically off by a few to tens of percent; a gap that large more likely means broken
  training, a stand-in for the paper's method or data, or a unit mismatch (a count against a
  rate, a fraction against a percent).
  - For an **absolute** claim the paper's number is compared with the measured mean.
  - For a **relative** claim ("A beats B"), the rule applies when the paper also states the
    method's own value and the experiment ran at full scale: a measured method value more than
    twice away from it makes the run Inconclusive, whatever the measured difference says. The
    experiment is then not the paper's setup. A factor of about 100 is not counted as a gap
    (a fraction against a percent). Without the method's own value in the paper, a relative
    claim is decided on the difference alone.
  - A measured value far *better* than the paper's absolute value is still reported as
    Reproduced, and a mismatch whose numbers happen to be close is not caught; nor is a
    measured value of zero or of the opposite sign (an accuracy of 0, a negative R²).
- **Reduced scale.** Most papers train longer than a 30-minute budget allows, so most runs
  are at reduced scale. A relative claim ("A beats B by 1.7 points") can then be
  *Reproduced at reduced scale*. An absolute number ("94.1% top-1") cannot: it is
  **Inconclusive** (`REDUCED_SCALE_ABSOLUTE`), and the measured values are shown for reference.
  A trend that reverses at reduced scale is also Inconclusive, never "not reproduced".
- **The verdict is decided by a pure function** of the claim, the measurements and the config
  (`app/verdict/engine.py`). Language models write the explanation only, from facts written by
  code. An explanation that names a different verdict is discarded and replaced by a template.

## Where a run can be wrong

- **Claim extraction.** The Reader model picks the claims and their numbers from the paper text.
  Each claim carries its source (table or section) so it can be checked. PDF-only papers rely on
  the Vision model to read tables, and a page it cannot read is skipped with a
  `VISION_PAGE_SKIPPED` warning.
- **The experiment is re-implemented.** The Coder writes the experiment from the paper (and the
  official repository when one is found). It can differ from the authors' setup. The planner's
  deviations (smaller model, fewer epochs, another dataset split) are listed under
  *Limitations* on every verdict.
- **Stand-ins.** A script that says it uses a proxy, a stand-in, a placeholder or dummy data
  for the paper's method or data cannot finish until it changes (`STAND_IN_FLAGGED`), and the
  run's Limitations name the line. This is a tripwire on the script's own words, not a proof: a
  stand-in that nothing in the script admits to is not caught by it, only by the implausibility
  rule above when its numbers are far enough off.
- **Smoke runs.** The Coder has at most 8 smoke runs to get a passing script, and is told how
  many it has left after each one. A run that never gets a passing smoke run fails
  (`SMOKE_EXHAUSTED`) without a verdict. A script that starts processes or runs other files is
  refused before it spends a smoke run, and so is one that looks for a dataset's parquet files
  with a non-recursive pattern: Hugging Face keeps them in subfolders, and smoke runs only see
  a small subset, so that mistake would show only in the experiment.
- **Extra options on generated data.** When the script calls a scikit-learn data generator
  (`make_classification`, `make_moons`, ...) with an option away from the library's default
  that neither the paper nor the claim gives, `finish` names it once and asks the Coder to
  remove it: an extra option changes the paper's data. The Coder can confirm it by finishing
  again unchanged, and the check reads names and numbers, not prose: an option the paper
  describes only in words can be flagged, and a changed option written elsewhere (a variable,
  another function) is not seen.
- **Percent vs fraction.** The Coder declares the metric's unit (`fraction`, `percent` or
  `raw`), and the smoke run checks it. For values between 0 and 1 both units fit, so the
  declaration alone decides. Any conversion is printed as a "Scale:" line and recorded in
  `verdict.json` (`docs/specs/verdict-engine.md`).
- **Repository discovery.** The official repository is found in the paper, on Hugging Face, or by
  web search ranked by a model. A repository found by search is marked `official: false`, and
  discovery can degrade (`DISCOVERY_DEGRADED`) when a provider is unavailable. When the authors'
  PyPI package links to the discovered repository and is a known popular package, it is added
  to the plan, even when the repository was found by search. An unknown package is not added on
  the strength of that link, since a PyPI page's links are self-declared.

## What the system does not support

- Input is an **arXiv ID only**: no URLs, no PDF uploads, no other preprint servers.
- **Datasets** come with scikit-learn, are generated in the script, or are Hugging Face parquet
  files, with a size cap. The experiment runs offline, so nothing else can be downloaded. A plan
  that needs other data is re-planned once, then runs without it and says so
  (`DATASET_UNAVAILABLE`).
  - The plan keeps **one split per dataset**, since the provisioner fetches one split per
    dataset ID. When the paper evaluates on another split, the Coder holds out part of the
    fetched one, and the result is a measurement on that held-out part.
  - A few well-known datasets map to fixed public parquet copies. **Adult** maps to
    `AiresPucrs/adult-census-income` on Hugging Face: 32,561 rows, the UCI training portion
    only, so a paper's test-set number is compared with a held-out part of the training data.
    It is a copy in a personal account, fetched at its latest revision (the provisioner pins
    no revision): its owner could change the rows, and with them a verdict. Only parquet files
    are fetched and no dataset code runs.
- **Packages** pass a policy decided by code: plain names only, a denylist, typosquat checks,
  a minimum release history, at most 5 per request and 2 requests per run. Installs are wheels
  only and hash-locked: a package that publishes only a source distribution is refused at
  planning, and nothing is built from source. A planned package that fails the policy is
  dropped with a `PACKAGE_REJECTED` warning and the experiment is written without it; a package
  the Coder asks for later that fails the policy ends the run. A planned version that is not a
  plain specifier (`0.7.3`, `latest`) is dropped, and the package is judged and installed at
  its newest compatible version.
- **CPU only in the live service.** Experiments run in a Nebius Token Factory Sandbox with
  4 vCPUs and about 4 GB of memory, for at most 30 minutes, with $1 of inference. Every
  experiment gets the whole 30 minutes, whatever the plan estimated; one that needs longer for
  its seeds ends Inconclusive (`BUDGET`). Anything that needs a GPU (deep-network training, LLM
  papers) runs only at a much reduced scale, or ends Inconclusive. A sandbox also has a fixed
  disk: a plan whose packages, repository and data do not fit (InterpretML with `interpret`,
  LightGBM and XGBoost) fails its smoke runs and ends `SMOKE_EXHAUSTED`. The GPU path (one
  NVIDIA L40S per run, as a Nebius AI Cloud job) is implemented and tested but not used in
  production: see
  [specs/hybrid-deploy.md](specs/hybrid-deploy.md). Every verdict lists the hardware it ran on.
- **Capacity.** With no capacity available for the experiment (the platform refuses, or the job
  is still queued after 20 minutes) the run ends **Inconclusive (capacity)**. A used-up project
  quota ends the run with a message naming the quota.

## Known failure cases on real papers

Our real-paper evaluation set (`eval/set_b.yaml`) shows where runs stop before a verdict. The
README has the latest numbers. Fixed in code over our rounds:

- **The same dataset planned twice** ended in a contract error. A plan now keeps one split per
  dataset.
- **A planned package with no wheel** made provisioning fail. Such a package is now refused at
  planning.
- **A dataset that was not available offline** (Adult) left the Coder without data. Adult now
  maps to a public Hugging Face copy; other data with no parquet copy still ends as
  `DATASET_UNAVAILABLE`.
- **The Coder ran out of smoke runs.** It now sees how many it has left, but a hard enough setup
  can still exhaust them.
- **A stand-in function** measured in place of the paper's gave a *Reproduced* relative claim.
  The relative implausibility rule above now makes such a run Inconclusive when the paper states
  the method's own value; a stand-in whose numbers happen to be close is still not caught.
- **A parquet glob that missed the data** (the files sit in `data/`) failed only in the
  experiment. Such a glob is now refused before a smoke run.
- **A planned version the policy could not parse** made it refuse the authors' package. The
  package is now judged without that version.

Still open in the latest round:

- **A sandbox's disk** can be too small for a heavy stack (InterpretML: `interpret`, LightGBM,
  XGBoost, the repository and the data), and no smoke run can start.
- **The Coder's own bugs**: on NAU it found no passing smoke run within 8, and on Distillation
  the full run read the smoke-test data, which exists only in smoke runs.
- **CPU and 30 minutes**: deep networks (Distillation, KAN) train far less than in the paper,
  so their numbers land far from it and the verdict is *Inconclusive*.

## Security boundaries

- Paper text, repository files and model output are untrusted, and are wrapped as untrusted
  data before they enter a prompt. Code written by a model or taken from the paper's repository
  runs only in Token Factory Sandboxes: a fresh microVM per run, networking disabled, no
  credentials, inputs mounted read-only, an unprivileged user, capped time. These layers reduce
  risk; they are not a proof that a malicious paper cannot misbehave inside its own sandbox.
- **Provisioning runs on the control-plane host.** The provisioner image (our code) fetches
  wheels, the repository and parquet files in a local container: unprivileged, no capabilities,
  read-only root, capped memory and processes. It never imports the wheels or runs the
  repository, but it does parse those untrusted bytes, and the container shares the host's
  kernel: a container escape there would reach the host and its keys. Its network reaches the
  public internet only (private ranges, the host and the metadata service are dropped), so,
  unlike the designed egress gateway, egress is not an allowlist. The package policy, the hash
  lock, wheels only and parquet only limit what it fetches.
- Runs are private by default. The public gallery is curated by hand, and a run can be taken down
  on request (`docs/OPERATIONS.md`).

## Assumptions still to verify on the platform

- Sandboxes time is priced like a CPU instance (`config/pricing.yaml`) as an estimate, to
  reconcile with the invoice.
- For the GPU path (not used in production): whether time spent waiting in PROVISIONING is
  billed (cloud cost counts from submission until it is confirmed), whether a job in `DELETING`
  can still write, and whether a quota error's `requested` field includes current usage (only a
  quota with limit 0 is treated as final).

## Accessibility

The web UI uses semantic HTML: labelled form controls, tables with captions and header cells,
and live status regions for the run timeline. Charts and the architecture diagram have text
alternatives. Focus is visible for keyboard navigation, and the colour scheme follows the
system's light or dark setting. Verdicts are always written in words, never shown by colour
alone.
