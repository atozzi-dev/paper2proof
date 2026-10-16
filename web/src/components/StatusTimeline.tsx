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
        {PIPELINE.map(({ step, label }) => {
          const attempts = steps.filter((s) => s.step === step)
          const latest = attempts.at(-1)
          const state = latest?.state ?? 'pending'
          return (
            <li key={step} className={`step step-${state}`}>
              <span className="step-dot" aria-hidden="true" />
              <span className="step-label">{label}</span>
              <span className="muted">
                {latest ? STATE_TEXT[latest.state] : 'pending'}
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
