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
  let seeds = ''
  if (used && planned && used.join() !== planned.join()) {
    seeds = ` (seeds ${used.join(', ')} of the planned ${planned.join(', ')})`
  } else if (used) seeds = ` (seeds ${used.join(', ')})`
  out.push({ label: 'Seeds', value: `${verdict.n_seeds} seeds${seeds}` })
  out.push({ label: 'Hardware', value: hardwareText(c?.hardware ?? null, jobs) })
  const mode = c?.code ?? p?.mode
  if (mode) {
    out.push({
      label: 'Code',
      value: mode === 'REPO_FIRST' ? "Adapted from the authors' repository" : 'Written from the paper (no official code used)',
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
    value: unique.length ? unique.join(' · ') : known ? 'None recorded' : 'not available yet',
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
