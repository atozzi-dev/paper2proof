import type { Claim, Verdict } from '../types/api'
import { fmtNum } from '../run/verdictCopy'

interface Figure {
  label: string
  value: string
  kind?: 'paper' | 'ours'
}

const signed = (x: number) => (x > 0 ? `+${fmtNum(x)}` : fmtNum(x))

/** The numbers the verdict compares, side by side: the paper's value (or claimed gain), our
 * mean, our 95% interval, and what the rule needs (docs/specs/verdict-engine.md). */
export function verdictFigures(claim: Claim | null, v: Verdict): Figure[] {
  const out: Figure[] = []
  const { w, v: reported, rho, delta } = v.tolerance
  const lower = claim?.higher_is_better === false
  if (reported !== undefined) {
    const std = claim?.reported_std
    out.push({ label: 'The paper', value: `${fmtNum(reported)}${std ? ` ± ${fmtNum(std)}` : ''}`, kind: 'paper' })
  } else if (delta !== undefined) {
    out.push({ label: 'The paper', value: `${signed(delta)} vs ${claim?.baseline_name ?? 'baseline'}`, kind: 'paper' })
  }
  if (v.mean !== null) out.push({ label: 'We measured', value: fmtNum(v.mean), kind: 'ours' })
  if (v.ci_low !== null && v.ci_high !== null) {
    out.push({ label: '95% interval', value: `${fmtNum(v.ci_low)} to ${fmtNum(v.ci_high)}` })
  }
  if (w !== undefined && reported !== undefined) {
    // one-sided: a mean at most w short of the reported value counts
    out.push({ label: 'Needed', value: lower ? `≤ ${fmtNum(reported + w)}` : `≥ ${fmtNum(reported - w)}` })
  } else if (rho !== undefined && delta !== undefined) {
    out.push({ label: 'Gain needed', value: `${lower ? '≤' : '≥'} ${fmtNum(rho * delta)}` })
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
          </dd>
        </div>
      ))}
    </dl>
  )
}
