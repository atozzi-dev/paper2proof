// Short sums of a run for the report pages: built from the deterministic verdicts and the
// recorded conditions only, never from model text (invariant 2).
import type { PlanReady, RunConditions, VerdictKind } from '../types/api'
import { VERDICT_HEADLINE } from './verdictCopy'

/** Most cautionary first: the order every list of verdicts on the site uses. */
export const CAUTION_ORDER: readonly VerdictKind[] = [
  'NOT_REPRODUCED',
  'INCONCLUSIVE',
  'REPRODUCED_AT_REDUCED_SCALE',
  'REPRODUCED',
]

export interface VerdictCount {
  kind: VerdictKind
  count: number
}

const ENTRY = /(\d+) (REPRODUCED_AT_REDUCED_SCALE|NOT_REPRODUCED|REPRODUCED|INCONCLUSIVE)\b/g
const MORE = /\+(\d+) more/

/** The server's one-line summary ("1 REPRODUCED · 1 NOT_REPRODUCED · +2 more", see
 * app/verdict/summary.py) as counts per kind, plus the claims it folded into "+n more". */
export function parseSummary(summary: string | null): { counts: VerdictCount[]; more: number } {
  if (!summary) return { counts: [], more: 0 }
  const found = new Map<VerdictKind, number>()
  for (const m of summary.matchAll(ENTRY)) found.set(m[2] as VerdictKind, Number(m[1]))
  const counts = CAUTION_ORDER.filter((k) => found.has(k)).map((kind) => ({ kind, count: found.get(kind) ?? 0 }))
  return { counts, more: Number(MORE.exec(summary)?.[1] ?? 0) }
}

/** A verdict's mandated copy, after its count when there is one ("2 not reproduced under our conditions"). */
export function pillText(kind: VerdictKind, count?: number): string {
  const text = VERDICT_HEADLINE[kind]
  return count === undefined ? text : `${count} ${text.charAt(0).toLowerCase()}${text.slice(1)}`
}

const PREDICATE: Record<VerdictKind, string> = {
  NOT_REPRODUCED: 'not reproduced under our conditions',
  INCONCLUSIVE: 'inconclusive',
  REPRODUCED_AT_REDUCED_SCALE: 'reproduced at reduced scale',
  REPRODUCED: 'reproduced',
}

/** One sentence on the verdicts of a run, the most cautionary first. */
export function inShort(kinds: readonly VerdictKind[]): string {
  if (kinds.length === 1) return `The claim tested was ${PREDICATE[kinds[0]]}.`
  const parts = CAUTION_ORDER.map((kind) => ({ kind, n: kinds.filter((k) => k === kind).length }))
    .filter((p) => p.n > 0)
    .map((p) => `${p.n} ${p.n === 1 ? 'was' : 'were'} ${PREDICATE[p.kind]}`)
  const listed = parts.length > 1 ? `${parts.slice(0, -1).join(', ')} and ${parts.at(-1)}` : parts[0]
  return `Of the ${kinds.length} claims tested, ${listed}.`
}

const SANDBOX_PLATFORM = 'sandbox' // app/execution/sandbox_jobs.py

/** The conditions of a run in a few words each: scale, setup, seeds, hardware, code. The
 * recorded conditions win over the plan; what neither knows is left out, never guessed. */
export function conditionChips(c: RunConditions | null, ready: PlanReady | null): string[] {
  const p = ready?.plan
  const out: string[] = []
  const scale = c?.scale.scale ?? p?.scale
  if (scale) out.push(scale === 'FULL' ? 'full scale' : 'reduced scale')
  const setup = c?.setup ?? []
  if (setup.length) {
    const differ = setup.filter((s) => !s.matches_paper).length
    out.push(
      differ
        ? `${differ} of ${setup.length} setup conditions differ`
        : `${setup.length} of ${setup.length} setup conditions match`,
    )
  }
  const seeds = c?.seeds_used ?? p?.seeds
  if (seeds) out.push(`${seeds.length} seed${seeds.length === 1 ? '' : 's'}`)
  const hw = c?.hardware
  if (hw?.platform === SANDBOX_PLATFORM) out.push('CPU sandbox, no network')
  else if (hw?.platform) {
    const gpu = hw.gpu ? (['cpu', 'unknown'].includes(hw.gpu.toLowerCase()) ? 'CPU only' : hw.gpu) : null // as hardwareText
    out.push(gpu ? `${hw.platform}, ${gpu}` : hw.platform)
  }
  const mode = c?.code ?? p?.mode
  if (mode) out.push(mode === 'REPO_FIRST' ? "code adapted from the authors' repository" : 'code written from the paper')
  return out
}
