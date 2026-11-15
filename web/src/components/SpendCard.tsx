import { useId } from 'react'
import { costReceipt, formatUsd } from '../run/cost'
import type { CostUpdate, ModelCall } from '../types/api'

interface Props {
  calls: readonly ModelCall[]
  update: CostUpdate | null
  jobs: number
  estimateUsd: number | null
}

/** What the run has spent so far: every model call, and the cloud jobs once reported. */
export function SpendCard({ calls, update, jobs, estimateUsd }: Props) {
  const headingId = useId()
  const r = costReceipt(calls, update)
  const n = calls.length
  return (
    <section className="spend-card" aria-labelledby={headingId}>
      <h2 id={headingId} className="kicker kicker-accent">
        Spent so far
      </h2>
      <p className="spend-total">{formatUsd(r.totalUsd)}</p>
      <p className="small">
        {n} model call{n === 1 ? '' : 's'} · {jobs} cloud job{jobs === 1 ? '' : 's'}
        {!r.cloudReported && jobs > 0 && ' (cloud cost not reported yet)'}
      </p>
      {estimateUsd !== null && <p className="small">Estimated before the run: {formatUsd(estimateUsd)}</p>}
    </section>
  )
}
