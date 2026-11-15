import type { Claim, Verdict } from '../types/api'
import { fmtNum } from './verdictCopy'

/** A shaded stretch of the axis; a null end runs to the edge of the chart. */
export interface Zone {
  from: number | null
  to: number | null
  text: string
}

export interface ChartSpec {
  /** The zone the rule compares the interval with: the tolerance band, or the gain needed. */
  zone: Zone | null
  /** The paper's own value: the reported value, or the claimed gain. */
  paper: { at: number; text: string } | null
  /** A relative claim is a difference: zero is where the method stops winning. */
  zero: boolean
  label: string
}

const signed = (x: number) => (x > 0 ? `+${fmtNum(x)}` : fmtNum(x))

/** What to draw: ABSOLUTE claims compare the CI with the band around the reported value,
 * RELATIVE claims compare the CI of the per-seed difference with 0 and ρ·Δ. */
export function chartSpec(claim: Claim | null, v: Verdict): ChartSpec | null {
  if (v.ci_low === null || v.ci_high === null || v.mean === null) return null
  const ci = `95% CI ${fmtNum(v.ci_low)} to ${fmtNum(v.ci_high)} (mean ${fmtNum(v.mean)})`
  const { w, v: reported, rho, delta } = v.tolerance
  if (w !== undefined && reported !== undefined) {
    // One-sided (docs/specs/verdict-engine.md, ABSOLUTE): a mean at most w short of the
    // reported value counts, and so does any better one.
    const lower = claim?.higher_is_better === false
    const edge = lower ? reported + w : reported - w
    const at = lower ? '≤' : '≥'
    const metric = claim ? ` ${claim.metric}` : ''
    return {
      zone: { from: lower ? null : edge, to: lower ? edge : null, text: `${at} ${fmtNum(edge)} counts as reproduced` },
      paper: { at: reported, text: `paper ${fmtNum(reported)}` },
      zero: false,
      label: `${ci}; the paper reports ${fmtNum(reported)}${metric}, with a tolerance of ${fmtNum(w)}: a mean of ${fmtNum(edge)} or ${lower ? 'less' : 'more'} counts as reproduced`,
    }
  }
  if (rho !== undefined && delta !== undefined) {
    const needed = rho * delta
    const lower = claim?.higher_is_better === false // lower is better: at most rho × delta
    return {
      zone: {
        from: lower ? null : needed,
        to: lower ? needed : null,
        text: `${lower ? '≤' : '≥'} ${fmtNum(needed)} needed`,
      },
      paper: { at: delta, text: `paper ${signed(delta)}` },
      zero: true,
      label: `${ci} of the per-seed difference vs ${claim?.baseline_name ?? 'the baseline'}; needs a gain of at least ${fmtNum(needed)} (ρ ${fmtNum(rho)} × claimed ${fmtNum(delta)})`,
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
  const out: number[] = []
  for (let t = Math.ceil(min / step) * step; t <= max + step * 1e-9; t += step) {
    out.push(Number(t.toPrecision(12)))
  }
  return out
}
