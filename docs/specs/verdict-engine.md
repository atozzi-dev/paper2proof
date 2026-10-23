# Spec — Verdict engine

Source: architecture §3.3 (v0.2). This spec is normative.

## Purpose

`decide(claim, measurements, cfg) -> Verdict` turns measured numbers into one of four verdicts. It is a **pure function**: no I/O, no randomness, no LLM. The asymmetry is deliberate: it is easy to say "inconclusive" and hard to say "not reproduced".

## Types (in `app/domain/models.py`)

```python
class ClaimType(StrEnum):
    ABSOLUTE = "ABSOLUTE"   # "method reaches 94.1% top-1 on CIFAR-10"
    RELATIVE = "RELATIVE"   # "method beats ResNet-18 by 1.7 points" / "beats ResNet-18"

class Scale(StrEnum):
    FULL = "FULL"
    REDUCED = "REDUCED"

class VerdictKind(StrEnum):
    REPRODUCED = "REPRODUCED"
    REPRODUCED_AT_REDUCED_SCALE = "REPRODUCED_AT_REDUCED_SCALE"
    NOT_REPRODUCED = "NOT_REPRODUCED"
    INCONCLUSIVE = "INCONCLUSIVE"

class ReasonCode(StrEnum):
    BUDGET = "BUDGET"
    CAPACITY = "CAPACITY"
    MISSING_DATA = "MISSING_DATA"
    RUNTIME_ERROR = "RUNTIME_ERROR"
    UNDERSPECIFIED_CLAIM = "UNDERSPECIFIED_CLAIM"
    INSUFFICIENT_SEEDS = "INSUFFICIENT_SEEDS"
    HIGH_VARIANCE = "HIGH_VARIANCE"
    REDUCED_SCALE_ABSOLUTE = "REDUCED_SCALE_ABSOLUTE"
    REDUCED_SCALE_CONTRADICTS = "REDUCED_SCALE_CONTRADICTS"
    NOT_MEASURED = "NOT_MEASURED"  # set by VERDICT, never by decide()

class Claim(BaseModel):
    id: str
    text: str
    claim_type: ClaimType
    metric: str
    higher_is_better: bool
    dataset: str
    setting: str
    reported_value: float | None        # required for ABSOLUTE
    reported_std: float | None = None
    baseline_name: str | None = None    # required for RELATIVE
    claimed_delta: float | None = None  # RELATIVE; None means "just better" (treated as 0)
    source_ref: str                     # "Table 2", "Sec 4.1", "Fig 3"

class Measurements(BaseModel):
    scale: Scale
    per_seed: list[float]
    baseline_per_seed: list[float] | None = None   # same length and seed order as per_seed

class VerdictConfig(BaseModel):
    tol_rel: float = 0.02
    tol_abs: dict[str, float] = {}      # per metric name
    default_tol_abs: float = 0.0
    rho: float = 0.5
    min_seeds: int = 3
    confidence: float = 0.95

class Verdict(BaseModel):
    kind: VerdictKind
    reason: ReasonCode | None           # set iff kind == INCONCLUSIVE
    n_seeds: int
    mean: float | None
    std: float | None
    ci_low: float | None
    ci_high: float | None
    tolerance: dict[str, float]         # {"w": ..., "v": ...} or {"rho": ..., "delta": ...}
```

Config is loaded from `config/verdict.yaml`; its SHA-256 is stored with every verdict.

## Algorithm

Normalize direction first: if `higher_is_better` is false, negate `reported_value`, `claimed_delta`, every measurement and every baseline measurement. After that, "higher is better" everywhere.

```
if claim.claim_type == ABSOLUTE and claim.reported_value is None: return INCONCLUSIVE(UNDERSPECIFIED_CLAIM)
if claim.claim_type == RELATIVE and m.baseline_per_seed is None:  return INCONCLUSIVE(UNDERSPECIFIED_CLAIM)
if len(m.per_seed) < cfg.min_seeds:                               return INCONCLUSIVE(INSUFFICIENT_SEEDS)
if any value is not finite or |value| > 1e100 (MAX_METRIC_ABS): return INCONCLUSIVE(RUNTIME_ERROR)

k      = len(values)
mean   = average(values)
s      = sample std (ddof=1)
half   = t.ppf((1 + cfg.confidence) / 2, df=k-1) * s / sqrt(k)
ci     = (mean - half, mean + half)
```

`values` is `per_seed` for ABSOLUTE and `d_i = per_seed[i] - baseline_per_seed[i]` for RELATIVE.

### ABSOLUTE

```
v = reported_value
w = max(2 * (reported_std or 0), cfg.tol_rel * abs(v), cfg.tol_abs.get(metric, cfg.default_tol_abs))
if scale == REDUCED:            return INCONCLUSIVE(REDUCED_SCALE_ABSOLUTE)   # numbers still returned
if mean >= v - w:               return REPRODUCED
if ci_high < v - w:             return NOT_REPRODUCED
return INCONCLUSIVE(HIGH_VARIANCE)
```

### RELATIVE

```
delta = claimed_delta or 0.0
positive = ci_low > 0 and mean >= cfg.rho * delta
if positive:  return REPRODUCED if scale == FULL else REPRODUCED_AT_REDUCED_SCALE
if ci_high <= 0:
    return NOT_REPRODUCED if scale == FULL else INCONCLUSIVE(REDUCED_SCALE_CONTRADICTS)
return INCONCLUSIVE(HIGH_VARIANCE)
```

Use `scipy.stats.t.ppf`. If `s == 0`, then `half = 0` (deterministic runs are allowed).

`mean`, `std`, `ci_low` and `ci_high` are reported back in the **original direction** (un-negate before returning).

## Scale alignment before `decide()` (`app/verdict/scale.py`)

Papers often state accuracy-like metrics in percent ("94.1%", "by 1.7 points") while scripts
write fractions (0.941). The factor is decided from two declared facts, never inferred from the
measured values:
- **the metric's unit**: each of the Coder's `finish` measures declares a `unit` (`fraction`,
  `percent` or `raw`). SMOKE_TESTING refuses it when the last smoke run's values for that
  measure's keys do not fit (`fraction` in [0, 1], `percent` in [0, 100]; `raw` is unchecked)
  and stores it, per claim, in `smoke_result.json`;
- **whether the claim is in percent**: the claim's own number (`reported_value`, or
  `claimed_delta` for RELATIVE, compared without its sign) appears in its text followed by `%`
  (LaTeX `\%` too), "percent", "per cent", "points" or "pp", optionally with a `± std` in
  between (`94.1 ± 0.2%`, `94.1 \pm 0.2\%`). A `%` attached to any other number does not
  count.

VERDICT calls `align_to_claim(claim, measurements, unit)` before `decide()`:

| Unit | Claim | Factor |
|---|---|---|
| `fraction` | in percent | ×100 (both arms of a RELATIVE claim) |
| `percent` | ABSOLUTE, higher is better, not in percent, number in [0, 1] | ×0.01 |
| anything else (incl. `raw`, or no unit in a run from before this field) | any | ×1 |

- A bare RELATIVE delta measured in percent is never divided: "improves by 0.5" may be in
  points.
- A lower-is-better metric is never divided: shrinking an error or a loss makes it look
  better, and the one-sided ABSOLUTE comparison would turn a failure into REPRODUCED.
- The decided values (both arms) are checked against the declared unit again, since the smoke
  run was only a sample. If they don't fit, the factor is ×1 and the `Scale:` line says so.

The conversion is deterministic (invariant 2 holds). It gets its own `Scale:` line in the
verdict's Limitations, and that line is written by code: one of the `ScaleNote` constants.
`verdict.json` keeps, for each claim, `measurements` (aligned), `measured` (as the script
wrote them) and `scale_factor`.

A run decides every claim of its plan. A claim the experiment does not measure (the Coder
declared it unmeasured at `finish`) is decided as if it had no seeds, and VERDICT replaces
INSUFFICIENT_SEEDS with `NOT_MEASURED`: still INCONCLUSIVE, the reason set by code.

Known limit: for values in [0, 1], both units pass the smoke check, so the Coder's declaration
alone picks ×100 or ×1. A wrong declaration there can mislead in either direction: a 0.9%
success rate declared as a fraction becomes 90. That gives the Coder no power it lacks
already, since it writes the script that produces the values. The declared unit is recorded
in `smoke_result.json` and the Proof Bundle, and the factor in `verdict.json` and the
`Scale:` line, so a reviewer can always see it.

## Required tests (`tests/verdict/test_engine.py`)

Table-driven examples, all with `cfg` defaults unless stated:

| # | Claim | Measurements | Expected |
|---|---|---|---|
| 1 | ABSOLUTE v=0.941, metric `top1_accuracy` (tol_abs 0.005) | FULL [0.940, 0.942, 0.939] | REPRODUCED |
| 2 | ABSOLUTE v=0.941 | FULL [0.95, 0.96, 0.955] (better than paper) | REPRODUCED |
| 3 | ABSOLUTE v=0.941 | FULL [0.85, 0.851, 0.849] | NOT_REPRODUCED |
| 4 | ABSOLUTE v=0.941 | FULL [0.80, 0.95, 0.88] | INCONCLUSIVE/HIGH_VARIANCE |
| 5 | ABSOLUTE v=0.941 | REDUCED [0.85, 0.85, 0.85] | INCONCLUSIVE/REDUCED_SCALE_ABSOLUTE |
| 6 | RELATIVE delta=0.017 | FULL method [0.92,0.921,0.919], base [0.90,0.901,0.902] | REPRODUCED |
| 7 | RELATIVE delta=0.017 | REDUCED same as #6 | REPRODUCED_AT_REDUCED_SCALE |
| 8 | RELATIVE delta=0.017 | FULL method == base - 0.01 each seed | NOT_REPRODUCED |
| 9 | RELATIVE delta=0.017 | REDUCED method == base - 0.01 each seed | INCONCLUSIVE/REDUCED_SCALE_CONTRADICTS |
| 10 | ABSOLUTE loss v=0.30, higher_is_better=False | FULL [0.29, 0.30, 0.31] | REPRODUCED |
| 11 | any | 2 seeds | INCONCLUSIVE/INSUFFICIENT_SEEDS |
| 12 | ABSOLUTE v=None | any | INCONCLUSIVE/UNDERSPECIFIED_CLAIM |
| 13 | any | contains NaN | INCONCLUSIVE/RUNTIME_ERROR |

Property-based tests with Hypothesis (`tests/verdict/test_engine_properties.py`):

- **P1 (no false accusation at reduced scale):** for any claim and any REDUCED measurements, `kind != NOT_REPRODUCED`.
- **P2 (variance monotonicity):** if `decide(c, m)` is INCONCLUSIVE/HIGH_VARIANCE, then spreading the same values further apart around the same mean never yields NOT_REPRODUCED.
- **P3 (better is never worse):** for ABSOLUTE FULL, adding a constant `ε > 0` to every measurement never changes REPRODUCED into anything else.
- **P4 (direction symmetry):** negating every measurement, every baseline measurement, `reported_value` and `claimed_delta`, and flipping `higher_is_better`, yields the same `kind`.
- **P5 (purity):** two calls with equal inputs return equal outputs.
- **P6:** `reason is not None` if and only if `kind == INCONCLUSIVE`.
