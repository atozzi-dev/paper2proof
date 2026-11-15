import type { Claim, Verdict } from '../types/api'
import { fmtNum } from '../run/verdictCopy'

interface Figure {
  label: string
  value: string
  kind?: 'paper' | 'ours'
  /** Small words under the number (the baseline a gain is against). */
  note?: string
}

const signed = (x: number) => (x > 0 ? `+${fmtNum(x)}` : fmtNum(x))

/** The numbers the verdict compares, side by side: the paper's value (or claimed gain), our
 * mean, our 95% interval, and what the rule needs (docs/specs/verdict-engine.md). */
function verdictFigures(claim: Claim | null, v: Verdict): Figure[] {
  const out: Figure[] = []
  const { w, v: reported, rho, delta } = v.tolerance
  const lower = claim?.higher_is_better === false
  if (reported !== undefined) {
    const std = claim?.reported_std
    out.push({ label: 'The paper', value: `${fmtNum(reported)}${std ? ` ± ${fmtNum(std)}` : ''}`, kind: 'paper' })
  } else if (delta !== undefined) {
    const base = claim?.baseline_name ?? 'baseline'
    const just = claim !== null && (claim.claimed_delta === null || claim.claimed_delta === undefined)
    // "just better" reaches the engine as a delta of 0: never show it as a claimed gain of 0
    out.push(
      just
        ? { label: 'The paper', value: 'better', note: `than ${base}`, kind: 'paper' }
        : { label: 'The paper', value: signed(delta), note: `vs ${base}`, kind: 'paper' },
    )
  }
  if (v.mean !== null) out.push({ label: 'We measured', value: fmtNum(v.mean), kind: 'ours' })
  if (v.ci_low !== null && v.ci_high !== null) {
    out.push({ label: '95% interval', value: `${fmtNum(v.ci_low)} to ${fmtNum(v.ci_high)}` })
  }
  // When a reason decided the verdict (reduced scale, implausible gap...), the comparison is
  // only a threshold: never call it what was needed.
  const overridden = v.reason !== null && v.reason !== undefined
  if (w !== undefined && reported !== undefined) {
    // one-sided: a mean at most w short of the reported value counts
    out.push({ label: overridden ? 'Threshold' : 'Needed', value: lower ? `≤ ${fmtNum(reported + w)}` : `≥ ${fmtNum(reported - w)}` })
  } else if (rho !== undefined && delta !== undefined) {
    out.push({ label: overridden ? 'Threshold' : 'Gain needed', value: `${lower ? '≤' : '≥'} ${fmtNum(rho * delta)}` })
  }
  return out
}

export function VerdictFigures({ claim, verdict, size = 'lg' }: { claim: Claim | null; verdict: Verdict; size?: 'lg' | 'md' }) {
  const figures = verdictFigures(claim, verdict)
  if (figures.length === 0) return null
  return (
    <dl className={`figures figures-${size}`}>
      {figures.map((f) => (
        <div key={f.label} className="figure">
          <dt className="kicker">{f.label}</dt>
          <dd className={f.kind === 'ours' ? 'figure-ours' : undefined}>
            {f.kind === 'paper' ? <span className="mark">{f.value}</span> : f.value}
            {f.note && <span className="figure-note"> {f.note}</span>}
          </dd>
        </div>
      ))}
    </dl>
  )
}
