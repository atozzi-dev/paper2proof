import type { Claim, Verdict } from '../types/api'
import { fmtNum } from './verdictCopy'

interface Marker {
  at: number
  label: string
}

interface ChartSpec {
  band: [number, number] | null
  markers: Marker[]
  label: string
}

/** What to draw: ABSOLUTE claims compare the CI with the band around the reported value,
 * RELATIVE claims compare the CI of the per-seed difference with 0 and ρ·Δ. */
export function chartSpec(claim: Claim | null, v: Verdict): ChartSpec | null {
  if (v.ci_low === null || v.ci_high === null || v.mean === null) return null
  const ci = `95% CI ${fmtNum(v.ci_low)} to ${fmtNum(v.ci_high)} (mean ${fmtNum(v.mean)})`
  const { w, v: reported, rho, delta } = v.tolerance
  if (w !== undefined && reported !== undefined) {
    const band: [number, number] = [reported - w, reported + w]
    return {
      band,
      markers: [{ at: reported, label: 'paper' }],
      label: `${ci}; tolerance band ${fmtNum(band[0])} to ${fmtNum(band[1])} around the reported ${fmtNum(reported)}${claim ? ` ${claim.metric}` : ''}`,
    }
  }
  if (rho !== undefined && delta !== undefined) {
    const needed = rho * delta
    return {
      band: null,
      markers: [
        { at: 0, label: '0' },
        { at: needed, label: 'ρ·Δ' },
      ],
      label: `${ci} of the per-seed difference vs ${claim?.baseline_name ?? 'the baseline'}; needs a gain of at least ${fmtNum(needed)} (ρ ${fmtNum(rho)} × claimed ${fmtNum(delta)})`,
    }
  }
  return { band: null, markers: [], label: ci }
}
