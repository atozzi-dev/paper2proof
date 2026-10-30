import { useState } from 'react'
import { ApiError, api } from '../api/client'
import { formatUsd } from '../run/cost'
import type { PlanReady } from '../types/api'

interface Props {
  runId: string
  plan: PlanReady
  approval: { expires_at: string; pending: boolean } | null
  /** Replays are read-only: no decision buttons. */
  readOnly: boolean
}

export function PlanCard({ runId, plan: ready, approval, readOnly }: Props) {
  const p = ready.plan
  const packages = p.packages ?? []
  const datasets = p.datasets ?? []
  const notes = p.notes ?? []
  const [busy, setBusy] = useState(false)
  const [decided, setDecided] = useState<'approved' | 'rejected' | null>(null)
  const [error, setError] = useState<string | null>(null)

  async function decide(kind: 'approved' | 'rejected') {
    setBusy(true)
    setError(null)
    try {
      await (kind === 'approved' ? api.approve(runId) : api.reject(runId))
      setDecided(kind)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'The decision could not be sent. Try again.')
    } finally {
      setBusy(false)
    }
  }

  const awaiting = approval?.pending && !decided && !readOnly
  return (
    <section className="card" aria-labelledby="plan-h">
      <h2 id="plan-h">Plan</h2>
      <dl className="facts">
        <dt>Approach</dt>
        <dd>{p.mode === 'REPO_FIRST' ? "Adapt the authors' repository" : 'Implement from the paper'}</dd>
        <dt>Scale</dt>
        <dd>{p.scale === 'FULL' ? 'Full, as in the paper' : 'Reduced (smaller than the paper)'}</dd>
        <dt>Seeds</dt>
        <dd>{p.seeds.join(', ')}</dd>
        <dt>Claims tested</dt>
        <dd>{p.claim_ids.join(', ')}</dd>
        <dt>Estimated compute time</dt>
        <dd>{p.gpu_minutes_est.toFixed(0)} min</dd>
        <dt>Estimated cost</dt>
        <dd>{formatUsd(ready.est_cost_usd)}</dd>
        {packages.length > 0 && (
          <>
            <dt>Packages</dt>
            <dd>{packages.map((x) => `${x.name}${x.specifier ?? ''}`).join(', ')}</dd>
          </>
        )}
        {datasets.length > 0 && (
          <>
            <dt>Datasets</dt>
            <dd>{datasets.map((d) => `${d.id}${d.split ? ` (${d.split})` : ''}`).join(', ')}</dd>
          </>
        )}
      </dl>
      {notes.length > 0 && (
        <ul className="notes" aria-label="Plan notes">
          {notes.map((n, i) => (
            <li key={i}>{n}</li>
          ))}
        </ul>
      )}
      {awaiting && (
        <div className="approval" role="group" aria-label="Approve the plan">
          <p>
            This plan needs your approval before any compute is spent. It expires at{' '}
            {new Date(approval.expires_at).toLocaleString()}.
          </p>
          <button type="button" onClick={() => decide('approved')} disabled={busy}>
            Approve
          </button>{' '}
          <button type="button" className="secondary" onClick={() => decide('rejected')} disabled={busy}>
            Reject
          </button>
        </div>
      )}
      {decided && <p role="status">Plan {decided}.</p>}
      {error && <p role="alert" className="error">{error}</p>}
    </section>
  )
}
