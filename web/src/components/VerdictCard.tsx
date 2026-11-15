import { useId } from 'react'
import type { Claim, Verdict } from '../types/api'
import { REASON_TEXT, fmtNum, verdictConditions, type ConditionInputs } from '../run/verdictCopy'
import { CiBandChart } from './CiBandChart'
import { ClaimQuote } from './ClaimQuote'
import { SafeMarkdown } from './SafeMarkdown'
import { VerdictFigures } from './VerdictFigures'
import { VerdictPill } from './VerdictPill'

interface Props extends ConditionInputs {
  /** Null when the claim is not loaded (yet): the verdict is still shown, never dropped. */
  claim: Claim | null
  claimId?: string
  verdict: Verdict
  explanationMd: string
}

/** One claim's verdict, as a short article: the claim, the verdict (its heading), the numbers,
 * the chart, and the conditions it was reached under beside it (invariant 10). */
export function VerdictCard({ claim, claimId, verdict, explanationMd, ...inputs }: Props) {
  const headingId = useId()
  const id = claim?.id ?? claimId
  const conditions = verdictConditions(verdict, { ...inputs, claimId: id })
  return (
    <article className="verdict-article" aria-labelledby={headingId}>
      <div className="section-head verdict-head">
        <h2 className="verdict-claim-no">Claim {id}</h2>
        {claim && (
          <p className="muted small">
            {claim.claim_type === 'ABSOLUTE' ? 'absolute' : 'relative'} · {claim.metric} · {claim.source_ref}
          </p>
        )}
      </div>
      <div className="report-columns">
        <div className="report-main">
          {claim ? (
            <>
              <ClaimQuote claim={claim} />
              <p className="muted small">
                {claim.dataset}
                {claim.setting && ` · ${claim.setting}`}
              </p>
            </>
          ) : (
            <p className="muted">Claim {claimId} (details not loaded yet)</p>
          )}
          <div className="verdict-line">
            <VerdictPill id={headingId} kind={verdict.kind} as="h3" size="lg" />
          </div>
          {verdict.reason && <p className="verdict-reason">{REASON_TEXT[verdict.reason]}</p>}
          <VerdictFigures claim={claim} verdict={verdict} />
          {verdict.mean !== null && (
            <p className="muted small">
              Measured {claim?.metric ?? 'value'}: mean {fmtNum(verdict.mean)}
              {verdict.std !== null && ` ± ${fmtNum(verdict.std)} (std)`} over {verdict.n_seeds} seeds
            </p>
          )}
          <CiBandChart claim={claim} verdict={verdict} />
          {explanationMd && (
            <details className="explanation">
              <summary>The Judge model&rsquo;s explanation (the verdict above is computed by code)</summary>
              <SafeMarkdown text={explanationMd} />
            </details>
          )}
        </div>
        <aside className="report-aside" aria-label={`Conditions of claim ${id ?? ''}`.trim()}>
          <p className="kicker">Under these conditions</p>
          <ul className="conditions" aria-label="Conditions">
            {conditions.map((c) => (
              <li key={c.label}>
                <strong>{c.label}:</strong> {c.value}
              </li>
            ))}
          </ul>
        </aside>
      </div>
    </article>
  )
}
