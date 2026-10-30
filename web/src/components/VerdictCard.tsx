import { useId } from 'react'
import type { Claim, Verdict } from '../types/api'
import { REASON_TEXT, VERDICT_HEADLINE, fmtNum, verdictConditions, type ConditionInputs } from '../run/verdictCopy'
import { CiBandChart } from './CiBandChart'
import { SafeMarkdown } from './SafeMarkdown'
import { VerdictIcon } from './VerdictIcon'

interface Props extends ConditionInputs {
  /** Null when the claim is not loaded (yet): the verdict is still shown, never dropped. */
  claim: Claim | null
  claimId?: string
  verdict: Verdict
  explanationMd: string
}

/** The numbers the verdict compares, side by side (ABSOLUTE: the band; RELATIVE: the gain). */
function figures(claim: Claim | null, v: Verdict): { label: string; value: string; emphasis?: boolean }[] {
  const out: { label: string; value: string; emphasis?: boolean }[] = []
  const { w, v: reported, rho, delta } = v.tolerance
  if (reported !== undefined) {
    const std = claim?.reported_std
    out.push({ label: 'Paper says', value: `${fmtNum(reported)}${std ? ` ± ${fmtNum(std)}` : ''}` })
  } else if (delta !== undefined) {
    const signed = delta > 0 ? `+${fmtNum(delta)}` : fmtNum(delta)
    out.push({ label: 'Paper says', value: `${signed} vs ${claim?.baseline_name ?? 'baseline'}` })
  }
  if (v.mean !== null) out.push({ label: 'We measured', value: fmtNum(v.mean), emphasis: true })
  if (v.ci_low !== null && v.ci_high !== null) {
    out.push({ label: '95% interval', value: `${fmtNum(v.ci_low)} – ${fmtNum(v.ci_high)}` })
  }
  if (w !== undefined && reported !== undefined) {
    out.push({ label: 'Tolerance band', value: `${fmtNum(reported - w)} – ${fmtNum(reported + w)}` })
  } else if (rho !== undefined && delta !== undefined) {
    const at = claim?.higher_is_better === false ? '≤' : '≥' // lower is better: at most rho × delta
    out.push({ label: 'Gain needed', value: `${at} ${fmtNum(rho * delta)}` })
  }
  return out
}

export function VerdictCard({ claim, claimId, verdict, explanationMd, ...inputs }: Props) {
  const headingId = useId()
  const conditions = verdictConditions(verdict, { ...inputs, claimId: claim?.id ?? claimId })
  const kind = verdict.kind.toLowerCase()
  const numbers = figures(claim, verdict)
  return (
    <article className={`verdict verdict-${kind}`} aria-labelledby={headingId}>
      <div className="verdict-top">
        <div className="verdict-main">
          <span className={`verdict-badge verdict-badge-${kind}`}>
            <VerdictIcon kind={verdict.kind} size={16} />
            Verdict{claim ? ` · claim ${claim.id}` : claimId ? ` · claim ${claimId}` : ''}
          </span>
          <h3 id={headingId} className="verdict-headline">
            {VERDICT_HEADLINE[verdict.kind]}
          </h3>
          {verdict.reason && <p className="verdict-reason">{REASON_TEXT[verdict.reason]}</p>}
          {claim ? (
            <>
              <blockquote className="verdict-claim">&ldquo;{claim.text}&rdquo;</blockquote>
              <p className="muted small">
                {claim.source_ref} · {claim.dataset} · {claim.setting}
              </p>
            </>
          ) : (
            <p className="muted">Claim {claimId} (details not loaded yet)</p>
          )}
        </div>
        {numbers.length > 0 && (
          <dl className="verdict-figures">
            {numbers.map((n) => (
              <div key={n.label} className={['figure', n.emphasis && 'emphasis', n.value.length > 9 && 'wide'].filter(Boolean).join(' ')}>
                <dt>{n.label}</dt>
                <dd className="mono">{n.value}</dd>
              </div>
            ))}
          </dl>
        )}
      </div>
      {verdict.mean !== null && (
        <p className="muted small">
          Measured {claim?.metric ?? 'value'}: mean {fmtNum(verdict.mean)}
          {verdict.std !== null && ` ± ${fmtNum(verdict.std)} (std)`} over {verdict.n_seeds} seeds
        </p>
      )}
      <CiBandChart claim={claim} verdict={verdict} />
      <div className="verdict-bottom">
        {explanationMd && (
          <details className="explanation" open>
            <summary>Explanation (written by the Judge model; the verdict above is computed by code)</summary>
            <SafeMarkdown text={explanationMd} />
          </details>
        )}
        <div className="verdict-conditions">
          <h4>Conditions</h4>
          <ul className="conditions" aria-label="Conditions">
            {conditions.map((c) => (
              <li key={c.label}>
                <strong>{c.label}:</strong> {c.value}
              </li>
            ))}
          </ul>
        </div>
      </div>
    </article>
  )
}
