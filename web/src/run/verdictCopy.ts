// UI copy rules for verdicts (docs/specs/api-and-events.md "Copy rules", invariant 10).
import type {
  EventPayloads,
  PlanReady,
  ReasonCode,
  RunConditions,
  Scale,
  Verdict,
  VerdictKind,
  Warning,
} from '../types/api'

export const VERDICT_HEADLINE: Record<VerdictKind, string> = {
  REPRODUCED: 'Reproduced',
  REPRODUCED_AT_REDUCED_SCALE: 'Reproduced at reduced scale',
  NOT_REPRODUCED: 'Not reproduced under our conditions',
  INCONCLUSIVE: 'Inconclusive',
}

export const REASON_TEXT: Record<ReasonCode, string> = {
  BUDGET: 'The run reached its budget before enough measurements were collected.',
  CAPACITY: 'No GPU was available on any platform we tried, so the claim could not be tested.',
  MISSING_DATA: 'Data the experiment needs was not available.',
  RUNTIME_ERROR: 'The experiment failed while running.',
  UNDERSPECIFIED_CLAIM: 'The claim does not state a value we can test.',
  NOT_MEASURED: 'The experiment could not measure this claim.',
  INSUFFICIENT_SEEDS: 'Fewer seeds finished than the minimum needed for a decision.',
  HIGH_VARIANCE: 'The confidence interval is too wide to decide either way.',
  REDUCED_SCALE_ABSOLUTE:
    'An absolute number cannot be checked at reduced scale; the measured values are shown for reference.',
  REDUCED_SCALE_CONTRADICTS: 'The claimed trend was not observed at reduced scale.',
  IMPLAUSIBLE_GAP:
    "The measured value is several times off the paper's: more likely a broken experiment or a unit mismatch than a wrong claim.",
  BASELINE_MISMATCH:
    "Our baseline did not reach the paper's own baseline, so the run is not at the paper's level and its result cannot count against the claim.",
}

/** Plain-words titles of the warning codes (docs/specs/api-and-events.md, `warning` event). */
const WARNING_TITLE: Readonly<Record<string, string>> = {
  DISCOVERY_DEGRADED: 'Search partly unavailable',
  ABSOLUTE_AT_REDUCED_SCALE: 'Absolute value at reduced scale',
  PACKAGE_REJECTED: 'Package rejected',
  DATASET_UNAVAILABLE: 'Dataset unavailable',
  CLAIM_NOT_MEASURED: 'Claim not measured',
  PACKAGES_REQUESTED: 'Packages requested',
  REPO_SNAPSHOT_UNAVAILABLE: 'Repository files unavailable',
  REPO_TOO_LARGE: 'Repository too large to fetch',
  STAND_IN_FLAGGED: 'Stand-in refused',
  SETUP_DEVIATION: "Not the paper's setup",
  VISION_PAGE_SKIPPED: 'A page could not be read',
  JOB_PREEMPTED: 'GPU job preempted',
  NO_GPU_CAPACITY: 'No capacity for the experiment',
  BUNDLE_UNSIGNED: 'Proof Bundle not signed',
  BUNDLE_FAILED: 'No Proof Bundle',
}

/** The title of a warning; a code the UI does not know yet is shown as it came. */
export function warningTitle(code: string): string {
  return Object.hasOwn(WARNING_TITLE, code) ? WARNING_TITLE[code] : code
}

/** Warnings that change the experiment, listed as deviations from the paper's setup. */
const DEVIATION_WARNINGS = new Set([
  'ABSOLUTE_AT_REDUCED_SCALE',
  'PACKAGE_REJECTED',
  'DATASET_UNAVAILABLE',
  'REPO_SNAPSHOT_UNAVAILABLE',
  'REPO_TOO_LARGE',
  'STAND_IN_FLAGGED',
  'JOB_PREEMPTED',
  'DISCOVERY_DEGRADED',
])

export interface Condition {
  label: string
  value: string
}

export interface ConditionInputs {
  plan: PlanReady | null
  warnings: readonly Warning[]
  jobs: Readonly<Record<string, EventPayloads['job.status']>>
  /** Recorded by the server once the results are analyzed (RunView.conditions); preferred. */
  conditions?: RunConditions | null
  /** The claim the verdict is about: its own seeds when the run measured several claims. */
  claimId?: string
}

const NO_SCALE_FACTOR = 'Reduced scale (smaller than the paper; the scale factor is not reported for this run)'

function scaleText(scale: Scale, factor: RunConditions['scale'] | null): string {
  if (scale === 'FULL') return 'Full scale, as in the paper'
  if (!factor) return NO_SCALE_FACTOR
  const parts: string[] = []
  if (factor.fraction_of_data !== null && factor.fraction_of_data !== undefined) {
    parts.push(`${fmtNum(factor.fraction_of_data * 100)}% of the data`)
  }
  if (factor.epochs !== null && factor.epochs !== undefined) {
    parts.push(
      factor.paper_epochs !== null && factor.paper_epochs !== undefined
        ? `${factor.epochs} of the paper's ${factor.paper_epochs} epochs`
        : `${factor.epochs} epochs`,
    )
  }
  return parts.length ? `Reduced scale: ${parts.join(', ')}` : NO_SCALE_FACTOR
}

const SANDBOX_PLATFORM = 'sandbox' // app/execution/sandbox_jobs.py

function hardwareText(
  recorded: RunConditions['hardware'] | null,
  jobs: Readonly<Record<string, EventPayloads['job.status']>>,
): string {
  const parts: string[] = []
  if (recorded?.platform) {
    const preset = recorded.preset ? `, preset ${recorded.preset}` : ''
    const mode = recorded.preemptible === null ? '' : recorded.preemptible ? ' (preemptible)' : ' (on demand)'
    // the hybrid deploy runs experiments in Token Factory Sandboxes, CPU only (no GPU)
    const service = recorded.platform === SANDBOX_PLATFORM ? 'Nebius Token Factory Sandboxes' : `Nebius AI Cloud ${recorded.platform}`
    parts.push(`${service}${preset}${mode}`)
  }
  const gpu = recorded?.gpu
  if (gpu) parts.push(['cpu', 'unknown'].includes(gpu.toLowerCase()) ? 'CPU only, no GPU' : `GPU reported by the job: ${gpu}`)
  if (parts.length) return parts.join(' · ')
  const exec = Object.values(jobs).filter((j) => j.kind === 'exec')
  return exec.length ? `Nebius job ${exec.map((j) => j.name).join(', ')}` : 'Not reported for this run'
}

/** As app/verdict/limitations.py::matched_text: numbers were found in the code, choices are declared. */
function matchedText(setup: { kind: string }[]): string {
  const numbers = setup.filter((s) => s.kind === 'NUMBER').length
  const choices = setup.length - numbers
  const parts = []
  if (numbers) parts.push(`${numbers} number${numbers === 1 ? '' : 's'} found in the code`)
  if (choices) parts.push(`${choices} choice${choices === 1 ? '' : 's'} as declared by the Coder`)
  const head =
    setup.length === 1 ? 'the 1 listed condition matches the paper' : `all ${setup.length} listed conditions match the paper`
  return `${head} (${parts.join(', ')})`
}

/** The conditions a verdict was reached under: scale, seeds, hardware, code, deviations. */
export function verdictConditions(
  verdict: Verdict,
  { plan, warnings, jobs, conditions = null, claimId }: ConditionInputs,
): Condition[] {
  const p = plan?.plan
  const c = conditions
  const out: Condition[] = []
  const scale = c?.scale.scale ?? p?.scale
  out.push({ label: 'Scale', value: scale ? scaleText(scale, c?.scale ?? null) : 'not reported yet' })
  const used = (claimId !== undefined ? c?.seeds_used_by_claim[claimId] : undefined) ?? c?.seeds_used ?? p?.seeds
  const planned = c?.seeds_planned ?? p?.seeds
  // seeds the time target left out never ran: "of the planned" would read them as failures
  const notStarted = c?.seeds_not_started ?? []
  let seeds = ''
  if (used && planned && notStarted.length) {
    const ran = planned.filter((s) => !notStarted.includes(s))
    const of = used.join() !== ran.join() ? ` of the ${ran.length} that ran` : ''
    seeds = ` (seeds ${used.join(', ')}${of}; time limit reached: ${notStarted.length} more not started)`
  } else if (used && planned && used.join() !== planned.join()) {
    seeds = ` (seeds ${used.join(', ')} of the planned ${planned.join(', ')})`
  } else if (used) seeds = ` (seeds ${used.join(', ')})`
  out.push({ label: 'Seeds', value: `${verdict.n_seeds} seeds${seeds}` })
  out.push({ label: 'Hardware', value: hardwareText(c?.hardware ?? null, jobs) })
  // A-47: planned for a GPU, it ran its CPU plan: say why, next to the verdict (invariant 10)
  if (c?.gpu_fallback) out.push({ label: 'GPU', value: `Planned for one GPU; ${c.gpu_fallback}` })
  const mode = c?.code ?? p?.mode
  if (mode) {
    out.push({
      label: 'Code',
      value: mode === 'REPO_FIRST' ? "Adapted from the authors' repository" : 'Written from the paper (no official code used)',
    })
  }
  const setup = c?.setup ?? []
  if (setup.length) {
    const differ = setup.filter((s) => !s.matches_paper)
    out.push({
      label: "Paper's setup",
      value: differ.length
        ? `${differ.length} of ${setup.length} listed conditions differ from the paper: ` +
          differ.map((s) => `${s.name} (the paper uses ${s.paper}, this run used ${s.used})`).join(' · ')
        : matchedText(setup),
    })
  }
  const known = c !== null || p !== undefined
  const deviations = [
    ...(c?.deviations ?? p?.notes ?? []),
    ...warnings.filter((w) => DEVIATION_WARNINGS.has(w.code)).map((w) => w.message),
  ]
  const unique = [...new Set(deviations)]
  out.push({
    label: 'Deviations from the paper',
    // Without the plan we can't know: never claim there were none.
    value: unique.length
      ? unique.join(' · ')
      : setup.some((s) => !s.matches_paper)
        ? `see "Paper's setup"` // never "None recorded" next to a setup difference
        : known
          ? 'None recorded'
          : 'not available yet',
  })
  return out
}

/** Up to 4 significant digits, without trailing zeros. */
export function fmtNum(x: number): string {
  if (!Number.isFinite(x)) return String(x)
  return String(Number(x.toPrecision(4)))
}

const KIND_TOKEN = /\b(REPRODUCED_AT_REDUCED_SCALE|NOT_REPRODUCED|REPRODUCED|INCONCLUSIVE)\b/g

/** Server summaries may carry raw verdict kinds: always show them with the UI copy. */
export function verdictSummaryText(summary: string): string {
  return summary.replace(KIND_TOKEN, (kind) => VERDICT_HEADLINE[kind as VerdictKind])
}
