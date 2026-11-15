import { useId } from 'react'
import type { StreamMode } from '../api/sse'
import { fmtDuration } from '../run/format'
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

type Mark = 'ok' | 'running' | 'failed' | 'skipped' | 'pending'

/** What a step with no attempt means: an approval the run went past was not needed (the plan
 * cost less than the auto-approval threshold); in a finished run a step never reached was
 * skipped; otherwise it is still to come. */
function notStarted(step: string, index: number, steps: readonly StepEntry[], status: RunStatus | null) {
  const later = new Set(PIPELINE.slice(index + 1).map((p) => p.step))
  if (step === 'AWAITING_APPROVAL' && steps.some((s) => later.has(s.step))) {
    return { state: 'skipped' as const, text: 'not needed (under the cost threshold)' }
  }
  if (status !== null && TERMINAL.has(status)) return { state: 'skipped' as const, text: 'skipped' }
  return { state: 'pending' as const, text: 'pending' }
}

/** A shape per state, so it never relies on colour alone. */
function StepMark({ state }: { state: Mark }) {
  return (
    <svg className={`step-mark step-mark-${state}`} width="26" height="26" viewBox="0 0 26 26" aria-hidden="true">
      {state === 'ok' && <path d="M6.5 13.5l4.5 4.5L20 8.5" fill="none" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" />}
      {state === 'failed' && <path d="M8 8l10 10M18 8L8 18" fill="none" strokeWidth="2.8" strokeLinecap="round" />}
      {state === 'skipped' && <line x1="8" y1="13" x2="18" y2="13" strokeWidth="3" strokeLinecap="round" />}
      {state === 'pending' && <circle cx="13" cy="13" r="8.5" fill="none" strokeWidth="2.2" />}
      {state === 'running' && (
        <>
          <circle cx="13" cy="13" r="9.5" fill="none" strokeWidth="3.2" className="step-mark-track" />
          <path d="M13 3.5 a9.5 9.5 0 0 1 9.5 9.5" fill="none" strokeWidth="3.2" strokeLinecap="round" />
        </>
      )}
    </svg>
  )
}

export function StatusTimeline({
  steps,
  status,
  mode,
  replay,
  level = 2,
}: {
  steps: readonly StepEntry[]
  status: RunStatus | null
  mode: StreamMode
  replay: boolean
  /** 3 inside the appendix, under its own h2. */
  level?: 2 | 3
}) {
  const headingId = useId()
  const Heading = level === 3 ? 'h3' : 'h2'
  const finished = status !== null && TERMINAL.has(status)
  // The step the run is on: the last one it reached, or the next once that one is done.
  const last = PIPELINE.reduce((n, p, i) => (steps.some((s) => s.step === p.step) ? i : n), -1)
  const lastDone = last >= 0 && steps.filter((s) => s.step === PIPELINE[last].step).at(-1)?.state === 'ok'
  const current = Math.min(last + (lastDone ? 2 : 1), PIPELINE.length) || 1
  return (
    <section className="timeline-section" aria-labelledby={headingId}>
      <Heading id={headingId}>{finished ? `${STATUS_TEXT[status]} · ${PIPELINE.length} steps` : `Step ${current} of ${PIPELINE.length}`}</Heading>
      <p className="run-status">
        <span className={`status-badge status-${(status ?? 'QUEUED').toLowerCase()}`}>{STATUS_TEXT[status ?? 'QUEUED']}</span>
        <span className="muted" role="status">
          {replay && mode !== 'closed' ? 'Replaying a recorded run' : MODE_TEXT[mode]}
        </span>
      </p>
      <ol className="timeline">
        {PIPELINE.map(({ step, label }, index) => {
          const attempts = steps.filter((s) => s.step === step)
          const latest = attempts.at(-1)
          const idle = latest ? null : notStarted(step, index, steps, status)
          const state: Mark = latest?.state ?? idle?.state ?? 'pending'
          return (
            <li key={step} className={`step step-${state}`} aria-current={state === 'running' ? 'step' : undefined}>
              <StepMark state={state} />
              <span className="step-label">
                {label}{' '}
                <span className="step-state">
                  · {latest ? STATE_TEXT[latest.state] : idle?.text}
                  {attempts.length > 1 && ` · attempt ${latest?.attempt}`}
                  {latest?.error_code && ` · ${latest.error_code}`}
                </span>
              </span>
              <span className="step-time">{latest?.duration_ms !== undefined ? fmtDuration(latest.duration_ms) : ''}</span>
            </li>
          )
        })}
      </ol>
    </section>
  )
}
