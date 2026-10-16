import { useId } from 'react'
import type { Claim, Verdict } from '../types/api'
import { REASON_TEXT, VERDICT_HEADLINE, fmtNum, verdictConditions, type ConditionInputs } from '../run/verdictCopy'
import { CiBandChart } from './CiBandChart'
import { SafeMarkdown } from './SafeMarkdown'

interface Props extends ConditionInputs {
  /** Null when the claim is not loaded (yet): the verdict is still shown, never dropped. */
  claim: Claim | null
  claimId?: string
  verdict: Verdict
  explanationMd: string
}

export function VerdictCard({ claim, claimId, verdict, explanationMd, ...inputs }: Props) {
  const headingId = useId()
  const conditions = verdictConditions(verdict, inputs)
  return (
    <article className={`verdict verdict-${verdict.kind.toLowerCase()}`} aria-labelledby={headingId}>
      <p className="verdict-claim">
        {claim ? (
          <>
            <span className="muted">
              Claim {claim.id} · {claim.source_ref}
            </span>
            <br />
            {claim.text}
          </>
        ) : (
          <span className="muted">Claim {claimId} (details not loaded yet)</span>
        )}
      </p>
      <h3 id={headingId} className="verdict-headline">
        {VERDICT_HEADLINE[verdict.kind]}
      </h3>
      {verdict.reason && <p className="verdict-reason">{REASON_TEXT[verdict.reason]}</p>}
      {verdict.mean !== null && (
        <p className="muted">
          Measured {claim?.metric ?? 'value'}: mean {fmtNum(verdict.mean)}
          {verdict.std !== null && ` ± ${fmtNum(verdict.std)} (std)`} over {verdict.n_seeds} seeds
        </p>
      )}
      <CiBandChart claim={claim} verdict={verdict} />
      <ul className="conditions" aria-label="Conditions">
        {conditions.map((c) => (
          <li key={c.label}>
            <strong>{c.label}:</strong> {c.value}
          </li>
        ))}
      </ul>
      {explanationMd && (
        <details className="explanation" open>
          <summary>Explanation (written by the Judge model; the verdict above is computed by code)</summary>
          <SafeMarkdown text={explanationMd} />
        </details>
      )}
    </article>
  )
}
