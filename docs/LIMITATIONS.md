# Limitations and known failure cases

Paper2Proof re-runs one experiment under stated conditions and reports what it measured. A
verdict is a statement about those conditions, never about the paper's honesty. This page lists
what the system cannot do, where it can be wrong, and how each case shows up in a run.

## What a verdict means

- **"Not reproduced under our conditions"** is only possible at full scale. It needs the whole
  confidence interval to fall outside the tolerance band: the larger of 2% of the claimed
  value, the claim's own reported std (×2) and a per-metric absolute tolerance
  (`config/verdict.yaml`). A run with fewer than 3 usable seeds is always **Inconclusive**.
- **A measured value several times off the paper's** (more than 3 times, either way, same
  sign) is **Inconclusive** (`IMPLAUSIBLE_GAP`) instead of "not reproduced" for an absolute
  claim. A wrong claim is typically off by a few to tens of percent; a gap that large more
  likely means broken training or a unit mismatch (a count against a rate, a fraction
  against a percent). A measured value far *better* than the paper's is still reported as
  Reproduced, and a mismatch whose numbers happen to be close is not caught; nor is a
  measured value of zero or of the opposite sign (an accuracy of 0, a negative R²).
- **Reduced scale.** Most papers train longer than a 30-minute budget allows, so most runs
  are at reduced scale. A relative claim ("A beats B by 1.7 points") can then be
  *Reproduced at reduced scale*. An absolute number ("94.1% top-1") cannot: it is
  **Inconclusive** (`REDUCED_SCALE_ABSOLUTE`), and the measured values are shown for reference.
  A trend that reverses at reduced scale is also Inconclusive, never "not reproduced".
- **The verdict is decided by a pure function** of the claim, the measurements and the config
  (`app/verdict/engine.py`). Language models write the explanation only. An explanation that
  names a different verdict is discarded and replaced by a template.

## Where a run can be wrong

- **Claim extraction.** The Reader model picks the claims and their numbers from the paper text.
  Each claim carries its source (table or section) so it can be checked. PDF-only papers rely on
  the Vision model to read tables, and a page it cannot read is skipped with a
  `VISION_PAGE_SKIPPED` warning.
- **The experiment is re-implemented.** The Coder writes the experiment from the paper (and the
  official repository when one is found). It can differ from the authors' setup. The planner's
  deviations (smaller model, fewer epochs, another dataset split) are listed under
  *Limitations* on every verdict.
- **Percent vs fraction.** The Coder declares the metric's unit (`fraction`, `percent` or
  `raw`), and the smoke run checks it. For values between 0 and 1 both units fit, so the
  declaration alone decides. Any conversion is printed as a "Scale:" line and recorded in
  `verdict.json` (`docs/specs/verdict-engine.md`).
- **Repository discovery.** The official repository is found in the paper, on Hugging Face, or by
  web search ranked by a model. A repository found by search is marked `official: false`, and
  discovery can degrade (`DISCOVERY_DEGRADED`) when a provider is unavailable.

## What the system does not support

- Input is an **arXiv ID only**: no URLs, no PDF uploads, no other preprint servers.
- **Datasets** come from Hugging Face, with a size cap. Packages pass an allowlist policy. A plan
  that needs anything else is rejected or runs without it, and says so.
- **CPU only in the live service.** Experiments run in a Nebius Token Factory sandbox with
  4 vCPUs and about 4 GB of memory, for at most 30 minutes, with $1 of inference. Anything that
  needs a GPU (deep-network training, LLM papers) runs only at a much reduced scale, or ends
  Inconclusive. The GPU path (one NVIDIA L40S per run) is implemented but not enabled: see
  [specs/hybrid-deploy.md](specs/hybrid-deploy.md). Every verdict lists the hardware it ran on.
- **Capacity.** With no capacity available for the experiment (the platform refuses, or the job
  is still queued after 20 minutes) the run ends **Inconclusive (capacity)**. A used-up project
  quota ends the run with a message naming the quota.

## Security boundaries

- Paper text, repository files and model output are untrusted. Generated code runs only in
  isolated jobs: no credentials, no internet during the experiment, allowlisted egress during
  provisioning, read-only root, capped resources. These layers reduce risk; they are not a proof
  that a malicious paper cannot misbehave inside its own sandbox.
- Runs are private by default. The public gallery is curated by hand, and a run can be taken down
  on request (`docs/OPERATIONS.md`).

## Assumptions still to verify on the platform

- Whether time spent waiting in PROVISIONING is billed. Cloud cost counts from submission until it
  is confirmed.
- Whether a job in `DELETING` can still write.
- Whether a quota error's `requested` field includes current usage. Only a quota with limit 0 is
  treated as final.

## Accessibility

The web UI uses semantic HTML: labelled form controls, tables with captions and header cells,
and live status regions for the run timeline. Charts and the architecture diagram have text
alternatives. Focus is visible for keyboard navigation, and the colour scheme follows the
system's light or dark setting. Verdicts are always written in words, never shown by colour
alone.
