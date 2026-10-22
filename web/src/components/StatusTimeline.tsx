import type { StreamMode } from '../api/sse'
import type { StepEntry } from '../run/state'
import { PIPELINE, STATUS_TEXT } from '../run/labels'
import type { RunStatus } from '../types/api'

const STATE_TEXT = { running: 'running', ok: 'done', failed: 'failed' } as const

const MODE_TEXT: Record<StreamMode, string> = {
  connecting: 'Connecting…',
  live: 'Live',
  reconnecting: 'Connection lost, reconnecting…',
  polling: 'Live updates via polling',
  closed: 'Up to date',
  not_found: 'Not available',
}

const TERMINAL: ReadonlySet<RunStatus> = new Set(['COMPLETED', 'FAILED', 'CANCELLED', 'BUDGET_EXCEEDED'])

/** What a step with no attempt means: an approval the run went past was not needed (the plan
 * cost less than the auto-approval threshold); in a finished run a step never reached was
 * skipped; otherwise it is still to come. */
function notStarted(step: string, index: number, steps: readonly StepEntry[], status: RunStatus | null) {
  const later = new Set(PIPELINE.slice(index + 1).map((p) => p.step))
  if (step === 'AWAITING_APPROVAL' && steps.some((s) => later.has(s.step))) {
    return { state: 'skipped', text: 'not needed (under the cost threshold)' }
  }
  if (status !== null && TERMINAL.has(status)) return { state: 'skipped', text: 'skipped' }
  return { state: 'pending', text: 'pending' }
}

function seconds(ms: number | undefined) {
  return ms === undefined ? '' : ` · ${(ms / 1000).toFixed(ms < 10_000 ? 1 : 0)} s`
}

export function StatusTimeline({
  steps,
  status,
  mode,
  replay,
}: {
  steps: readonly StepEntry[]
  status: RunStatus | null
  mode: StreamMode
  replay: boolean
}) {
  return (
    <section aria-labelledby="timeline-h">
      <h2 id="timeline-h">Status</h2>
      <p className="run-status">
        <span className={`badge status-${(status ?? 'QUEUED').toLowerCase()}`}>{STATUS_TEXT[status ?? 'QUEUED']}</span>{' '}
        <span className="muted" role="status">
          {replay && mode !== 'closed' ? 'Replaying a recorded run' : MODE_TEXT[mode]}
        </span>
      </p>
      <ol className="timeline">
        {PIPELINE.map(({ step, label }, index) => {
          const attempts = steps.filter((s) => s.step === step)
          const latest = attempts.at(-1)
          const idle = latest ? null : notStarted(step, index, steps, status)
          const state = latest?.state ?? idle?.state
          return (
            <li key={step} className={`step step-${state}`}>
              <span className="step-dot" aria-hidden="true" />
              <span className="step-label">{label}</span>
              <span className="muted">
                {latest ? STATE_TEXT[latest.state] : idle?.text}
                {attempts.length > 1 && ` · attempt ${latest?.attempt}`}
                {latest && seconds(latest.duration_ms)}
                {latest?.error_code && ` · ${latest.error_code}`}
              </span>
            </li>
          )
        })}
      </ol>
    </section>
  )
}
