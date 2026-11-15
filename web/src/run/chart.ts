import type { Claim, Verdict } from '../types/api'
import { fmtNum } from './verdictCopy'

/** A shaded stretch of the axis; a null end runs to the edge of the chart. */
export interface Zone {
  from: number | null
  to: number | null
  text: string
}

export interface ChartSpec {
  /** The zone the rule compares our mean with: from the paper's value minus the tolerance,
   * or from the gain needed. */
  zone: Zone | null
  /** The paper's own value: the reported value, or the claimed gain (none for "just better"). */
  paper: { at: number; text: string } | null
  /** A relative claim is a difference: zero is where the method stops winning. */
  zero: boolean
  label: string
}

const signed = (x: number) => (x > 0 ? `+${fmtNum(x)}` : fmtNum(x))

/** What to draw, as docs/specs/verdict-engine.md decides (numbers in the metric's own direction):
 * ABSOLUTE compares our mean with the reported value minus the tolerance (one-sided);
 * RELATIVE needs the whole interval of the per-seed difference past 0 and a mean of ρ·Δ or more.
 * When the verdict carries a reason, the reason decided it: the zone is only a threshold then. */
export function chartSpec(claim: Claim | null, v: Verdict): ChartSpec | null {
  if (v.ci_low === null || v.ci_high === null || v.mean === null) return null
  const ci = `95% CI ${fmtNum(v.ci_low)} to ${fmtNum(v.ci_high)} (mean ${fmtNum(v.mean)})`
  const { w, v: reported, rho, delta } = v.tolerance
  const lower = claim?.higher_is_better === false
  const at = lower ? '≤' : '≥'
  const overridden = v.reason !== null && v.reason !== undefined
  const counts = v.reason === 'REDUCED_SCALE_ABSOLUTE' ? 'would count at full scale' : overridden ? 'is the threshold' : null
  if (w !== undefined && reported !== undefined) {
    const edge = lower ? reported + w : reported - w
    const metric = claim ? ` ${claim.metric}` : ''
    return {
      zone: { from: lower ? null : edge, to: lower ? edge : null, text: `${at} ${fmtNum(edge)} ${counts ?? 'counts as reproduced'}` },
      paper: { at: reported, text: `paper ${fmtNum(reported)}` },
      zero: false,
      label: `${ci}; the paper reports ${fmtNum(reported)}${metric}, with a tolerance of ${fmtNum(w)}: a mean of ${fmtNum(edge)} or ${lower ? 'less' : 'more'} counts as reproduced at full scale`,
    }
  }
  if (rho !== undefined && delta !== undefined) {
    const needed = rho * delta
    // The whole interval must clear zero too, so the zone never starts on the wrong side of it.
    const edge = lower ? Math.min(0, needed) : Math.max(0, needed)
    const just = claim !== null && (claim.claimed_delta === null || claim.claimed_delta === undefined)
    const base = claim?.baseline_name ?? 'the baseline'
    return {
      zone: { from: lower ? null : edge, to: lower ? edge : null, text: `${at} ${fmtNum(needed)} ${overridden ? 'was the threshold' : 'needed'}` },
      paper: just ? null : { at: delta, text: `paper ${signed(delta)}` },
      zero: true,
      label:
        `${ci} of the per-seed difference vs ${base}; ` +
        (lower
          ? `needs a mean change of at most ${fmtNum(needed)}, with the whole interval below 0`
          : `needs a mean gain of at least ${fmtNum(needed)}, with the whole interval above 0`) +
        (just ? ' (the paper claims only that it is better)' : ` (ρ ${fmtNum(rho)} × claimed ${fmtNum(delta)})`),
    }
  }
  return { zone: null, paper: null, zero: false, label: ci }
}

/** About `count` round tick values covering [min, max] (steps of 1, 2 or 5 × 10^k). */
export function ticks(min: number, max: number, count = 6): number[] {
  const span = max - min
  if (!(span > 0) || !Number.isFinite(span)) return [min]
  const raw = span / count
  const pow = 10 ** Math.floor(Math.log10(raw))
  const step = [1, 2, 5, 10].map((m) => m * pow).find((s) => s >= raw) ?? 10 * pow
  const first = Math.ceil(min / step)
  const out: number[] = []
  // by index, never by adding the step to a running total: a step below the precision of
  // the values would never move it (and stop at 50 whatever happens)
  for (let i = 0; i < 50; i++) {
    const t = Number(((first + i) * step).toPrecision(12))
    if (t > max + step * 1e-9) break
    if (out.length === 0 || t !== out[out.length - 1]) out.push(t)
  }
  return out
}
