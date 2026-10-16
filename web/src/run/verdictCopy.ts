// UI copy rules for verdicts (docs/specs/api-and-events.md "Copy rules", invariant 10).
import type { EventPayloads, PlanReady, ReasonCode, Verdict, VerdictKind, Warning } from '../types/api'

export const VERDICT_HEADLINE: Record<VerdictKind, string> = {
  REPRODUCED: 'Reproduced',
  REPRODUCED_AT_REDUCED_SCALE: 'Reproduced at reduced scale',
  NOT_REPRODUCED: 'Not reproduced under our conditions',
  INCONCLUSIVE: 'Inconclusive',
}

export const REASON_TEXT: Record<ReasonCode, string> = {
  BUDGET: 'The run reached its budget before enough measurements were collected.',
  CAPACITY: 'No GPU capacity was available in time.',
  MISSING_DATA: 'Data the experiment needs was not available.',
  RUNTIME_ERROR: 'The experiment failed while running.',
  UNDERSPECIFIED_CLAIM: 'The claim does not state a value we can test.',
  INSUFFICIENT_SEEDS: 'Fewer seeds finished than the minimum needed for a decision.',
  HIGH_VARIANCE: 'The confidence interval is too wide to decide either way.',
  REDUCED_SCALE_ABSOLUTE:
    'An absolute number cannot be checked at reduced scale; the measured values are shown for reference.',
  REDUCED_SCALE_CONTRADICTS: 'The claimed trend was not observed at reduced scale.',
}

/** Warnings that change the experiment, listed as deviations from the paper's setup. */
const DEVIATION_WARNINGS = new Set([
  'ABSOLUTE_AT_REDUCED_SCALE',
  'PACKAGE_REJECTED',
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
}

/** The conditions a verdict was reached under: scale, seeds, hardware, code, deviations. */
export function verdictConditions(verdict: Verdict, { plan, warnings, jobs }: ConditionInputs): Condition[] {
  const p = plan?.plan
  const out: Condition[] = []
  if (!p) out.push({ label: 'Scale', value: 'not reported yet' })
  else {
    out.push({
      label: 'Scale',
      value:
        p.scale === 'FULL'
          ? 'Full scale, as in the paper'
          : 'Reduced scale (smaller than the paper; the scale factor is not reported for this run)',
    })
  }
  const seeds = p ? ` (seeds ${p.seeds.join(', ')})` : ''
  out.push({ label: 'Seeds', value: `${verdict.n_seeds} seeds${seeds}` })
  const exec = Object.values(jobs).filter((j) => j.kind === 'exec')
  out.push({
    label: 'Hardware',
    value: exec.length
      ? `Nebius AI Cloud GPU job ${exec.map((j) => j.name).join(', ')}`
      : 'Not reported for this run',
  })
  if (p) {
    out.push({
      label: 'Code',
      value:
        p.mode === 'REPO_FIRST'
          ? "Adapted from the authors' repository"
          : 'Written from the paper (no official code used)',
    })
  }
  const deviations = [
    ...(p?.notes ?? []),
    ...warnings.filter((w) => DEVIATION_WARNINGS.has(w.code)).map((w) => w.message),
  ]
  const unique = [...new Set(deviations)]
  out.push({
    label: 'Deviations from the paper',
    // Without the plan we can't know: never claim there were none.
    value: unique.length ? unique.join(' · ') : p ? 'None recorded' : 'not available yet',
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
